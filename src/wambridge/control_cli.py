"""Small command surface used by the foobar2000 component."""

from __future__ import annotations

import argparse
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass

from .cli_common import (
    RAW_MAX_VOLUME,
    RAW_MIN_VOLUME,
    add_target_arguments,
    bounded_float,
    bounded_int,
    configure_logging,
)
from .connections import (
    STANDBY_RELEASE_POLL,
    STANDBY_RELEASE_TIMEOUT,
    wait_until_released,
)
from .profiles import ProfileError, ProfileStore, resolve_device
from .samsung import (
    MAX_SLEEP_TIMER_SECONDS,
    WamApiError,
    WamStatus,
    get_mute,
    get_status,
    get_volume,
    set_mute,
    set_sleep_timer,
    set_volume,
    stop_playback,
)

LOGGER = logging.getLogger("wambridge")
DEFAULT_SAFE_VOLUME = 3
DEFAULT_RETRIES = 3
DEFAULT_RETRY_DELAY = 0.35


class ControlError(RuntimeError):
    """Raised when the component-facing control action cannot be completed."""


@dataclass(frozen=True, slots=True)
class Target:
    """Resolved speaker address used by one control invocation."""

    ip: str
    port: int


@dataclass(frozen=True, slots=True)
class Verification:
    """Result of checking whether a mutation reached the requested state."""

    confirmed: bool
    available: bool
    detail: str | None = None


raw_volume = bounded_int("volume", minimum=RAW_MIN_VOLUME, maximum=RAW_MAX_VOLUME)
"""Parse the raw 0..30 volume scale measured on the physical M5."""

sleep_seconds = bounded_int(
    "sleep timer seconds",
    minimum=0,
    maximum=MAX_SLEEP_TIMER_SECONDS,
)
"""Parse the measured SetSleepTimer range; zero cancels a pending timer."""

positive_int = bounded_int("retries", minimum=1)
"""Parse a positive retry count."""

nonnegative_float = bounded_float("retry delay", minimum=0.0)
"""Parse a non-negative retry delay."""


def build_parser() -> argparse.ArgumentParser:
    """Create the narrow CLI consumed by the foobar component."""
    parser = argparse.ArgumentParser(
        prog="wambridge-control",
        description="Control a Samsung WAM speaker for the foobar2000 component.",
    )
    parser.add_argument(
        "action",
        choices=(
            "status",
            "emergency-stop",
            "standby",
            "volume-up",
            "volume-down",
            "safe-volume",
            "set-volume",
            "sleep-timer",
        ),
    )
    parser.add_argument(
        "--level",
        type=raw_volume,
        help="Raw 0..30 level for set-volume",
    )
    parser.add_argument(
        "--seconds",
        type=sleep_seconds,
        help="Seconds for sleep-timer; zero cancels a pending timer",
    )
    add_target_arguments(parser, device_help="Saved device alias; defaults to M5")
    parser.add_argument(
        "--safe-volume",
        type=raw_volume,
        default=DEFAULT_SAFE_VOLUME,
        help=f"Raw recovery volume 0..30 (default: {DEFAULT_SAFE_VOLUME})",
    )
    parser.add_argument("--retries", type=positive_int, default=DEFAULT_RETRIES)
    parser.add_argument(
        "--retry-delay",
        type=nonnegative_float,
        default=DEFAULT_RETRY_DELAY,
    )
    parser.add_argument("--verbose", action="store_true")
    return parser


def resolve_target(args: argparse.Namespace) -> Target:
    """Resolve a direct address or saved alias without importing the full CLI."""
    if args.speaker:
        return Target(args.speaker, args.port)
    store = ProfileStore(args.config)
    profile = resolve_device(
        args.device or "M5",
        store=store,
        timeout=args.discovery_timeout,
        local_addresses=args.interfaces,
        scan=not args.no_scan,
    )
    return Target(profile.last_ip, profile.port)


def _attempt(
    label: str,
    operation: Callable[[], object],
    *,
    retries: int,
    retry_delay: float,
) -> tuple[bool, str | None]:
    """Retry a mutation whose timeout may still mean it was applied."""
    last_error: WamApiError | None = None
    for attempt in range(1, retries + 1):
        try:
            operation()
            return True, None
        except WamApiError as error:
            last_error = error
            LOGGER.warning("%s attempt %s/%s: %s", label, attempt, retries, error)
            if attempt < retries and retry_delay:
                time.sleep(retry_delay)
    return False, str(last_error) if last_error else f"{label} failed"


def _status_lines(status: WamStatus) -> list[str]:
    playback = status.playback
    return [
        f"function={playback.function or ''}",
        f"submode={playback.submode or ''}",
        f"play_status={playback.play_status or ''}",
        f"provider={playback.cp_name or ''}",
        f"title={playback.title or ''}",
        f"volume={status.volume}",
        f"muted={'on' if status.muted else 'off'}",
        # `unknown`, matching `cli.py`, not an empty value: this firmware never
        # answers `GetPowerStatus`, so the field is always absent here and a
        # bare `power=` reads like a speaker that reported nothing rather than
        # a command that was never going to be answered.
        f"power={status.power_status or 'unknown'}",
    ]


def _verify_recovery(target: Target, safe_volume: int) -> Verification:
    try:
        muted = get_mute(target.ip, port=target.port, timeout=2.0)
        volume = get_volume(target.ip, port=target.port, timeout=2.0)
    except WamApiError as error:
        return Verification(False, False, str(error))
    if muted:
        return Verification(False, True, "speaker still reports mute=on")
    if volume != safe_volume:
        return Verification(
            False,
            True,
            f"speaker reports volume={volume}, expected {safe_volume}",
        )
    return Verification(True, True)


def emergency_stop(
    target: Target,
    *,
    safe_volume: int,
    retries: int,
    retry_delay: float,
) -> list[str]:
    """Stop, unmute and restore a known-safe raw volume despite lost replies."""
    results = [
        _attempt(
            "stop playback",
            lambda: stop_playback(target.ip, port=target.port, timeout=3.0),
            retries=retries,
            retry_delay=retry_delay,
        ),
        _attempt(
            "unmute",
            lambda: set_mute(target.ip, False, port=target.port, timeout=3.0),
            retries=retries,
            retry_delay=retry_delay,
        ),
        _attempt(
            "restore safe volume",
            lambda: set_volume(
                target.ip,
                safe_volume,
                port=target.port,
                timeout=3.0,
            ),
            retries=retries,
            retry_delay=retry_delay,
        ),
    ]
    verification = _verify_recovery(target, safe_volume)
    mutations_sent = all(success for success, _ in results)
    if (verification.available and not verification.confirmed) or (
        not verification.available and not mutations_sent
    ):
        mutation_error = next(
            (error for success, error in results if not success and error),
            None,
        )
        raise ControlError(
            "Emergency stop could not be verified. The M5 control port may be wedged; "
            f"power-cycle the speaker. Last detail: {verification.detail or mutation_error}"
        )
    lines = [
        "action=emergency-stop",
        f"volume={safe_volume}",
        "muted=off",
        f"verified={'yes' if verification.confirmed else 'no'}",
    ]
    if not verification.available and verification.detail:
        lines.append(f"warning={verification.detail}")
    return lines


def standby(
    target: Target,
    *,
    retries: int,
    retry_delay: float,
    release_timeout: float = STANDBY_RELEASE_TIMEOUT,
    release_poll: float = STANDBY_RELEASE_POLL,
) -> list[str]:
    """Stop playback, mute, and confirm nothing is still attached.

    This sends no power command: the firmware is left awake and simply quiet.
    What it does guarantee is that nothing of ours is holding the speaker, and
    that is the point - the M5 reaches its own idle power-down only once every
    program talking to it has let go.

    That is also why this reading is not the whole story for a speaker that
    stays lit. It survived shutting the whole computer down, and a powered-off
    host holds no sockets - because the speaker was still holding a
    ``SetUrlPlayback`` session whose source had vanished, which no amount of
    closing sockets undoes. The PCM helper's release is what ends that session,
    and once it does the speaker goes dark on its own: measured 2026-08-16 at
    17 min 4 s, against 33 minutes and still lit after a session that sent no
    release. This reading proves only that nothing here is holding on.
    """
    stop_result = _attempt(
        "standby stop",
        lambda: stop_playback(
            target.ip,
            standby=True,
            port=target.port,
            timeout=3.0,
        ),
        retries=retries,
        retry_delay=retry_delay,
    )
    mute_result = _attempt(
        "standby mute",
        lambda: set_mute(target.ip, True, port=target.port, timeout=3.0),
        retries=retries,
        retry_delay=retry_delay,
    )
    try:
        muted = get_mute(target.ip, port=target.port, timeout=2.0)
        verification = Verification(
            muted,
            True,
            None if muted else "speaker still reports mute=off",
        )
    except WamApiError as error:
        verification = Verification(False, False, str(error))
    mutations_sent = stop_result[0] and mute_result[0]
    if (verification.available and not verification.confirmed) or (
        not verification.available and not mutations_sent
    ):
        raise ControlError(
            "Standby could not be verified. The M5 control port may be wedged; "
            f"power-cycle the speaker. Last detail: "
            f"{verification.detail or mute_result[1] or stop_result[1]}"
        )
    held = wait_until_released(
        target.ip,
        timeout=release_timeout,
        poll=release_poll,
        # Not this process: the stop, the mute and the verification each opened
        # and closed a connection of their own, and waiting those out would
        # report this action's own requests as something holding the speaker.
        # A leaked helper is a different process, so it still counts.
        own_pid=os.getpid(),
    )
    lines = [
        "action=standby",
        "muted=on",
        f"verified={'yes' if verification.confirmed else 'no'}",
        f"holding={'unknown' if held is None else held}",
    ]
    if not verification.available and verification.detail:
        lines.append(f"warning={verification.detail}")
    if held:
        # Not fatal: the mute and stop both landed, and the caller may simply
        # have asked while something else was still streaming. Saying so is the
        # point - a silent standby is how the speaker stopped sleeping before.
        # State the reading, not a prediction. What happens to the speaker's
        # power once these close is unmeasured, and promising sleep would send
        # the user waiting for a transition that may never come instead of
        # reaching for the sleep timer, which is the only known power lever.
        lines.append(
            f"warning={held} local connection(s) still attached to the speaker; "
            "standby sends no power command, and a speaker something is still "
            "holding does not start its idle countdown"
        )
    return lines


def change_volume(
    target: Target,
    delta: int,
    *,
    retries: int,
    retry_delay: float,
) -> list[str]:
    """Move one raw volume step without exceeding the measured M5 range."""
    current = get_volume(target.ip, port=target.port)
    target_volume = max(RAW_MIN_VOLUME, min(RAW_MAX_VOLUME, current + delta))
    success, error = _attempt(
        "set volume",
        lambda: set_volume(target.ip, target_volume, port=target.port, timeout=3.0),
        retries=retries,
        retry_delay=retry_delay,
    )
    if not success:
        raise ControlError(f"Could not set volume: {error}")
    return [
        f"action={'volume-up' if delta > 0 else 'volume-down'}",
        f"volume={target_volume}",
    ]


def set_exact_volume(
    target: Target,
    level: int,
    *,
    action: str,
    retries: int,
    retry_delay: float,
) -> list[str]:
    """Set one raw level without reading the speaker first or touching mute.

    The volume slider sends absolute levels, so this must not spend a round
    trip on `GetVolume` the way `change_volume` does: a drag would double the
    traffic on the shared control port for a value it already knows.
    """
    success, error = _attempt(
        f"set volume {level}",
        lambda: set_volume(target.ip, level, port=target.port, timeout=3.0),
        retries=retries,
        retry_delay=retry_delay,
    )
    if not success:
        raise ControlError(f"Could not set volume: {error}")
    return [f"action={action}", f"volume={level}"]


def set_sleep_timer_action(
    target: Target,
    seconds: int,
    *,
    retries: int,
    retry_delay: float,
) -> list[str]:
    """Arm or cancel the speaker sleep timer without touching playback state."""
    success, error = _attempt(
        f"set sleep timer {seconds}",
        lambda: set_sleep_timer(target.ip, seconds, port=target.port, timeout=3.0),
        retries=retries,
        retry_delay=retry_delay,
    )
    if not success:
        raise ControlError(f"Could not set sleep timer: {error}")
    return [
        "action=sleep-timer",
        f"seconds={seconds}",
        f"state={'off' if seconds == 0 else 'armed'}",
    ]


def set_safe_volume(
    target: Target,
    safe_volume: int,
    *,
    retries: int,
    retry_delay: float,
) -> list[str]:
    """Set the configured raw safe level without changing mute state."""
    return set_exact_volume(
        target,
        safe_volume,
        action="safe-volume",
        retries=retries,
        retry_delay=retry_delay,
    )


def run(args: argparse.Namespace) -> list[str]:
    """Execute one component-facing action and return machine-readable lines."""
    target = resolve_target(args)
    prefix = [f"speaker={target.ip}", f"port={target.port}"]
    if args.action == "status":
        return prefix + _status_lines(get_status(target.ip, port=target.port))
    if args.action == "emergency-stop":
        return prefix + emergency_stop(
            target,
            safe_volume=args.safe_volume,
            retries=args.retries,
            retry_delay=args.retry_delay,
        )
    if args.action == "standby":
        return prefix + standby(
            target,
            retries=args.retries,
            retry_delay=args.retry_delay,
        )
    if args.action == "volume-up":
        return prefix + change_volume(
            target,
            1,
            retries=args.retries,
            retry_delay=args.retry_delay,
        )
    if args.action == "volume-down":
        return prefix + change_volume(
            target,
            -1,
            retries=args.retries,
            retry_delay=args.retry_delay,
        )
    if args.action == "set-volume":
        if args.level is None:
            raise ControlError("set-volume requires --level")
        return prefix + set_exact_volume(
            target,
            args.level,
            action="set-volume",
            retries=args.retries,
            retry_delay=args.retry_delay,
        )
    if args.action == "sleep-timer":
        if args.seconds is None:
            raise ControlError("sleep-timer requires --seconds")
        return prefix + set_sleep_timer_action(
            target,
            args.seconds,
            retries=args.retries,
            retry_delay=args.retry_delay,
        )
    return prefix + set_safe_volume(
        target,
        args.safe_volume,
        retries=args.retries,
        retry_delay=args.retry_delay,
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(args.verbose)
    try:
        for line in run(args):
            print(line)
    except (ControlError, ProfileError, WamApiError, ValueError) as error:
        LOGGER.error("%s", error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

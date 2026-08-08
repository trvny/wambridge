"""Small command surface used by the foobar2000 component."""

from __future__ import annotations

import argparse
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from .cli_common import add_target_arguments, bounded_float, bounded_int, configure_logging
from .connections import attached_connections_to
from .profiles import ProfileError, ProfileStore, resolve_device
from .samsung import (
    WamApiError,
    WamStatus,
    get_mute,
    get_status,
    get_volume,
    set_mute,
    set_volume,
    stop_playback,
)

LOGGER = logging.getLogger("wambridge")
RAW_MIN_VOLUME = 0
RAW_MAX_VOLUME = 30
DEFAULT_SAFE_VOLUME = 3
DEFAULT_RETRIES = 3
DEFAULT_RETRY_DELAY = 0.35
# A helper that is shutting down needs a moment to drop its sockets, so a single
# reading right after the stop would report a hold that is about to clear.
STANDBY_RELEASE_TIMEOUT = 5.0
STANDBY_RELEASE_POLL = 0.5


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
        ),
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
        f"power={status.power_status or ''}",
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


def wait_until_released(
    speaker_ip: str,
    *,
    timeout: float = STANDBY_RELEASE_TIMEOUT,
    poll: float = STANDBY_RELEASE_POLL,
) -> int | None:
    """Wait for local sockets against the speaker to drop, and report the count.

    Returns ``None`` when the socket table could not be read. That is reported
    as unknown rather than as zero: claiming nothing is attached when it could
    not be checked is the failure this exists to prevent.
    """
    deadline = time.monotonic() + timeout
    held = attached_connections_to(speaker_ip)
    while held is not None and held > 0 and time.monotonic() < deadline:
        time.sleep(poll)
        held = attached_connections_to(speaker_ip)
    return held


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
    What it does guarantee is that nothing of ours is holding the speaker. No
    idle power-down was found on this firmware, so whether release alone lets it
    sleep is unmeasured; a leaked helper keeping the control socket and the
    audio pull open is the leading suspect for the speaker staying lit.
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
            "standby sends no power command, so it stays awake either way"
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


def set_safe_volume(
    target: Target,
    safe_volume: int,
    *,
    retries: int,
    retry_delay: float,
) -> list[str]:
    """Set the configured raw safe level without changing mute state."""
    success, error = _attempt(
        "set safe volume",
        lambda: set_volume(target.ip, safe_volume, port=target.port, timeout=3.0),
        retries=retries,
        retry_delay=retry_delay,
    )
    if not success:
        raise ControlError(f"Could not set safe volume: {error}")
    return ["action=safe-volume", f"volume={safe_volume}"]


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

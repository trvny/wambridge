"""Argument types, target options and logging shared by the WAM Bridge CLIs."""

from __future__ import annotations

import argparse
import logging
from collections.abc import Callable
from pathlib import Path

from .discovery import DiscoveredSpeaker, discover
from .profiles import ProfileStore, resolve_device
from .samsung import DEFAULT_PORT

LOGGER = logging.getLogger("wambridge")
DEFAULT_DISCOVERY_TIMEOUT = 4.0
DEFAULT_MAX_START_VOLUME = 10

# The M5's own scale, measured: values above 30 are silently clamped while still
# returning success. Here rather than in one CLI because it is a fact about the
# device, and more than one entry point now has to bound a level against it.
RAW_MIN_VOLUME = 0
RAW_MAX_VOLUME = 30


def _range_message(label: str, minimum: float | None, maximum: float | None) -> str:
    if minimum is not None and maximum is not None:
        return f"{label} must be between {minimum:g} and {maximum:g}"
    if minimum is not None:
        return f"{label} must be at least {minimum:g}"
    return f"{label} must be at most {maximum:g}"


def bounded_int(
    label: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> Callable[[str], int]:
    """Build an argparse type parsing an integer inside an inclusive range."""

    def parse(value: str) -> int:
        try:
            parsed = int(value)
        except ValueError as error:
            raise argparse.ArgumentTypeError(f"{label} must be an integer") from error
        if (minimum is not None and parsed < minimum) or (
            maximum is not None and parsed > maximum
        ):
            raise argparse.ArgumentTypeError(_range_message(label, minimum, maximum))
        return parsed

    return parse


def bounded_float(
    label: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> Callable[[str], float]:
    """Build an argparse type parsing a float inside an inclusive range."""

    def parse(value: str) -> float:
        try:
            parsed = float(value)
        except ValueError as error:
            raise argparse.ArgumentTypeError(f"{label} must be a number") from error
        if (minimum is not None and parsed < minimum) or (
            maximum is not None and parsed > maximum
        ):
            raise argparse.ArgumentTypeError(_range_message(label, minimum, maximum))
        return parsed

    return parse


def add_target_arguments(
    parser: argparse.ArgumentParser,
    *,
    port: int = DEFAULT_PORT,
    device_help: str = "Saved device alias; current IP is resolved automatically",
) -> None:
    """Add the speaker, profile and discovery options every CLI understands."""
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--speaker", help="Speaker IPv4 address")
    target.add_argument("--device", help=device_help)
    parser.add_argument("--port", type=int, default=port, help="Samsung WAM API port")
    parser.add_argument(
        "--config",
        type=Path,
        help="Override the per-user device profile file",
    )
    parser.add_argument(
        "--discovery-timeout",
        type=float,
        default=DEFAULT_DISCOVERY_TIMEOUT,
        help="Seconds to wait for SSDP replies before the API-scan fallback",
    )
    parser.add_argument(
        "--interface",
        action="append",
        dest="interfaces",
        help="Local IPv4 used for SSDP; repeat to try multiple interfaces",
    )
    parser.add_argument(
        "--no-scan",
        action="store_true",
        help="Disable fallback scanning of local /24 networks on port 55001",
    )


def configure_logging(verbose: bool) -> None:
    """Apply the log level and format used by every WAM Bridge command."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )


def find_speakers(args: argparse.Namespace) -> list[DiscoveredSpeaker]:
    """Run discovery using CLI diagnostics and fallback settings."""
    return discover(
        timeout=args.discovery_timeout,
        local_addresses=args.interfaces,
        port=args.port,
        scan=not args.no_scan,
    )


def select_discovered_speaker(args: argparse.Namespace) -> str:
    """Discover exactly one speaker."""
    speakers = find_speakers(args)
    if not speakers:
        raise RuntimeError("No Samsung WAM speaker found; pass --speaker IP")
    if len(speakers) > 1:
        addresses = ", ".join(speaker.ip for speaker in speakers)
        raise RuntimeError(
            f"More than one Samsung WAM found ({addresses}); pass --speaker IP"
        )
    return speakers[0].ip


def select_speaker(
    args: argparse.Namespace,
    store: ProfileStore,
) -> tuple[str, int]:
    """Select a direct address, resolve a saved profile or discover one speaker."""
    if args.device:
        profile = resolve_device(
            args.device,
            store=store,
            timeout=args.discovery_timeout,
            local_addresses=args.interfaces,
            scan=not args.no_scan,
        )
        LOGGER.info(
            "Resolved saved device %s (%s) to %s",
            profile.alias,
            profile.device_id,
            profile.last_ip,
        )
        return profile.last_ip, profile.port
    if args.speaker:
        return args.speaker, args.port
    return select_discovered_speaker(args), args.port

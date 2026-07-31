"""Command-line listener for Samsung WAM responses and unsolicited events."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

from .discovery import discover
from .profiles import ProfileError, ProfileStore, resolve_device
from .samsung import DEFAULT_PORT, WamApiError
from .wam_events import WamEvent, WamEventError, listen_events

LOGGER = logging.getLogger("wambridge")


def client_uuid(value: str) -> str:
    """Validate and normalize a client UUID supplied on the command line."""
    try:
        return str(UUID(value))
    except ValueError as error:
        raise argparse.ArgumentTypeError("client UUID must be a valid UUID") from error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wambridge-events",
        description=(
            "Listen to responses and unsolicited events broadcast by a Samsung "
            "WAM speaker on TCP 55001."
        ),
    )
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--speaker", help="Speaker IPv4 address")
    target.add_argument("--device", help="Saved device alias")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--client-uuid",
        type=client_uuid,
        help="Client UUID; a temporary UUID is generated when omitted",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=0.0,
        help="Stop after this many seconds; 0 listens until Ctrl+C",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Print the complete XML body after each summary",
    )
    parser.add_argument("--config", type=Path, help="Override device profile file")
    parser.add_argument(
        "--discovery-timeout",
        type=float,
        default=4.0,
        help="Seconds to resolve speakers",
    )
    parser.add_argument(
        "--interface",
        action="append",
        dest="interfaces",
        help="Local IPv4 used for discovery; repeat when needed",
    )
    parser.add_argument(
        "--no-scan",
        action="store_true",
        help="Disable fallback scanning of local /24 networks",
    )
    parser.add_argument("--verbose", action="store_true")
    return parser


def select_speaker(args: argparse.Namespace, store: ProfileStore) -> tuple[str, int]:
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

    speakers = discover(
        timeout=args.discovery_timeout,
        local_addresses=args.interfaces,
        port=args.port,
        scan=not args.no_scan,
    )
    if not speakers:
        raise RuntimeError("No Samsung WAM speaker found")
    if len(speakers) > 1:
        addresses = ", ".join(speaker.ip for speaker in speakers)
        raise RuntimeError(
            f"More than one Samsung WAM found ({addresses}); pass --speaker IP"
        )
    return speakers[0].ip, args.port


def _short_value(value: str, limit: int = 120) -> str:
    compact = " ".join(value.split())
    return compact if len(compact) <= limit else f"{compact[: limit - 3]}..."


def format_event(event: WamEvent) -> str:
    """Build one compact, grep-friendly event summary."""
    timestamp = datetime.now().astimezone().isoformat(timespec="milliseconds")
    parts = [timestamp, event.method or "Unknown"]
    if event.result:
        parts.append(f"result={event.result}")
    if event.error_code:
        parts.append(f"errCode={event.error_code}")
    if event.user_identifier:
        parts.append(f"user={event.user_identifier}")

    ignored = {"user_identifier"}
    for name, value in event.values.items():
        if name.casefold() in ignored:
            continue
        parts.append(f"{name}={_short_value(value)}")
    return " ".join(parts)


def run(args: argparse.Namespace) -> int:
    if args.duration < 0:
        raise ValueError("duration cannot be negative")
    store = ProfileStore(args.config)
    speaker_ip, speaker_port = select_speaker(args, store)
    identity = args.client_uuid or str(uuid4())
    print(
        f"Listening to Samsung WAM {speaker_ip}:{speaker_port} "
        f"with mobileUUID {identity}. Press Ctrl+C to stop."
    )
    for event in listen_events(
        speaker_ip,
        identity,
        port=speaker_port,
        duration=args.duration,
    ):
        print(format_event(event), flush=True)
        if args.raw:
            print(event.body, flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )
    try:
        return run(args)
    except KeyboardInterrupt:
        print("\nStopping")
        return 130
    except (OSError, ProfileError, RuntimeError, ValueError, WamApiError, WamEventError) as error:
        LOGGER.error("%s", error)
        return 1


if __name__ == "__main__":
    sys.exit(main())

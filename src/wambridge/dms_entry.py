"""Entry point for the browse-aware Samsung DMS diagnostic."""

from __future__ import annotations

from time import sleep

from . import dms_cli
from .dms_browse import SamsungBrowseServer
from .dms_probe import SsdpAdvertiser


def _run_ladder(
    speaker_ip: str,
    speaker_port: int,
    server: SamsungBrowseServer,
    host_ip: str,
    ssdp: SsdpAdvertiser,
) -> bool:
    """Continue through every command variant until the MP3 is requested."""

    dms_cli.LOGGER.info(
        "Attempt 1/3: SetIpInfo + SSDP + SetSharePlaybackControl"
    )
    dms_cli._register_server(speaker_ip, speaker_port, server, host_ip)
    ssdp.announce()
    accepted = dms_cli._start_share(
        speaker_ip,
        speaker_port,
        server,
        server.udn,
    )
    if server.request_started.wait(timeout=6.0):
        return accepted

    if server.has_contact:
        dms_cli.LOGGER.warning(
            "MediaServer contact occurred without an MP3 request; trying "
            "SetNewFolderPlaybackControl"
        )
    else:
        dms_cli.LOGGER.warning(
            "No MediaServer contact after share control; trying "
            "SetNewFolderPlaybackControl"
        )

    dms_cli.LOGGER.info("Attempt 2/3: APK folder fallback")
    ssdp.announce()
    accepted = (
        dms_cli._start_folder_fallback(speaker_ip, speaker_port, server) or accepted
    )
    if server.request_started.wait(timeout=8.0):
        return accepted

    if server.browse_requested.is_set():
        dms_cli.LOGGER.warning(
            "M5 browsed ContentDirectory but did not fetch the item; "
            "re-registering and retrying share control"
        )
    else:
        dms_cli.LOGGER.warning(
            "No MP3 request after folder fallback; re-registering and "
            "retrying share control"
        )

    dms_cli.LOGGER.info("Attempt 3/3: re-register and retry raw UUID")
    ssdp.announce(repeats=3)
    dms_cli._register_server(speaker_ip, speaker_port, server, host_ip)
    sleep(0.5)
    accepted = (
        dms_cli._start_share(
            speaker_ip,
            speaker_port,
            server,
            server.uuid,
        )
        or accepted
    )
    server.request_started.wait(timeout=10.0)
    return accepted


def main(argv: list[str] | None = None) -> int:
    """Run the existing CLI with the browse-aware server and ladder."""

    dms_cli.SamsungDmsServer = SamsungBrowseServer
    dms_cli._run_ladder = _run_ladder
    return dms_cli.main(argv)

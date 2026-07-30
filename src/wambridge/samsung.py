"""Small client for the local Samsung WAM HTTP API."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from urllib.parse import quote
from urllib.request import ProxyHandler, build_opener
from xml.etree import ElementTree

DEFAULT_PORT = 55001
MIN_VOLUME = 0
MAX_VOLUME = 100
LOCAL_OPENER = build_opener(ProxyHandler({}))


class WamApiError(RuntimeError):
    """Raised when a speaker rejects or cannot receive a command."""


@dataclass(frozen=True, slots=True)
class WamResponse:
    """Parsed response returned by the speaker."""

    method: str | None
    result: str | None
    body: str
    values: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class WamIdentity:
    """Stable identity and current display name returned by a speaker."""

    device_id: str
    name: str | None


@dataclass(frozen=True, slots=True)
class WamPlaybackStatus:
    """Current source and playback state reported by the speaker."""

    function: str | None = None
    submode: str | None = None
    play_status: str | None = None
    cp_name: str | None = None
    title: str | None = None
    description: str | None = None

    @property
    def is_native_cp(self) -> bool:
        """Return whether a named native content provider is active."""
        return bool(
            self.submode == "cp"
            and self.cp_name
            and self.cp_name.casefold() != "unknown"
        )


@dataclass(frozen=True, slots=True)
class WamStatus:
    """Snapshot used by the command-line status view."""

    playback: WamPlaybackStatus
    volume: int
    muted: bool
    power_status: str | None


def build_command(
    method: str,
    arguments: list[tuple[str, str | int, str]] | None = None,
    *,
    power_on: bool = False,
) -> str:
    """Build the XML command accepted by the Samsung WAM API."""
    parts = ["<pwron>on</pwron>"] if power_on else []
    parts.append(f"<name>{method}</name>")
    for name, value, value_type in arguments or []:
        if value_type == "cdata":
            safe_value = str(value).replace("]]>", "]]]]><![CDATA[>")
            parts.append(
                f'<p type="cdata" name="{name}" val="empty">'
                f"<![CDATA[{safe_value}]]></p>"
            )
        elif value_type in {"str", "dec"}:
            parts.append(f'<p type="{value_type}" name="{name}" val="{value}"/>')
        else:
            raise ValueError(f"Unsupported WAM value type: {value_type}")
    return "".join(parts)


def build_api_url(
    speaker_ip: str,
    method: str,
    arguments: list[tuple[str, str | int, str]] | None = None,
    *,
    port: int = DEFAULT_PORT,
    api_type: str = "UIC",
    power_on: bool = False,
) -> str:
    """Build a complete local WAM API URL."""
    command = build_command(method, arguments, power_on=power_on)
    return f"http://{speaker_ip}:{port}/{api_type}?cmd={quote(command, safe='')}"


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_response(body: str) -> WamResponse:
    """Parse and validate one XML response from a Samsung WAM speaker."""
    try:
        root = ElementTree.fromstring(body)  # nosec B314 - small local response
    except ElementTree.ParseError as error:
        raise WamApiError(
            f"Samsung WAM returned invalid XML: {body[:200]}"
        ) from error

    response_node = root.find("response")
    result = response_node.get("result") if response_node is not None else None
    response_method = root.findtext("method")
    if result != "ok":
        error_code = response_node.get("errcode") if response_node is not None else None
        suffix = f" (error {error_code})" if error_code else ""
        raise WamApiError(
            f"Samsung WAM rejected {response_method or 'request'}{suffix}"
        )

    values: dict[str, str] = {}
    if response_node is not None:
        for node in response_node.iter():
            if node is response_node:
                continue
            name = node.get("name") or _local_name(node.tag)
            value = node.get("val")
            if value is None and node.text:
                value = node.text.strip()
            if name and value:
                values[name] = value

    return WamResponse(
        method=response_method,
        result=result,
        body=body,
        values=values,
    )


def request(
    speaker_ip: str,
    method: str,
    arguments: list[tuple[str, str | int, str]] | None = None,
    *,
    port: int = DEFAULT_PORT,
    timeout: float = 5.0,
    api_type: str = "UIC",
    power_on: bool = False,
) -> WamResponse:
    """Send one command and validate the returned XML."""
    url = build_api_url(
        speaker_ip,
        method,
        arguments,
        port=port,
        api_type=api_type,
        power_on=power_on,
    )
    try:
        with LOCAL_OPENER.open(  # nosec B310 - deliberately local API
            url,
            timeout=timeout,
        ) as response:
            body = response.read().decode("utf-8", errors="replace")
    except OSError as error:
        raise WamApiError(
            f"Cannot reach Samsung WAM at {speaker_ip}:{port}: {error}"
        ) from error
    return parse_response(body)


def normalize_device_id(value: str) -> str:
    """Normalize separator and case differences in Samsung device IDs."""
    return "".join(character for character in value.upper() if character.isalnum())


def _first_value(response: WamResponse, *names: str) -> str | None:
    for name in names:
        if value := response.values.get(name):
            return value
    return None


def _validate_volume(level: int) -> int:
    if isinstance(level, bool) or not isinstance(level, int):
        raise ValueError("Volume must be an integer between 0 and 100")
    if not MIN_VOLUME <= level <= MAX_VOLUME:
        raise ValueError(f"Volume must be between {MIN_VOLUME} and {MAX_VOLUME}")
    return level


def _parse_switch(value: str, label: str) -> bool:
    normalized = value.strip().casefold()
    if normalized in {"on", "1", "true"}:
        return True
    if normalized in {"off", "0", "false"}:
        return False
    raise WamApiError(f"Samsung WAM returned invalid {label}: {value!r}")


def probe(
    speaker_ip: str,
    *,
    port: int = DEFAULT_PORT,
    timeout: float = 5.0,
) -> WamResponse:
    """Check that the target is a responding Samsung WAM speaker."""
    return request(speaker_ip, "GetSpkName", port=port, timeout=timeout)


def get_volume(
    speaker_ip: str,
    *,
    port: int = DEFAULT_PORT,
    timeout: float = 5.0,
) -> int:
    """Return the current speaker volume as a value from 0 to 100."""
    response = request(speaker_ip, "GetVolume", port=port, timeout=timeout)
    raw_volume = _first_value(
        response,
        "volume",
        "volumelevel",
        "volume_level",
        "level",
    )
    if raw_volume is None and len(response.values) == 1:
        raw_volume = next(iter(response.values.values()))
    if raw_volume is None:
        raise WamApiError("Samsung WAM response did not contain a volume level")
    try:
        volume = int(raw_volume)
    except ValueError as error:
        raise WamApiError(
            f"Samsung WAM returned invalid volume: {raw_volume!r}"
        ) from error
    try:
        return _validate_volume(volume)
    except ValueError as error:
        raise WamApiError(
            f"Samsung WAM returned invalid volume: {volume}"
        ) from error


def set_volume(
    speaker_ip: str,
    level: int,
    *,
    port: int = DEFAULT_PORT,
    timeout: float = 5.0,
) -> WamResponse:
    """Set the speaker volume to an explicit value from 0 to 100."""
    volume = _validate_volume(level)
    return request(
        speaker_ip,
        "SetVolume",
        [("volume", volume, "dec")],
        port=port,
        timeout=timeout,
        power_on=True,
    )


def get_mute(
    speaker_ip: str,
    *,
    port: int = DEFAULT_PORT,
    timeout: float = 5.0,
) -> bool:
    """Return whether the speaker is muted."""
    response = request(speaker_ip, "GetMute", port=port, timeout=timeout)
    raw_mute = _first_value(response, "mute", "mutestatus")
    if raw_mute is None:
        raise WamApiError("Samsung WAM response did not contain mute state")
    return _parse_switch(raw_mute, "mute state")


def set_mute(
    speaker_ip: str,
    muted: bool,
    *,
    port: int = DEFAULT_PORT,
    timeout: float = 5.0,
) -> WamResponse:
    """Mute or unmute the speaker."""
    if not isinstance(muted, bool):
        raise ValueError("Muted must be a boolean")
    return request(
        speaker_ip,
        "SetMute",
        [("mute", "on" if muted else "off", "str")],
        port=port,
        timeout=timeout,
        power_on=True,
    )


def get_play_status(
    speaker_ip: str,
    *,
    port: int = DEFAULT_PORT,
    timeout: float = 5.0,
) -> WamPlaybackStatus:
    """Return source and playback state from the UIC API."""
    response = request(speaker_ip, "GetPlayStatus", port=port, timeout=timeout)
    return WamPlaybackStatus(
        function=_first_value(response, "function"),
        submode=_first_value(response, "submode"),
        play_status=_first_value(response, "playstatus"),
        cp_name=_first_value(response, "cpname"),
    )


def get_radio_info(
    speaker_ip: str,
    *,
    port: int = DEFAULT_PORT,
    timeout: float = 5.0,
) -> WamPlaybackStatus:
    """Return current native content-provider metadata."""
    response = request(
        speaker_ip,
        "GetRadioInfo",
        port=port,
        timeout=timeout,
        api_type="CPM",
    )
    return WamPlaybackStatus(
        function="wifi",
        submode="cp",
        play_status=_first_value(response, "playstatus"),
        cp_name=_first_value(response, "cpname"),
        title=_first_value(response, "title"),
        description=_first_value(response, "description"),
    )


def get_playback_status(
    speaker_ip: str,
    *,
    port: int = DEFAULT_PORT,
    timeout: float = 5.0,
) -> WamPlaybackStatus:
    """Return play status enriched with native provider metadata."""
    status = get_play_status(speaker_ip, port=port, timeout=timeout)
    if status.submode != "cp":
        return status
    try:
        radio = get_radio_info(speaker_ip, port=port, timeout=timeout)
    except WamApiError:
        return status
    return replace(
        status,
        play_status=radio.play_status or status.play_status,
        cp_name=radio.cp_name or status.cp_name,
        title=radio.title,
        description=radio.description,
    )


def get_power_status(
    speaker_ip: str,
    *,
    port: int = DEFAULT_PORT,
    timeout: float = 5.0,
) -> str | None:
    """Return the firmware's raw power-status value."""
    response = request(
        speaker_ip,
        "GetPowerStatus",
        port=port,
        timeout=timeout,
    )
    return _first_value(response, "powerStatus", "powerstatus")


def get_status(
    speaker_ip: str,
    *,
    port: int = DEFAULT_PORT,
    timeout: float = 5.0,
) -> WamStatus:
    """Return a compact speaker status snapshot."""
    return WamStatus(
        playback=get_playback_status(
            speaker_ip,
            port=port,
            timeout=timeout,
        ),
        volume=get_volume(speaker_ip, port=port, timeout=timeout),
        muted=get_mute(speaker_ip, port=port, timeout=timeout),
        power_status=get_power_status(
            speaker_ip,
            port=port,
            timeout=timeout,
        ),
    )


def set_playback_control(
    speaker_ip: str,
    command: str,
    *,
    api_type: str,
    port: int = DEFAULT_PORT,
    timeout: float = 10.0,
) -> WamResponse:
    """Send one native playback command."""
    normalized_api = api_type.upper()
    allowed = {
        "UIC": {"pause", "resume"},
        "CPM": {"play", "pause", "stop"},
    }
    if normalized_api not in allowed:
        raise ValueError("Playback API must be UIC or CPM")
    if command not in allowed[normalized_api]:
        choices = ", ".join(sorted(allowed[normalized_api]))
        raise ValueError(
            f"{normalized_api} playback command must be one of: {choices}"
        )
    return request(
        speaker_ip,
        "SetPlaybackControl",
        [("playbackcontrol", command, "str")],
        port=port,
        timeout=timeout,
        api_type=normalized_api,
        power_on=True,
    )


def pause_playback(
    speaker_ip: str,
    *,
    port: int = DEFAULT_PORT,
    timeout: float = 10.0,
) -> WamResponse:
    """Pause native-provider or local playback."""
    status = get_playback_status(speaker_ip, port=port, timeout=timeout)
    api_type = "CPM" if status.is_native_cp else "UIC"
    return set_playback_control(
        speaker_ip,
        "pause",
        api_type=api_type,
        port=port,
        timeout=timeout,
    )


def resume_playback(
    speaker_ip: str,
    *,
    port: int = DEFAULT_PORT,
    timeout: float = 10.0,
) -> WamResponse:
    """Resume TuneIn or DLNA playback when supported."""
    status = get_playback_status(speaker_ip, port=port, timeout=timeout)
    if status.is_native_cp:
        command = "play"
        api_type = "CPM"
    elif status.submode == "dlna":
        command = "resume"
        api_type = "UIC"
    else:
        raise WamApiError(
            "URL playback cannot be resumed reliably; start the source again"
        )
    response = set_playback_control(
        speaker_ip,
        command,
        api_type=api_type,
        port=port,
        timeout=timeout,
    )
    if get_mute(speaker_ip, port=port, timeout=timeout):
        set_mute(speaker_ip, False, port=port, timeout=timeout)
    return response


def stop_playback(
    speaker_ip: str,
    *,
    standby: bool = False,
    port: int = DEFAULT_PORT,
    timeout: float = 10.0,
) -> WamResponse:
    """Stop TuneIn or safely quiesce URL/DLNA playback."""
    status = get_playback_status(speaker_ip, port=port, timeout=timeout)
    if status.is_native_cp:
        response = set_playback_control(
            speaker_ip,
            "stop",
            api_type="CPM",
            port=port,
            timeout=timeout,
        )
    else:
        response = set_playback_control(
            speaker_ip,
            "pause",
            api_type="UIC",
            port=port,
            timeout=timeout,
        )
        standby = True
    if standby:
        set_mute(speaker_ip, True, port=port, timeout=timeout)
    return response


def get_device_id(
    speaker_ip: str,
    *,
    port: int = DEFAULT_PORT,
    timeout: float = 5.0,
) -> str:
    """Return the speaker's stable 12-character ID from the CPM API."""
    response = request(
        speaker_ip,
        "GetDeviceId",
        port=port,
        timeout=timeout,
        api_type="CPM",
    )
    raw_device_id = _first_value(response, "device_id", "deviceid")
    if not raw_device_id:
        raise WamApiError("Samsung WAM response did not contain device_id")
    device_id = normalize_device_id(raw_device_id)
    if not device_id:
        raise WamApiError("Samsung WAM returned an empty device_id")
    return device_id


def identify(
    speaker_ip: str,
    *,
    port: int = DEFAULT_PORT,
    timeout: float = 5.0,
) -> WamIdentity:
    """Read stable identity and current display name from a WAM speaker."""
    name_response = probe(speaker_ip, port=port, timeout=timeout)
    name = _first_value(name_response, "spkname", "speakername")
    device_id = get_device_id(speaker_ip, port=port, timeout=timeout)
    return WamIdentity(device_id=device_id, name=name)


def play_url(
    speaker_ip: str,
    stream_url: str,
    *,
    port: int = DEFAULT_PORT,
    timeout: float = 10.0,
) -> WamResponse:
    """Tell the speaker to fetch and play a local HTTP stream."""
    return request(
        speaker_ip,
        "SetUrlPlayback",
        [
            ("url", stream_url, "cdata"),
            ("buffersize", 0, "dec"),
            ("seektime", 0, "dec"),
            ("resume", 0, "dec"),
        ],
        port=port,
        timeout=timeout,
    )

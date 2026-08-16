"""Small client for the local Samsung WAM HTTP API."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, replace
from urllib.parse import quote
from urllib.request import ProxyHandler, build_opener
from xml.etree import ElementTree
from xml.sax.saxutils import escape

LOGGER = logging.getLogger(__name__)

DEFAULT_PORT = 55001
MIN_VOLUME = 0
MAX_VOLUME = 100
# A day, which is already past any use for a timer that exists to let a speaker
# go dark after listening. The bound is here because there is no upper end
# otherwise: an oversized value is accepted by the speaker as a plausible
# `sleeptime` and simply never fires, which looks exactly like a timer that does
# not work. The component setting that will feed this does not exist on `main`
# yet; when it lands it takes the same range rather than inventing a second one.
MAX_SLEEP_TIMER_SECONDS = 86400
MAX_RESPONSE_BYTES = 1024 * 1024
API_TYPES = ("UIC", "CPM")
# Default wait for a command this firmware answers by staying silent rather
# than refusing, so that asking costs a second instead of five. `GetSpkName`
# answers in 0.14 s and `get_volume` in 0.12 s, so a second is generous for one
# that is going to answer at all.
#
# Seven commands are in that class - `GetPowerStatus`, `GetLedStatus`,
# `GetStandbyMode`, `GetSpkStatus`, `GetFeature`, `GetPowerSaving` and
# `GetAutoPowerDown` - but only the first has a wrapper here, so this is the
# default of exactly one function today. Give any of the others a wrapper and
# it wants this as its default too; nothing applies it automatically.
SILENT_COMMAND_TIMEOUT = 1.0
LOCAL_OPENER = build_opener(ProxyHandler({}))
_NAME_RE = re.compile(r"\A[A-Za-z_][A-Za-z0-9_.-]*\Z")
_HOST_RE = re.compile(r"\A[A-Za-z0-9._:%\[\]-]+\Z")


class WamApiError(RuntimeError):
    """Raised when a speaker rejects or cannot receive a command."""


class WamModeError(WamApiError):
    """Raised when the speaker's submode cannot accept local playback."""


@dataclass(frozen=True, slots=True)
class WamResponse:
    """Parsed response returned by the speaker."""

    method: str | None
    result: str | None
    body: str
    values: dict[str, str] = field(default_factory=dict)
    matched: bool = True
    """False when the speaker answered with an unrelated event.

    The firmware often replies to a command with whatever it happens to be
    broadcasting, for example ``PausePlaybackEvent`` during a URL handover or
    ``CurrentFunc`` right after ``SetIpInfo``. Such a body says nothing about
    whether the command succeeded, so it must not be read as a rejection.
    """


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


def _attribute_value(value: str | int) -> str:
    """Escape one value for a double-quoted XML attribute.

    ``quoteattr`` would switch to single quotes around a value containing a
    double quote, and nothing measured says the firmware's parser accepts that
    spelling, so the quoting stays fixed and the value is escaped instead.
    """

    return escape(str(value), {'"': "&quot;"})


def _validate_xml_name(value: str, label: str) -> str:
    """Reject a method or parameter name that is not a plain XML name."""

    if not _NAME_RE.match(value):
        raise ValueError(f"Invalid WAM {label}: {value!r}")
    return value


def build_command(
    method: str,
    arguments: list[tuple[str, str | int, str]] | None = None,
    *,
    power_on: bool = False,
) -> str:
    """Build the XML command accepted by the Samsung WAM API.

    Values reach the speaker's parser inside an attribute or a CDATA section, so
    every one of them is escaped. A station name or profile field carrying a
    quote or an angle bracket would otherwise close the attribute and inject
    parameters into the command.
    """
    _validate_xml_name(method, "method")
    parts = ["<pwron>on</pwron>"] if power_on else []
    parts.append(f"<name>{method}</name>")
    for name, value, value_type in arguments or []:
        _validate_xml_name(name, "parameter name")
        if value_type == "cdata":
            safe_value = str(value).replace("]]>", "]]]]><![CDATA[>")
            parts.append(
                f'<p type="cdata" name="{name}" val="empty">'
                f"<![CDATA[{safe_value}]]></p>"
            )
        elif value_type in {"str", "dec"}:
            parts.append(
                f'<p type="{value_type}" name="{name}" '
                f'val="{_attribute_value(value)}"/>'
            )
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
    validate_speaker_address(speaker_ip, port)
    if api_type.upper() not in API_TYPES:
        raise ValueError(f"WAM API type must be one of: {', '.join(API_TYPES)}")
    command = build_command(method, arguments, power_on=power_on)
    return (
        f"http://{speaker_ip}:{port}/{api_type.upper()}"
        f"?cmd={quote(command, safe='')}"
    )


def validate_speaker_address(speaker_ip: str, port: int = DEFAULT_PORT) -> None:
    """Reject an address that would change the request instead of addressing it.

    The address is interpolated into a URL and, on the persistent control
    connection, into request headers. A value carrying a slash, a space or a
    line break would target another path or append headers of its own.
    """

    if not speaker_ip or not _HOST_RE.match(speaker_ip):
        raise ValueError(f"Invalid Samsung WAM address: {speaker_ip!r}")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError(f"Invalid Samsung WAM port: {port!r}")


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


# Commands whose reply carries an unrelated name. Only entries observed on a
# physical M5 belong here; guesses would reintroduce the bug this table fixes.
_RESPONSE_ALIASES: dict[str, tuple[str, ...]] = {
    "SetPlaybackControl": ("PlaybackStatus",),
    "SetSharePlaybackControl": ("MusicInfo",),
    "SetNewFolderPlaybackControl": ("MusicInfo",),
    "SetPlayPreset": ("RadioInfo", "CpInfo", "PlayStatus"),
}

# Unsolicited state changes. They can arrive in place of any reply and say
# nothing about the command that was sent, so they never count as a match.
_EVENT_SUFFIX = "Event"


def _normalise_method(name: str) -> str:
    stripped = name
    for prefix in ("Set", "Get", "Current"):
        if stripped.startswith(prefix):
            stripped = stripped[len(prefix) :]
            break
    return "".join(character for character in stripped if character.isalnum()).lower()


def methods_agree(command: str, response_method: str) -> bool:
    """Report whether a reply belongs to the command that was sent."""

    if command == response_method:
        return True
    if response_method.endswith(_EVENT_SUFFIX):
        return False
    if response_method in _RESPONSE_ALIASES.get(command, ()):
        return True
    left, right = _normalise_method(command), _normalise_method(response_method)
    if not left or not right:
        return False
    return left in right or right in left


def _error_code(root: ElementTree.Element, response_node: ElementTree.Element) -> str:
    """Return the error code, whichever spelling and shape the firmware used.

    Measured on a physical M5: the code arrives as a child element spelled
    ``errCode``, not only as the ``errcode`` attribute the original parser
    looked for. Missing it made every failure look identical.
    """

    for spelling in ("errcode", "errCode"):
        if (attribute := response_node.get(spelling)) is not None:
            return attribute
    for spelling in ("errCode", "errcode"):
        for node in (response_node, root):
            if (text := node.findtext(spelling)) is not None and text.strip():
                return text.strip()
    return ""


def parse_response(body: str, *, expected: str | None = None) -> WamResponse:
    """Parse one XML response from a Samsung WAM speaker.

    ``expected`` is the command that was sent. When the speaker answers with a
    different method it is an unsolicited event rather than this command's
    result, so it is returned with ``matched=False`` instead of raising.
    """
    try:
        root = ElementTree.fromstring(body)  # nosec B314 - small local response
    except ElementTree.ParseError as error:
        raise WamApiError(
            f"Samsung WAM returned invalid XML: {body[:200]}"
        ) from error

    response_node = root.find("response")
    result = response_node.get("result") if response_node is not None else None
    response_method = root.findtext("method")

    matched = True
    if expected is not None and response_method is not None:
        matched = methods_agree(expected, response_method)

    if matched and result != "ok":
        error_code = (
            _error_code(root, response_node) if response_node is not None else ""
        )
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
        matched=matched,
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
            # Bounded: the speaker is a network peer, and an unbounded read of
            # whatever answers on port 55001 is a memory-exhaustion primitive.
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise WamApiError(
                    f"Samsung WAM response exceeded {MAX_RESPONSE_BYTES} bytes"
                )
            body = raw.decode("utf-8", errors="replace")
    except OSError as error:
        raise WamApiError(
            f"Cannot reach Samsung WAM at {speaker_ip}:{port}: {error}"
        ) from error
    return parse_response(body, expected=method)


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


def set_sleep_timer(
    speaker_ip: str,
    seconds: int,
    *,
    port: int = DEFAULT_PORT,
    timeout: float = 5.0,
) -> WamResponse:
    """Arm the sleep timer, or clear it when ``seconds`` is zero."""
    return request(
        speaker_ip,
        "SetSleepTimer",
        sleep_timer_arguments(seconds),
        port=port,
        timeout=timeout,
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


def sleep_timer_arguments(seconds: int) -> list[tuple[str, str | int, str]]:
    """Build the arguments for ``SetSleepTimer``.

    Measured on the M5 on 2026-08-02: ``sleeptime`` counts **seconds**, not
    minutes, and on firing the timer clears itself back to ``sleepoption=off``,
    so arming one leaves nothing configured behind.

    This is the only power lever the firmware answers - ``GetPowerSaving`` and
    ``GetAutoPowerDown`` do not exist here. That does not make it the only way
    the speaker goes dark: it does that on its own once every program talking to
    it has let go, which is its normal behaviour and was measured on 2026-08-16.
    The timer is a fallback for when something has not let go, not the mechanism.

    Zero clears a pending timer rather than arming an immediate one.

    The request parameter is ``option`` while the reply reports ``sleepoption``.
    Both spellings are measured - see ``tools/wam-probes/capture.log`` - so the
    asymmetry belongs to the speaker and is not a typo to tidy away.

    The upper bound is a day. Without one, a mistyped value reaches the speaker
    as a plausible-looking ``sleeptime`` that simply never fires, which is
    indistinguishable from the timer not working at all.
    """
    if (
        isinstance(seconds, bool)
        or not isinstance(seconds, int)
        or not 0 <= seconds <= MAX_SLEEP_TIMER_SECONDS
    ):
        raise ValueError(
            "Sleep timer seconds must be a whole number of seconds between 0 "
            f"and {MAX_SLEEP_TIMER_SECONDS}: {seconds!r}"
        )
    return [
        ("option", "start" if seconds > 0 else "off", "str"),
        ("sleeptime", seconds, "dec"),
    ]


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


def require_local_playback_mode(
    speaker_ip: str,
    *,
    port: int = DEFAULT_PORT,
    timeout: float = 5.0,
) -> str:
    """Fail fast when the speaker cannot start locally offered playback.

    In submode ``cp`` the speaker keeps serving a native content provider. It
    still fetches an offered URL or DLNA object over HTTP and then stays silent,
    which looks like a protocol bug but is a mode problem.

    The speaker also returns to ``cp`` on its own after a failed attempt or while
    idle, so this must be checked immediately before each attempt rather than
    once per session. Nothing observed clears it from software:
    ``SetPlaybackControl stop`` refuses, ``SetFunc wifi`` is accepted without
    effect, ``SetUrlPlayback`` does nothing and a full standby leaves it as is.
    """

    submode = get_play_status(speaker_ip, port=port, timeout=timeout).submode
    if submode == "cp":
        raise WamModeError(
            "Speaker is in content-provider mode (submode=cp). Locally offered "
            "playback would be fetched and never started, and no command clears "
            "this state - power-cycle the speaker and retry."
        )
    return submode or ""


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
    except WamApiError as error:
        LOGGER.debug(
            "Could not enrich play status with provider metadata: %s", error
        )
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
    timeout: float = SILENT_COMMAND_TIMEOUT,
) -> str | None:
    """Return the firmware's raw power-status value.

    Measured on the M5: this command is not implemented and the speaker stays
    silent, so every call here runs out its timeout. The short wait is this
    function's *default* rather than a clamp on what it is given. Both halves
    matter: a caller that says nothing gets the guardrail without having to know
    the command is special, and a caller on a firmware that does answer this,
    slowly, can still ask for five seconds and get them. Silently shortening a
    timeout somebody wrote down would be its own bug.

    Callers that want a snapshot rather than this one field should go through
    `_best_effort_power_status`, which also survives the silence.
    """
    response = request(
        speaker_ip,
        "GetPowerStatus",
        port=port,
        timeout=timeout,
    )
    return _first_value(response, "powerStatus", "powerstatus")


def _best_effort_power_status(
    speaker_ip: str,
    *,
    port: int,
    timeout: float,
) -> str | None:
    """Return the power status, or ``None`` when the speaker does not answer.

    This firmware does not implement ``GetPowerStatus``: like ``GetFeature`` and
    the rest of its class, it stays silent instead of refusing, so the call
    always runs out its timeout.
    Letting that propagate cost the whole snapshot - ``status`` reported a
    healthy speaker as unreachable over one field it was never going to fill,
    and sent its owner to the wall socket at 02:40 for nothing. Optional detail
    is dropped the way ``get_playback_status`` already drops provider metadata.

    The caller's ``timeout`` is a budget here, not an instruction: whichever of
    it and the silent-command default is shorter wins. A generous one must not
    be spent entirely on silence - measured on the M5, ``GetSpkName`` answers in
    0.14 s and ``get_volume`` in 0.12 s while this one takes whatever it is
    given, every time - and a tight one must still be honoured, or a snapshot
    asked for in 0.1 s would sit here for a second. The public
    ``get_power_status`` keeps its own timeout exactly as written; only this
    path, which is assembling a snapshot out of several reads, trims it.
    """
    try:
        return get_power_status(
            speaker_ip,
            port=port,
            timeout=min(timeout, SILENT_COMMAND_TIMEOUT),
        )
    except WamApiError as error:
        LOGGER.debug("Speaker did not report power status: %s", error)
        return None


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
        power_status=_best_effort_power_status(
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


def _require_raw_uuid(value: str, label: str) -> None:
    """Reject the ``uuid:`` prefix.

    Measured on a physical M5: a prefixed identifier makes the firmware ignore
    the command completely, with no reply and no error. ``GetDmsList`` entries do
    use ``uuid:`` in ``dmsid``, but that is a different field.
    """

    if not value:
        raise ValueError(f"{label} cannot be empty")
    if value.startswith("uuid:"):
        raise ValueError(f"{label} must be a raw UUID, without the uuid: prefix")


def register_share_source(
    speaker_ip: str,
    client_uuid: str,
    server_address: str,
    *,
    port: int = DEFAULT_PORT,
    timeout: float = 5.0,
) -> WamResponse:
    """Register this client and its media server address with the speaker."""

    _require_raw_uuid(client_uuid, "Client UUID")
    if ":" not in server_address:
        raise ValueError("Server address must be host:port")
    return request(
        speaker_ip,
        "SetIpInfo",
        [("uuid", client_uuid, "str"), ("ip", server_address, "str")],
        port=port,
        timeout=timeout,
    )


def play_share(
    speaker_ip: str,
    *,
    device_udn: str,
    object_id: str,
    source_name: str = "WAMBridge",
    playtime: int = 0,
    port: int = DEFAULT_PORT,
    timeout: float = 5.0,
) -> WamResponse:
    """Start playback of one object offered by this client's media server.

    ``device_udn`` must be the same raw UUID passed to
    :func:`register_share_source`; the speaker resolves it against that
    registration to find the server address.

    ``playertype`` and ``source_name`` were measured to have no effect on this
    path, so no fallback variants are attempted.
    """

    _require_raw_uuid(device_udn, "device_udn")
    if not object_id:
        raise ValueError("Object ID cannot be empty")
    if playtime < 0:
        raise ValueError("Playtime cannot be negative")
    return request(
        speaker_ip,
        "SetSharePlaybackControl",
        [
            ("playbackcontrol", "play", "str"),
            ("playertype", "allshare", "str"),
            ("sourcename", source_name, "cdata"),
            ("playtime", playtime, "dec"),
            ("device_udn", device_udn, "str"),
            ("objectid", object_id, "str"),
        ],
        port=port,
        timeout=timeout,
    )

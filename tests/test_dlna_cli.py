from unittest import TestCase
from unittest.mock import patch

from wambridge.dlna import AV_TRANSPORT_SERVICE, UpnpService
from wambridge.dlna_cli import _secure_stop, _wait_for_completion


SERVICE = UpnpService(
    service_type=AV_TRANSPORT_SERVICE,
    service_id="AVTransport",
    control_url="http://10.0.0.118/control",
)


class DlnaShutdownTests(TestCase):
    @patch("wambridge.dlna_cli.stop")
    @patch("wambridge.dlna_cli.set_volume")
    @patch("wambridge.dlna_cli.set_mute")
    def test_does_not_mutate_untouched_speaker(
        self,
        mute_mock,
        volume_mock,
        stop_mock,
    ) -> None:
        _secure_stop(
            None,
            speaker_ip="10.0.0.118",
            speaker_port=55001,
            previous_volume=None,
            previous_mute=None,
            speaker_touched=False,
            transport_touched=False,
        )

        mute_mock.assert_not_called()
        volume_mock.assert_not_called()
        stop_mock.assert_not_called()

    @patch("wambridge.dlna_cli._transport_state", return_value="STOPPED")
    @patch("wambridge.dlna_cli.sleep")
    def test_completion_accepts_stopped_on_first_poll(
        self,
        sleep_mock,
        state_mock,
    ) -> None:
        _wait_for_completion(SERVICE, poll_interval=0)

        sleep_mock.assert_called_once_with(0)
        state_mock.assert_called_once_with(SERVICE)

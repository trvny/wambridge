import socket
import sys
from unittest import TestCase, skipUnless
from unittest.mock import patch

from wambridge.connections import established_connections_to


class ConnectionCountTests(TestCase):
    def test_unknown_off_windows(self) -> None:
        with patch("wambridge.connections.sys") as sys_mock:
            sys_mock.platform = "linux"
            self.assertIsNone(established_connections_to("10.0.0.118"))

    def test_unknown_for_malformed_address(self) -> None:
        self.assertIsNone(established_connections_to("not-an-address"))
        self.assertIsNone(established_connections_to(""))

    @skipUnless(sys.platform.startswith("win"), "socket table is a Windows API")
    def test_counts_real_connections(self) -> None:
        """Guard against the counter that always answers zero.

        A checker that cannot see a hold is worse than no checker: it reports
        the speaker as free while a leaked helper keeps it awake.
        """
        before = established_connections_to("127.0.0.1")
        self.assertIsNotNone(before)

        server = socket.socket()
        self.addCleanup(server.close)
        server.bind(("127.0.0.1", 0))
        server.listen(2)
        client = socket.create_connection(server.getsockname())
        self.addCleanup(client.close)
        accepted, _ = server.accept()
        self.addCleanup(accepted.close)

        during = established_connections_to("127.0.0.1")
        # Both ends are local, so one connection shows up as two rows. Asserting
        # a delta rather than a total keeps this honest on a busy machine.
        self.assertGreaterEqual(during - before, 2)

import socket
import sys
from unittest import TestCase, skipUnless
from unittest.mock import patch

from wambridge.connections import attached_connections_to


class ConnectionCountTests(TestCase):
    def test_unknown_off_windows(self) -> None:
        with patch("wambridge.connections.sys") as sys_mock:
            sys_mock.platform = "linux"
            self.assertIsNone(attached_connections_to("10.0.0.118"))

    def test_unknown_for_malformed_address(self) -> None:
        self.assertIsNone(attached_connections_to("not-an-address"))
        self.assertIsNone(attached_connections_to(""))

    @skipUnless(sys.platform.startswith("win"), "socket table is a Windows API")
    def test_counts_open_connections(self) -> None:
        """Guard against the counter that always answers zero.

        A checker that cannot see a hold is worse than no checker: it reports
        the speaker as free while something is still attached.
        """
        before = attached_connections_to("127.0.0.1")
        self.assertIsNotNone(before)

        server = socket.socket()
        self.addCleanup(server.close)
        server.bind(("127.0.0.1", 0))
        server.listen(2)
        client = socket.create_connection(server.getsockname())
        self.addCleanup(client.close)
        accepted, _ = server.accept()
        self.addCleanup(accepted.close)

        during = attached_connections_to("127.0.0.1")
        # Both ends are local, so one connection shows up as two rows. Asserting
        # a delta rather than a total keeps this honest on a busy machine.
        self.assertGreaterEqual(during - before, 2)

    @skipUnless(sys.platform.startswith("win"), "socket table is a Windows API")
    def test_counts_half_closed_connections(self) -> None:
        """The case this exists for is a killed session, not a live one.

        A hard-killed helper has its sockets closed by the kernel at once, so
        they sit in FIN_WAIT or CLOSE_WAIT. Counting only ESTABLISHED would
        report nothing attached in exactly the scenario being investigated.
        """
        before = attached_connections_to("127.0.0.1")

        server = socket.socket()
        self.addCleanup(server.close)
        server.bind(("127.0.0.1", 0))
        server.listen(2)
        client = socket.create_connection(server.getsockname())
        accepted, _ = server.accept()
        self.addCleanup(accepted.close)

        # One side goes away without the other closing: the peer is left in
        # CLOSE_WAIT and the closer in FIN_WAIT, never ESTABLISHED again.
        client.close()

        during = attached_connections_to("127.0.0.1")
        self.assertGreaterEqual(
            during - before,
            1,
            "a half-closed socket must still count as attached",
        )

import os
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

    @skipUnless(sys.platform.startswith("win"), "socket table is a Windows API")
    def test_own_pid_does_not_hide_a_socket_the_caller_still_holds(self) -> None:
        """A leak of our own is a leak, and must not vanish from our own count.

        `own_pid` exists to skip *closing* sockets, not to excuse the caller.
        Dropping them wholesale would make a helper's own leaked connection
        invisible to the very line printed to prove it let go - `holding=0`
        would then mean "nobody checked", which is what this counter exists to
        prevent.
        """
        server = socket.socket()
        self.addCleanup(server.close)
        server.bind(("127.0.0.1", 0))
        server.listen(2)
        client = socket.create_connection(server.getsockname())
        self.addCleanup(client.close)
        accepted, _ = server.accept()
        self.addCleanup(accepted.close)

        counted = attached_connections_to("127.0.0.1")
        with_own = attached_connections_to("127.0.0.1", own_pid=os.getpid())

        # Both ends are ESTABLISHED and belong to this process. Neither is
        # closing, so neither may be skipped.
        self.assertEqual(counted, with_own)

    @skipUnless(sys.platform.startswith("win"), "socket table is a Windows API")
    def test_own_pid_skips_a_socket_the_caller_has_already_closed(self) -> None:
        """Measured on 2026-08-15: a locally closed socket lingers 0.5-1.5 s.

        A reading taken right after a teardown waited that out and then
        reported the teardown itself as a hold. That delay is paid on every
        helper exit, and a helper exit precedes every seek.
        """
        server = socket.socket()
        self.addCleanup(server.close)
        server.bind(("127.0.0.1", 0))
        server.listen(2)
        client = socket.create_connection(server.getsockname())
        accepted, _ = server.accept()
        self.addCleanup(accepted.close)

        before = attached_connections_to("127.0.0.1", own_pid=os.getpid())
        # This end closes; the peer does not. The closer goes to FIN_WAIT and
        # is the caller's own business, while the peer sits in CLOSE_WAIT
        # holding on and still counts.
        client.close()
        after = attached_connections_to("127.0.0.1", own_pid=os.getpid())

        self.assertEqual(before - after, 1)

"""A released question can cause sixty simultaneous fresh TCP connections."""
from concurrent.futures import ThreadPoolExecutor
import socket
import unittest
import test_workspace_routes as fixture

class HTTPBurst(unittest.TestCase):
    def test_listener_accepts_quiz_sized_pending_connection_burst(self):
        server_type=getattr(fixture.S,'EventHTTPServer',fixture.S.ThreadingHTTPServer)
        server=server_type(('127.0.0.1',0),fixture.S.Handler)
        self.addCleanup(server.server_close)
        # Hold acceptance briefly, as scheduler/CPU work can do in a real burst.
        # This tests the listen queue without issuing HTTP or touching event data.
        def connect(_):
            try:return socket.create_connection(server.server_address,timeout=.5)
            except OSError:return None
        with ThreadPoolExecutor(max_workers=60) as workers:
            sockets=list(workers.map(connect,range(60)))
        for connection in sockets:
            if connection:connection.close()
        self.assertEqual(sum(x is not None for x in sockets),60)

if __name__=='__main__':unittest.main()

import json
import os
import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import start_local


class LocalStartTests(unittest.TestCase):
    def test_running_app_is_recognized_and_reused(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'extractor_version': 'test', 'capabilities': {}}).encode())

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            with patch.dict(os.environ, {'PORT': str(port)}):
                self.assertEqual(start_local.select_port(), (port, True))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_unrelated_occupied_port_is_not_reused(self):
        with socket.socket() as occupied:
            occupied.bind(('127.0.0.1', 0))
            occupied.listen()
            port = occupied.getsockname()[1]
            with patch.dict(os.environ, {'PORT': str(port)}):
                chosen, existing = start_local.select_port()
            self.assertFalse(existing)
            self.assertNotEqual(chosen, port)
            with socket.socket() as check:
                check.bind(('127.0.0.1', chosen))


if __name__ == '__main__':
    unittest.main()

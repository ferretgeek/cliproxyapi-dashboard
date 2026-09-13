"""Authenticated fake upstream used only by container integration tests."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        expected = os.environ['FIXTURE_KEY']
        if self.path == '/v1/models':
            allowed = self.headers.get('Authorization') == 'Bearer ' + expected
            payload = {'data': [{'id': 'fixture-model'}]}
        else:
            allowed = self.headers.get('X-Management-Key') == expected
            payload = {'port': 8317}
        self.send_response(200 if allowed else 401)
        self.send_header('X-CPA-VERSION', 'v7.2.1')
        self.send_header('X-CPA-COMMIT', 'a' * 40)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(payload if allowed else {'error': 'unauthorized'}).encode())

    def log_message(self, *args):
        pass


if __name__ == '__main__':
    ThreadingHTTPServer(('0.0.0.0', 8317), Handler).serve_forever()

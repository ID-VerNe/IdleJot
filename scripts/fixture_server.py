"""Shared fake-server base for isolated HTTP fixtures.

Fixture handlers across the check scripts repeated the same boilerplate:
a quiet ``log_message``, JSON request/response encoding, and a threaded
``ThreadingHTTPServer`` started and stopped by hand. ``FixtureHandler``
absorbs the per-request parts; :func:`fixture_server` absorbs the
lifecycle. Per-fixture route tables, recorded requests and sent-message
state stay in each script.

``network_guard`` contract (see ``check_fixture_support.network_guard``):
the guard keeps its ports set by reference, so a caller may pass that set
as ``ports=`` and the bound port is registered automatically. Callers
that install the guard after starting the fixture can instead read
``server.server_port`` from the yielded server and add it themselves.
"""
from contextlib import contextmanager
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FixtureHandler(BaseHTTPRequestHandler):
    """Quiet JSON fixture handler base.

    Subclasses implement their own ``do_GET``/``do_POST`` route tables and
    keep their own state; this base only removes log noise and centralizes
    body/response encoding.
    """

    def log_message(self, *args):
        pass

    def read_json(self):
        """Decode the request body as JSON."""
        return json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))))

    def send_json(self, payload, status=200):
        """Send ``payload`` as a JSON response body."""
        raw = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def begin_sse(self):
        """Open a 200 ``text/event-stream`` response."""
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()

    def send_sse(self, payload):
        """Write one SSE ``data:`` chunk."""
        self.wfile.write(('data: ' + json.dumps(payload, ensure_ascii=False) + '\n\n').encode())

    def end_sse(self):
        """Write the terminating ``[DONE]`` chunk."""
        self.wfile.write(b'data: [DONE]\n\n')


@contextmanager
def fixture_server(handler, *, port=0, ports=None):
    """Serve ``handler`` on a loopback port for the duration of the block.

    ``port=0`` binds an ephemeral port; pass a fixed port when the
    surrounding test dictates one. ``ports`` may be a ``network_guard``
    ports set; the bound port is added to it so guarded loopback
    connections to the fixture stay allowed. Yields the running server;
    read ``server.server_port`` from it.
    """
    server = ThreadingHTTPServer(('127.0.0.1', port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        if ports is not None:
            ports.add(server.server_port)
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        if thread.is_alive():
            raise AssertionError('fixture server did not stop')

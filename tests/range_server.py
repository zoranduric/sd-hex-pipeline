"""Static file server with HTTP Range support, for tests.

Run as a separate process: GDAL holds the GIL while doing HTTP I/O, so an
in-process server would deadlock. Prints the bound port on stdout.
Usage: python range_server.py <root_dir>
"""

import functools
import http.server
import sys
from pathlib import Path


class RangeHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path.startswith("/fail/"):
            self.send_error(500, "simulated outage")
            return
        rng = self.headers.get("Range")
        path = Path(self.translate_path(self.path))
        if not rng or not path.is_file():
            return super().do_GET()
        data = path.read_bytes()
        start, _, end = rng.replace("bytes=", "").partition("-")
        start = int(start)
        end = min(int(end) if end else len(data) - 1, len(data) - 1)
        chunk = data[start : end + 1]
        self.send_response(206)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
        self.send_header("Content-Length", str(len(chunk)))
        self.end_headers()
        self.wfile.write(chunk)

    def do_HEAD(self):
        if self.path.startswith("/fail/"):
            self.send_error(500, "simulated outage")
            return
        super().do_HEAD()


if __name__ == "__main__":
    handler = functools.partial(RangeHandler, directory=sys.argv[1])
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    print(httpd.server_address[1], flush=True)
    httpd.serve_forever()

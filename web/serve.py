#!/usr/bin/env python3
"""Optional local web server for the Music Library static site.

The site is designed to run by simply double-clicking ``index.html`` (it uses
classic <script> tags and data-as-JS, so no fetch/CORS is involved). This helper
is only a convenience if you prefer a real ``http://localhost`` origin.

    python serve.py            # serve this folder on http://localhost:8000
    python serve.py 9000       # choose a port
"""
from __future__ import annotations

import http.server
import socketserver
import sys
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    with socketserver.TCPServer(("127.0.0.1", port), Handler) as httpd:
        print(f"Serving {WEB_DIR} at http://localhost:{port}/  (Ctrl+C to stop)")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

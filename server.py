#!/usr/bin/env python3
"""
Optional self-hosted runner -- same handler as the Vercel function, served by
the standard library. For anyone who would rather run this themselves than use
a serverless tier.

    python3 server.py            # listens on 127.0.0.1:8000
"""

import os
import sys
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "api"))

from convert import handler  # noqa: E402

if __name__ == "__main__":
    host = os.environ.get("CGM_HOST", "127.0.0.1")
    port = int(os.environ.get("CGM_PORT", "8000"))
    print("listening on http://%s:%d/api/convert" % (host, port))
    ThreadingHTTPServer((host, port), handler).serve_forever()

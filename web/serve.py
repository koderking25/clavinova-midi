"""Local server for the browser version, sending the two headers that let a page use
more than one processor core.

Browsers only hand out shared memory, and therefore multi threaded computation, to pages
that send:

    Cross-Origin-Opener-Policy: same-origin
    Cross-Origin-Embedder-Policy: credentialless

"credentialless" is used rather than "require-corp" so the page can still load the
runtime from a public CDN, which would otherwise be blocked.

    python web/serve.py [port]

Cloudflare Pages and Workers can send the same two headers, so the deployed site gets
the same benefit.
"""
import functools
import http.server
import os
import sys

EXTRA_TYPES = {
    ".mjs": "text/javascript",
    ".js": "text/javascript",
    ".onnx": "application/octet-stream",
    ".bin": "application/octet-stream",
    ".wasm": "application/wasm",
    ".json": "application/json",
}


class Handler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Embedder-Policy", "credentialless")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def guess_type(self, path):
        ext = os.path.splitext(path)[1].lower()
        return EXTRA_TYPES.get(ext) or super().guess_type(path)

    def log_message(self, fmt, *args):
        if "404" in (fmt % args):
            super().log_message(fmt, *args)


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8792
    root = os.path.dirname(os.path.abspath(__file__))
    handler = functools.partial(Handler, directory=root)
    print(f"serving {root} on http://127.0.0.1:{port} with shared memory enabled")
    http.server.ThreadingHTTPServer(("127.0.0.1", port), handler).serve_forever()


if __name__ == "__main__":
    main()

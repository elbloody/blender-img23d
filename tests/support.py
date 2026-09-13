# SPDX-License-Identifier: GPL-3.0-or-later
"""Outils partagés : serveur HTTP jetable et contexte de travail factice."""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.jobs import JobContext  # noqa: E402


class RecordingContext(JobContext):
    """``JobContext`` qui mémorise tout, pour pouvoir l'inspecter dans un test."""

    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []
        self.slept: list[float] = []

    def report(self, message: str, progress: float | None = None) -> None:
        self.messages.append(message)
        super().report(message, progress)

    def sleep(self, seconds: float) -> None:
        # Les tests ne doivent pas vraiment attendre : on note et on continue.
        self.slept.append(seconds)
        self.raise_if_cancelled()


class FakeServer:
    """Serveur HTTP local piloté par une table ``(méthode, chemin) -> réponse``.

    Une réponse est soit ``(status, content_type, corps)``, soit un appelable
    recevant ``(handler, corps_de_requête)`` et rendant ce même triplet.
    """

    def __init__(self, routes: dict) -> None:
        self.routes = routes
        self.requests: list[tuple[str, str, bytes]] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # silence
                pass

            def _handle(self, method: str) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                server.requests.append((method, self.path, body))

                route = server.routes.get((method, self.path.split("?")[0]))
                if route is None:
                    self.send_error(404, "route inconnue")
                    return

                status, content_type, payload = route(self, body) if callable(route) else route
                if isinstance(payload, (dict, list)):
                    payload = json.dumps(payload).encode()
                elif isinstance(payload, str):
                    payload = payload.encode()

                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

        self._httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def __enter__(self) -> "FakeServer":
        self._thread.start()
        return self

    def __exit__(self, *exc_info) -> bool:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)
        return False

    @property
    def url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    def bodies(self, method: str, path: str) -> list[bytes]:
        return [body for m, p, body in self.requests if m == method and p.split("?")[0] == path]

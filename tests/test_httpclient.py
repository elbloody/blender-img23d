# SPDX-License-Identifier: GPL-3.0-or-later
"""Le client HTTP maison : multipart, erreurs, téléchargement."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from .support import FakeServer, RecordingContext

from backends import BackendError  # noqa: E402
from backends import httpclient  # noqa: E402
from core.jobs import JobCancelled  # noqa: E402


class TestMultipart(unittest.TestCase):
    def test_mapping_form(self):
        content_type, body = httpclient.encode_multipart(
            fields={"tier": "Regular"},
            files={"file": ("a.png", b"\x89PNG", "image/png")},
        )
        boundary = content_type.split("boundary=")[1]
        self.assertIn(f"--{boundary}".encode(), body)
        self.assertIn(b'name="tier"', body)
        self.assertIn(b'filename="a.png"', body)
        self.assertIn(b"\x89PNG", body)
        self.assertTrue(body.endswith(f"--{boundary}--\r\n".encode()))

    def test_repeated_field_names(self):
        """Certaines API attendent plusieurs vues sous le même champ."""
        _, body = httpclient.encode_multipart(
            files=[
                ("images", "front.png", b"FRONT", "image/png"),
                ("images", "back.png", b"BACK", "image/png"),
            ]
        )
        self.assertEqual(body.count(b'name="images"'), 2)
        self.assertIn(b"FRONT", body)
        self.assertIn(b"BACK", body)

    def test_empty_is_valid(self):
        content_type, body = httpclient.encode_multipart()
        self.assertTrue(content_type.startswith("multipart/form-data"))
        self.assertTrue(body.endswith(b"--\r\n"))


class TestGuessExtension(unittest.TestCase):
    def test_from_mime(self):
        self.assertEqual(httpclient.guess_extension("model/gltf-binary"), ".glb")
        self.assertEqual(httpclient.guess_extension("application/sla"), ".stl")

    def test_falls_back_to_url(self):
        self.assertEqual(
            httpclient.guess_extension("application/octet-stream", "https://x/y/m.obj?sig=1"),
            ".obj",
        )

    def test_default_when_nothing_matches(self):
        self.assertEqual(httpclient.guess_extension("", "https://x/y/z"), ".glb")


class TestRequests(unittest.TestCase):
    def test_json_round_trip(self):
        with FakeServer({("POST", "/echo"): (200, "application/json", {"ok": True})}) as server:
            self.assertEqual(httpclient.post_json(f"{server.url}/echo", {"a": 1}), {"ok": True})
            self.assertEqual(server.bodies("POST", "/echo"), [b'{"a": 1}'])

    def test_http_error_mentions_status_and_body(self):
        with FakeServer({("GET", "/x"): (503, "text/plain", "surcharge")}) as server:
            with self.assertRaises(BackendError) as caught:
                httpclient.request(f"{server.url}/x")
            self.assertIn("503", str(caught.exception))
            self.assertIn("surcharge", str(caught.exception))

    def test_non_json_body_gives_a_readable_error(self):
        with FakeServer({("GET", "/x"): (200, "text/html", "<html>oups</html>")}) as server:
            with self.assertRaises(BackendError) as caught:
                httpclient.get_json(f"{server.url}/x")
            self.assertIn("non-JSON", str(caught.exception))

    def test_connection_refused_is_wrapped(self):
        with self.assertRaises(BackendError):
            httpclient.request("http://127.0.0.1:1/nope", timeout=2)


class TestDownload(unittest.TestCase):
    def test_writes_file_and_reports_progress(self):
        payload = b"glTF" + b"\x00" * 200_000
        with FakeServer({("GET", "/m.glb"): (200, "model/gltf-binary", payload)}) as server:
            ctx = RecordingContext()
            with tempfile.TemporaryDirectory() as directory:
                destination = Path(directory) / "out.glb"
                httpclient.download(f"{server.url}/m.glb", destination, ctx=ctx)
                self.assertEqual(destination.read_bytes(), payload)
                self.assertTrue(any("%" in message for message in ctx.messages))

    def test_no_partial_file_is_left_behind_on_cancel(self):
        payload = b"\x00" * 500_000
        with FakeServer({("GET", "/m.glb"): (200, "model/gltf-binary", payload)}) as server:
            ctx = RecordingContext()
            ctx.cancel()
            with tempfile.TemporaryDirectory() as directory:
                destination = Path(directory) / "out.glb"
                with self.assertRaises(JobCancelled):
                    httpclient.download(f"{server.url}/m.glb", destination, ctx=ctx)
                self.assertEqual(list(Path(directory).iterdir()), [])


if __name__ == "__main__":
    unittest.main()

# SPDX-License-Identifier: GPL-3.0-or-later
"""Backend HTTP générique : les trois formes de réponse acceptées."""

from __future__ import annotations

import base64
import json
import unittest

from .support import FakeServer, RecordingContext

from backends import BackendError, GenerationRequest, SourceImage, create_backend  # noqa: E402

GLB = b"glTF" + b"\x00" * 64


def make_request(**kwargs) -> GenerationRequest:
    images = kwargs.pop(
        "images",
        [SourceImage(data=b"\x89PNG-front", name="f.png", view="FRONT")],
    )
    return GenerationRequest(images=images, **kwargs)


class TestHttpBackend(unittest.TestCase):
    def backend(self, server, **config):
        return create_backend("HTTP", {"url": server.url, "poll_interval": 0.0, **config})

    # -- diagnostic --------------------------------------------------------
    def test_check_reads_server_metadata(self):
        routes = {("GET", "/health"): (200, "application/json", {"gpu": "T4", "model": "hy3d"})}
        with FakeServer(routes) as server:
            status = self.backend(server).check()
        self.assertTrue(status.ok)
        self.assertTrue(any("T4" in detail for detail in status.details))

    def test_check_fails_without_url(self):
        status = create_backend("HTTP", {}).check()
        self.assertFalse(status.ok)
        self.assertIn("URL", status.message)

    def test_check_reports_unreachable_server(self):
        status = create_backend("HTTP", {"url": "http://127.0.0.1:1"}).check()
        self.assertFalse(status.ok)
        self.assertIn("injoignable", status.message)

    # -- les trois formes de réponse ---------------------------------------
    def test_synchronous_binary_response(self):
        routes = {("POST", "/generate"): (200, "model/gltf-binary", GLB)}
        with FakeServer(routes) as server:
            result = self.backend(server).generate(make_request(), RecordingContext())
        self.assertEqual(result.path.read_bytes(), GLB)
        self.assertEqual(result.format, "glb")

    def test_inline_base64_response(self):
        payload = {"model_b64": base64.b64encode(GLB).decode(), "format": "glb", "textured": True}
        with FakeServer({("POST", "/generate"): (200, "application/json", payload)}) as server:
            result = self.backend(server).generate(make_request(), RecordingContext())
        self.assertEqual(result.path.read_bytes(), GLB)
        self.assertTrue(result.textured)

    def test_asynchronous_job_is_polled_until_done(self):
        calls = {"n": 0}

        def status(handler, body):
            calls["n"] += 1
            if calls["n"] < 3:
                return 200, "application/json", {"status": "running", "progress": 30 * calls["n"]}
            return 200, "application/json", {"status": "done", "model_url": "/artifacts/m.glb"}

        routes = {
            ("POST", "/generate"): (200, "application/json", {"job_id": "j1"}),
            ("GET", "/jobs/j1"): status,
            ("GET", "/artifacts/m.glb"): (200, "model/gltf-binary", GLB),
        }
        with FakeServer(routes) as server:
            ctx = RecordingContext()
            result = self.backend(server).generate(make_request(), ctx)

        self.assertEqual(result.path.read_bytes(), GLB)
        self.assertEqual(calls["n"], 3)
        self.assertEqual(len(ctx.slept), 2, "une attente entre chaque sondage")

    # -- transmission des paramètres ---------------------------------------
    def test_images_and_params_are_sent(self):
        images = [
            SourceImage(data=b"FRONT", name="f.png", view="FRONT"),
            SourceImage(data=b"BACK", name="b.png", view="BACK"),
        ]
        with FakeServer({("POST", "/generate"): (200, "model/gltf-binary", GLB)}) as server:
            self.backend(server).generate(
                make_request(images=images, seed=9, octree_resolution=512, prompt="un chat"),
                RecordingContext(),
            )
            payload = json.loads(server.bodies("POST", "/generate")[0])

        self.assertEqual([image["view"] for image in payload["images"]], ["FRONT", "BACK"])
        self.assertEqual(base64.b64decode(payload["images"][1]["data"]), b"BACK")
        self.assertEqual(payload["params"]["seed"], 9)
        self.assertEqual(payload["params"]["octree_resolution"], 512)
        self.assertEqual(payload["params"]["prompt"], "un chat")

    def test_api_key_becomes_an_authorization_header(self):
        captured = {}

        def generate(handler, body):
            captured["auth"] = handler.headers.get("Authorization")
            return 200, "model/gltf-binary", GLB

        with FakeServer({("POST", "/generate"): generate}) as server:
            self.backend(server, api_key="secret-42").generate(make_request(), RecordingContext())
        self.assertEqual(captured["auth"], "Bearer secret-42")

    # -- erreurs -----------------------------------------------------------
    def test_remote_error_state_is_raised(self):
        routes = {
            ("POST", "/generate"): (200, "application/json", {"job_id": "j1"}),
            ("GET", "/jobs/j1"): (200, "application/json", {"status": "error", "error": "OOM"}),
        }
        with FakeServer(routes) as server, self.assertRaises(BackendError) as caught:
            self.backend(server).generate(make_request(), RecordingContext())
        self.assertIn("OOM", str(caught.exception))

    def test_response_without_model_is_rejected(self):
        with FakeServer({("POST", "/generate"): (200, "application/json", {"ok": 1})}) as server, self.assertRaises(BackendError) as caught:
            self.backend(server).generate(make_request(), RecordingContext())
        self.assertIn("model_b64", str(caught.exception))

    def test_invalid_base64_is_reported_clearly(self):
        payload = {"model_b64": "pas du base64 !!"}
        with FakeServer({("POST", "/generate"): (200, "application/json", payload)}) as server, self.assertRaises(BackendError) as caught:
            self.backend(server).generate(make_request(), RecordingContext())
        self.assertIn("base64", str(caught.exception))

    def test_cancellation_stops_the_polling_loop(self):
        routes = {
            ("POST", "/generate"): (200, "application/json", {"job_id": "j1"}),
            ("GET", "/jobs/j1"): (200, "application/json", {"status": "running"}),
        }
        with FakeServer(routes) as server:
            ctx = RecordingContext()
            ctx.cancel()
            from core.jobs import JobCancelled

            with self.assertRaises(JobCancelled):
                self.backend(server).generate(make_request(), ctx)

    def test_non_http_url_is_refused(self):
        backend = create_backend("HTTP", {"url": "ftp://exemple.test"})
        with self.assertRaises(BackendError):
            backend.generate(make_request(), RecordingContext())


if __name__ == "__main__":
    unittest.main()

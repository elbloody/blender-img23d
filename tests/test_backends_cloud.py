# SPDX-License-Identifier: GPL-3.0-or-later
"""Backend cloud : les trois adaptateurs de fournisseur.

Les serveurs sont simulés, mais les formes de requête et de réponse sont
celles des API réelles. Si un fournisseur change son schéma, c'est ici que
ça se verra en premier.
"""

from __future__ import annotations

import base64
import json
import unittest

from .support import FakeServer, RecordingContext

from backends import BackendError, GenerationRequest, SourceImage, create_backend  # noqa: E402
from backends.cloud import PROVIDERS  # noqa: E402

GLB = b"glTF" + b"\x00" * 32


def request() -> GenerationRequest:
    return GenerationRequest(
        images=[SourceImage(data=b"\x89PNG", name="f.png", view="FRONT")],
        with_texture=True,
        face_limit=30000,
    )


class TestMeshy(unittest.TestCase):
    def routes(self, server_holder):
        polls = {"n": 0}

        def task(handler, body):
            polls["n"] += 1
            if polls["n"] < 2:
                return 200, "application/json", {"status": "IN_PROGRESS", "progress": 45}
            return (
                200,
                "application/json",
                {
                    "status": "SUCCEEDED",
                    "progress": 100,
                    "model_urls": {"glb": server_holder["url"] + "/files/m.glb"},
                },
            )

        return {
            ("POST", "/image-to-3d"): (200, "application/json", {"result": "task-7"}),
            ("GET", "/image-to-3d/task-7"): task,
            ("GET", "/files/m.glb"): (200, "model/gltf-binary", GLB),
        }, polls

    def test_full_cycle(self):
        holder = {}
        routes, polls = self.routes(holder)
        with FakeServer(routes) as server:
            holder["url"] = server.url
            backend = create_backend(
                "CLOUD",
                {"provider": "MESHY", "api_key": "k", "api_base": server.url, "poll_interval": 0.0},
            )
            result = backend.generate(request(), RecordingContext())

            payload = json.loads(server.bodies("POST", "/image-to-3d")[0])

        self.assertEqual(result.path.read_bytes(), GLB)
        self.assertEqual(result.meta["provider"], "MESHY")
        self.assertEqual(polls["n"], 2)
        self.assertTrue(payload["image_url"].startswith("data:image/png;base64,"))
        self.assertEqual(base64.b64decode(payload["image_url"].split(",")[1]), b"\x89PNG")
        self.assertTrue(payload["should_texture"])
        self.assertEqual(payload["target_polycount"], 30000)

    def test_task_error_message_is_surfaced(self):
        routes = {
            ("POST", "/image-to-3d"): (200, "application/json", {"result": "t"}),
            ("GET", "/image-to-3d/t"): (
                200,
                "application/json",
                {"status": "FAILED", "task_error": {"message": "crédits épuisés"}},
            ),
        }
        with FakeServer(routes) as server:
            backend = create_backend(
                "CLOUD",
                {"provider": "MESHY", "api_key": "k", "api_base": server.url, "poll_interval": 0.0},
            )
            with self.assertRaises(BackendError) as caught:
                backend.generate(request(), RecordingContext())
        self.assertIn("crédits épuisés", str(caught.exception))

    def test_missing_task_id_is_rejected(self):
        with FakeServer({("POST", "/image-to-3d"): (200, "application/json", {})}) as server:
            backend = create_backend(
                "CLOUD", {"provider": "MESHY", "api_key": "k", "api_base": server.url}
            )
            with self.assertRaises(BackendError) as caught:
                backend.generate(request(), RecordingContext())
        self.assertIn("identifiant de tâche", str(caught.exception))


class TestTripo(unittest.TestCase):
    def test_upload_then_task_then_download(self):
        holder = {}
        routes = {
            ("POST", "/upload"): (
                200,
                "application/json",
                {"code": 0, "data": {"image_token": "tok-1"}},
            ),
            ("POST", "/task"): (200, "application/json", {"code": 0, "data": {"task_id": "t-9"}}),
            ("GET", "/task/t-9"): lambda h, b: (
                200,
                "application/json",
                {
                    "code": 0,
                    "data": {
                        "status": "success",
                        "progress": 100,
                        "output": {"pbr_model": holder["url"] + "/files/m.glb"},
                    },
                },
            ),
            ("GET", "/files/m.glb"): (200, "model/gltf-binary", GLB),
        }
        with FakeServer(routes) as server:
            holder["url"] = server.url
            backend = create_backend(
                "CLOUD",
                {"provider": "TRIPO", "api_key": "k", "api_base": server.url, "poll_interval": 0.0},
            )
            result = backend.generate(request(), RecordingContext())

            upload = server.bodies("POST", "/upload")[0]
            task = json.loads(server.bodies("POST", "/task")[0])

        self.assertEqual(result.path.read_bytes(), GLB)
        self.assertIn(b'filename="f.png"', upload, "l'image part bien en multipart")
        self.assertIn(b"\x89PNG", upload)
        self.assertEqual(task["type"], "image_to_model")
        self.assertEqual(task["file"], {"type": "png", "file_token": "tok-1"})
        self.assertEqual(task["face_limit"], 30000)

    def test_missing_token_is_rejected(self):
        routes = {("POST", "/upload"): (200, "application/json", {"code": 0, "data": {}})}
        with FakeServer(routes) as server:
            backend = create_backend(
                "CLOUD", {"provider": "TRIPO", "api_key": "k", "api_base": server.url}
            )
            with self.assertRaises(BackendError) as caught:
                backend.generate(request(), RecordingContext())
        self.assertIn("jeton d'image", str(caught.exception))


class TestRodin(unittest.TestCase):
    def test_full_cycle(self):
        holder = {}
        states = {"n": 0}

        def status(handler, body):
            states["n"] += 1
            done = states["n"] >= 2
            return (
                200,
                "application/json",
                {"jobs": [{"status": "Done" if done else "Generating"}]},
            )

        routes = {
            ("POST", "/rodin"): (
                200,
                "application/json",
                {"uuid": "u-1", "jobs": {"subscription_key": "sub-1"}},
            ),
            ("POST", "/status"): status,
            ("POST", "/download"): lambda h, b: (
                200,
                "application/json",
                {"list": [{"name": "m.glb", "url": holder["url"] + "/files/m.glb"}]},
            ),
            ("GET", "/files/m.glb"): (200, "model/gltf-binary", GLB),
        }
        with FakeServer(routes) as server:
            holder["url"] = server.url
            backend = create_backend(
                "CLOUD",
                {"provider": "RODIN", "api_key": "k", "api_base": server.url, "poll_interval": 0.0},
            )
            result = backend.generate(request(), RecordingContext())

            submit = server.bodies("POST", "/rodin")[0]
            status_body = json.loads(server.bodies("POST", "/status")[0])

        self.assertEqual(result.path.read_bytes(), GLB)
        self.assertIn(b'name="images"', submit)
        self.assertIn(b'name="material"', submit)
        self.assertEqual(status_body, {"subscription_key": "sub-1"})

    def test_multiview_sends_every_image(self):
        routes = {
            ("POST", "/rodin"): (200, "application/json", {"uuid": "u", "jobs": {}}),
        }
        images = [
            SourceImage(data=b"AVANT", name="f.png", view="FRONT"),
            SourceImage(data=b"ARRIERE", name="b.png", view="BACK"),
            SourceImage(data=b"GAUCHE", name="l.png", view="LEFT"),
        ]
        with FakeServer(routes) as server:
            backend = create_backend(
                "CLOUD", {"provider": "RODIN", "api_key": "k", "api_base": server.url}
            )
            with self.assertRaises(BackendError):
                backend.generate(GenerationRequest(images=images), RecordingContext())
            submit = server.bodies("POST", "/rodin")[0]

        self.assertEqual(submit.count(b'name="images"'), 3)
        for blob in (b"AVANT", b"ARRIERE", b"GAUCHE"):
            self.assertIn(blob, submit)


class TestCloudConfiguration(unittest.TestCase):
    def test_every_provider_declares_a_default_endpoint(self):
        for identifier, provider in PROVIDERS.items():
            with self.subTest(provider=identifier):
                self.assertTrue(provider.default_base.startswith("https://"))
                self.assertTrue(provider.label)

    def test_unknown_provider(self):
        status = create_backend("CLOUD", {"provider": "INEXISTANT", "api_key": "k"}).check()
        self.assertFalse(status.ok)
        self.assertIn("MESHY", status.message)

    def test_missing_api_key(self):
        status = create_backend("CLOUD", {"provider": "MESHY"}).check()
        self.assertFalse(status.ok)
        self.assertIn("Clé API", status.message)

    def test_check_reports_the_endpoint(self):
        status = create_backend("CLOUD", {"provider": "TRIPO", "api_key": "k"}).check()
        self.assertTrue(status.ok)
        self.assertTrue(any("tripo3d.ai" in detail for detail in status.details))


if __name__ == "__main__":
    unittest.main()

# SPDX-License-Identifier: GPL-3.0-or-later
"""Backend local : le dialogue avec le worker lancé en sous-processus.

Aucun besoin de PyTorch ici : on remplace le worker par un script qui parle le
même protocole ligne-à-ligne. C'est précisément ce que ce protocole permet.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from .support import RecordingContext

from backends import BackendError, GenerationRequest, SourceImage, create_backend  # noqa: E402
from backends.local import LocalBackend  # noqa: E402
from core.jobs import JobCancelled  # noqa: E402

WORKER_OK = '''
import json, sys, pathlib
job = json.loads(pathlib.Path(sys.argv[sys.argv.index("--job") + 1]).read_text())
print("bruit de fond ignoré")
print('@@IMG23D_PROGRESS ' + json.dumps({"message": "Diffusion", "progress": 0.5}))
out = pathlib.Path(job["output_dir"]); out.mkdir(parents=True, exist_ok=True)
mesh = out / "mesh.glb"
mesh.write_bytes(b"glTF" + json.dumps({
    "images": [pathlib.Path(i["path"]).read_text(errors="replace") for i in job["images"]],
    "views": [i["view"] for i in job["images"]],
    "octree": job["octree_resolution"],
    "repo": job["model_repo"],
}).encode())
print('@@IMG23D_RESULT ' + json.dumps({"output": str(mesh), "textured": False,
                                       "meta": {"faces": 1234}}))
'''

WORKER_ERROR = '''
import json, sys
print('@@IMG23D_ERROR ' + json.dumps({"message": "CUDA out of memory"}))
sys.exit(1)
'''

WORKER_CRASH = '''
import sys
print("Traceback (most recent call last):")
print("ImportError: No module named hy3dgen")
sys.exit(3)
'''

WORKER_SILENT = '''
import sys
sys.exit(0)
'''


class LocalBackendCase(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="img23d-test-"))
        self.addCleanup(shutil.rmtree, self.directory, True)

    def worker(self, source: str) -> Path:
        path = self.directory / "worker.py"
        path.write_text(textwrap.dedent(source), encoding="utf-8")
        return path

    def backend(self, source: str, **config) -> LocalBackend:
        return create_backend(
            "LOCAL",
            {
                "python_executable": sys.executable,
                "worker_script": str(self.worker(source)),
                "model_repo": "tests/modele",
                **config,
            },
        )

    def request(self, **kwargs) -> GenerationRequest:
        images = kwargs.pop(
            "images",
            [
                SourceImage(data=b"image-de-face", name="f.png", view="FRONT"),
                SourceImage(data=b"image-de-dos", name="b.png", view="BACK"),
            ],
        )
        return GenerationRequest(images=images, **kwargs)


class TestLocalGeneration(LocalBackendCase):
    def test_job_is_handed_over_and_result_read_back(self):
        ctx = RecordingContext()
        result = self.backend(WORKER_OK).generate(
            self.request(octree_resolution=384), ctx
        )

        self.assertTrue(result.path.is_file())
        payload = json.loads(result.path.read_bytes()[4:])
        self.assertEqual(payload["images"], ["image-de-face", "image-de-dos"])
        self.assertEqual(payload["views"], ["FRONT", "BACK"])
        self.assertEqual(payload["octree"], 384)
        self.assertEqual(payload["repo"], "tests/modele")
        self.assertEqual(result.meta["faces"], 1234)

    def test_progress_lines_reach_the_context(self):
        ctx = RecordingContext()
        self.backend(WORKER_OK).generate(self.request(), ctx)
        self.assertIn("Diffusion", ctx.messages)
        self.assertGreaterEqual(ctx.snapshot()[0], 0.5)

    def test_result_survives_the_temporary_workdir(self):
        """Le worker écrit dans un dossier détruit ensuite : on doit rapatrier."""
        result = self.backend(WORKER_OK).generate(self.request(), RecordingContext())
        self.assertTrue(result.path.is_file())
        self.assertFalse(any(part.startswith("img23d-local-") for part in result.path.parts))

    def test_declared_error_is_surfaced(self):
        with self.assertRaises(BackendError) as caught:
            self.backend(WORKER_ERROR).generate(self.request(), RecordingContext())
        self.assertIn("CUDA out of memory", str(caught.exception))

    def test_crash_reports_the_exit_code_and_the_tail(self):
        with self.assertRaises(BackendError) as caught:
            self.backend(WORKER_CRASH).generate(self.request(), RecordingContext())
        message = str(caught.exception)
        self.assertIn("code 3", message)
        self.assertIn("hy3dgen", message)

    def test_silent_worker_is_an_error_not_a_success(self):
        with self.assertRaises(BackendError) as caught:
            self.backend(WORKER_SILENT).generate(self.request(), RecordingContext())
        self.assertIn("aucun résultat", str(caught.exception))

    def test_cancellation_is_propagated(self):
        source = "import time\nfor _ in range(200): time.sleep(0.1)\n"
        ctx = RecordingContext()
        ctx.cancel()
        with self.assertRaises(JobCancelled):
            self.backend(source).generate(self.request(), ctx)


class TestLocalConfiguration(LocalBackendCase):
    def test_check_without_interpreter(self):
        status = create_backend("LOCAL", {}).check()
        self.assertFalse(status.ok)
        self.assertIn("Python", status.message)

    def test_check_with_a_bogus_interpreter(self):
        status = create_backend("LOCAL", {"python_executable": "/n/existe/pas"}).check()
        self.assertFalse(status.ok)
        self.assertIn("introuvable", status.message)

    def test_check_detects_missing_torch(self):
        status = create_backend("LOCAL", {"python_executable": sys.executable}).check()
        # L'environnement de test n'a pas PyTorch : c'est exactement le cas
        # que l'utilisateur rencontrera avant d'avoir monté son environnement.
        self.assertFalse(status.ok)
        self.assertIn("PyTorch", status.message)

    def test_missing_worker_script_is_refused(self):
        backend = create_backend(
            "LOCAL",
            {"python_executable": sys.executable, "worker_script": "/n/existe/pas.py"},
        )
        with self.assertRaises(BackendError):
            backend.generate(self.request(), RecordingContext())

    def test_cache_dir_is_exported_as_hf_home(self):
        source = '''
        import json, os, sys, pathlib
        job = json.loads(pathlib.Path(sys.argv[sys.argv.index("--job") + 1]).read_text())
        out = pathlib.Path(job["output_dir"]); out.mkdir(parents=True, exist_ok=True)
        mesh = out / "mesh.glb"; mesh.write_text(os.environ.get("HF_HOME", ""))
        print('@@IMG23D_RESULT ' + json.dumps({"output": str(mesh)}))
        '''
        result = self.backend(source, cache_dir="/tmp/mon-cache").generate(
            self.request(), RecordingContext()
        )
        self.assertEqual(result.path.read_text(), "/tmp/mon-cache")

    def test_default_worker_is_the_one_shipped(self):
        backend = create_backend("LOCAL", {"python_executable": sys.executable})
        self.assertEqual(backend._worker_path().name, "local_worker.py")
        self.assertTrue(backend._worker_path().is_file())


class TestShippedWorker(unittest.TestCase):
    """Le worker fourni doit au moins démarrer et signaler proprement l'absence
    de dépendances : c'est le message que verra l'utilisateur sans PyTorch."""

    def test_missing_dependencies_give_an_actionable_message(self):
        import subprocess

        from backends import local as local_module

        worker = Path(local_module.__file__).with_name("local_worker.py")
        with tempfile.TemporaryDirectory() as directory:
            job = Path(directory) / "job.json"
            image = Path(directory) / "a.png"
            image.write_bytes(b"\x89PNG")
            job.write_text(
                json.dumps(
                    {
                        "images": [{"path": str(image), "view": "FRONT"}],
                        "output_dir": directory,
                        "model_repo": "x/y",
                    }
                )
            )
            completed = subprocess.run(
                [sys.executable, str(worker), "--job", str(job)],
                capture_output=True,
                text=True,
                timeout=120,
                env={**os.environ, "PYTHONPATH": ""},
            )

        self.assertEqual(completed.returncode, 3)
        prefix, _, payload = completed.stdout.partition("@@IMG23D_ERROR ")
        self.assertEqual(prefix, "", "rien ne doit précéder la ligne d'erreur")
        message = json.loads(payload)["message"]
        self.assertIn("Dépendance manquante", message)
        self.assertIn("torch", message)

    def test_unreadable_job_file(self):
        import subprocess

        from backends import local as local_module

        worker = Path(local_module.__file__).with_name("local_worker.py")
        completed = subprocess.run(
            [sys.executable, str(worker), "--job", "/n/existe/pas.json"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("Job illisible", completed.stdout)


if __name__ == "__main__":
    unittest.main()

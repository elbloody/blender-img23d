# SPDX-License-Identifier: GPL-3.0-or-later
"""Test d'intégration de l'extension dans un vrai Blender.

Il empaquette le dépôt, l'installe par le mécanisme officiel des extensions,
puis déroule la chaîne complète : image → backend → GLB → import → remesh →
étanchéité → STL au millimètre près.

Deux façons de le lancer :

    blender --background --python tests/blender/test_integration.py
    python -m tests.blender.test_integration        # avec le module pip `bpy`

Il échoue avec un code de sortie non nul, ce qui le rend utilisable en CI.
"""

from __future__ import annotations

import base64
import json
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# Cette suite dure plus d'une minute et exige Blender. Elle est donc ignorée
# par `unittest discover`, qui doit rester rapide et utilisable sans Blender ;
# on la lance explicitement (voir l'en-tête du module).
if __name__ != "__main__":
    raise unittest.SkipTest(
        "suite d'intégration : lancer `blender --background --python "
        "tests/blender/test_integration.py`"
    )

import bpy  # noqa: E402

REPO = Path(__file__).resolve().parent.parent.parent
EXTENSION_ID = "img23d"
MODULE = f"bl_ext.user_default.{EXTENSION_ID}"

#: Exclus de l'archive : ce sont les mêmes motifs que ``[build]`` du manifeste.
EXCLUDED = {"tests", "docs", ".github", "__pycache__", ".git"}


_INSTALLED = False


def package_and_enable() -> None:
    """Installe l'extension depuis le dépôt courant, comme le ferait un utilisateur.

    Une seule fois par processus : réinstaller à chaque classe de test coûte
    plusieurs secondes pour rien.
    """
    global _INSTALLED
    if _INSTALLED:
        return

    import addon_utils

    staging = Path(tempfile.mkdtemp(prefix="img23d-package-"))
    root = staging / EXTENSION_ID

    listing = subprocess.run(
        ["git", "ls-files", "-co", "--exclude-standard"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()

    for line in listing:
        relative = Path(line)
        if set(relative.parts) & EXCLUDED:
            continue
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / relative, destination)

    archive = Path(shutil.make_archive(str(staging / EXTENSION_ID), "zip", staging, EXTENSION_ID))
    bpy.ops.extensions.package_install_files(
        filepath=str(archive), repo="user_default", enable_on_install=True
    )
    addon_utils.enable(MODULE, default_set=True, persistent=True)
    shutil.rmtree(staging, ignore_errors=True)
    _INSTALLED = True


class FakeBackendServer:
    """Serveur conforme au protocole du backend HTTP, en mode asynchrone."""

    def __init__(self, model: bytes) -> None:
        self.model = model
        self.status_calls = 0
        self.params: dict = {}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path == "/health":
                    return self._json({"backend": "test", "gpu": "aucun"})
                if self.path.startswith("/jobs/"):
                    outer.status_calls += 1
                    if outer.status_calls < 2:
                        return self._json({"status": "running", "progress": 40})
                    return self._json(
                        {
                            "status": "done",
                            "model_b64": base64.b64encode(outer.model).decode(),
                            "format": "glb",
                        }
                    )
                self.send_error(404)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"] or 0)))
                outer.params = body["params"]
                outer.images = body["images"]
                self._json({"job_id": "job-1"})

            def _json(self, payload):
                raw = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self._httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc_info):
        self._httpd.shutdown()
        self._httpd.server_close()
        return False

    @property
    def url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"


def _job_modal():
    """Le module interne qui suit le travail en cours."""
    return sys.modules[f"{MODULE}.ops._job_modal"]


def clear_scene() -> None:
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)


class IntegrationCase(unittest.TestCase):
    """Base commune : extension installée, dossier de travail jetable."""

    @classmethod
    def setUpClass(cls):
        package_and_enable()
        cls.directory = Path(tempfile.mkdtemp(prefix="img23d-integration-"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.directory, ignore_errors=True)

    @property
    def settings(self):
        return bpy.context.scene.img23d

    @property
    def state(self):
        return bpy.context.window_manager.img23d_state


class TestRegistration(IntegrationCase):
    def test_scene_and_window_properties_exist(self):
        self.assertTrue(hasattr(bpy.context.scene, "img23d"))
        self.assertTrue(hasattr(bpy.context.window_manager, "img23d_state"))

    def test_preferences_are_reachable(self):
        preferences = bpy.context.preferences.addons[MODULE].preferences
        self.assertIn("python_executable", preferences.backend_config("LOCAL"))
        self.assertEqual(preferences.backend_config("CLOUD")["provider"], "MESHY")

    def test_every_operator_is_registered(self):
        for name in (
            "generate", "cancel", "import_last", "check_backend",
            "analyze", "prepare_print", "scale_to_size",
            "export", "export_as",
            "add_images", "remove_image", "clear_images", "move_image",
        ):
            with self.subTest(operator=name):
                self.assertTrue(hasattr(bpy.ops.img23d, name))

    def test_every_panel_is_registered(self):
        for name in (
            "IMG23D_PT_source", "IMG23D_PT_generate", "IMG23D_PT_import",
            "IMG23D_PT_print", "IMG23D_PT_export", "IMG23D_UL_images",
        ):
            with self.subTest(panel=name):
                self.assertTrue(hasattr(bpy.types, name))


class TestImagePreparation(IntegrationCase):
    def source_image(self, width: int, height: int) -> Path:
        image = bpy.data.images.new("src", width=width, height=height, alpha=True)
        path = self.directory / f"src_{width}x{height}.png"
        image.filepath_raw, image.file_format = str(path), "PNG"
        image.save()
        bpy.data.images.remove(image)
        return path

    def size_of(self, data: bytes) -> tuple[int, int]:
        path = self.directory / "measure.png"
        path.write_bytes(data)
        image = bpy.data.images.load(str(path), check_existing=False)
        size = tuple(image.size)
        bpy.data.images.remove(image)
        return size

    def test_large_images_are_downscaled(self):
        from bl_ext.user_default.img23d.core.imageprep import prepare_from_paths

        path = self.source_image(2000, 1200)
        prepared = prepare_from_paths([(str(path), "FRONT")], max_size=512)

        self.assertEqual(len(prepared), 1)
        self.assertEqual(prepared[0].view, "FRONT")
        self.assertEqual(prepared[0].data[:4], b"\x89PNG")
        self.assertEqual(max(self.size_of(prepared[0].data)), 512)

    def test_small_images_are_left_alone(self):
        from bl_ext.user_default.img23d.core.imageprep import prepare_from_paths

        path = self.source_image(200, 100)
        prepared = prepare_from_paths([(str(path), "AUTO")], max_size=1024)
        self.assertEqual(self.size_of(prepared[0].data), (200, 100))

    def test_no_datablock_is_leaked(self):
        from bl_ext.user_default.img23d.core.imageprep import prepare_from_paths

        path = self.source_image(64, 64)
        before = len(bpy.data.images)
        prepare_from_paths([(str(path), "AUTO")] * 3, max_size=32)
        self.assertEqual(len(bpy.data.images), before)

    def test_missing_file_is_reported(self):
        from bl_ext.user_default.img23d.core.imageprep import ImagePrepError, prepare_from_paths

        with self.assertRaises(ImagePrepError):
            prepare_from_paths([("/n/existe/pas.png", "AUTO")])

    def test_unsupported_format_is_reported(self):
        from bl_ext.user_default.img23d.core.imageprep import ImagePrepError, prepare_from_paths

        path = self.directory / "note.txt"
        path.write_text("pas une image")
        with self.assertRaises(ImagePrepError) as caught:
            prepare_from_paths([(str(path), "AUTO")])
        self.assertIn("Format non géré", str(caught.exception))


class TestFullPipeline(IntegrationCase):
    """Le scénario complet, du backend au fichier prêt pour le slicer."""

    def reference_glb(self) -> bytes:
        clear_scene()
        bpy.ops.mesh.primitive_monkey_add()
        path = self.directory / "reference.glb"
        bpy.ops.export_scene.gltf(
            filepath=str(path), export_format="GLB", use_selection=True
        )
        return path.read_bytes()

    def test_generate_import_prepare_export(self):
        from bl_ext.user_default.img23d.backends import GenerationRequest, create_backend
        from bl_ext.user_default.img23d.core import printprep, scene as scene_utils
        from bl_ext.user_default.img23d.core.imageprep import prepare_from_paths
        from bl_ext.user_default.img23d.core.jobs import JobContext

        model = self.reference_glb()

        # 1. Image source réelle, préparée par Blender.
        image = bpy.data.images.new("pipeline", width=800, height=800, alpha=True)
        source = self.directory / "pipeline.png"
        image.filepath_raw, image.file_format = str(source), "PNG"
        image.save()
        bpy.data.images.remove(image)
        prepared = prepare_from_paths([(str(source), "FRONT")], max_size=512)

        # 2. Génération via un serveur conforme au protocole HTTP documenté.
        with FakeBackendServer(model) as server:
            backend = create_backend("HTTP", {"url": server.url, "poll_interval": 0.0})
            self.assertTrue(backend.check().ok)

            ctx = JobContext()
            result = backend.generate(
                GenerationRequest(images=prepared, seed=3, octree_resolution=384), ctx
            )

            self.assertGreaterEqual(server.status_calls, 2, "le mode asynchrone est exercé")
            self.assertEqual(server.params["octree_resolution"], 384)
            self.assertEqual(server.images[0]["view"], "FRONT")

        self.assertEqual(result.path.read_bytes(), model, "le GLB traverse la chaîne intact")
        self.assertGreater(ctx.snapshot()[0], 0.5, "la progression est remontée")

        # 3. Import et normalisation.
        clear_scene()
        objects = scene_utils.import_mesh(result.path, name="img23d_result")
        obj = scene_utils.join_objects(objects, bpy.context)
        scene_utils.normalize_transform(
            obj, upright=scene_utils.needs_upright(result.path, "AUTO")
        )

        lowest = min((obj.matrix_world @ vertex.co).z for vertex in obj.data.vertices)
        self.assertAlmostEqual(lowest, 0.0, places=5, msg="le modèle est posé sur Z=0")

        scene_utils.scale_to_height(obj, 0.080)
        before = printprep.analyze(obj, unit_scale=1000.0)
        self.assertAlmostEqual(max(before.dimensions_mm), 80.0, places=2)
        self.assertFalse(before.watertight, "Suzanne a des bords ouverts : le cas intéressant")

        # 4. Préparation à l'impression.
        options = printprep.PrepOptions(
            remesh=True, voxel_size_mm=1.0, repair=True, merge_distance_mm=0.02,
            fill_holes=True, triangulate=True, decimate=True, target_triangles=20_000,
        )
        steps = printprep.prepare(bpy.context, obj, options)
        after = printprep.analyze(obj, unit_scale=1000.0)

        self.assertGreaterEqual(len(steps), 3, steps)
        self.assertTrue(after.watertight, after.headline())
        self.assertFalse(after.inverted, "les normales pointent vers l'extérieur")
        self.assertLessEqual(after.triangles, 20_000)
        self.assertAlmostEqual(max(after.dimensions_mm), 80.0, delta=2.0)

        # 5. Export STL, relu octet par octet.
        stl = printprep.export_stl(
            bpy.context, [obj], self.directory / "final.stl", global_scale=1000.0
        )
        raw = stl.read_bytes()
        triangles = int.from_bytes(raw[80:84], "little")

        self.assertEqual(len(raw), 84 + triangles * 50, "en-tête STL binaire cohérent")
        self.assertEqual(triangles, after.triangles)

        coordinates = [[], [], []]
        for index in range(triangles):
            offset = 84 + index * 50 + 12
            for corner in range(3):
                for axis, value in enumerate(struct.unpack_from("<3f", raw, offset + corner * 12)):
                    coordinates[axis].append(value)
        span = max(max(axis) - min(axis) for axis in coordinates)
        self.assertAlmostEqual(span, 80.0, delta=1.0, msg="le STL est bien en millimètres")


class TestOperators(IntegrationCase):
    """Les opérateurs non modaux, tels qu'appelés par les boutons du panneau."""

    def setUp(self):
        clear_scene()
        bpy.ops.mesh.primitive_uv_sphere_add()
        self.obj = bpy.context.active_object
        self.settings.export_path = f"{self.directory}/"

    def test_analyze_fills_the_report(self):
        self.assertEqual(bpy.ops.img23d.analyze(), {"FINISHED"})
        self.assertTrue(self.state.mesh_report)
        self.assertTrue(self.state.mesh_watertight, "une UV-sphere est fermée")

    def test_prepare_print_keeps_the_mesh_watertight(self):
        self.settings.use_remesh = True
        self.settings.voxel_size_mm = 2.0
        self.assertEqual(bpy.ops.img23d.prepare_print(), {"FINISHED"})
        self.assertTrue(self.state.mesh_watertight)

    def test_export_does_not_overwrite_silently(self):
        self.settings.export_name = "piece"
        self.obj.select_set(True)

        self.assertEqual(bpy.ops.img23d.export(), {"FINISHED"})
        self.assertTrue((self.directory / "piece.stl").is_file())

        self.assertEqual(bpy.ops.img23d.export(), {"FINISHED"})
        self.assertTrue((self.directory / "piece_001.stl").is_file())

    def test_export_honours_the_unit(self):
        self.settings.export_name = "unite"
        self.settings.export_unit = "M"
        self.obj.select_set(True)
        bpy.ops.img23d.export(overwrite=True)

        raw = (self.directory / "unite.stl").read_bytes()
        triangles = int.from_bytes(raw[80:84], "little")
        xs = [
            struct.unpack_from("<3f", raw, 84 + i * 50 + 12 + c * 12)[0]
            for i in range(triangles)
            for c in range(3)
        ]
        self.assertAlmostEqual(max(xs) - min(xs), 2.0, delta=0.01, msg="rayon 1 m, donc 2 m")

    def test_scale_to_size(self):
        self.settings.target_size_mm = 45.0
        self.assertEqual(bpy.ops.img23d.scale_to_size(), {"FINISHED"})

        from bl_ext.user_default.img23d.core import printprep

        report = printprep.analyze(self.obj, unit_scale=1000.0)
        self.assertAlmostEqual(max(report.dimensions_mm), 45.0, places=2)

    def test_image_list_operators(self):
        self.settings.images.clear()
        for name, view in (("a.png", "FRONT"), ("b.png", "BACK")):
            item = self.settings.images.add()
            item.filepath = str(self.directory / name)
            item.view = view

        self.assertEqual(len(self.settings.enabled_images()), 2)

        self.settings.images[0].enabled = False
        self.assertEqual(len(self.settings.enabled_images()), 1)

        self.settings.active_image_index = 1
        self.assertEqual(bpy.ops.img23d.move_image(direction="UP"), {"FINISHED"})
        self.assertTrue(self.settings.images[0].filepath.endswith("b.png"))

        self.assertEqual(bpy.ops.img23d.clear_images(), {"FINISHED"})
        self.assertEqual(len(self.settings.images), 0)

    def test_check_names_the_backend_it_tested(self):
        """Remplir la section d'un backend ne le sélectionne pas.

        Sans le libellé, l'échec du backend actif se lit comme un échec de
        celui qu'on vient de configurer, et on cherche au mauvais endroit.
        """
        preferences = bpy.context.preferences.addons[MODULE].preferences
        for backend, attendu in (("LOCAL", "Local"), ("KAGGLE", "Kaggle")):
            with self.subTest(backend=backend):
                preferences.backend = backend
                self.state.running = False
                _job_modal()._ACTIVE_JOB = None
                bpy.ops.img23d.check_backend()
                self.assertIn(attendu, self.state.status)

    def test_a_stale_busy_flag_does_not_freeze_the_buttons(self):
        """Un opérateur modal privé d'évènements laissait le drapeau levé.

        Les boutons restaient alors grisés définitivement, sans rien pour
        les débloquer hormis un redémarrage de Blender.
        """
        self.state.running = True
        _job_modal()._ACTIVE_JOB = None

        self.assertTrue(bpy.ops.img23d.check_backend.poll())
        self.assertFalse(self.state.running, "le drapeau se rétablit de lui-même")

    def test_generate_is_blocked_without_images(self):
        self.settings.images.clear()
        self.assertFalse(bpy.ops.img23d.generate.poll())


def main() -> int:
    runner = unittest.TextTestRunner(verbosity=2, stream=sys.stdout)
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    return 0 if runner.run(suite).wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())

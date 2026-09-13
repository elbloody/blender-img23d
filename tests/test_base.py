# SPDX-License-Identifier: GPL-3.0-or-later
"""Le contrat commun : requêtes, registre, hygiène de la couche backends."""

from __future__ import annotations

import ast
import pathlib
import unittest

from .support import RecordingContext  # noqa: F401  (installe sys.path)

from backends import (  # noqa: E402
    Backend,
    BackendStatus,
    BackendUnavailable,
    GenerationRequest,
    GenerationResult,
    SourceImage,
    backend_items,
    create_backend,
    get_backend_class,
    iter_backends,
)
from backends.base import safe_filename  # noqa: E402


class TestGenerationRequest(unittest.TestCase):
    def test_at_least_one_image_is_required(self):
        with self.assertRaises(ValueError):
            GenerationRequest(images=[])

    def test_primary_prefers_the_front_view(self):
        images = [
            SourceImage(data=b"a", view="LEFT"),
            SourceImage(data=b"b", view="FRONT"),
        ]
        self.assertIs(GenerationRequest(images=images).primary, images[1])

    def test_primary_falls_back_to_the_first_image(self):
        images = [SourceImage(data=b"a", view="LEFT"), SourceImage(data=b"b", view="TOP")]
        self.assertIs(GenerationRequest(images=images).primary, images[0])

    def test_multiview_detection_and_summary(self):
        single = GenerationRequest(images=[SourceImage(data=b"a")])
        self.assertFalse(single.is_multiview)
        multi = GenerationRequest(
            images=[SourceImage(data=b"a", view="FRONT"), SourceImage(data=b"b", view="BACK")],
            octree_resolution=384,
        )
        self.assertTrue(multi.is_multiview)
        self.assertIn("front, back", multi.summary())
        self.assertIn("384", multi.summary())


class TestSourceImage(unittest.TestCase):
    def test_extension_follows_the_mime_type(self):
        self.assertEqual(SourceImage(data=b"", mime="image/png").extension, ".png")
        self.assertEqual(SourceImage(data=b"", mime="image/jpeg").extension, ".jpg")
        self.assertEqual(SourceImage(data=b"", mime="inconnu/x").extension, ".png")


class TestGenerationResult(unittest.TestCase):
    def test_path_is_always_a_path(self):
        result = GenerationResult(path="/tmp/m.glb", backend="HTTP")
        self.assertIsInstance(result.path, pathlib.Path)


class TestRegistry(unittest.TestCase):
    def test_the_four_backends_are_registered(self):
        self.assertEqual(
            [backend.id for backend in iter_backends()], ["LOCAL", "HTTP", "KAGGLE", "CLOUD"]
        )

    def test_lookup_is_case_insensitive(self):
        self.assertIs(get_backend_class("http"), get_backend_class("HTTP"))

    def test_unknown_backend_lists_the_known_ones(self):
        with self.assertRaises(KeyError) as caught:
            get_backend_class("COLAB")
        self.assertIn("KAGGLE", str(caught.exception))

    def test_enum_items_are_well_formed(self):
        for identifier, label, description in backend_items():
            with self.subTest(backend=identifier):
                self.assertTrue(identifier.isupper())
                self.assertTrue(label)
                self.assertTrue(description)

    def test_every_backend_implements_the_contract(self):
        for backend_class in iter_backends():
            with self.subTest(backend=backend_class.id):
                instance = create_backend(backend_class.id, {})
                self.assertIsInstance(instance, Backend)
                self.assertIsInstance(instance.check(), BackendStatus)


class TestBackendHelpers(unittest.TestCase):
    def test_require_explains_where_to_fix_it(self):
        backend = create_backend("HTTP", {})
        with self.assertRaises(BackendUnavailable) as caught:
            backend.require("absent", "Truc")
        self.assertIn("Préférences", str(caught.exception))

    def test_cfg_falls_back_on_blank_strings(self):
        backend = create_backend("HTTP", {"a": "   "})
        self.assertEqual(backend.cfg("a", "defaut"), "defaut")
        self.assertEqual(backend.cfg("absent", "defaut"), "defaut")

    def test_safe_filename(self):
        self.assertEqual(safe_filename("mon perso / v2"), "mon_perso_v2")
        self.assertEqual(safe_filename("///"), "img23d")


class TestLayerHygiene(unittest.TestCase):
    """La couche backends ne doit jamais importer ``bpy``.

    C'est ce qui la rend testable hors Blender — et réutilisable par un
    serveur MCP. Un import ajouté par mégarde casserait les deux d'un coup.
    """

    def modules(self):
        root = pathlib.Path(__file__).resolve().parent.parent
        for directory in ("backends", "core"):
            for path in sorted((root / directory).rglob("*.py")):
                yield path

    def test_no_bpy_import_in_the_portable_layer(self):
        portable = {"backends", "core/jobs.py"}
        for path in self.modules():
            relative = path.relative_to(path.parents[1]).as_posix()
            if path.parent.name != "backends" and relative != "core/jobs.py":
                continue
            with self.subTest(module=relative):
                tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
                imported = set()
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        imported.update(alias.name.split(".")[0] for alias in node.names)
                    elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                        imported.add(node.module.split(".")[0])
                self.assertNotIn("bpy", imported, f"{relative} importe bpy")
                self.assertNotIn("bmesh", imported, f"{relative} importe bmesh")
        self.assertTrue(portable)

    def test_backends_use_relative_imports_between_themselves(self):
        """Les extensions Blender exigent des imports relatifs."""
        root = pathlib.Path(__file__).resolve().parent.parent
        for path in sorted((root / "backends").glob("*.py")):
            if path.name == "local_worker.py":
                continue  # script autonome, lancé hors de Blender
            with self.subTest(module=path.name):
                tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
                for node in ast.walk(tree):
                    if isinstance(node, ast.ImportFrom) and node.module in {"backends", "core"}:
                        self.fail(f"{path.name} utilise un import absolu : {node.module}")


if __name__ == "__main__":
    unittest.main()

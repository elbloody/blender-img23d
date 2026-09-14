# SPDX-License-Identifier: GPL-3.0-or-later
"""Backend Kaggle : génération du kernel et pilotage du CLI.

Le vrai CLI ``kaggle`` est remplacé par un script qui joue le même scénario
(push, statuts successifs, récupération de la sortie).
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import shutil
import stat
import sys
import tempfile
import textwrap
import unittest
import unittest.mock
from pathlib import Path

from .support import RecordingContext

from backends import BackendError, GenerationRequest, SourceImage, create_backend  # noqa: E402
from backends.kaggle import KaggleBackend, _parse_status  # noqa: E402

FAKE_CLI = '''
import json, pathlib, sys

state = pathlib.Path(__file__).with_suffix(".state.json")
calls = json.loads(state.read_text()) if state.exists() else []
calls.append(sys.argv[1:])
state.write_text(json.dumps(calls))

args = sys.argv[1:]
if args[:2] == ["kernels", "push"]:
    directory = pathlib.Path(args[args.index("-p") + 1])
    (state.parent / "pushed").write_text(str(directory))
    for name in ("img23d_kernel.py", "kernel-metadata.json"):
        (state.parent / name).write_bytes((directory / name).read_bytes())
    print("Kernel version 1 successfully pushed.")
elif args[:2] == ["kernels", "status"]:
    n = sum(1 for c in calls if c[:2] == ["kernels", "status"])
    # Format du vrai CLI 2.2.x, vérifié dans son code source.
    etat = "RUNNING" if n < 2 else "COMPLETE"
    print('%s has status "KernelWorkerStatus.%s"' % (args[2], etat))
elif args[:2] == ["kernels", "output"]:
    out = pathlib.Path(args[args.index("-p") + 1])
    out.mkdir(parents=True, exist_ok=True)
    (out / "mesh.glb").write_bytes(b"glTF-depuis-kaggle")
    (out / "img23d_result.json").write_text('{"output": "mesh.glb"}')
    print("Output downloaded.")
elif args[:2] == ["kernels", "list"]:
    print("ref  title")
else:
    sys.stderr.write("commande inconnue: %s\\n" % args)
    sys.exit(2)
'''

FAILING_CLI = '''
import sys
sys.stderr.write("401 - Unauthorized\\n")
sys.exit(1)
'''

#: Ce que rend le vrai CLI quand il ne trouve aucun identifiant : un code de
#: retour nul, mais un texte explicite sur la sortie standard.
NO_CREDENTIALS_CLI = '''
print("Authentication required to call the Kaggle API.")
'''


class KaggleCase(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="img23d-kaggle-"))
        self.addCleanup(shutil.rmtree, self.directory, True)
        # Isole le test du vrai ~/.kaggle/kaggle.json de la machine.
        os.environ["KAGGLE_CONFIG_DIR"] = str(self.directory / "vide")
        self.addCleanup(os.environ.pop, "KAGGLE_CONFIG_DIR", None)

    def cli(self, source: str = FAKE_CLI) -> Path:
        script = self.directory / "fake_kaggle.py"
        script.write_text(textwrap.dedent(source), encoding="utf-8")
        launcher = self.directory / "kaggle"
        launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
        launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        return launcher

    def backend(self, source: str = FAKE_CLI, **config) -> KaggleBackend:
        return create_backend(
            "KAGGLE",
            {
                "cli_path": str(self.cli(source)),
                "username": "moi",
                "api_key": "cle",
                "poll_interval": 0.0,
                **config,
            },
        )

    def calls(self) -> list[list[str]]:
        state = self.directory / "fake_kaggle.state.json"
        return json.loads(state.read_text()) if state.exists() else []

    def request(self, **kwargs) -> GenerationRequest:
        images = kwargs.pop(
            "images", [SourceImage(data=b"\x89PNG-front", name="f.png", view="FRONT")]
        )
        return GenerationRequest(images=images, **kwargs)


class TestKaggleGeneration(KaggleCase):
    def test_full_cycle_push_poll_output(self):
        ctx = RecordingContext()
        result = self.backend().generate(self.request(), ctx)

        self.assertEqual(result.path.read_bytes(), b"glTF-depuis-kaggle")
        self.assertEqual(result.meta["kernel"], "moi/img23d-worker")

        verbs = [call[:2] for call in self.calls()]
        self.assertIn(["kernels", "push"], verbs)
        self.assertIn(["kernels", "output"], verbs)
        self.assertEqual(verbs.count(["kernels", "status"]), 2, "sondé jusqu'à complétion")

    def test_kernel_metadata_requests_a_gpu(self):
        self.backend().generate(self.request(), RecordingContext())
        metadata = json.loads((self.directory / "kernel-metadata.json").read_text())
        self.assertEqual(metadata["id"], "moi/img23d-worker")
        self.assertTrue(metadata["enable_gpu"])
        self.assertTrue(metadata["enable_internet"])
        self.assertTrue(metadata["is_private"])
        self.assertEqual(metadata["kernel_type"], "script")

    def test_generated_kernel_embeds_images_and_params(self):
        self.backend().generate(
            self.request(octree_resolution=512, seed=11, images=[
                SourceImage(data=b"AVANT", name="f.png", view="FRONT"),
                SourceImage(data=b"ARRIERE", name="b.png", view="BACK"),
            ]),
            RecordingContext(),
        )
        script = (self.directory / "img23d_kernel.py").read_text()
        encoded = script.split('b64decode("')[1].split('")')[0]
        job = json.loads(base64.b64decode(encoded))

        self.assertEqual(job["octree_resolution"], 512)
        self.assertEqual(job["seed"], 11)
        self.assertEqual([image["view"] for image in job["images"]], ["FRONT", "BACK"])
        self.assertEqual(base64.b64decode(job["images"][0]["data"]), b"AVANT")

    def test_kernel_installs_hunyuan_from_its_repository(self):
        """`hy3dgen` n'est pas sur PyPI : il doit être cloné, pas pip-installé.

        Un `pip install hy3dgen` part sans erreur visible et échoue vingt
        minutes plus tard, sur Kaggle, par un « No module named hy3dgen ».
        """
        self.backend().generate(self.request(), RecordingContext())
        script = (self.directory / "img23d_kernel.py").read_text()
        encoded = script.split('b64decode("')[1].split('")')[0]
        setup = json.loads(base64.b64decode(encoded))["setup"]

        joint = " ; ".join(setup)
        self.assertIn("git clone", joint)
        self.assertIn("Hunyuan3D-2", joint)
        self.assertNotIn("pip install -q hy3dgen", joint)
        self.assertNotRegex(joint, r"pip install[^;]*\bhy3dgen\b")

    def test_kernel_stops_when_the_setup_fails(self):
        """Une installation ratée doit arrêter le kernel, pas le laisser courir."""
        self.backend().generate(self.request(), RecordingContext())
        script = (self.directory / "img23d_kernel.py").read_text()
        self.assertIn("returncode != 0", script)
        self.assertIn("SystemExit", script)

    def test_inputs_are_written_outside_the_output_directory(self):
        """/kaggle/working est rapatrié en entier : n'y mettons que le résultat."""
        self.backend().generate(self.request(), RecordingContext())
        script = (self.directory / "img23d_kernel.py").read_text()
        entrees = script.split("ENTREES = pathlib.Path(")[1].split(")")[0]
        self.assertNotIn("/kaggle/working", entrees)

    def test_generated_kernel_is_valid_python(self):
        """Un kernel mal formaté échouerait 20 minutes plus tard, côté Kaggle."""
        import ast

        self.backend().generate(self.request(), RecordingContext())
        source = (self.directory / "img23d_kernel.py").read_text()
        ast.parse(source)  # lève SyntaxError si le gabarit est cassé
        self.assertIn("Hunyuan3DDiTFlowMatchingPipeline", source)
        self.assertIn("/kaggle/working", source)

    def test_credentials_are_passed_through_the_environment(self):
        source = '''
        import os, pathlib, sys
        pathlib.Path(__file__).with_suffix(".env.json").write_text(
            '{"u": "%s", "k": "%s"}' % (os.environ.get("KAGGLE_USERNAME"),
                                        os.environ.get("KAGGLE_KEY")))
        print('ref has status "KernelWorkerStatus.COMPLETE"')
        '''
        backend = self.backend(source, username="alice", api_key="cle-secrete")
        # Ce faux CLI ne rend aucun maillage : seul l'environnement nous intéresse.
        with contextlib.suppress(BackendError):
            backend.generate(self.request(), RecordingContext())
        env = json.loads((self.directory / "fake_kaggle.env.json").read_text())
        self.assertEqual(env, {"u": "alice", "k": "cle-secrete"})

    def test_oversized_images_are_refused_before_upload(self):
        big = SourceImage(data=b"\x00" * 9_000_000, name="f.png", view="FRONT")
        with self.assertRaises(BackendError) as caught:
            self.backend().generate(self.request(images=[big]), RecordingContext())
        self.assertIn("Taille max des images", str(caught.exception))
        self.assertEqual(self.calls(), [], "rien ne doit être envoyé")

    def test_missing_mesh_points_to_the_kernel_logs(self):
        source = '''
        import pathlib, sys
        args = sys.argv[1:]
        if args[:2] == ["kernels", "status"]:
            print('ref has status "KernelWorkerStatus.COMPLETE"')
        elif args[:2] == ["kernels", "output"]:
            out = pathlib.Path(args[args.index("-p") + 1]); out.mkdir(parents=True, exist_ok=True)
            (out / "log.txt").write_text("CUDA out of memory")
        '''
        with self.assertRaises(BackendError) as caught:
            self.backend(source).generate(self.request(), RecordingContext())
        self.assertIn("kaggle.com/code/moi/img23d-worker", str(caught.exception))

    def test_failed_kernel_is_reported(self):
        source = '''
        import sys
        if sys.argv[1:3] == ["kernels", "status"]:
            print('ref has status "KernelWorkerStatus.ERROR"')
        '''
        with self.assertRaises(BackendError) as caught:
            self.backend(source).generate(self.request(), RecordingContext())
        self.assertIn("échoué", str(caught.exception))

    def test_cli_failure_surfaces_stderr(self):
        with self.assertRaises(BackendError) as caught:
            self.backend(FAILING_CLI).generate(self.request(), RecordingContext())
        self.assertIn("401", str(caught.exception))

    def test_check_does_not_blame_credentials_for_a_network_error(self):
        """Un « identifiants refusés » sur une panne réseau égare l'utilisateur."""
        reseau = r"""
        import sys
        sys.stderr.write("Max retries exceeded with url: /v1/kernels (Connection refused)\n")
        sys.exit(1)
        """
        status = self.backend(reseau).check()
        self.assertFalse(status.ok)
        self.assertNotIn("refusée", status.message)
        self.assertTrue(any("Max retries" in detail for detail in status.details))
        self.assertTrue(any("connexion" in detail for detail in status.details))

    def test_check_blames_credentials_on_an_auth_error(self):
        auth = r"""
        import sys
        sys.stderr.write("401 Unauthorized\n")
        sys.exit(1)
        """
        status = self.backend(auth).check()
        self.assertFalse(status.ok)
        self.assertTrue(any("Identifiants refusés" in detail for detail in status.details))


#: Faux CLI imitant le nouveau système : le jeton porte l'identité, que le
#: CLI expose via `config view`, exactement comme le vrai (vérifié dans son
#: code source : `_authenticate_with_access_token` puis `_introspect_token`).
TOKEN_CLI = r"""
import json, os, pathlib, sys

state = pathlib.Path(__file__).with_suffix(".state.json")
calls = json.loads(state.read_text()) if state.exists() else []
calls.append(sys.argv[1:])
state.write_text(json.dumps(calls))

args = sys.argv[1:]
jeton = os.environ.get("KAGGLE_API_TOKEN", "")
if not jeton.startswith("KGAT_"):
    sys.stderr.write("401 Unauthorized\n")
    sys.exit(1)

if args[:2] == ["config", "view"]:
    print("Configuration values from /home/moi/.kaggle")
    print("- username: pseudoduajeton")
    print("- auth_method: access_token")
    print("- path: None")
elif args[:2] == ["kernels", "list"]:
    print("ref  title")
elif args[:2] == ["kernels", "push"]:
    print("Kernel version 1 successfully pushed.")
elif args[:2] == ["kernels", "status"]:
    print('%s has status "KernelWorkerStatus.COMPLETE"' % args[2])
elif args[:2] == ["kernels", "output"]:
    out = pathlib.Path(args[args.index("-p") + 1])
    out.mkdir(parents=True, exist_ok=True)
    (out / "mesh.glb").write_bytes(b"glTF-par-jeton")
"""


class TestKaggleApiToken(KaggleCase):
    """Le nouveau système de jeton (KGAT_…) qui remplace utilisateur + clé."""

    def token_backend(self, **config):
        return create_backend(
            "KAGGLE",
            {
                "cli_path": str(self.cli(TOKEN_CLI)),
                "api_token": "KGAT_0b5f044f5482721caa2e7a0e9ec2f943",
                "poll_interval": 0.0,
                **config,
            },
        )

    def test_token_is_passed_through_the_environment(self):
        status = self.token_backend().check()
        self.assertTrue(status.ok, status.message)

    def test_username_is_discovered_from_the_token(self):
        """Le jeton porte l'identité : ne pas la redemander à l'utilisateur."""
        backend = self.token_backend()
        self.assertEqual(backend.username, "pseudoduajeton")
        self.assertEqual(backend.slug, "pseudoduajeton/img23d-worker")

    def test_username_lookup_is_cached(self):
        backend = self.token_backend()
        for _ in range(4):
            self.assertEqual(backend.username, "pseudoduajeton")
        views = [call for call in self.calls() if call[:2] == ["config", "view"]]
        self.assertEqual(len(views), 1, "un seul appel au CLI, pas un par lecture")

    def test_explicit_username_wins_over_the_token(self):
        backend = self.token_backend(username="jeChoisis")
        self.assertEqual(backend.username, "jechoisis")
        self.assertEqual(self.calls(), [], "aucun appel CLI n'est nécessaire")

    def test_full_generation_with_only_a_token(self):
        result = self.token_backend().generate(self.request(), RecordingContext())
        self.assertEqual(result.path.read_bytes(), b"glTF-par-jeton")
        self.assertEqual(result.meta["kernel"], "pseudoduajeton/img23d-worker")

    def test_an_invalid_token_is_reported(self):
        status = self.token_backend(api_token="pas-un-jeton").check()
        self.assertFalse(status.ok)
        self.assertTrue(any("Identifiants refusés" in detail for detail in status.details))

    def test_check_names_the_credential_source(self):
        status = self.token_backend().check()
        self.assertTrue(any("jeton d'API" in detail for detail in status.details))


class TestKaggleConfiguration(KaggleCase):
    def test_check_without_cli(self):
        status = create_backend("KAGGLE", {"cli_path": "/n/existe/pas"}).check()
        self.assertFalse(status.ok)
        self.assertIn("introuvable", status.message)

    def test_check_without_credentials_points_to_the_browser_login(self):
        """Sans identifiants, la route la plus simple est « kaggle auth login ».

        Elle n'exige aucun jeton à recopier — donc rien à abîmer en le collant.
        """
        cli = self.cli(NO_CREDENTIALS_CLI)
        status = create_backend("KAGGLE", {"cli_path": str(cli)}).check()
        self.assertFalse(status.ok)
        self.assertTrue(any("auth login" in detail for detail in status.details))

    def test_oauth_session_is_recognised(self):
        """« kaggle auth login » range sa session dans ~/.kaggle/credentials.json.

        Énumérer les fichiers connus pour décider d'essayer ou non refusait
        cette session : Kaggle a déjà changé de mécanisme deux fois.
        """
        config = self.directory / "config"
        config.mkdir(exist_ok=True)
        (config / "credentials.json").write_text('{"access_token": "x"}')
        os.environ["KAGGLE_CONFIG_DIR"] = str(config)

        backend = create_backend("KAGGLE", {"cli_path": str(self.cli()), "username": "moi"})
        status = backend.check()
        self.assertTrue(status.ok, status.message)
        self.assertTrue(any("auth login" in detail for detail in status.details))

    def test_check_succeeds_with_credentials(self):
        status = self.backend().check()
        self.assertTrue(status.ok, status.message)
        self.assertTrue(any("moi/img23d-worker" in detail for detail in status.details))

    def test_slug_is_validated(self):
        backend = create_backend("KAGGLE", {"username": "moi", "kernel_slug": "Slug Invalide !"})
        with self.assertRaises(BackendError):
            _ = backend.slug

    def test_slug_defaults_to_the_username(self):
        self.assertEqual(
            create_backend("KAGGLE", {"username": "Alice"}).slug, "alice/img23d-worker"
        )

    def test_cli_is_found_in_a_dedicated_environment(self):
        """Le champ des préférences est remis à zéro par chaque réinstallation.

        Sans cette recherche, il faut ressaisir le chemin complet après chaque
        mise à jour de l'extension — et le CLI n'est presque jamais dans le PATH.
        """
        faux_home = self.directory / "maison"
        binaire = faux_home / ".img23d-kaggle" / "bin" / "kaggle"
        binaire.parent.mkdir(parents=True)
        binaire.write_text("#!/bin/sh\nexit 0\n")
        binaire.chmod(0o755)

        with unittest.mock.patch.dict(os.environ, {"HOME": str(faux_home)}):
            backend = create_backend("KAGGLE", {})
            self.assertEqual(backend.cli, str(binaire))

            # Un chemin saisi explicitement reste prioritaire.
            impose = create_backend("KAGGLE", {"cli_path": "/un/autre/kaggle"})
            self.assertEqual(impose.cli, "/un/autre/kaggle")

    def test_cli_falls_back_to_the_bare_name(self):
        """Sans rien trouver, on laisse le PATH décider et le message le dira."""
        faux_home = self.directory / "maison-vide"
        faux_home.mkdir()
        with unittest.mock.patch.dict(os.environ, {"HOME": str(faux_home), "PATH": ""}):
            self.assertEqual(create_backend("KAGGLE", {}).cli, "kaggle")

    def test_status_parsing_accepts_both_cli_formats(self):
        """Les deux formats circulent selon la version du CLI Kaggle.

        Ne pas reconnaître « KernelWorkerStatus.COMPLETE » ferait attendre
        l'utilisateur jusqu'au délai maximum sur un kernel déjà terminé.
        """
        cas = {
            'ref has status "complete"': "complete",
            'ref has status "KernelWorkerStatus.COMPLETE"': "complete",
            'ref has status "KernelWorkerStatus.ERROR"': "error",
            'ref has status "KernelWorkerStatus.QUEUED"': "queued",
            'ref has status "KernelWorkerStatus.CANCEL_ACKNOWLEDGED"': "cancel_acknowledged",
            "has status running": "running",
            "sortie inattendue": "",
        }
        for sortie, attendu in cas.items():
            with self.subTest(sortie=sortie):
                self.assertEqual(_parse_status(sortie), attendu)


if __name__ == "__main__":
    unittest.main()

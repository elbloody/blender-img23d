# SPDX-License-Identifier: GPL-3.0-or-later
"""Backend « Kaggle » : GPU gratuit via l'API officielle des kernels.

Contrairement à Colab, Kaggle expose une vraie API headless
(``kaggle kernels push / status / output``) : pas de tunnel à maintenir, pas
de session à garder ouverte. Le cycle est toujours le même :

1. on écrit un script Python autonome (le *kernel*) qui embarque les images
   en base64 et les paramètres de génération ;
2. ``kaggle kernels push`` l'envoie et déclenche l'exécution sur GPU ;
3. ``kaggle kernels status`` est interrogé jusqu'à complétion ;
4. ``kaggle kernels output`` rapatrie le GLB produit.

Le CLI ``kaggle`` n'est pas dans le Python de Blender : on l'appelle en
sous-processus, exactement comme le backend local.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from .base import (
    Backend,
    BackendError,
    BackendStatus,
    BackendTimeout,
    BackendUnavailable,
    GenerationRequest,
    GenerationResult,
)

#: Kaggle refuse les sources de kernel trop volumineuses ; on garde une marge.
MAX_EMBEDDED_BYTES = 8 * 1024 * 1024

SLUG_RE = re.compile(r"^[a-z0-9-]+/[a-z0-9-]+$")

#: Préparation de l'environnement Kaggle, exécutée en tête de kernel.
#:
#: `hy3dgen` n'existe pas sur PyPI : c'est le paquet Python *contenu dans* le
#: dépôt Hunyuan3D-2. Il faut donc cloner puis installer le dépôt — un simple
#: `pip install hy3dgen` échouerait avec « No module named hy3dgen ».
#: Le clone va dans /tmp et non dans /kaggle/working, sinon tout le dépôt
#: serait rapatrié avec le maillage.
DEFAULT_SETUP = [
    "pip install -q trimesh rembg onnxruntime",
    "git clone --depth 1 https://github.com/Tencent-Hunyuan/Hunyuan3D-2.git /tmp/hunyuan3d",
    "pip install -q -e /tmp/hunyuan3d",
]

KERNEL_TEMPLATE = '''"""Kernel généré par l'extension Blender img23d. Ne pas éditer à la main."""
import base64
import json
import pathlib
import subprocess
import sys

JOB = json.loads(base64.b64decode("{job_b64}").decode("utf-8"))
OUT = pathlib.Path("/kaggle/working")
OUT.mkdir(parents=True, exist_ok=True)

# Les images d'entrée restent hors de /kaggle/working : ce dossier est
# rapatrié en entier, inutile de redescendre ce qu'on vient d'envoyer.
ENTREES = pathlib.Path("/tmp/img23d_entrees")
ENTREES.mkdir(parents=True, exist_ok=True)

image_paths = []
for index, entry in enumerate(JOB["images"]):
    path = ENTREES / f"input_{{index:02d}}_{{entry['view'].lower()}}{{entry['ext']}}"
    path.write_bytes(base64.b64decode(entry["data"]))
    image_paths.append((entry["view"].upper(), path))

for commande in JOB["setup"]:
    print("[img23d] $", commande, flush=True)
    termine = subprocess.run(commande, shell=True)
    if termine.returncode != 0:
        raise SystemExit(
            "[img23d] Preparation de l'environnement echouee (code %s) : %s"
            % (termine.returncode, commande)
        )

sys.path.insert(0, "/tmp/hunyuan3d")

import torch
from PIL import Image
from hy3dgen.shapegen import Hunyuan3DDiTFlowMatchingPipeline

images = []
for view, path in image_paths:
    image = Image.open(path)
    images.append((view, image.convert("RGBA") if image.mode != "RGBA" else image))

if JOB.get("remove_background", True):
    try:
        from hy3dgen.rembg import BackgroundRemover

        remover = BackgroundRemover()
        images = [(view, remover(image)) for view, image in images]
    except Exception as exc:
        print("Detourage ignore:", exc)

pipeline = Hunyuan3DDiTFlowMatchingPipeline.from_pretrained(JOB["model_repo"])

view_map = {{view.lower(): image for view, image in images}}
model_input = view_map if len(images) > 1 else images[0][1]

generator = torch.Generator(device="cpu").manual_seed(int(JOB.get("seed", 0)))
mesh = pipeline(
    image=model_input,
    num_inference_steps=int(JOB.get("steps", 30)),
    guidance_scale=float(JOB.get("guidance", 5.5)),
    octree_resolution=int(JOB.get("octree_resolution", 256)),
    generator=generator,
)
if isinstance(mesh, (list, tuple)):
    mesh = mesh[0]

face_limit = int(JOB.get("face_limit") or 0)
if face_limit and hasattr(mesh, "faces") and len(mesh.faces) > face_limit:
    try:
        mesh = mesh.simplify_quadric_decimation(face_limit)
    except Exception as exc:
        print("Decimation ignoree:", exc)

mesh.export(str(OUT / "mesh.glb"))
(OUT / "img23d_result.json").write_text(
    json.dumps({{"output": "mesh.glb", "faces": int(len(mesh.faces)) if hasattr(mesh, "faces") else 0}})
)
print("IMG23D_DONE")
'''


class KaggleBackend(Backend):
    id = "KAGGLE"
    label = "Kaggle Kernels"
    description = "GPU cloud gratuit (30 h/semaine) via l'API officielle des kernels."

    # -- configuration -----------------------------------------------------
    @property
    def cli(self) -> str:
        return str(self.cfg("cli_path", "kaggle") or "kaggle")

    @property
    def username(self) -> str:
        name = str(self.cfg("username", "") or "")
        if name:
            return name.strip().lower()
        credentials = _read_credentials_file()
        return str(credentials.get("username", "")).strip().lower()

    @property
    def slug(self) -> str:
        slug = str(self.cfg("kernel_slug", "") or "").strip().lower()
        if not slug:
            user = self.username
            if not user:
                raise BackendUnavailable(
                    "Nom d'utilisateur Kaggle inconnu : renseigne-le, ou installe ~/.kaggle/kaggle.json."
                )
            slug = f"{user}/img23d-worker"
        if not SLUG_RE.match(slug):
            raise BackendError(
                f"Slug de kernel invalide : {slug!r} (format attendu : utilisateur/nom-du-kernel)."
            )
        return slug

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        username = str(self.cfg("username", "") or "").strip()
        key = str(self.cfg("api_key", "") or "").strip()
        if username and key:
            env["KAGGLE_USERNAME"] = username
            env["KAGGLE_KEY"] = key
        return env

    # -- diagnostic --------------------------------------------------------
    def check(self) -> BackendStatus:
        if shutil.which(self.cli) is None and not Path(self.cli).exists():
            return BackendStatus.failure(
                f"CLI Kaggle introuvable ({self.cli})",
                "Installe-le hors de Blender : pip install --user kaggle",
            )

        has_inline = bool(self.cfg("username")) and bool(self.cfg("api_key"))
        credentials = _read_credentials_file()
        if not has_inline and not credentials:
            return BackendStatus.failure(
                "Aucune identification Kaggle",
                "Renseigne username + clé API, ou dépose ~/.kaggle/kaggle.json.",
            )

        try:
            completed = subprocess.run(
                [self.cli, "kernels", "list", "--mine", "-p", "1"],
                capture_output=True,
                text=True,
                timeout=90,
                env=self._env(),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return BackendStatus.failure("Appel du CLI Kaggle impossible", str(exc))

        if completed.returncode != 0:
            return BackendStatus.failure(
                "Identification Kaggle refusée",
                (completed.stderr or completed.stdout).strip()[:200],
            )

        details = [f"Identifié comme {self.username or 'utilisateur Kaggle'}"]
        try:
            details.append(f"Kernel cible : {self.slug}")
        except BackendError as exc:
            return BackendStatus.failure(str(exc))
        return BackendStatus.success("Kaggle prêt", *details)

    # -- génération --------------------------------------------------------
    def generate(self, request: GenerationRequest, ctx) -> GenerationResult:
        slug = self.slug
        workdir = Path(tempfile.mkdtemp(prefix="img23d-kaggle-"))
        try:
            ctx.report("Préparation du kernel Kaggle…", 0.05)
            self._write_kernel(workdir, slug, request)

            ctx.report("Envoi du kernel (kaggle kernels push)…", 0.12)
            self._run(["kernels", "push", "-p", str(workdir)], timeout=600)

            self._wait(slug, ctx)

            ctx.report("Récupération de la sortie…", 0.9)
            output_dir = workdir / "output"
            output_dir.mkdir(exist_ok=True)
            self._run(["kernels", "output", slug, "-p", str(output_dir)], timeout=900)

            mesh = _find_mesh(output_dir)
            if mesh is None:
                raise BackendError(
                    "Aucun maillage dans la sortie du kernel. "
                    f"Ouvre https://www.kaggle.com/code/{slug} pour lire ses logs."
                )

            final = Path(tempfile.mkdtemp(prefix="img23d-out-")) / mesh.name
            shutil.copy2(mesh, final)
            return GenerationResult(
                path=final,
                backend=self.id,
                format=final.suffix.lstrip(".") or "glb",
                meta={"kernel": slug, "url": f"https://www.kaggle.com/code/{slug}"},
            )
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    # -- interne -----------------------------------------------------------
    def _write_kernel(self, workdir: Path, slug: str, request: GenerationRequest) -> None:
        images = []
        total = 0
        for image in request.images:
            encoded = base64.b64encode(image.data).decode("ascii")
            total += len(encoded)
            images.append(
                {
                    "view": image.view,
                    "ext": image.extension,
                    "data": encoded,
                }
            )
        if total > MAX_EMBEDDED_BYTES:
            raise BackendError(
                f"Images trop lourdes pour être embarquées dans le kernel ({total // 1024} Ko). "
                "Baisse « Taille max des images » dans le panneau Source."
            )

        job = {
            "images": images,
            "prompt": request.prompt,
            "seed": request.seed,
            "steps": request.steps,
            "guidance": request.guidance,
            "octree_resolution": request.octree_resolution,
            "face_limit": request.face_limit,
            "remove_background": request.remove_background,
            "with_texture": request.with_texture,
            "model_repo": str(self.cfg("model_repo", "tencent/Hunyuan3D-2mini")),
            "setup": list(self.cfg("setup_commands", DEFAULT_SETUP)),
        }
        script = KERNEL_TEMPLATE.format(
            job_b64=base64.b64encode(json.dumps(job).encode("utf-8")).decode("ascii")
        )
        (workdir / "img23d_kernel.py").write_text(script, encoding="utf-8")

        metadata = {
            "id": slug,
            "title": slug.split("/")[-1].replace("-", " ").title(),
            "code_file": "img23d_kernel.py",
            "language": "python",
            "kernel_type": "script",
            "is_private": True,
            "enable_gpu": True,
            "enable_internet": True,
            "dataset_sources": [],
            "competition_sources": [],
            "kernel_sources": [],
        }
        (workdir / "kernel-metadata.json").write_text(
            json.dumps(metadata, indent=2), encoding="utf-8"
        )

    def _wait(self, slug: str, ctx) -> None:
        interval = float(self.cfg("poll_interval", 20.0))
        deadline = time.monotonic() + float(self.cfg("timeout_minutes", 45.0)) * 60.0
        started = time.monotonic()

        while True:
            ctx.raise_if_cancelled()
            if time.monotonic() > deadline:
                raise BackendTimeout(
                    f"Le kernel {slug} n'a pas terminé à temps. "
                    f"Suis-le sur https://www.kaggle.com/code/{slug}"
                )

            output = self._run(["kernels", "status", slug], timeout=120)
            state = _parse_status(output)
            elapsed = int(time.monotonic() - started)

            if state in {"complete", "completed", "success"}:
                ctx.report(f"Kernel terminé en {elapsed // 60} min {elapsed % 60} s", 0.85)
                return
            if state in {"error", "cancelAcknowledged", "cancelled", "failed"}:
                raise BackendError(
                    f"Le kernel Kaggle a échoué ({state}). "
                    f"Logs : https://www.kaggle.com/code/{slug}"
                )

            label = {"queued": "en file d'attente", "running": "en cours d'exécution"}.get(
                state, state or "en attente"
            )
            # Sans progression réelle côté Kaggle, on interpole sur le temps écoulé.
            fraction = 0.15 + 0.65 * min(elapsed / max(deadline - started, 1.0), 1.0)
            ctx.report(f"Kernel {label} ({elapsed // 60} min)", fraction)
            ctx.sleep(interval)

    def _run(self, args: list[str], timeout: float) -> str:
        command = [self.cli, *args]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=self._env(),
                check=False,
            )
        except FileNotFoundError as exc:
            raise BackendUnavailable(f"CLI Kaggle introuvable : {self.cli}") from exc
        except subprocess.TimeoutExpired as exc:
            raise BackendTimeout(f"`{' '.join(command)}` n'a pas répondu.") from exc

        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()[:400]
            raise BackendError(f"`kaggle {' '.join(args)}` a échoué : {detail}")
        return completed.stdout


def _parse_status(output: str) -> str:
    """Extrait l'état depuis la sortie de ``kaggle kernels status``.

    Le CLI affiche par exemple : ``has status "running"``.
    """
    match = re.search(r'status\s+"?([A-Za-z]+)"?', output or "")
    return match.group(1).lower() if match else ""


def _read_credentials_file() -> dict:
    path = Path(os.environ.get("KAGGLE_CONFIG_DIR", Path.home() / ".kaggle")) / "kaggle.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _find_mesh(directory: Path) -> Path | None:
    candidates = [
        path
        for suffix in (".glb", ".gltf", ".obj", ".ply", ".stl")
        for path in sorted(directory.rglob(f"*{suffix}"))
    ]
    return candidates[0] if candidates else None

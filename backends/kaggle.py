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
from .hostexec import (
    child_env,
    host_command,
    python_module_command,
    sandbox_hint,
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

    def __init__(self, config=None) -> None:
        super().__init__(config)
        self._cached_username: str | None = None

    # -- configuration -----------------------------------------------------
    @property
    def cli(self) -> str:
        """Chemin du CLI Kaggle, deviné si l'utilisateur n'a rien imposé.

        Le champ des préférences revient à sa valeur par défaut à chaque
        réinstallation de l'extension. Or le CLI vit rarement dans le PATH :
        l'installer proprement sur Fedora ou Ubuntu passe par un
        environnement dédié, dont le dossier bin n'y est pas. Sans cette
        recherche, il faut ressaisir le chemin après chaque mise à jour.
        """
        configured = str(self.cfg("cli_path", "") or "").strip()
        if configured and configured != "kaggle":
            return configured  # chemin explicite : on n'y touche pas
        if shutil.which("kaggle"):
            return "kaggle"
        found = _find_cli()
        return found or "kaggle"

    @property
    def username(self) -> str:
        name = str(self.cfg("username", "") or "")
        if name:
            return name.strip().lower()
        credentials = _read_credentials_file()
        from_file = str(credentials.get("username", "")).strip().lower()
        return from_file or self._username_from_cli()

    @property
    def slug(self) -> str:
        slug = str(self.cfg("kernel_slug", "") or "").strip().lower()
        if not slug:
            user = self.username
            if not user:
                raise BackendUnavailable(
                    "Nom d'utilisateur Kaggle introuvable. Renseigne-le dans le champ "
                    "« Utilisateur » des préférences : c'est le pseudo affiché sur ton "
                    "profil Kaggle."
                )
            slug = f"{user}/img23d-worker"
        if not SLUG_RE.match(slug):
            raise BackendError(
                f"Slug de kernel invalide : {slug!r} (format attendu : utilisateur/nom-du-kernel)."
            )
        return slug

    def _env(self) -> dict[str, str]:
        """Identifiants passés au CLI par l'environnement.

        Kaggle a deux systèmes en circulation, et le CLI essaie le nouveau
        en premier :

        * le **jeton d'API** (``KGAT_…``), une seule valeur, qui porte aussi
          l'identité de son propriétaire ;
        * l'ancien couple **utilisateur + clé**, désormais présenté comme
          « Legacy API Credentials » sur le site.

        On transmet ce dont on dispose, sans en privilégier un.
        """
        env = child_env()

        token = str(self.cfg("api_token", "") or "").strip()
        if token:
            env["KAGGLE_API_TOKEN"] = token

        username = str(self.cfg("username", "") or "").strip()
        key = str(self.cfg("api_key", "") or "").strip()
        if username and key:
            env["KAGGLE_USERNAME"] = username
            env["KAGGLE_KEY"] = key
        return env

    def _credential_sources(self) -> list[str]:
        """Les sources d'identification disponibles, pour le diagnostic."""
        sources = []
        if str(self.cfg("api_token", "") or "").strip():
            sources.append("jeton d'API (préférences)")
        if str(self.cfg("username", "") or "").strip() and str(self.cfg("api_key", "") or "").strip():
            sources.append("utilisateur + clé (préférences)")
        directory = _config_dir()
        for nom, description in (
            ("credentials.json", "session ouverte par « kaggle auth login »"),
            ("access_token", "jeton dans ~/.kaggle/access_token"),
            ("kaggle.json", "ancien fichier ~/.kaggle/kaggle.json"),
        ):
            if (directory / nom).is_file():
                sources.append(description)
        return sources

    def _username_from_cli(self) -> str:
        """Demande son identité au CLI : le jeton d'API la porte déjà.

        Évite à l'utilisateur de retaper un nom que l'outil connaît. Un échec
        n'est pas fatal : on laisse le champ des préférences prendre le relais.
        """
        if self._cached_username is not None:
            return self._cached_username

        self._cached_username = ""
        try:
            completed = subprocess.run(
                self._command(["config", "view"]),
                capture_output=True,
                text=True,
                timeout=120,
                env=self._env(),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return ""

        if completed.returncode == 0:
            self._cached_username = _parse_username(completed.stdout)
        return self._cached_username

    # -- diagnostic --------------------------------------------------------
    def check(self) -> BackendStatus:
        if shutil.which(self.cli) is None and not Path(self.cli).exists():
            # Sous Flatpak, « introuvable » veut souvent dire « invisible depuis
            # le bac à sable » plutôt que « pas installé » : le dire évite une
            # réinstallation inutile.
            indice = sandbox_hint("le CLI Kaggle")
            if indice:
                return BackendStatus.failure(
                    f"CLI Kaggle invisible depuis Blender ({self.cli})", indice
                )
            return BackendStatus.failure(
                f"CLI Kaggle introuvable ({self.cli})",
                "Installe-le hors de Blender, dans un environnement dédié :",
                "python3 -m venv ~/.img23d-kaggle && ~/.img23d-kaggle/bin/pip install kaggle",
                "L'extension le trouvera ensuite toute seule.",
            )

        # On n'énumère plus les identifiants pour décider d'essayer ou non :
        # Kaggle en a déjà changé deux fois, et une liste sera toujours en
        # retard. On laisse le CLI trancher, et on rapporte ce qu'il dit.
        sources = self._credential_sources()

        # Sonde : `config view`, et non `kernels list`. Lister les notebooks
        # d'un compte qui n'en a aucun rend « Not found » — un compte neuf
        # aurait donc été déclaré non identifié. `config view` ne teste que
        # l'identification, et rend le pseudo au passage.
        try:
            completed = subprocess.run(
                self._command(["config", "view"]),
                capture_output=True,
                text=True,
                timeout=120,
                env=self._env(),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return BackendStatus.failure("Appel du CLI Kaggle impossible", str(exc))

        sortie = f"{completed.stdout}\n{completed.stderr}"
        identite = _parse_username(completed.stdout)
        if completed.returncode != 0 or "Authentication required" in sortie or not identite:
            # La cause peut être une clé invalide comme une panne réseau : on
            # ne préjuge pas, on montre ce que le CLI a dit.
            detail = (completed.stderr or completed.stdout).strip()
            if not sources or "Authentication required" in sortie:
                indice = (
                    f"Aucune identification trouvée. Le plus simple : ouvre un terminal "
                    f"et lance « {self.cli} auth login », puis connecte-toi dans le navigateur."
                )
            elif any(code in detail for code in ("401", "403")) or "credential" in detail.lower():
                indice = "Identifiants refusés : le jeton a peut-être expiré ou été régénéré."
            elif "ModuleNotFoundError" in detail or "No module named" in detail:
                indice = sandbox_hint("le CLI Kaggle") or (
                    "Le CLI a démarré mais ne trouve pas ses modules : son "
                    "environnement Python est incomplet. Réinstalle-le."
                )
            else:
                indice = sandbox_hint("le CLI Kaggle") or "Vérifie ta connexion réseau."
            return BackendStatus.failure(
                "Le CLI Kaggle n'a pas abouti", detail[:300], indice
            )

        self._cached_username = identite
        details = [
            f"Identifié comme {self.username or identite}",
            f"CLI : {self.cli}",
        ]
        if sources:
            details.append(f"Source : {sources[0]}")
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
            if state in {
                "error",
                "failed",
                "cancel_requested",
                "cancel_acknowledged",
                "cancelled",
                "canceled",
            }:
                raise BackendError(
                    f"Le kernel Kaggle a échoué ({state}). "
                    f"Logs : https://www.kaggle.com/code/{slug}"
                )

            # `new_script` signifie « poussé, pas encore démarré » : on continue.
            label = {
                "queued": "en file d'attente",
                "running": "en cours d'exécution",
                "new_script": "pas encore démarré",
            }.get(state, state or "en attente")
            # Sans progression réelle côté Kaggle, on interpole sur le temps écoulé.
            fraction = 0.15 + 0.65 * min(elapsed / max(deadline - started, 1.0), 1.0)
            ctx.report(f"Kernel {label} ({elapsed // 60} min)", fraction)
            ctx.sleep(interval)

    def _command(self, args: list[str]) -> list[str]:
        """Construit l'appel au CLI, en contournant les deux pièges connus.

        Le lanceur d'un venv dépend de sa ligne shebang : on appelle plutôt
        l'interpréteur du venv directement. Et sous Flatpak, on demande à
        l'hôte d'exécuter la commande.
        """
        direct = python_module_command(self.cli, "kaggle")
        base = direct if direct else [self.cli]
        return host_command([*base, *args])

    def _run(self, args: list[str], timeout: float) -> str:
        command = self._command(args)
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

    Le format a changé selon les versions du CLI, et les deux circulent :

        ancien  : monpseudo/img23d-worker has status "complete"
        2.2.x   : monpseudo/img23d-worker has status "KernelWorkerStatus.COMPLETE"

    On accepte les deux et on rend toujours la forme courte en minuscules
    (``complete``, ``error``, ``cancel_acknowledged``…). Ne pas reconnaître la
    fin d'exécution coûterait à l'utilisateur toute la durée du délai
    d'attente, sur un kernel pourtant terminé depuis longtemps.
    """
    match = re.search(r'status\s+"?([A-Za-z_.]+)"?', output or "")
    if not match:
        return ""
    return match.group(1).rsplit(".", 1)[-1].lower()


#: Emplacements habituels d'un CLI Kaggle installé hors du PATH, dans l'ordre
#: de préférence : d'abord celui que documente le README de l'extension.
_CLI_CANDIDATES = (
    "~/.img23d-kaggle/bin/kaggle",
    "~/.local/bin/kaggle",
    "~/.local/pipx/venvs/kaggle/bin/kaggle",
    "~/kaggle-env/bin/kaggle",
    "~/.venv/bin/kaggle",
)


def _parse_username(output: str) -> str:
    """Lit le pseudo dans la sortie de ``kaggle config view``.

    Le CLI affiche ``- username: elbloody``, ou ``- username: None`` quand il
    n'est identifié par rien.
    """
    match = re.search(r"^[-\s]*username\s*:\s*(\S+)", output or "", re.MULTILINE)
    if not match or match.group(1).lower() == "none":
        return ""
    return match.group(1).strip().lower()


def _find_cli() -> str:
    """Cherche le CLI aux emplacements habituels. Rend "" si rien ne convient."""
    for candidate in _CLI_CANDIDATES:
        path = Path(candidate).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return ""


def _config_dir() -> Path:
    return Path(os.environ.get("KAGGLE_CONFIG_DIR", Path.home() / ".kaggle"))


def _read_credentials_file() -> dict:
    """Lit ~/.kaggle/kaggle.json, l'ancien format d'identifiants."""
    try:
        return json.loads((_config_dir() / "kaggle.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _find_mesh(directory: Path) -> Path | None:
    candidates = [
        path
        for suffix in (".glb", ".gltf", ".obj", ".ply", ".stl")
        for path in sorted(directory.rglob(f"*{suffix}"))
    ]
    return candidates[0] if candidates else None

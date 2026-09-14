# SPDX-License-Identifier: GPL-3.0-or-later
"""Backend « local » : génération sur le GPU de la machine.

Blender embarque son propre interpréteur Python, dans lequel on ne peut pas
installer PyTorch sans risquer de casser l'installation. On ne charge donc
jamais le modèle dans Blender : on lance un *worker* (``local_worker.py``)
dans l'interpréteur Python d'un environnement séparé, que l'utilisateur
désigne dans les préférences, et on dialogue avec lui par stdout.
"""

from __future__ import annotations

import json
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from . import hostexec
from .base import (
    Backend,
    BackendError,
    BackendStatus,
    BackendUnavailable,
    GenerationRequest,
    GenerationResult,
)

#: Préfixes du protocole ligne-à-ligne parlé par ``local_worker.py``.
PROGRESS_PREFIX = "@@IMG23D_PROGRESS "
RESULT_PREFIX = "@@IMG23D_RESULT "
ERROR_PREFIX = "@@IMG23D_ERROR "

WORKER_NAME = "local_worker.py"


class LocalBackend(Backend):
    id = "LOCAL"
    label = "Local (ton propre GPU)"
    description = "Aucun compte, aucun cloud. Nécessite un environnement Python avec PyTorch."

    # -- diagnostic --------------------------------------------------------
    def check(self) -> BackendStatus:
        python = str(self.cfg("python_executable", "") or "")
        if not python:
            return BackendStatus.failure(
                "Aucun interpréteur Python configuré",
                "Renseigne le Python d'un environnement contenant torch + hy3dgen.",
            )
        if not Path(python).exists() and shutil.which(python) is None:
            return BackendStatus.failure(f"Interpréteur introuvable : {python}")

        details: list[str] = []
        probe = (
            "import json, torch;"
            "d = {'torch': torch.__version__, 'cuda': torch.cuda.is_available(),"
            " 'device': torch.cuda.get_device_name(0) if torch.cuda.is_available() else '',"
            " 'vram': (torch.cuda.get_device_properties(0).total_memory // (1024**3))"
            " if torch.cuda.is_available() else 0};"
            "print(json.dumps(d))"
        )
        try:
            completed = subprocess.run(
                [python, "-c", probe],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return BackendStatus.failure(f"Impossible d'interroger {python}", str(exc))

        if completed.returncode != 0:
            return BackendStatus.failure(
                "PyTorch absent de cet environnement",
                (completed.stderr or "").strip().splitlines()[-1:][0]
                if completed.stderr.strip()
                else "Installe torch dans l'environnement ciblé.",
            )

        try:
            info = json.loads(completed.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return BackendStatus.failure("Réponse inattendue de l'interpréteur")

        details.append(f"PyTorch {info.get('torch')}")
        if not info.get("cuda"):
            details.append("Aucun GPU CUDA détecté : la génération tournera sur CPU (très lent).")
            return BackendStatus(True, "Utilisable, mais sans accélération GPU", details)

        vram = int(info.get("vram") or 0)
        details.append(f"GPU : {info.get('device')} ({vram} Go de VRAM)")
        if vram < 6:
            details.append(
                "Moins de 6 Go de VRAM : prévois une résolution d'octree basse, ou un autre backend."
            )
        return BackendStatus.success("Backend local prêt", *details)

    # -- génération --------------------------------------------------------
    def generate(self, request: GenerationRequest, ctx) -> GenerationResult:
        python = self.require("python_executable", "Interpréteur Python local")
        worker = self._worker_path()

        workdir = Path(tempfile.mkdtemp(prefix="img23d-local-"))
        try:
            job_file = self._write_job(workdir, request)
            ctx.report("Démarrage du worker local…", 0.05)

            command = [python, str(worker), "--job", str(job_file)]
            extra = str(self.cfg("extra_args", "") or "").split()
            command.extend(extra)

            # Purge PYTHONHOME/PYTHONPATH : hérités de Blender, ils
            # détourneraient l'interpréteur du worker vers celui de Blender.
            env = hostexec.child_env()
            env.setdefault("PYTHONUNBUFFERED", "1")
            env.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
            cache_dir = str(self.cfg("cache_dir", "") or "")
            if cache_dir:
                env["HF_HOME"] = cache_dir

            result = self._run_worker(command, env, ctx)
            output = Path(result["output"])
            if not output.exists():
                raise BackendError(f"Le worker annonce {output}, mais le fichier est absent.")

            # Le dossier temporaire est supprimé ensuite : on rapatrie le fichier.
            final = Path(tempfile.mkdtemp(prefix="img23d-out-")) / output.name
            shutil.copy2(output, final)
            return GenerationResult(
                path=final,
                backend=self.id,
                format=final.suffix.lstrip(".") or "glb",
                textured=bool(result.get("textured")),
                meta={"worker": str(worker), "python": python, **result.get("meta", {})},
            )
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    # -- interne -----------------------------------------------------------
    def _worker_path(self) -> Path:
        override = str(self.cfg("worker_script", "") or "")
        if override:
            path = Path(override).expanduser()
            if not path.exists():
                raise BackendUnavailable(f"Script worker introuvable : {path}")
            return path
        return Path(__file__).with_name(WORKER_NAME)

    def _write_job(self, workdir: Path, request: GenerationRequest) -> Path:
        images_dir = workdir / "images"
        images_dir.mkdir(parents=True, exist_ok=True)

        images = []
        for index, image in enumerate(request.images):
            path = images_dir / f"{index:02d}_{image.view.lower()}{image.extension}"
            path.write_bytes(image.data)
            images.append({"path": str(path), "view": image.view})

        job = {
            "images": images,
            "output_dir": str(workdir / "out"),
            "prompt": request.prompt,
            "seed": request.seed,
            "steps": request.steps,
            "guidance": request.guidance,
            "octree_resolution": request.octree_resolution,
            "face_limit": request.face_limit,
            "remove_background": request.remove_background,
            "with_texture": request.with_texture,
            "model_repo": str(self.cfg("model_repo", "tencent/Hunyuan3D-2mini")),
            "device": str(self.cfg("device", "cuda")),
            "low_vram": bool(self.cfg("low_vram", False)),
        }
        job_file = workdir / "job.json"
        job_file.write_text(json.dumps(job, indent=2), encoding="utf-8")
        return job_file

    def _run_worker(self, command: list[str], env: dict[str, str], ctx) -> dict:
        creation_flags = 0
        if sys.platform == "win32":  # évite une fenêtre console qui clignote
            creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=env,
                creationflags=creation_flags,
            )
        except OSError as exc:
            raise BackendUnavailable(f"Lancement impossible : {exc}") from exc

        # La lecture de stdout doit se faire dans un thread : itérer
        # directement dessus bloque jusqu'à la ligne suivante, et un worker
        # silencieux rendrait l'annulation inopérante pendant tout ce temps.
        lines: queue.Queue = queue.Queue()
        reader = threading.Thread(
            target=_pump_stdout, args=(process.stdout, lines), daemon=True
        )
        reader.start()

        result: dict | None = None
        error_message = ""
        tail: list[str] = []

        try:
            while True:
                if ctx.cancelled:
                    break
                try:
                    line = lines.get(timeout=0.2)
                except queue.Empty:
                    continue
                if line is None:  # fin de flux
                    break

                if line.startswith(PROGRESS_PREFIX):
                    payload = _parse(line[len(PROGRESS_PREFIX) :])
                    ctx.report(
                        str(payload.get("message", "Génération…")),
                        _as_fraction(payload.get("progress")),
                    )
                elif line.startswith(RESULT_PREFIX):
                    result = _parse(line[len(RESULT_PREFIX) :])
                elif line.startswith(ERROR_PREFIX):
                    error_message = str(_parse(line[len(ERROR_PREFIX) :]).get("message", line))
                elif line.strip():
                    tail.append(line)
                    del tail[:-40]
                    ctx.log(line, "DEBUG")
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
            reader.join(timeout=5)
            if process.stdout is not None:
                process.stdout.close()

        ctx.raise_if_cancelled()

        code = process.wait()
        if error_message:
            raise BackendError(f"Worker local : {error_message}")
        if code != 0:
            hint = "\n".join(tail[-8:]) or "aucune sortie"
            raise BackendError(f"Le worker local s'est arrêté (code {code}).\n{hint}")
        if result is None:
            raise BackendError("Le worker local n'a rendu aucun résultat.")
        return result


def _pump_stdout(stream, lines: "queue.Queue") -> None:
    """Recopie les lignes du worker dans une file, puis signale la fin par ``None``."""
    try:
        for raw in stream:
            lines.put(raw.rstrip("\n"))
    finally:
        lines.put(None)


def _parse(payload: str) -> dict:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return {"message": payload.strip()}
    return data if isinstance(data, dict) else {"message": str(data)}


def _as_fraction(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return min(max(number, 0.0), 1.0)

# SPDX-License-Identifier: GPL-3.0-or-later
"""Worker exécuté HORS de Blender, dans un environnement Python avec PyTorch.

Ce fichier n'est jamais importé par l'extension : il est lancé en
sous-processus par ``backends/local.py``. Il lit un job JSON, charge le
pipeline Hunyuan3D et écrit un GLB, en annonçant sa progression sur stdout :

    @@IMG23D_PROGRESS {"progress": 0.4, "message": "Diffusion…"}
    @@IMG23D_RESULT   {"output": "/chemin/mesh.glb", "textured": false}
    @@IMG23D_ERROR    {"message": "…"}

Toute autre ligne est traitée comme un simple log.

Installation de l'environnement attendu (exemple CUDA 12.1) :

    python -m venv ~/.img23d-env
    ~/.img23d-env/bin/pip install torch --index-url https://download.pytorch.org/whl/cu121
    ~/.img23d-env/bin/pip install "huggingface_hub" "pillow" "trimesh"
    # puis Hunyuan3D-2 : https://github.com/Tencent-Hunyuan/Hunyuan3D-2
    ~/.img23d-env/bin/pip install -e /chemin/vers/Hunyuan3D-2
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

PROGRESS_PREFIX = "@@IMG23D_PROGRESS "
RESULT_PREFIX = "@@IMG23D_RESULT "
ERROR_PREFIX = "@@IMG23D_ERROR "


def emit(prefix: str, payload: dict) -> None:
    sys.stdout.write(prefix + json.dumps(payload) + "\n")
    sys.stdout.flush()


def progress(message: str, fraction: float | None = None) -> None:
    payload: dict = {"message": message}
    if fraction is not None:
        payload["progress"] = fraction
    emit(PROGRESS_PREFIX, payload)


def load_images(job: dict):
    from PIL import Image

    images = []
    for entry in job["images"]:
        image = Image.open(entry["path"])
        if image.mode != "RGBA":
            image = image.convert("RGBA")
        images.append((entry.get("view", "AUTO").upper(), image))
    return images


def remove_background(images):
    """Détourage optionnel : sans ``rembg``, on garde l'image telle quelle."""
    try:
        from hy3dgen.rembg import BackgroundRemover
    except ImportError:
        try:
            from rembg import remove as rembg_remove
        except ImportError:
            progress("rembg absent : détourage ignoré")
            return images
        return [(view, rembg_remove(image)) for view, image in images]

    remover = BackgroundRemover()
    return [(view, remover(image)) for view, image in images]


def build_pipeline(job: dict):
    from hy3dgen.shapegen import Hunyuan3DDiTFlowMatchingPipeline

    progress(f"Chargement du modèle {job['model_repo']}…", 0.1)
    pipeline = Hunyuan3DDiTFlowMatchingPipeline.from_pretrained(job["model_repo"])
    device = job.get("device") or "cuda"
    if hasattr(pipeline, "to"):
        pipeline.to(device)
    if job.get("low_vram") and hasattr(pipeline, "enable_flashvdm"):
        pipeline.enable_flashvdm()
    return pipeline


def run(job: dict) -> dict:
    import torch

    output_dir = Path(job["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)

    images = load_images(job)
    if job.get("remove_background", True):
        progress("Détourage des images…", 0.05)
        images = remove_background(images)

    pipeline = build_pipeline(job)

    # Le pipeline multiview attend un dict {vue: image} ; le pipeline
    # mono-image attend une seule image. On choisit selon ce qu'on a.
    view_map = {view.lower(): image for view, image in images}
    if len(images) > 1:
        prompt_images = {k: v for k, v in view_map.items() if k in {"front", "back", "left", "right"}}
        model_input = prompt_images or view_map
    else:
        model_input = images[0][1]

    generator = torch.Generator(device="cpu").manual_seed(int(job.get("seed", 0)))
    progress("Diffusion de la forme…", 0.25)

    meshes = pipeline(
        image=model_input,
        num_inference_steps=int(job.get("steps", 30)),
        guidance_scale=float(job.get("guidance", 5.5)),
        octree_resolution=int(job.get("octree_resolution", 256)),
        generator=generator,
    )
    mesh = meshes[0] if isinstance(meshes, (list, tuple)) else meshes
    progress("Forme générée", 0.7)

    textured = False
    if job.get("with_texture"):
        try:
            from hy3dgen.texgen import Hunyuan3DPaintPipeline

            progress("Génération de la texture…", 0.75)
            painter = Hunyuan3DPaintPipeline.from_pretrained(job["model_repo"])
            mesh = painter(mesh, image=view_map.get("front") or images[0][1])
            textured = True
        except Exception as exc:  # noqa: BLE001 - la forme reste exploitable sans texture
            progress(f"Texture ignorée ({type(exc).__name__}: {exc})")

    face_limit = int(job.get("face_limit") or 0)
    if face_limit and hasattr(mesh, "faces") and len(mesh.faces) > face_limit:
        progress(f"Décimation vers {face_limit} faces…", 0.9)
        try:
            mesh = mesh.simplify_quadric_decimation(face_limit)
        except Exception as exc:  # noqa: BLE001 - Blender saura décimer à la place
            progress(f"Décimation ignorée ({exc}) : Blender s'en chargera")

    output = output_dir / "mesh.glb"
    progress("Écriture du GLB…", 0.95)
    mesh.export(str(output))

    return {
        "output": str(output),
        "textured": textured,
        "meta": {
            "model_repo": job["model_repo"],
            "faces": int(len(mesh.faces)) if hasattr(mesh, "faces") else 0,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Worker img23d (génération locale)")
    parser.add_argument("--job", required=True, help="Fichier JSON décrivant le travail")
    args = parser.parse_args(argv)

    try:
        job = json.loads(Path(args.job).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        emit(ERROR_PREFIX, {"message": f"Job illisible : {exc}"})
        return 2

    try:
        result = run(job)
    except ImportError as exc:
        emit(
            ERROR_PREFIX,
            {
                "message": (
                    f"Dépendance manquante ({exc}). "
                    "Installe torch, pillow, trimesh et hy3dgen dans cet environnement."
                )
            },
        )
        return 3
    except Exception as exc:  # noqa: BLE001 - la trace part dans les logs
        sys.stderr.write(traceback.format_exc())
        emit(ERROR_PREFIX, {"message": f"{type(exc).__name__}: {exc}"})
        return 1

    emit(RESULT_PREFIX, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

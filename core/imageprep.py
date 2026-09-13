# SPDX-License-Identifier: GPL-3.0-or-later
"""Normalisation des images sources, en s'appuyant sur Blender.

Aucune dépendance externe n'est nécessaire : Blender sait déjà ouvrir,
redimensionner et réencoder une image. Les backends ne reçoivent donc que des
octets PNG/JPEG propres, tous à la même taille maximale.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import bpy

from ..backends.base import SourceImage, safe_filename

#: Extensions acceptées en entrée.
SUPPORTED = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp", ".tga", ".exr"}

_MIME = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}


class ImagePrepError(Exception):
    """Une image source est inutilisable (fichier absent, format refusé…)."""


def prepare_from_paths(
    entries: list[tuple[str, str]],
    max_size: int = 1024,
    file_format: str = "PNG",
) -> list[SourceImage]:
    """Charge, redimensionne et encode chaque ``(chemin, vue)``.

    Les fichiers temporaires produits sont supprimés avant de rendre la main :
    seuls les octets restent en mémoire.
    """
    file_format = file_format.upper()
    if file_format not in _MIME:
        raise ImagePrepError(f"Format de sortie non géré : {file_format}")

    prepared: list[SourceImage] = []
    for raw_path, view in entries:
        path = Path(bpy.path.abspath(raw_path)).resolve()
        if not path.is_file():
            raise ImagePrepError(f"Image introuvable : {path}")
        if path.suffix.lower() not in SUPPORTED:
            raise ImagePrepError(
                f"Format non géré : {path.suffix or path.name}. "
                f"Formats acceptés : {', '.join(sorted(SUPPORTED))}"
            )
        prepared.append(_encode(path, view, max_size, file_format))
    return prepared


def prepare_from_datablock(
    image: "bpy.types.Image",
    view: str = "AUTO",
    max_size: int = 1024,
    file_format: str = "PNG",
) -> SourceImage:
    """Encode une image déjà chargée dans le fichier .blend."""
    copy = image.copy()
    try:
        return _encode_datablock(copy, safe_filename(image.name), view, max_size, file_format.upper())
    finally:
        bpy.data.images.remove(copy)


def _encode(path: Path, view: str, max_size: int, file_format: str) -> SourceImage:
    image = bpy.data.images.load(str(path), check_existing=False)
    try:
        return _encode_datablock(image, safe_filename(path.stem), view, max_size, file_format)
    finally:
        bpy.data.images.remove(image)


def _encode_datablock(
    image: "bpy.types.Image",
    stem: str,
    view: str,
    max_size: int,
    file_format: str,
) -> SourceImage:
    width, height = image.size
    if width == 0 or height == 0:
        raise ImagePrepError(f"Image vide ou illisible : {image.name}")

    if max_size > 0 and max(width, height) > max_size:
        ratio = max_size / float(max(width, height))
        image.scale(max(int(width * ratio), 1), max(int(height * ratio), 1))

    extension = ".png" if file_format == "PNG" else (".jpg" if file_format == "JPEG" else ".webp")
    handle, temp_path = tempfile.mkstemp(prefix="img23d-src-", suffix=extension)
    os.close(handle)
    try:
        image.file_format = file_format
        image.filepath_raw = temp_path
        image.save()
        data = Path(temp_path).read_bytes()
    finally:
        Path(temp_path).unlink(missing_ok=True)

    if not data:
        raise ImagePrepError(f"Réencodage impossible pour {image.name}")

    return SourceImage(
        data=data,
        name=f"{stem}{extension}",
        view=(view or "AUTO").upper(),
        mime=_MIME[file_format],
    )

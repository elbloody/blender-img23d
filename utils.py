# SPDX-License-Identifier: GPL-3.0-or-later
"""Petits utilitaires partagés par les opérateurs et l'interface."""

from __future__ import annotations

import re
from pathlib import Path

import bpy

_INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def tag_redraw(context: bpy.types.Context, area_types: tuple[str, ...] = ("VIEW_3D",)) -> None:
    """Force le redessin des zones concernées.

    Un opérateur modal qui met à jour une barre de progression doit le demander
    explicitement : Blender ne redessine pas de lui-même entre deux timers.
    """
    window = getattr(context, "window", None)
    windows = [window] if window else list(context.window_manager.windows)
    for win in windows:
        if win is None or win.screen is None:
            continue
        for area in win.screen.areas:
            if area.type in area_types:
                area.tag_redraw()


def set_status(context: bpy.types.Context, message: str, progress: float | None = None) -> None:
    """Met à jour l'état volatile affiché dans le panneau."""
    state = context.window_manager.img23d_state
    state.status = message
    if progress is not None:
        state.progress = min(max(progress, 0.0), 1.0)


def safe_stem(name: str, fallback: str = "img23d_model") -> str:
    """Nettoie un nom de fichier saisi par l'utilisateur."""
    cleaned = _INVALID_FILENAME.sub("_", (name or "").strip()).strip(". ")
    return cleaned or fallback


def resolve_export_path(directory: str, stem: str, extension: str) -> Path:
    """Construit un chemin d'export absolu, en résolvant les chemins ``//`` de Blender."""
    base = Path(bpy.path.abspath(directory or "//"))
    extension = extension if extension.startswith(".") else f".{extension}"
    return (base / f"{safe_stem(stem)}{extension}").resolve()


def unique_path(path: Path) -> Path:
    """Ajoute un suffixe numérique plutôt que d'écraser un fichier existant."""
    if not path.exists():
        return path
    for index in range(1, 1000):
        candidate = path.with_name(f"{path.stem}_{index:03d}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"Impossible de trouver un nom libre à côté de {path}")


def human_duration(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    if seconds < 60:
        return f"{seconds} s"
    return f"{seconds // 60} min {seconds % 60:02d} s"

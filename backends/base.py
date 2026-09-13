# SPDX-License-Identifier: GPL-3.0-or-later
"""Contrat commun à tous les backends de génération.

Un backend reçoit une :class:`GenerationRequest` (des images + des paramètres)
et rend une :class:`GenerationResult` (un fichier 3D sur le disque local). Il
ne sait rien de Blender, et l'extension ne sait rien de son fonctionnement
interne : c'est ce qui permet de changer de backend sans rien casser ailleurs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


class BackendError(Exception):
    """Échec fonctionnel d'un backend (réponse invalide, erreur distante…)."""


class BackendUnavailable(BackendError):
    """Le backend n'est pas utilisable en l'état (config absente, outil manquant)."""


class BackendTimeout(BackendError):
    """Le backend n'a pas répondu dans le délai imparti."""


@dataclass(frozen=True)
class SourceImage:
    """Une image source déjà normalisée (redimensionnée, encodée)."""

    data: bytes
    name: str = "image.png"
    view: str = "AUTO"
    mime: str = "image/png"

    @property
    def extension(self) -> str:
        return {
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/webp": ".webp",
        }.get(self.mime, ".png")


@dataclass
class GenerationRequest:
    """Paramètres d'une génération, indépendants du backend."""

    images: list[SourceImage]
    prompt: str = ""
    seed: int = 0
    steps: int = 30
    guidance: float = 5.5
    octree_resolution: int = 256
    face_limit: int = 0
    remove_background: bool = True
    with_texture: bool = False
    symmetry: str = "AUTO"
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.images:
            raise ValueError("Au moins une image source est requise")

    @property
    def primary(self) -> SourceImage:
        """L'image principale : la vue de face si elle existe, sinon la première."""
        for image in self.images:
            if image.view == "FRONT":
                return image
        return self.images[0]

    @property
    def is_multiview(self) -> bool:
        return len(self.images) > 1

    def summary(self) -> str:
        views = ", ".join(image.view.lower() for image in self.images)
        return f"{len(self.images)} vue(s) [{views}], résolution {self.octree_resolution}"


@dataclass
class GenerationResult:
    """Le fichier 3D produit, toujours sur le disque local."""

    path: Path
    backend: str
    format: str = "glb"
    textured: bool = False
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.path = Path(self.path)


@dataclass
class BackendStatus:
    """Résultat d'un diagnostic ``check()``, affiché tel quel dans l'interface."""

    ok: bool
    message: str
    details: list[str] = field(default_factory=list)

    @classmethod
    def failure(cls, message: str, *details: str) -> "BackendStatus":
        return cls(False, message, list(details))

    @classmethod
    def success(cls, message: str, *details: str) -> "BackendStatus":
        return cls(True, message, list(details))


class Backend:
    """Classe de base. Les sous-classes implémentent ``check`` et ``generate``."""

    id: str = ""
    label: str = ""
    description: str = ""
    #: Formats que le backend peut rendre, par ordre de préférence.
    output_formats: tuple[str, ...] = ("glb",)

    def __init__(self, config: Mapping[str, Any] | None = None) -> None:
        self.config: dict[str, Any] = dict(config or {})

    # -- API publique ------------------------------------------------------
    def check(self) -> BackendStatus:
        """Diagnostic rapide, sans rien générer."""
        raise NotImplementedError

    def generate(self, request: GenerationRequest, ctx) -> GenerationResult:
        """Génère le maillage. ``ctx`` est un :class:`core.jobs.JobContext`."""
        raise NotImplementedError

    # -- utilitaires pour les sous-classes ---------------------------------
    def cfg(self, key: str, default: Any = None) -> Any:
        value = self.config.get(key, default)
        if isinstance(value, str):
            value = value.strip()
            if not value and default is not None:
                return default
        return value

    def require(self, key: str, human_name: str) -> str:
        value = str(self.cfg(key, "") or "")
        if not value:
            raise BackendUnavailable(
                f"{human_name} manquant : renseigne-le dans Préférences ▸ Add-ons ▸ Image → 3D."
            )
        return value


_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def safe_filename(name: str, fallback: str = "img23d") -> str:
    """Nettoie un nom pour qu'il soit utilisable comme nom de fichier."""
    cleaned = _SAFE_NAME.sub("_", name).strip("._-")
    return cleaned or fallback

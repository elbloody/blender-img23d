# SPDX-License-Identifier: GPL-3.0-or-later
"""Catalogue des backends disponibles.

C'est le seul endroit qui connaît la liste complète : l'interface, les
préférences et les opérateurs passent tous par ici. Ajouter un backend se
résume donc à écrire une classe et à l'inscrire dans :data:`_BACKENDS`.
"""

from __future__ import annotations

from typing import Any, Iterator, Mapping

from .base import Backend
from .cloud import CloudBackend
from .http_generic import HttpBackend
from .kaggle import KaggleBackend
from .local import LocalBackend

_BACKENDS: tuple[type[Backend], ...] = (
    LocalBackend,
    HttpBackend,
    KaggleBackend,
    CloudBackend,
)


def iter_backends() -> Iterator[type[Backend]]:
    yield from _BACKENDS


def get_backend_class(backend_id: str) -> type[Backend]:
    wanted = (backend_id or "").upper()
    for backend in _BACKENDS:
        if backend.id == wanted:
            return backend
    known = ", ".join(backend.id for backend in _BACKENDS)
    raise KeyError(f"Backend inconnu : {backend_id!r} (connus : {known})")


def create_backend(backend_id: str, config: Mapping[str, Any] | None = None) -> Backend:
    return get_backend_class(backend_id)(config or {})


def backend_items() -> list[tuple[str, str, str]]:
    """Items prêts à l'emploi pour une ``EnumProperty`` Blender."""
    return [(backend.id, backend.label, backend.description) for backend in _BACKENDS]

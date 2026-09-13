# SPDX-License-Identifier: GPL-3.0-or-later
"""Couche backends : tout ce qui transforme des images en fichier 3D.

Aucun module de ce paquet n'importe ``bpy``. C'est volontaire : le jour où le
serveur MCP décrit dans le README doit réutiliser ces backends, il suffit de
les importer tels quels, et ils restent testables sans Blender.
"""

from .base import (  # noqa: F401
    Backend,
    BackendError,
    BackendStatus,
    BackendTimeout,
    BackendUnavailable,
    GenerationRequest,
    GenerationResult,
    SourceImage,
)
from .registry import backend_items, create_backend, get_backend_class, iter_backends  # noqa: F401

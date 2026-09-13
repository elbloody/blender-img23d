# SPDX-License-Identifier: GPL-3.0-or-later
"""Opérateurs de l'extension."""

from __future__ import annotations

import bpy

from . import check, export, generate, images, printprep

_MODULES = (images, check, generate, printprep, export)


def register() -> None:
    for module in _MODULES:
        for cls in module.classes:
            bpy.utils.register_class(cls)


def unregister() -> None:
    for module in reversed(_MODULES):
        for cls in reversed(module.classes):
            bpy.utils.unregister_class(cls)

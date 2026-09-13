# SPDX-License-Identifier: GPL-3.0-or-later
"""Interface : panneaux de la barre latérale de la vue 3D."""

from __future__ import annotations

import bpy

from . import panels

_MODULES = (panels,)


def register() -> None:
    for module in _MODULES:
        for cls in module.classes:
            bpy.utils.register_class(cls)


def unregister() -> None:
    for module in reversed(_MODULES):
        for cls in reversed(module.classes):
            bpy.utils.unregister_class(cls)

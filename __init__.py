# SPDX-License-Identifier: GPL-3.0-or-later
"""Image → 3D : génération d'un maillage imprimable à partir d'images.

Point d'entrée de l'extension. Les métadonnées ne sont pas ici mais dans
``blender_manifest.toml`` : c'est le format des extensions Blender 4.2+, qui
remplace l'ancien dictionnaire ``bl_info``.

Organisation du code :

``backends/``  les quatre façons de générer (local, HTTP, Kaggle, cloud).
               Aucun import de ``bpy`` : testable et réutilisable hors Blender.
``core/``      le travail concret — threads, images, import, impression 3D.
``ops/``       les opérateurs Blender, c'est-à-dire les boutons.
``ui/``        les panneaux de la barre latérale.
"""

from __future__ import annotations

from . import ops, preferences, properties, ui

#: Ordre d'enregistrement : les préférences et les propriétés d'abord, car les
#: opérateurs et les panneaux s'appuient dessus dès leur premier affichage.
_MODULES = (preferences, properties, ops, ui)


def register() -> None:
    for module in _MODULES:
        module.register()


def unregister() -> None:
    for module in reversed(_MODULES):
        module.unregister()

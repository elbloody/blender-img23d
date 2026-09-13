# SPDX-License-Identifier: GPL-3.0-or-later
"""Gestion de la liste d'images sources."""

from __future__ import annotations

from pathlib import Path

import bpy
from bpy.props import BoolProperty, CollectionProperty, EnumProperty, IntProperty, StringProperty
from bpy.types import Operator, OperatorFileListElement

from ..core.imageprep import SUPPORTED

#: Ordre dans lequel on attribue les vues quand plusieurs fichiers sont ajoutés
#: d'un coup : c'est l'ordre d'un turnaround classique.
_TURNAROUND = ("FRONT", "BACK", "LEFT", "RIGHT", "TOP", "BOTTOM")


class IMG23D_OT_add_images(Operator):
    """Ajoute une ou plusieurs images à la liste des sources"""

    bl_idname = "img23d.add_images"
    bl_label = "Ajouter des images"
    bl_options = {"REGISTER", "UNDO"}

    filepath: StringProperty(subtype="FILE_PATH")
    directory: StringProperty(subtype="DIR_PATH")
    files: CollectionProperty(type=OperatorFileListElement)
    filter_image: BoolProperty(default=True, options={"HIDDEN"})
    filter_folder: BoolProperty(default=True, options={"HIDDEN"})

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        settings = context.scene.img23d
        selected = [entry.name for entry in self.files if entry.name]
        if not selected and self.filepath:
            selected = [Path(self.filepath).name]
            self.directory = self.directory or str(Path(self.filepath).parent)
        if not selected:
            self.report({"WARNING"}, "Aucune image sélectionnée")
            return {"CANCELLED"}

        added = 0
        skipped: list[str] = []
        for name in sorted(selected):
            path = Path(self.directory) / name
            if path.suffix.lower() not in SUPPORTED:
                skipped.append(name)
                continue
            item = settings.images.add()
            item.filepath = str(path)
            index = len(settings.images) - 1
            item.view = _TURNAROUND[index] if index < len(_TURNAROUND) else "AUTO"
            added += 1

        settings.active_image_index = max(len(settings.images) - 1, 0)

        if skipped:
            self.report(
                {"WARNING"},
                f"{added} image(s) ajoutée(s), {len(skipped)} ignorée(s) : {', '.join(skipped[:3])}",
            )
        else:
            self.report({"INFO"}, f"{added} image(s) ajoutée(s)")
        return {"FINISHED"}


class IMG23D_OT_remove_image(Operator):
    """Retire l'image sélectionnée de la liste"""

    bl_idname = "img23d.remove_image"
    bl_label = "Retirer l'image"
    bl_options = {"REGISTER", "UNDO"}

    index: IntProperty(default=-1)

    @classmethod
    def poll(cls, context):
        return len(context.scene.img23d.images) > 0

    def execute(self, context):
        settings = context.scene.img23d
        index = self.index if self.index >= 0 else settings.active_image_index
        if not 0 <= index < len(settings.images):
            self.report({"WARNING"}, "Aucune image à retirer")
            return {"CANCELLED"}
        settings.images.remove(index)
        settings.active_image_index = min(index, max(len(settings.images) - 1, 0))
        return {"FINISHED"}


class IMG23D_OT_clear_images(Operator):
    """Vide la liste des images sources"""

    bl_idname = "img23d.clear_images"
    bl_label = "Tout retirer"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return len(context.scene.img23d.images) > 0

    def execute(self, context):
        count = len(context.scene.img23d.images)
        context.scene.img23d.images.clear()
        context.scene.img23d.active_image_index = 0
        self.report({"INFO"}, f"{count} image(s) retirée(s)")
        return {"FINISHED"}


class IMG23D_OT_move_image(Operator):
    """Déplace l'image dans la liste"""

    bl_idname = "img23d.move_image"
    bl_label = "Déplacer l'image"
    bl_options = {"REGISTER", "UNDO"}

    direction: EnumProperty(
        items=[("UP", "Haut", "Monter"), ("DOWN", "Bas", "Descendre")],
        default="UP",
    )

    @classmethod
    def poll(cls, context):
        return len(context.scene.img23d.images) > 1

    def execute(self, context):
        settings = context.scene.img23d
        index = settings.active_image_index
        target = index - 1 if self.direction == "UP" else index + 1
        if not 0 <= target < len(settings.images):
            return {"CANCELLED"}
        settings.images.move(index, target)
        settings.active_image_index = target
        return {"FINISHED"}


class IMG23D_UL_images(bpy.types.UIList):
    """Liste des images sources, avec leur vue associée."""

    def draw_item(self, context, layout, data, item, icon, active_data, active_prop, index):
        if self.layout_type in {"DEFAULT", "COMPACT"}:
            row = layout.row(align=True)
            row.prop(item, "enabled", text="")
            name = Path(item.filepath).name if item.filepath else "(aucun fichier)"
            sub = row.row()
            sub.enabled = item.enabled
            sub.label(text=name, icon="IMAGE_DATA")
            sub.prop(item, "view", text="")
        else:
            layout.label(text="", icon="IMAGE_DATA")


classes = (
    IMG23D_OT_add_images,
    IMG23D_OT_remove_image,
    IMG23D_OT_clear_images,
    IMG23D_OT_move_image,
    IMG23D_UL_images,
)

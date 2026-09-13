# SPDX-License-Identifier: GPL-3.0-or-later
"""Export du maillage préparé vers un fichier prêt pour le slicer."""

from __future__ import annotations

from pathlib import Path

import bpy
from bpy.props import BoolProperty, StringProperty
from bpy.types import Operator

from ..core import printprep as prep
from ..utils import resolve_export_path, unique_path


class IMG23D_OT_export(Operator):
    """Exporte les objets sélectionnés vers un fichier imprimable"""

    bl_idname = "img23d.export"
    bl_label = "Exporter"
    bl_options = {"REGISTER"}

    overwrite: BoolProperty(
        name="Écraser",
        description="Écrase le fichier existant au lieu de numéroter le nouveau",
        default=False,
    )
    filepath_override: StringProperty(
        name="Chemin",
        description="Chemin complet à utiliser. Vide = celui du panneau Export",
        subtype="FILE_PATH",
        default="",
        options={"HIDDEN"},
    )

    @classmethod
    def poll(cls, context):
        if not any(obj.type == "MESH" for obj in context.selected_objects):
            cls.poll_message_set("Sélectionne au moins un objet maillage")
            return False
        return True

    def execute(self, context):
        settings = context.scene.img23d
        objects = [obj for obj in context.selected_objects if obj.type == "MESH"]

        extension = ".stl" if settings.export_format == "STL" else ".obj"
        if self.filepath_override:
            path = Path(bpy.path.abspath(self.filepath_override)).with_suffix(extension)
        else:
            path = resolve_export_path(settings.export_path, settings.export_name, extension)

        if not self.overwrite:
            path = unique_path(path)

        # Un maillage non étanche passera le slicer en mode « réparation » avec
        # des résultats imprévisibles : autant prévenir maintenant.
        report = prep.analyze(objects[0], unit_scale=1000.0)

        try:
            if settings.export_format == "STL":
                prep.export_stl(
                    context,
                    objects,
                    path,
                    global_scale=settings.export_scale,
                    ascii_format=settings.export_ascii,
                )
            else:
                prep.export_obj(context, objects, path, global_scale=settings.export_scale)
        except (RuntimeError, OSError) as exc:
            self.report({"ERROR"}, f"Export impossible : {exc}")
            return {"CANCELLED"}

        unit = {"MM": "mm", "CM": "cm", "M": "m"}[settings.export_unit]
        message = f"{path.name} écrit ({len(objects)} objet(s), unité : {unit})"
        if not report.watertight:
            message += f" — attention : {report.headline()}"
            self.report({"WARNING"}, message)
        else:
            self.report({"INFO"}, message)

        print(f"[img23d] Export : {path}")
        return {"FINISHED"}


class IMG23D_OT_export_as(Operator):
    """Exporte via le sélecteur de fichiers"""

    bl_idname = "img23d.export_as"
    bl_label = "Exporter sous…"
    bl_options = {"REGISTER"}

    filepath: StringProperty(subtype="FILE_PATH")
    filter_glob: StringProperty(default="*.stl;*.obj", options={"HIDDEN"})

    @classmethod
    def poll(cls, context):
        return IMG23D_OT_export.poll(context)

    def invoke(self, context, event):
        settings = context.scene.img23d
        extension = ".stl" if settings.export_format == "STL" else ".obj"
        self.filepath = str(
            resolve_export_path(settings.export_path, settings.export_name, extension)
        )
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        if not self.filepath:
            self.report({"WARNING"}, "Aucun chemin choisi")
            return {"CANCELLED"}
        return bpy.ops.img23d.export(filepath_override=self.filepath, overwrite=True)


classes = (
    IMG23D_OT_export,
    IMG23D_OT_export_as,
)

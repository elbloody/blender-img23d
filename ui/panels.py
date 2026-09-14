# SPDX-License-Identifier: GPL-3.0-or-later
"""Panneaux de l'onglet « Image → 3D » dans la barre latérale (touche N).

L'ordre suit le déroulé réel du travail : sources, génération, import,
préparation à l'impression, export.
"""

from __future__ import annotations

from pathlib import Path

from bpy.types import Panel

from ..backends import get_backend_class
from ..core import printprep as prep
from ..ops._job_modal import is_busy
from ..preferences import resolve_backend_id

CATEGORY = "Image → 3D"


class Img23DPanel:
    """Réglages communs à tous les panneaux de l'extension."""

    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = CATEGORY


class IMG23D_PT_source(Img23DPanel, Panel):
    bl_idname = "IMG23D_PT_source"
    bl_label = "Images sources"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.img23d

        row = layout.row()
        row.template_list(
            "IMG23D_UL_images", "", settings, "images", settings, "active_image_index", rows=3
        )

        column = row.column(align=True)
        column.operator("img23d.add_images", icon="ADD", text="")
        column.operator("img23d.remove_image", icon="REMOVE", text="")
        column.separator()
        column.operator("img23d.move_image", icon="TRIA_UP", text="").direction = "UP"
        column.operator("img23d.move_image", icon="TRIA_DOWN", text="").direction = "DOWN"
        column.separator()
        column.operator("img23d.clear_images", icon="TRASH", text="")

        active = _active_image(settings)
        if active is not None:
            box = layout.box()
            box.use_property_split = True
            box.use_property_decorate = False
            box.prop(active, "filepath", text="Fichier")
            box.prop(active, "view")
            path = Path(active.filepath) if active.filepath else None
            if path is not None and not path.is_file():
                box.label(text="Fichier introuvable", icon="ERROR")

        column = layout.column(align=True)
        column.use_property_split = True
        column.use_property_decorate = False
        column.prop(settings, "max_image_size")
        column.prop(settings, "image_format")

        enabled = settings.enabled_images()
        if not enabled:
            layout.label(text="Ajoute au moins une image", icon="INFO")
        elif len(enabled) > 1:
            layout.label(text=f"{len(enabled)} vues : mode multi-vues", icon="CHECKMARK")


class IMG23D_PT_generate(Img23DPanel, Panel):
    bl_idname = "IMG23D_PT_generate"
    bl_label = "Génération"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.img23d
        state = context.window_manager.img23d_state

        column = layout.column(align=True)
        column.use_property_split = True
        column.use_property_decorate = False
        column.prop(settings, "backend")

        backend_id = resolve_backend_id(context)
        try:
            backend_label = get_backend_class(backend_id).label
        except KeyError:
            backend_label = backend_id
        row = column.row()
        row.enabled = False
        row.label(text=f"Actif : {backend_label}", icon="PLUGIN")

        row = layout.row(align=True)
        row.operator("img23d.check_backend", icon="CHECKMARK")
        row.operator("preferences.addon_show", icon="PREFERENCES", text="").module = __package__.rpartition(".")[0]

        if state.check_result:
            box = layout.box()
            box.scale_y = 0.8
            icon = "CHECKMARK" if state.check_ok else "ERROR"
            for index, line in enumerate(state.check_result.split(" · ")):
                box.label(text=line, icon=icon if index == 0 else "BLANK1")

        layout.separator()

        column = layout.column(align=True)
        column.use_property_split = True
        column.use_property_decorate = False
        column.prop(settings, "octree_resolution")
        column.prop(settings, "steps")
        column.prop(settings, "guidance")
        column.prop(settings, "seed")
        column.prop(settings, "face_limit")
        column.prop(settings, "symmetry")

        column = layout.column(align=True)
        column.prop(settings, "remove_background")
        column.prop(settings, "with_texture")
        if settings.with_texture:
            note = column.column()
            note.scale_y = 0.8
            note.label(text="La texture ne s'imprime pas : pour une pièce", icon="INFO")
            note.label(text="destinée au slicer, ce réglage est inutile.")

        layout.separator()

        if is_busy(context):
            box = layout.box()
            box.prop(state, "progress", text=state.status or "Travail en cours…", slider=True)
            row = box.row()
            row.scale_y = 1.2
            row.operator("img23d.cancel", icon="CANCEL")
            hint = box.column()
            hint.scale_y = 0.8
            hint.label(text="Échap dans la vue 3D annule aussi.", icon="INFO")
        else:
            row = layout.row()
            row.scale_y = 1.5
            row.operator("img23d.generate", icon="SHADERFX")

            if state.last_error:
                box = layout.box()
                box.scale_y = 0.8
                box.label(text=state.last_error[:120], icon="ERROR")
            elif state.status:
                row = layout.row()
                row.enabled = False
                row.label(text=state.status)

            if state.last_result:
                layout.operator("img23d.import_last", icon="IMPORT")


class IMG23D_PT_import(Img23DPanel, Panel):
    bl_idname = "IMG23D_PT_import"
    bl_parent_id = "IMG23D_PT_generate"
    bl_label = "Import"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        settings = context.scene.img23d

        layout.prop(settings, "upright_mode")
        layout.prop(settings, "auto_scale")
        row = layout.row()
        row.enabled = settings.auto_scale
        row.prop(settings, "target_size_mm", text="Taille cible (mm)")
        layout.prop(settings, "auto_prepare")


class IMG23D_PT_print(Img23DPanel, Panel):
    bl_idname = "IMG23D_PT_print"
    bl_label = "Impression 3D"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.img23d
        state = context.window_manager.img23d_state

        row = layout.row(align=True)
        row.operator("img23d.analyze", icon="VIEWZOOM")
        row.operator("img23d.scale_to_size", icon="FULLSCREEN_ENTER", text="Taille cible")

        if state.mesh_report:
            box = layout.box()
            box.scale_y = 0.9
            box.label(
                text=state.mesh_report,
                icon="CHECKMARK" if state.mesh_watertight else "ERROR",
            )

        layout.separator()

        column = layout.column(align=True)
        column.use_property_split = True
        column.use_property_decorate = False

        column.prop(settings, "use_remesh")
        sub = column.column(align=True)
        sub.enabled = settings.use_remesh
        sub.prop(settings, "voxel_size_mm", text="Voxel (mm)")
        sub.prop(settings, "remesh_adaptivity")

        # Le coût du remesh se voit avant de cliquer, pas après trois minutes
        # de gel. L'estimation vient de la boîte englobante : rien de coûteux
        # n'a sa place dans un draw().
        if settings.use_remesh:
            resolution = prep.remesh_resolution(context.active_object, settings.voxel_size_mm)
            if resolution > 0.0:
                ligne = column.column(align=True)
                ligne.scale_y = 0.8
                if resolution > prep.MAX_REMESH_RESOLUTION:
                    ligne.label(text=f"{resolution:.0f} voxels par côté : trop fin", icon="ERROR")
                    ligne.label(text="Mets le modèle à sa taille cible d'abord.", icon="BLANK1")
                else:
                    ligne.label(text=f"≈ {resolution:.0f} voxels par côté", icon="INFO")

        column.separator()
        column.prop(settings, "use_repair")
        sub = column.column(align=True)
        sub.enabled = settings.use_repair
        sub.prop(settings, "merge_distance_mm", text="Fusion (mm)")
        sub.prop(settings, "fill_holes")
        sub.prop(settings, "triangulate")

        column.separator()
        column.prop(settings, "use_decimate")
        sub = column.column(align=True)
        sub.enabled = settings.use_decimate
        sub.prop(settings, "target_triangles")

        layout.separator()
        row = layout.row()
        row.scale_y = 1.3
        row.operator("img23d.prepare_print", icon="MOD_REMESH")


class IMG23D_PT_export(Img23DPanel, Panel):
    bl_idname = "IMG23D_PT_export"
    bl_label = "Export"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.img23d

        column = layout.column(align=True)
        column.use_property_split = True
        column.use_property_decorate = False
        column.prop(settings, "export_format")
        column.prop(settings, "export_unit")
        if settings.export_format == "STL":
            column.prop(settings, "export_ascii")
        column.prop(settings, "export_path", text="Dossier")
        column.prop(settings, "export_name", text="Nom")

        row = layout.row(align=True)
        row.scale_y = 1.3
        row.operator("img23d.export", icon="EXPORT")
        row.operator("img23d.export_as", icon="FILEBROWSER", text="")


classes = (
    IMG23D_PT_source,
    IMG23D_PT_generate,
    IMG23D_PT_import,
    IMG23D_PT_print,
    IMG23D_PT_export,
)


def _active_image(settings):
    index = settings.active_image_index
    if 0 <= index < len(settings.images):
        return settings.images[index]
    return None

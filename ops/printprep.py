# SPDX-License-Identifier: GPL-3.0-or-later
"""Opérateurs de préparation à l'impression : analyse et nettoyage."""

from __future__ import annotations

from bpy.types import Operator

from ..core import printprep as prep
from ..utils import tag_redraw


def _target(context):
    """L'objet à traiter : l'objet actif, s'il s'agit bien d'un maillage."""
    obj = context.active_object
    return obj if obj is not None and obj.type == "MESH" else None


class IMG23D_OT_analyze(Operator):
    """Analyse l'étanchéité du maillage actif sans le modifier"""

    bl_idname = "img23d.analyze"
    bl_label = "Analyser le maillage"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        if _target(context) is None:
            cls.poll_message_set("Sélectionne un objet maillage")
            return False
        return True

    def execute(self, context):
        report = prep.analyze(_target(context), unit_scale=1000.0)
        state = context.window_manager.img23d_state
        state.mesh_report = report.headline()
        state.mesh_watertight = report.watertight

        dimensions = " × ".join(f"{value:.1f}" for value in report.dimensions_mm)
        summary = (
            f"{report.triangles} triangles, {dimensions} mm, "
            f"{report.volume_mm3 / 1000.0:.1f} cm³ — {report.headline()}"
        )
        print(f"[img23d] {summary}")
        self.report({"INFO"} if report.watertight else {"WARNING"}, summary)
        tag_redraw(context)
        return {"FINISHED"}


class IMG23D_OT_prepare_print(Operator):
    """Remesh, répare et décime le maillage actif pour l'impression 3D"""

    bl_idname = "img23d.prepare_print"
    bl_label = "Préparer pour l'impression"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        if _target(context) is None:
            cls.poll_message_set("Sélectionne un objet maillage")
            return False
        if context.mode != "OBJECT":
            cls.poll_message_set("Repasse en mode Objet")
            return False
        return True

    def execute(self, context):
        from .generate import _prep_options

        obj = _target(context)
        settings = context.scene.img23d
        options = _prep_options(settings)

        before = prep.analyze(obj, unit_scale=1000.0)
        try:
            steps = prep.prepare(context, obj, options)
        except (ValueError, RuntimeError) as exc:
            self.report({"ERROR"}, f"Préparation impossible : {exc}")
            return {"CANCELLED"}
        context.view_layer.update()
        after = prep.analyze(obj, unit_scale=1000.0)

        state = context.window_manager.img23d_state
        state.mesh_report = after.headline()
        state.mesh_watertight = after.watertight

        for step in steps:
            print(f"[img23d] {step}")

        message = (
            f"{before.triangles} → {after.triangles} triangles · {after.headline()}"
            if steps
            else "Aucune étape activée dans le panneau « Impression 3D »"
        )
        self.report({"INFO"} if after.watertight else {"WARNING"}, message)
        tag_redraw(context)
        return {"FINISHED"}


class IMG23D_OT_scale_to_size(Operator):
    """Met le maillage actif à la taille cible définie dans le panneau"""

    bl_idname = "img23d.scale_to_size"
    bl_label = "Mettre à la taille cible"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        if _target(context) is None:
            cls.poll_message_set("Sélectionne un objet maillage")
            return False
        return True

    def execute(self, context):
        from ..core import scene as scene_utils

        obj = _target(context)
        settings = context.scene.img23d
        factor = scene_utils.scale_to_height(obj, settings.target_size_mm / 1000.0)
        context.view_layer.update()
        self.report({"INFO"}, f"Échelle × {factor:.4g} → {settings.target_size_mm:g} mm")
        tag_redraw(context)
        return {"FINISHED"}


classes = (
    IMG23D_OT_analyze,
    IMG23D_OT_prepare_print,
    IMG23D_OT_scale_to_size,
)

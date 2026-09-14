# SPDX-License-Identifier: GPL-3.0-or-later
"""Opérateur principal : des images vers un maillage dans la scène.

Découpage volontaire du travail :

* **avant** le thread — tout ce qui touche à ``bpy`` (lecture et réencodage des
  images, lecture des préférences) se fait ici, sur le thread principal ;
* **dans** le thread — uniquement ``backend.generate()``, qui ne connaît que
  des octets et des dataclasses ;
* **après** le thread — l'import dans la scène, de nouveau sur le thread
  principal, dans :meth:`on_success`.
"""

from __future__ import annotations

from pathlib import Path

from bpy.types import Operator

from ..backends import GenerationRequest, create_backend
from ..core import printprep as prep
from ..core import scene as scene_utils
from ..core.imageprep import ImagePrepError, prepare_from_paths
from ..preferences import get_preferences, resolve_backend_id
from ..utils import human_duration, tag_redraw
from ._job_modal import JobModalMixin, cancel_active_job, is_busy


class IMG23D_OT_generate(JobModalMixin, Operator):
    """Génère un maillage 3D à partir des images sources"""

    bl_idname = "img23d.generate"
    bl_label = "Générer le modèle 3D"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        if is_busy(context):
            cls.poll_message_set("Une génération est déjà en cours")
            return False
        if not context.scene.img23d.enabled_images():
            cls.poll_message_set("Ajoute au moins une image source")
            return False
        return True

    def invoke(self, context, event):
        return self.execute(context)

    def execute(self, context):
        settings = context.scene.img23d
        preferences = get_preferences(context)
        backend_id = resolve_backend_id(context)

        # 1. Préparation des images — impérativement sur le thread principal.
        try:
            images = prepare_from_paths(
                settings.enabled_images(),
                max_size=settings.max_image_size,
                file_format=settings.image_format,
            )
        except ImagePrepError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        request = GenerationRequest(
            images=images,
            prompt=settings.prompt,
            seed=settings.seed,
            steps=settings.steps,
            guidance=settings.guidance,
            octree_resolution=int(settings.octree_resolution),
            face_limit=settings.face_limit,
            remove_background=settings.remove_background,
            with_texture=settings.with_texture,
            symmetry=settings.symmetry,
        )

        try:
            backend = create_backend(backend_id, preferences.backend_config(backend_id))
        except KeyError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        state = context.window_manager.img23d_state
        state.last_backend = backend.label
        state.status = f"Envoi vers « {backend.label} »…"

        # 2. Le thread ne voit que des octets : aucun accès à bpy.
        def run(ctx):
            ctx.report(f"Backend « {backend.label} » — {request.summary()}", 0.02)
            return backend.generate(request, ctx)

        return self.start_job(context, run, name=f"img23d-{backend_id.lower()}")

    # 3. Retour sur le thread principal : import dans la scène.
    def on_success(self, context, result, elapsed):
        settings = context.scene.img23d
        state = context.window_manager.img23d_state
        state.last_result = str(result.path)

        objects = scene_utils.import_mesh(result.path, name="img23d_result")
        obj = scene_utils.join_objects(objects, context)

        upright = scene_utils.needs_upright(result.path, settings.upright_mode)
        scene_utils.normalize_transform(obj, upright=upright)

        if settings.auto_scale:
            scene_utils.scale_to_height(obj, settings.target_size_mm / 1000.0)

        scene_utils.select_only(context, [obj])
        context.view_layer.update()

        steps: list[str] = []
        if settings.auto_prepare:
            steps = prep.prepare(context, obj, _prep_options(settings))

        report = prep.analyze(obj, unit_scale=1000.0)
        state.mesh_report = report.headline()
        state.mesh_watertight = report.watertight

        suffix = f" ({', '.join(steps)})" if steps else ""
        self.report(
            {"INFO"},
            f"Modèle importé depuis {result.backend} en {human_duration(elapsed)} — "
            f"{report.triangles} triangles{suffix}",
        )
        tag_redraw(context, ("VIEW_3D", "OUTLINER", "PROPERTIES"))
        return {"FINISHED"}

    def on_failure(self, context, error):
        context.window_manager.img23d_state.mesh_report = ""


class IMG23D_OT_cancel(Operator):
    """Demande l'arrêt de la génération en cours"""

    bl_idname = "img23d.cancel"
    bl_label = "Annuler"
    bl_options = {"INTERNAL"}

    @classmethod
    def poll(cls, context):
        return is_busy(context)

    def execute(self, context):
        if not cancel_active_job():
            self.report({"WARNING"}, "Aucun travail en cours")
            return {"CANCELLED"}
        context.window_manager.img23d_state.status = "Annulation demandée…"
        tag_redraw(context)
        return {"FINISHED"}


class IMG23D_OT_import_last(Operator):
    """Réimporte le dernier fichier généré, sans relancer de génération"""

    bl_idname = "img23d.import_last"
    bl_label = "Réimporter le dernier résultat"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        last = context.window_manager.img23d_state.last_result
        if not last:
            cls.poll_message_set("Aucune génération dans cette session")
            return False
        if not Path(last).is_file():
            cls.poll_message_set("Le fichier temporaire n'existe plus")
            return False
        return True

    def execute(self, context):
        settings = context.scene.img23d
        path = Path(context.window_manager.img23d_state.last_result)

        try:
            objects = scene_utils.import_mesh(path, name="img23d_result")
        except scene_utils.MeshImportError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        obj = scene_utils.join_objects(objects, context)
        scene_utils.normalize_transform(
            obj, upright=scene_utils.needs_upright(path, settings.upright_mode)
        )
        if settings.auto_scale:
            scene_utils.scale_to_height(obj, settings.target_size_mm / 1000.0)
        scene_utils.select_only(context, [obj])
        context.view_layer.update()

        self.report({"INFO"}, f"{path.name} réimporté")
        return {"FINISHED"}


def _prep_options(settings) -> prep.PrepOptions:
    """Traduit les propriétés de scène en options de préparation."""
    return prep.PrepOptions(
        remesh=settings.use_remesh,
        voxel_size_mm=settings.voxel_size_mm,
        remesh_adaptivity=settings.remesh_adaptivity,
        repair=settings.use_repair,
        merge_distance_mm=settings.merge_distance_mm,
        fill_holes=settings.fill_holes,
        recalculate_normals=True,
        triangulate=settings.triangulate,
        decimate=settings.use_decimate,
        target_triangles=settings.target_triangles,
    )


classes = (
    IMG23D_OT_generate,
    IMG23D_OT_cancel,
    IMG23D_OT_import_last,
)

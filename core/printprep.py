# SPDX-License-Identifier: GPL-3.0-or-later
"""Préparation à l'impression 3D : analyse, remesh, réparation, export.

Une texture ne s'imprime pas : ce qui compte ici, c'est la *forme*. Un
maillage imprimable doit être étanche (chaque arête bordée par exactement deux
faces), sans géométrie dégénérée, et à l'échelle réelle.

Tout passe par ``bmesh`` et les modificateurs plutôt que par ``bpy.ops.mesh.*``
en mode édition : pas de dépendance à un contexte d'interface, donc un
comportement identique en arrière-plan (``blender --background``) et dans l'UI.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import bmesh
import bpy


@dataclass
class MeshReport:
    """Photographie de l'état d'un maillage, telle qu'affichée dans le panneau."""

    vertices: int = 0
    triangles: int = 0
    polygons: int = 0
    boundary_edges: int = 0
    non_manifold_edges: int = 0
    loose_vertices: int = 0
    interior_faces: int = 0
    volume_mm3: float = 0.0
    dimensions_mm: tuple[float, float, float] = (0.0, 0.0, 0.0)
    inverted: bool = False

    @property
    def watertight(self) -> bool:
        return (
            self.boundary_edges == 0
            and self.non_manifold_edges == 0
            and self.loose_vertices == 0
            and self.triangles > 0
        )

    @property
    def issues(self) -> list[str]:
        problems: list[str] = []
        if self.boundary_edges:
            problems.append(f"{self.boundary_edges} arêtes de bord (trous)")
        if self.non_manifold_edges:
            problems.append(f"{self.non_manifold_edges} arêtes non-manifold")
        if self.loose_vertices:
            problems.append(f"{self.loose_vertices} sommets isolés")
        if self.interior_faces:
            problems.append(f"{self.interior_faces} faces internes")
        if self.inverted:
            problems.append("volume négatif : normales retournées")
        return problems

    def headline(self) -> str:
        if self.triangles == 0:
            return "Maillage vide"
        if self.watertight and not self.inverted:
            return "Étanche — prêt pour le slicer"
        return "Non étanche : " + ", ".join(self.issues)


@dataclass
class PrepOptions:
    """Réglages de la passe de préparation, remplis depuis le panneau."""

    remesh: bool = True
    voxel_size_mm: float = 0.8
    remesh_adaptivity: float = 0.0
    repair: bool = True
    merge_distance_mm: float = 0.02
    fill_holes: bool = True
    max_hole_sides: int = 0
    recalculate_normals: bool = True
    triangulate: bool = True
    decimate: bool = False
    target_triangles: int = 100_000
    steps: list[str] = field(default_factory=list)


def analyze(obj: bpy.types.Object, unit_scale: float = 1.0) -> MeshReport:
    """Analyse le maillage sans le modifier. ``unit_scale`` : mètres → millimètres."""
    if obj is None or obj.type != "MESH" or obj.data is None:
        return MeshReport()

    bm = bmesh.new()
    try:
        bm.from_mesh(obj.data)
        bm.transform(obj.matrix_world)

        boundary = 0
        non_manifold = 0
        interior = 0
        for edge in bm.edges:
            linked = len(edge.link_faces)
            if linked == 0 or linked > 2:
                non_manifold += 1
                if linked > 2:
                    interior += 1
            elif linked == 1:
                boundary += 1

        loose = sum(1 for vertex in bm.verts if not vertex.link_edges)
        triangles = sum(max(len(face.verts) - 2, 0) for face in bm.faces)
        volume = bm.calc_volume(signed=True)

        # Les dimensions sont mesurées sur la géométrie, pas lues dans
        # ``obj.dimensions`` : ce cache n'est rafraîchi qu'à la prochaine
        # évaluation du depsgraph, donc périmé juste après un remesh.
        coords = [vertex.co for vertex in bm.verts]
        if coords:
            dims = tuple(
                round((max(co[axis] for co in coords) - min(co[axis] for co in coords)) * unit_scale, 3)
                for axis in range(3)
            )
        else:
            dims = (0.0, 0.0, 0.0)

        return MeshReport(
            vertices=len(bm.verts),
            triangles=triangles,
            polygons=len(bm.faces),
            boundary_edges=boundary,
            non_manifold_edges=non_manifold,
            loose_vertices=loose,
            interior_faces=interior,
            volume_mm3=abs(volume) * (unit_scale**3),
            dimensions_mm=dims,
            inverted=volume < 0.0,
        )
    finally:
        bm.free()


def prepare(
    context: bpy.types.Context,
    obj: bpy.types.Object,
    options: PrepOptions,
    unit_scale: float = 1000.0,
) -> list[str]:
    """Applique la chaîne de préparation et rend la liste des étapes réalisées."""
    if obj is None or obj.type != "MESH":
        raise ValueError("Aucun objet maillage à préparer")

    steps: list[str] = []
    metres_per_mm = 1.0 / unit_scale

    if options.remesh and options.voxel_size_mm > 0.0:
        voxel = options.voxel_size_mm * metres_per_mm
        _apply_voxel_remesh(context, obj, voxel, options.remesh_adaptivity)
        steps.append(f"Remesh voxel à {options.voxel_size_mm:g} mm")

    if options.repair:
        merged, filled, flipped, removed = _repair(
            obj,
            merge_distance=options.merge_distance_mm * metres_per_mm,
            fill_holes=options.fill_holes,
            max_hole_sides=options.max_hole_sides,
            recalculate_normals=options.recalculate_normals,
            triangulate=options.triangulate,
        )
        if merged:
            steps.append(f"{merged} sommets fusionnés")
        if removed:
            steps.append(f"{removed} éléments isolés supprimés")
        if filled:
            steps.append(f"{filled} trous comblés")
        if flipped:
            steps.append("Normales recalculées vers l'extérieur")
        if options.triangulate:
            steps.append("Maillage triangulé")

    if options.decimate and options.target_triangles > 0:
        current = sum(max(len(p.vertices) - 2, 0) for p in obj.data.polygons)
        if current > options.target_triangles:
            ratio = options.target_triangles / float(current)
            _apply_decimate(context, obj, ratio)
            steps.append(f"Décimation {current} → ~{options.target_triangles} triangles")

    return steps


# ---------------------------------------------------------------------------
# Modificateurs
# ---------------------------------------------------------------------------
def _bake_modifiers(context: bpy.types.Context, obj: bpy.types.Object) -> None:
    """Applique tous les modificateurs sans passer par ``modifier_apply``.

    ``bpy.ops.object.modifier_apply`` exige un contexte précis (objet actif,
    mode objet, objet non masqué). Passer par le depsgraph évite tout ça.
    """
    depsgraph = context.evaluated_depsgraph_get()
    evaluated = obj.evaluated_get(depsgraph)
    baked = bpy.data.meshes.new_from_object(
        evaluated, preserve_all_data_layers=True, depsgraph=depsgraph
    )
    baked.name = obj.data.name

    previous = obj.data
    obj.data = baked
    obj.modifiers.clear()
    if previous.users == 0:
        bpy.data.meshes.remove(previous)


def _apply_voxel_remesh(
    context: bpy.types.Context,
    obj: bpy.types.Object,
    voxel_size: float,
    adaptivity: float,
) -> None:
    modifier = obj.modifiers.new(name="img23d_remesh", type="REMESH")
    modifier.mode = "VOXEL"
    modifier.voxel_size = max(voxel_size, 1e-6)
    modifier.adaptivity = max(adaptivity, 0.0)
    modifier.use_smooth_shade = False
    _bake_modifiers(context, obj)


def _apply_decimate(context: bpy.types.Context, obj: bpy.types.Object, ratio: float) -> None:
    modifier = obj.modifiers.new(name="img23d_decimate", type="DECIMATE")
    modifier.decimate_type = "COLLAPSE"
    modifier.ratio = min(max(ratio, 0.0001), 1.0)
    modifier.use_collapse_triangulate = True
    _bake_modifiers(context, obj)


# ---------------------------------------------------------------------------
# Réparation bmesh
# ---------------------------------------------------------------------------
def _repair(
    obj: bpy.types.Object,
    merge_distance: float,
    fill_holes: bool,
    max_hole_sides: int,
    recalculate_normals: bool,
    triangulate: bool,
) -> tuple[int, int, bool, int]:
    """Rend ``(sommets fusionnés, trous comblés, normales corrigées, éléments supprimés)``."""
    bm = bmesh.new()
    try:
        bm.from_mesh(obj.data)

        before_verts = len(bm.verts)
        if merge_distance > 0.0:
            bmesh.ops.remove_doubles(bm, verts=list(bm.verts), dist=merge_distance)
        merged = before_verts - len(bm.verts)

        bmesh.ops.dissolve_degenerate(bm, dist=max(merge_distance, 1e-7), edges=list(bm.edges))

        # Deux passes : supprimer les arêtes orphelines isole de nouveaux sommets.
        before_loose = len(bm.verts) + len(bm.edges)
        loose_edges = [edge for edge in bm.edges if not edge.link_faces]
        if loose_edges:
            bmesh.ops.delete(bm, geom=loose_edges, context="EDGES")
        loose_verts = [vertex for vertex in bm.verts if not vertex.link_faces]
        if loose_verts:
            bmesh.ops.delete(bm, geom=loose_verts, context="VERTS")
        removed = before_loose - (len(bm.verts) + len(bm.edges))

        filled = 0
        if fill_holes:
            boundary = [edge for edge in bm.edges if len(edge.link_faces) == 1]
            if boundary:
                before_faces = len(bm.faces)
                bmesh.ops.holes_fill(bm, edges=boundary, sides=max(max_hole_sides, 0))
                filled = len(bm.faces) - before_faces

        if triangulate and bm.faces:
            bmesh.ops.triangulate(bm, faces=list(bm.faces))

        flipped = False
        if recalculate_normals and bm.faces:
            bmesh.ops.recalc_face_normals(bm, faces=list(bm.faces))
            if bm.calc_volume(signed=True) < 0.0:
                # Volume négatif : le maillage est « retourné », on le remet à l'endroit.
                bmesh.ops.reverse_faces(bm, faces=list(bm.faces))
            flipped = True

        bm.to_mesh(obj.data)
        obj.data.update()
        return merged, filled, flipped, removed
    finally:
        bm.free()


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
def export_stl(
    context: bpy.types.Context,
    objects: list[bpy.types.Object],
    filepath: Path,
    global_scale: float = 1000.0,
    ascii_format: bool = False,
) -> Path:
    """Exporte ``objects`` en STL.

    ``global_scale=1000`` écrit un STL en millimètres à partir d'une scène en
    mètres : c'est ce qu'attendent Cura, PrusaSlicer et Bambu Studio.
    """
    filepath = Path(filepath)
    filepath.parent.mkdir(parents=True, exist_ok=True)

    with _selection(context, objects):
        if hasattr(bpy.ops.wm, "stl_export"):  # Blender 4.2+
            bpy.ops.wm.stl_export(
                filepath=str(filepath),
                export_selected_objects=True,
                apply_modifiers=True,
                global_scale=global_scale,
                ascii_format=ascii_format,
            )
        else:  # pragma: no cover - Blender < 4.2, hors périmètre du manifeste
            bpy.ops.export_mesh.stl(
                filepath=str(filepath),
                use_selection=True,
                use_mesh_modifiers=True,
                global_scale=global_scale,
                ascii=ascii_format,
            )
    return filepath


def export_obj(
    context: bpy.types.Context,
    objects: list[bpy.types.Object],
    filepath: Path,
    global_scale: float = 1000.0,
    export_materials: bool = True,
) -> Path:
    filepath = Path(filepath)
    filepath.parent.mkdir(parents=True, exist_ok=True)

    with _selection(context, objects):
        bpy.ops.wm.obj_export(
            filepath=str(filepath),
            export_selected_objects=True,
            apply_modifiers=True,
            global_scale=global_scale,
            export_materials=export_materials,
        )
    return filepath


class _selection:
    """Sélectionne temporairement ``objects``, puis restaure l'état précédent."""

    def __init__(self, context: bpy.types.Context, objects: list[bpy.types.Object]) -> None:
        self.context = context
        self.objects = [obj for obj in objects if obj is not None]
        self._previous: list[bpy.types.Object] = []
        self._active: bpy.types.Object | None = None

    def __enter__(self):
        view_layer = self.context.view_layer
        self._previous = [obj for obj in view_layer.objects if obj.select_get()]
        self._active = view_layer.objects.active
        for obj in view_layer.objects:
            obj.select_set(False)
        for obj in self.objects:
            obj.select_set(True)
        if self.objects:
            view_layer.objects.active = self.objects[0]
        return self

    def __exit__(self, *exc_info):
        view_layer = self.context.view_layer
        for obj in view_layer.objects:
            obj.select_set(False)
        for obj in self._previous:
            try:
                obj.select_set(True)
            except ReferenceError:  # objet supprimé entre-temps
                continue
        view_layer.objects.active = self._active
        return False

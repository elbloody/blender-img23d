# SPDX-License-Identifier: GPL-3.0-or-later
"""Import du maillage généré dans la scène Blender.

Tout ce qui touche à ``bpy.data`` doit tourner sur le thread principal : ces
fonctions sont donc appelées depuis l'opérateur modal, jamais depuis le thread
de génération.
"""

from __future__ import annotations

import math
from pathlib import Path

import bpy
import mathutils

#: Importeurs disponibles selon l'extension du fichier rendu par le backend.
_IMPORTERS = {
    ".glb": "gltf",
    ".gltf": "gltf",
    ".obj": "obj",
    ".ply": "ply",
    ".stl": "stl",
    ".fbx": "fbx",
}


class MeshImportError(Exception):
    """Le fichier rendu par le backend n'a pas pu être importé dans la scène."""


def import_mesh(filepath: Path, name: str = "img23d_result") -> list[bpy.types.Object]:
    """Importe ``filepath`` et rend les objets maillage créés."""
    filepath = Path(filepath)
    if not filepath.is_file():
        raise MeshImportError(f"Fichier introuvable : {filepath}")

    kind = _IMPORTERS.get(filepath.suffix.lower())
    if kind is None:
        raise MeshImportError(
            f"Format non géré à l'import : {filepath.suffix}. "
            f"Formats acceptés : {', '.join(sorted(_IMPORTERS))}"
        )

    before = set(bpy.data.objects)
    try:
        _run_importer(kind, filepath)
    except RuntimeError as exc:
        raise MeshImportError(f"Import de {filepath.name} refusé par Blender : {exc}") from exc

    created = [obj for obj in bpy.data.objects if obj not in before]
    meshes = [obj for obj in created if obj.type == "MESH"]
    if not meshes:
        for obj in created:
            bpy.data.objects.remove(obj, do_unlink=True)
        raise MeshImportError(f"{filepath.name} ne contient aucun maillage.")

    # Les vides et armatures venant du GLB n'ont aucun intérêt ici.
    for obj in created:
        if obj.type != "MESH":
            bpy.data.objects.remove(obj, do_unlink=True)

    for index, obj in enumerate(meshes):
        obj.name = name if index == 0 else f"{name}.{index:03d}"
        obj.parent = None

    return meshes


def _run_importer(kind: str, filepath: Path) -> None:
    path = str(filepath)
    if kind == "gltf":
        bpy.ops.import_scene.gltf(filepath=path)
    elif kind == "obj":
        if hasattr(bpy.ops.wm, "obj_import"):
            bpy.ops.wm.obj_import(filepath=path)
        else:  # Blender < 3.3
            bpy.ops.import_scene.obj(filepath=path)
    elif kind == "ply":
        if hasattr(bpy.ops.wm, "ply_import"):
            bpy.ops.wm.ply_import(filepath=path)
        else:
            bpy.ops.import_mesh.ply(filepath=path)
    elif kind == "stl":
        if hasattr(bpy.ops.wm, "stl_import"):
            bpy.ops.wm.stl_import(filepath=path)
        else:
            bpy.ops.import_mesh.stl(filepath=path)
    elif kind == "fbx":
        bpy.ops.import_scene.fbx(filepath=path)
    else:  # pragma: no cover - _IMPORTERS et cette fonction évoluent ensemble
        raise MeshImportError(f"Importeur non câblé : {kind}")


def join_objects(objects: list[bpy.types.Object], context: bpy.types.Context) -> bpy.types.Object:
    """Fusionne plusieurs objets en un seul (les backends rendent parfois des morceaux)."""
    if len(objects) == 1:
        return objects[0]

    with context.temp_override(
        active_object=objects[0],
        selected_objects=objects,
        selected_editable_objects=objects,
    ):
        bpy.ops.object.join()
    return objects[0]


#: Formats dont l'importeur Blender redresse déjà l'axe vertical.
_ALREADY_Z_UP = {".glb", ".gltf", ".fbx"}


def needs_upright(filepath: Path, mode: str = "AUTO") -> bool:
    """Décide s'il faut faire pivoter le modèle de 90° autour de X.

    Le glTF est Y-up, mais son importeur Blender applique déjà la conversion.
    Les OBJ/PLY/STL bruts rendus par certains backends, eux, arrivent couchés.
    """
    if mode == "FORCE":
        return True
    if mode == "NONE":
        return False
    return Path(filepath).suffix.lower() not in _ALREADY_Z_UP


def normalize_transform(obj: bpy.types.Object, upright: bool = False) -> None:
    """Pose l'objet debout, centré en X/Y, sa base exactement sur Z = 0.

    Tout est appliqué sur les données du maillage : l'objet ressort avec une
    transformation identité, ce qui évite les mauvaises surprises au remesh et
    à l'export (une échelle non appliquée fausse les dimensions du STL).
    """
    mesh = obj.data
    if mesh is None or not mesh.vertices:
        return

    # Ne jamais transformer un maillage partagé par d'autres objets.
    if mesh.users > 1:
        mesh = obj.data = mesh.copy()

    # 1. On fige la transformation de l'objet dans le maillage.
    mesh.transform(obj.matrix_world)
    obj.matrix_world = mathutils.Matrix.Identity(4)

    # 2. Conversion Y-up → Z-up si le format l'impose.
    if upright:
        mesh.transform(mathutils.Matrix.Rotation(math.radians(90.0), 4, "X"))

    # 3. Centrage X/Y sur la boîte englobante, base posée sur le sol.
    coords = [vertex.co for vertex in mesh.vertices]
    min_x = min(co.x for co in coords)
    max_x = max(co.x for co in coords)
    min_y = min(co.y for co in coords)
    max_y = max(co.y for co in coords)
    min_z = min(co.z for co in coords)
    offset = mathutils.Vector(((min_x + max_x) / 2.0, (min_y + max_y) / 2.0, min_z))
    mesh.transform(mathutils.Matrix.Translation(-offset))

    mesh.update()


def scale_to_height(obj: bpy.types.Object, target_metres: float) -> float:
    """Met le plus grand côté de l'objet à ``target_metres`` et rend le facteur.

    L'échelle est appliquée au maillage, pas à l'objet : les exportateurs STL
    n'ont alors plus aucune ambiguïté sur les dimensions réelles.
    """
    mesh = obj.data
    if mesh is None or not mesh.vertices or target_metres <= 0.0:
        return 1.0

    if mesh.users > 1:
        mesh = obj.data = mesh.copy()

    coords = [vertex.co for vertex in mesh.vertices]
    largest = max(
        max(co.x for co in coords) - min(co.x for co in coords),
        max(co.y for co in coords) - min(co.y for co in coords),
        max(co.z for co in coords) - min(co.z for co in coords),
    )
    if largest <= 1e-9:
        return 1.0

    factor = target_metres / largest
    mesh.transform(mathutils.Matrix.Scale(factor, 4))
    mesh.update()
    return factor


def select_only(context: bpy.types.Context, objects: list[bpy.types.Object]) -> None:
    """Ne laisse sélectionnés que ``objects``, le premier devenant actif."""
    for obj in context.view_layer.objects:
        obj.select_set(False)
    for obj in objects:
        obj.select_set(True)
    if objects:
        context.view_layer.objects.active = objects[0]

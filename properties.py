# SPDX-License-Identifier: GPL-3.0-or-later
"""Propriétés de scène et état d'exécution.

Deux familles bien distinctes :

* :class:`Img23DSettings`, rangé sur la ``Scene``, donc **sauvegardé** dans le
  fichier .blend : les réglages de génération et d'impression ;
* :class:`Img23DState`, rangé sur le ``WindowManager``, donc **volatile** :
  la progression du travail en cours, qui n'a aucun sens à la réouverture.

Aucun secret n'est stocké ici : les clés d'API vivent dans les préférences de
l'extension, jamais dans le .blend.
"""

from __future__ import annotations

import bpy
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)
from bpy.types import PropertyGroup

from .backends import backend_items

VIEW_ITEMS = [
    ("AUTO", "Auto", "Laisse le backend décider"),
    ("FRONT", "Face", "Vue de face"),
    ("BACK", "Dos", "Vue de dos"),
    ("LEFT", "Gauche", "Profil gauche"),
    ("RIGHT", "Droite", "Profil droit"),
    ("TOP", "Dessus", "Vue de dessus"),
    ("BOTTOM", "Dessous", "Vue de dessous"),
]


#: Blender ne garde pas de référence sur les chaînes rendues par une fonction
#: ``items`` : sans ce cache au niveau module, elles peuvent être ramassées par
#: le GC et l'interface affiche des libellés corrompus.
_BACKEND_ENUM_CACHE: list[tuple[str, str, str]] = []


def _backend_enum_items(self, context):
    """Items de backend, calculés une fois puis mémorisés."""
    if not _BACKEND_ENUM_CACHE:
        _BACKEND_ENUM_CACHE.append(
            ("PREFS", "Celui des préférences", "Utilise le backend choisi globalement")
        )
        _BACKEND_ENUM_CACHE.extend(backend_items())
    return _BACKEND_ENUM_CACHE


class Img23DSourceImage(PropertyGroup):
    """Une image source, avec la vue qu'elle représente."""

    filepath: StringProperty(
        name="Image",
        description="Fichier image à envoyer au backend",
        subtype="FILE_PATH",
        default="",
    )
    view: EnumProperty(
        name="Vue",
        description="Angle de prise de vue, utile pour les modèles multi-vues",
        items=VIEW_ITEMS,
        default="AUTO",
    )
    enabled: BoolProperty(
        name="Utiliser",
        description="Décoche pour exclure cette image de la prochaine génération",
        default=True,
    )


class Img23DSettings(PropertyGroup):
    """Réglages sauvegardés dans le .blend."""

    # -- source ------------------------------------------------------------
    images: CollectionProperty(type=Img23DSourceImage)
    active_image_index: IntProperty(name="Image active", default=0, min=0)
    max_image_size: IntProperty(
        name="Taille max des images",
        description=(
            "Les images plus grandes sont réduites avant l'envoi. "
            "Au-delà de 1024 px, on paie de la bande passante sans gagner en qualité"
        ),
        default=1024,
        min=256,
        max=4096,
        step=64,
    )
    image_format: EnumProperty(
        name="Encodage",
        description="Format d'encodage avant envoi au backend",
        items=[
            ("PNG", "PNG", "Sans perte, conserve la transparence (recommandé)"),
            ("JPEG", "JPEG", "Plus léger, mais perd le canal alpha"),
        ],
        default="PNG",
    )

    # -- génération --------------------------------------------------------
    backend: EnumProperty(
        name="Backend",
        description="Où la génération doit tourner pour cette scène",
        # Pas de `default` possible avec des items dynamiques : Blender retient
        # donc la première entrée, « Celui des préférences », ce qui est bien
        # le comportement voulu.
        items=_backend_enum_items,
    )
    prompt: StringProperty(
        name="Description",
        description="Texte optionnel guidant la génération (ignoré par certains backends)",
        default="",
    )
    seed: IntProperty(
        name="Graine",
        description="Même graine + mêmes images = même résultat",
        default=0,
        min=0,
    )
    steps: IntProperty(
        name="Étapes",
        description="Nombre d'étapes de diffusion. Au-delà de 50, le gain devient marginal",
        default=30,
        min=1,
        max=100,
    )
    guidance: FloatProperty(
        name="Guidage",
        description="Fidélité à l'image source. Trop haut, le maillage devient bruité",
        default=5.5,
        min=0.0,
        max=20.0,
        step=10,
        precision=1,
    )
    octree_resolution: EnumProperty(
        name="Résolution",
        description="Résolution de l'octree : le principal levier de qualité… et de VRAM",
        items=[
            ("128", "128 — brouillon", "Rapide, peu de VRAM, formes grossières"),
            ("256", "256 — équilibré", "Bon compromis par défaut"),
            ("384", "384 — détaillé", "Nettement plus lourd en VRAM"),
            ("512", "512 — maximum", "Qualité maximale, 12 Go de VRAM ou plus"),
        ],
        default="256",
    )
    remove_background: BoolProperty(
        name="Détourer le sujet",
        description="Retire le fond avant génération. À décocher si l'image est déjà détourée",
        default=True,
    )
    with_texture: BoolProperty(
        name="Générer la texture",
        description=(
            "Inutile pour l'impression 3D — la texture ne s'imprime pas — "
            "mais utile pour un rendu ou une prévisualisation"
        ),
        default=False,
    )
    face_limit: IntProperty(
        name="Limite de faces",
        description="Demande au backend de décimer. 0 = aucune limite imposée",
        default=0,
        min=0,
        soft_max=500_000,
    )
    symmetry: EnumProperty(
        name="Symétrie",
        description="Contrainte de symétrie, si le backend la gère",
        items=[
            ("AUTO", "Auto", "Le backend décide"),
            ("ON", "Forcée", "Impose une symétrie gauche/droite"),
            ("OFF", "Aucune", "Laisse le modèle libre"),
        ],
        default="AUTO",
    )

    # -- import ------------------------------------------------------------
    upright_mode: EnumProperty(
        name="Redressement",
        description="Conversion de l'axe vertical à l'import",
        items=[
            ("AUTO", "Auto", "Redresse seulement les formats qui en ont besoin (OBJ, PLY, STL)"),
            ("FORCE", "Forcer", "Applique toujours la rotation Y-up → Z-up"),
            ("NONE", "Aucun", "N'y touche pas"),
        ],
        default="AUTO",
    )
    auto_scale: BoolProperty(
        name="Mettre à l'échelle",
        description="Redimensionne le modèle importé à la hauteur cible",
        default=True,
    )
    target_size_mm: FloatProperty(
        name="Taille cible",
        description="Plus grande dimension du modèle, en millimètres",
        default=80.0,
        min=1.0,
        max=10_000.0,
        unit="NONE",
        precision=1,
    )
    auto_prepare: BoolProperty(
        name="Préparer après import",
        description="Enchaîne automatiquement la préparation à l'impression",
        default=False,
    )

    # -- préparation à l'impression ---------------------------------------
    use_remesh: BoolProperty(
        name="Remesh voxel",
        description="Reconstruit une topologie uniforme et fermée. La base d'un maillage imprimable",
        default=True,
    )
    voxel_size_mm: FloatProperty(
        name="Taille de voxel",
        description=(
            "Taille d'un voxel en millimètres. En dessous de la buse de ton imprimante "
            "(0,4 mm en général), tu ajoutes des triangles sans ajouter de détail imprimable"
        ),
        default=0.8,
        min=0.01,
        max=50.0,
        precision=2,
    )
    remesh_adaptivity: FloatProperty(
        name="Adaptativité",
        description="Réduit le nombre de triangles dans les zones planes",
        default=0.0,
        min=0.0,
        max=1.0,
        precision=2,
    )
    use_repair: BoolProperty(
        name="Réparer",
        description="Fusionne les doublons, supprime l'isolé, comble les trous, recalcule les normales",
        default=True,
    )
    merge_distance_mm: FloatProperty(
        name="Distance de fusion",
        description="Sommets plus proches que cette distance (mm) : fusionnés",
        default=0.02,
        min=0.0,
        max=5.0,
        precision=3,
    )
    fill_holes: BoolProperty(
        name="Combler les trous",
        description="Ferme les bords ouverts pour rendre le maillage étanche",
        default=True,
    )
    triangulate: BoolProperty(
        name="Trianguler",
        description="Le STL ne connaît que des triangles : autant le faire proprement ici",
        default=True,
    )
    use_decimate: BoolProperty(
        name="Décimer",
        description="Plafonne le nombre de triangles pour alléger le fichier",
        default=False,
    )
    target_triangles: IntProperty(
        name="Triangles cibles",
        description="Plafond de triangles après décimation",
        default=100_000,
        min=100,
        soft_max=2_000_000,
    )

    # -- export ------------------------------------------------------------
    export_path: StringProperty(
        name="Dossier d'export",
        description="Où écrire le fichier exporté",
        subtype="DIR_PATH",
        default="//",
    )
    export_name: StringProperty(
        name="Nom du fichier",
        description="Nom de base, sans extension",
        default="img23d_model",
    )
    export_format: EnumProperty(
        name="Format",
        description="Format d'export",
        items=[
            ("STL", "STL", "Format universel des slicers"),
            ("OBJ", "OBJ", "Conserve les matériaux, utile hors impression"),
        ],
        default="STL",
    )
    export_unit: EnumProperty(
        name="Unité du fichier",
        description="Unité dans laquelle écrire les coordonnées du fichier exporté",
        items=[
            ("MM", "Millimètres", "Ce qu'attendent Cura, PrusaSlicer et Bambu Studio"),
            ("CM", "Centimètres", "Pour les chaînes qui travaillent en centimètres"),
            ("M", "Mètres", "Échelle Blender brute, sans conversion"),
        ],
        default="MM",
    )
    export_ascii: BoolProperty(
        name="STL ASCII",
        description="Fichier lisible mais nettement plus gros. Le binaire suffit presque toujours",
        default=False,
    )

    # -- helpers -----------------------------------------------------------
    @property
    def export_scale(self) -> float:
        """Facteur d'échelle à l'export, depuis une scène exprimée en mètres."""
        return {"MM": 1000.0, "CM": 100.0, "M": 1.0}[self.export_unit]

    def enabled_images(self) -> list[tuple[str, str]]:
        """Les ``(chemin, vue)`` cochés et non vides, dans l'ordre du panneau."""
        return [
            (item.filepath, item.view)
            for item in self.images
            if item.enabled and item.filepath.strip()
        ]


class Img23DState(PropertyGroup):
    """État volatile du travail en cours (non sauvegardé)."""

    running: BoolProperty(name="Génération en cours", default=False)
    progress: FloatProperty(name="Progression", default=0.0, min=0.0, max=1.0, subtype="FACTOR")
    status: StringProperty(name="Statut", default="")
    last_error: StringProperty(name="Dernière erreur", default="")
    last_backend: StringProperty(name="Dernier backend", default="")
    last_result: StringProperty(name="Dernier fichier généré", default="")
    check_result: StringProperty(name="Diagnostic", default="")
    check_ok: BoolProperty(name="Diagnostic positif", default=False)
    mesh_report: StringProperty(name="État du maillage", default="")
    mesh_watertight: BoolProperty(name="Maillage étanche", default=False)


classes = (
    Img23DSourceImage,
    Img23DSettings,
    Img23DState,
)


def register() -> None:
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.img23d = PointerProperty(type=Img23DSettings)
    bpy.types.WindowManager.img23d_state = PointerProperty(type=Img23DState)


def unregister() -> None:
    del bpy.types.WindowManager.img23d_state
    del bpy.types.Scene.img23d
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)

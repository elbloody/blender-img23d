# SPDX-License-Identifier: GPL-3.0-or-later
"""Préférences de l'extension : choix du backend et secrets associés.

Les clés d'API vivent ici, dans les préférences utilisateur de Blender, et
**jamais** dans le fichier .blend : un .blend se partage, pas une clé. Blender
les écrit dans son ``userpref.blend`` personnel.
"""

from __future__ import annotations

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, StringProperty
from bpy.types import AddonPreferences

from .backends import backend_items

#: Même précaution que pour les propriétés de scène : on garde une référence.
_BACKEND_ITEMS = backend_items()


class Img23DPreferences(AddonPreferences):
    bl_idname = __package__

    backend: EnumProperty(
        name="Backend par défaut",
        description="Utilisé par toutes les scènes qui ne forcent pas un backend particulier",
        items=_BACKEND_ITEMS,
        default=_BACKEND_ITEMS[0][0],
    )

    # -- backend local -----------------------------------------------------
    local_python: StringProperty(
        name="Interpréteur Python",
        description=(
            "Python d'un environnement contenant torch et hy3dgen. "
            "Surtout PAS celui de Blender : y installer torch casserait l'installation"
        ),
        subtype="FILE_PATH",
        default="",
    )
    local_model_repo: StringProperty(
        name="Modèle",
        description="Dépôt Hugging Face du modèle de génération",
        default="tencent/Hunyuan3D-2mini",
    )
    local_device: EnumProperty(
        name="Périphérique",
        items=[
            ("cuda", "CUDA (NVIDIA)", "Carte NVIDIA"),
            ("mps", "MPS (Apple Silicon)", "Mac M1/M2/M3"),
            ("cpu", "CPU", "Très lent, dépannage uniquement"),
        ],
        default="cuda",
    )
    local_low_vram: BoolProperty(
        name="Mode VRAM réduite",
        description="Active les optimisations mémoire au prix d'un peu de vitesse",
        default=False,
    )
    local_cache_dir: StringProperty(
        name="Cache des modèles",
        description="Dossier HF_HOME. Vide = emplacement par défaut de Hugging Face",
        subtype="DIR_PATH",
        default="",
    )
    local_worker_script: StringProperty(
        name="Worker personnalisé",
        description="Remplace le script worker fourni. Vide = celui de l'extension",
        subtype="FILE_PATH",
        default="",
    )

    # -- backend HTTP ------------------------------------------------------
    http_url: StringProperty(
        name="URL du serveur",
        description="Racine de ton serveur d'inférence, par exemple http://192.168.1.42:8000",
        default="",
    )
    http_api_key: StringProperty(
        name="Clé d'API",
        description="Envoyée en en-tête Authorization. Laisse vide si le serveur est ouvert",
        subtype="PASSWORD",
        default="",
    )
    http_verify_tls: BoolProperty(
        name="Vérifier le certificat TLS",
        description="À ne décocher que pour un serveur local en HTTPS auto-signé",
        default=True,
    )
    http_timeout: FloatProperty(
        name="Délai maximum (s)",
        description="Au-delà, la génération est abandonnée",
        default=1800.0,
        min=30.0,
        max=21_600.0,
    )

    # -- backend Kaggle ----------------------------------------------------
    kaggle_cli: StringProperty(
        name="Commande kaggle",
        description="Chemin du CLI Kaggle. Laisse « kaggle » s'il est dans le PATH",
        default="kaggle",
    )
    kaggle_api_token: StringProperty(
        name="Jeton d'API",
        description=(
            "Le jeton affiché par Kaggle dans Settings ▸ API Tokens. "
            "Il commence par KGAT_. C'est la seule chose à remplir : il porte "
            "aussi ton identité"
        ),
        subtype="PASSWORD",
        default="",
    )
    kaggle_username: StringProperty(
        name="Utilisateur",
        description=(
            "Ton pseudo Kaggle. Laisse vide avec un jeton d'API : il est "
            "retrouvé automatiquement. À remplir seulement si le test échoue"
        ),
        default="",
    )
    kaggle_api_key: StringProperty(
        name="Clé (ancien système)",
        description=(
            "L'ancienne clé « Legacy API Credentials », à utiliser avec le champ "
            "Utilisateur. Inutile si tu as renseigné un jeton d'API"
        ),
        subtype="PASSWORD",
        default="",
    )
    kaggle_kernel_slug: StringProperty(
        name="Kernel",
        description="Slug du kernel à réutiliser. Vide = <utilisateur>/img23d-worker",
        default="",
    )
    kaggle_timeout_minutes: FloatProperty(
        name="Délai maximum (min)",
        description="Un kernel Kaggle peut attendre longtemps en file d'attente",
        default=45.0,
        min=5.0,
        max=540.0,
    )

    # -- backend cloud -----------------------------------------------------
    cloud_provider: EnumProperty(
        name="Fournisseur",
        items=[
            ("MESHY", "Meshy", "api.meshy.ai"),
            ("TRIPO", "Tripo", "api.tripo3d.ai"),
            ("RODIN", "Rodin (Hyper3D)", "hyperhuman.deemos.com"),
        ],
        default="MESHY",
    )
    cloud_api_key: StringProperty(
        name="Clé d'API",
        description="Clé du fournisseur choisi",
        subtype="PASSWORD",
        default="",
    )
    cloud_api_base: StringProperty(
        name="Point d'entrée",
        description="Vide = URL par défaut du fournisseur. À renseigner si son API bouge",
        default="",
    )
    cloud_timeout_minutes: FloatProperty(
        name="Délai maximum (min)",
        default=20.0,
        min=1.0,
        max=180.0,
    )

    # -- divers ------------------------------------------------------------
    verbose: BoolProperty(
        name="Journal détaillé",
        description="Écrit chaque étape dans la console système de Blender",
        default=False,
    )

    # -- interface ---------------------------------------------------------
    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False

        entete = layout.box()
        entete.label(text="Commence par choisir où la génération doit tourner :", icon="PLUGIN")
        entete.prop(self, "backend")
        layout.prop(self, "verbose")

        box = layout.box()
        box.label(text="Local (ton propre GPU)", icon="DESKTOP")
        column = box.column()
        column.prop(self, "local_python")
        column.prop(self, "local_model_repo")
        column.prop(self, "local_device")
        column.prop(self, "local_low_vram")
        column.prop(self, "local_cache_dir")
        column.prop(self, "local_worker_script")
        note = box.column(align=True)
        note.scale_y = 0.8
        note.label(text="Installe torch et hy3dgen dans un environnement séparé,", icon="INFO")
        note.label(text="jamais dans le Python de Blender.")

        box = layout.box()
        box.label(text="Endpoint HTTP générique", icon="URL")
        column = box.column()
        column.prop(self, "http_url")
        column.prop(self, "http_api_key")
        column.prop(self, "http_verify_tls")
        column.prop(self, "http_timeout")

        box = layout.box()
        box.label(text="Kaggle Kernels", icon="CONSOLE")
        column = box.column()
        column.prop(self, "kaggle_cli")
        column.prop(self, "kaggle_api_token")
        note = box.column(align=True)
        note.scale_y = 0.8
        note.label(text="Le jeton se copie sur kaggle.com ▸ Settings ▸ API Tokens", icon="INFO")
        note.label(text="▸ Generate New Token. Il commence par KGAT_.")
        column = box.column()
        column.prop(self, "kaggle_username")
        column.prop(self, "kaggle_api_key")
        column.prop(self, "kaggle_kernel_slug")
        column.prop(self, "kaggle_timeout_minutes")

        box = layout.box()
        box.label(text="API cloud payante", icon="WORLD")
        column = box.column()
        column.prop(self, "cloud_provider")
        column.prop(self, "cloud_api_key")
        column.prop(self, "cloud_api_base")
        column.prop(self, "cloud_timeout_minutes")

        # Remplir la section d'un backend ne le sélectionne pas : sans ce
        # rappel, on teste « Local » en croyant tester ce qu'on vient de saisir.
        actif = dict((identifier, label) for identifier, label, _ in _BACKEND_ITEMS)
        box = layout.box()
        box.label(text=f"Le test portera sur : {actif.get(self.backend, self.backend)}", icon="INFO")
        row = box.row()
        row.scale_y = 1.3
        row.operator("img23d.check_backend", icon="CHECKMARK")

        # Le compte rendu doit s'afficher là où l'on a cliqué. Le reléguer au
        # panneau latéral ne laisse ici qu'un message court sans sa cause.
        state = context.window_manager.img23d_state
        if state.check_result:
            rapport = box.box()
            rapport.scale_y = 0.85
            icone = "CHECKMARK" if state.check_ok else "ERROR"
            for index, ligne in enumerate(state.check_result.split(" · ")):
                for morceau in _wrap(ligne, 90):
                    rapport.label(text=morceau, icon=icone if index == 0 else "BLANK1")
                    icone = "BLANK1"

    # -- configuration des backends ---------------------------------------
    def backend_config(self, backend_id: str) -> dict:
        """Traduit les préférences en dictionnaire de configuration du backend."""
        configs = {
            "LOCAL": {
                "python_executable": self.local_python,
                "model_repo": self.local_model_repo,
                "device": self.local_device,
                "low_vram": self.local_low_vram,
                "cache_dir": self.local_cache_dir,
                "worker_script": self.local_worker_script,
            },
            "HTTP": {
                "url": self.http_url,
                "api_key": self.http_api_key,
                "verify_tls": self.http_verify_tls,
                "request_timeout": self.http_timeout,
                "job_timeout": self.http_timeout,
            },
            "KAGGLE": {
                "cli_path": self.kaggle_cli,
                "api_token": self.kaggle_api_token,
                "username": self.kaggle_username,
                "api_key": self.kaggle_api_key,
                "kernel_slug": self.kaggle_kernel_slug,
                "timeout_minutes": self.kaggle_timeout_minutes,
                "model_repo": self.local_model_repo,
            },
            "CLOUD": {
                "provider": self.cloud_provider,
                "api_key": self.cloud_api_key,
                "api_base": self.cloud_api_base,
                "timeout_minutes": self.cloud_timeout_minutes,
            },
        }
        return configs.get(backend_id.upper(), {})


def _wrap(text: str, width: int) -> list[str]:
    """Découpe une ligne trop longue : Blender ne replie pas ses libellés."""
    import textwrap

    return textwrap.wrap(text, width) or [""]


def get_preferences(context: bpy.types.Context) -> Img23DPreferences:
    """Raccourci vers les préférences de cette extension."""
    return context.preferences.addons[__package__].preferences


def resolve_backend_id(context: bpy.types.Context) -> str:
    """Backend effectif : celui de la scène, ou celui des préférences."""
    scene_choice = context.scene.img23d.backend
    if scene_choice and scene_choice != "PREFS":
        return scene_choice
    return get_preferences(context).backend


def register() -> None:
    bpy.utils.register_class(Img23DPreferences)


def unregister() -> None:
    bpy.utils.unregister_class(Img23DPreferences)

# SPDX-License-Identifier: GPL-3.0-or-later
"""Lancer un programme du système depuis Blender, proprement.

Deux pièges guettent tout sous-processus lancé depuis Blender :

**L'environnement Python hérité.** Blender héberge son propre interpréteur et
peut exporter ``PYTHONHOME`` ou ``PYTHONPATH``. Un programme Python enfant qui
en hérite cherche alors ses modules dans l'installation de Blender, et échoue
sur un ``ModuleNotFoundError`` incompréhensible — alors que le même programme
fonctionne parfaitement dans un terminal.

**Le bac à sable.** Installé en Flatpak ou en Snap, Blender ne voit pas le même
système que l'utilisateur. Un programme installé sur l'hôte y est soit
invisible, soit exécuté par un interpréteur différent de celui qui l'a
installé. Flatpak fournit ``flatpak-spawn --host`` pour en sortir ; encore
faut-il que la permission ait été accordée.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

#: Variables qui détournent un interpréteur Python enfant vers celui de Blender.
_POISONED = ("PYTHONHOME", "PYTHONPATH", "PYTHONEXECUTABLE", "PYTHONSTARTUP")


def child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """L'environnement courant, débarrassé de ce qui casserait un Python enfant."""
    env = {key: value for key, value in os.environ.items() if key not in _POISONED}
    env.update(extra or {})
    return env


def sandbox() -> str:
    """Rend ``"flatpak"``, ``"snap"`` ou ``""`` selon l'emballage de Blender."""
    if os.environ.get("FLATPAK_ID") or Path("/.flatpak-info").exists():
        return "flatpak"
    if os.environ.get("SNAP"):
        return "snap"
    return ""


def can_escape_sandbox() -> bool:
    """Le bac à sable laisse-t-il lancer un programme de l'hôte ?"""
    return sandbox() == "flatpak" and shutil.which("flatpak-spawn") is not None


def host_command(argv: list[str]) -> list[str]:
    """Préfixe la commande pour qu'elle s'exécute sur l'hôte, si nécessaire."""
    if can_escape_sandbox():
        return ["flatpak-spawn", "--host", *argv]
    return list(argv)


def python_module_command(script: str | os.PathLike, module: str) -> list[str] | None:
    """Transforme ``<venv>/bin/outil`` en ``<venv>/bin/python -m module``.

    Un lanceur de venv dépend de sa ligne shebang, qui peut être résolue vers
    un autre interpréteur — c'est précisément ce qui arrive sous Flatpak.
    Appeler l'interpréteur du venv directement contourne le problème.
    Rend ``None`` si aucun interpréteur voisin n'existe.
    """
    path = Path(script)
    if not path.is_absolute() or path.parent.name != "bin":
        return None
    for nom in ("python", "python3"):
        interpreteur = path.parent / nom
        if interpreteur.exists() and os.access(interpreteur, os.X_OK):
            return [str(interpreteur), "-m", module]
    return None


def sandbox_hint(programme: str) -> str:
    """Message expliquant un échec dû au bac à sable, avec la marche à suivre."""
    nature = sandbox()
    if nature == "flatpak":
        if can_escape_sandbox():
            return (
                f"Blender tourne en Flatpak et a lancé {programme} sur l'hôte, "
                "sans succès. Vérifie qu'il fonctionne dans un terminal."
            )
        return (
            "Blender tourne en Flatpak : il ne voit pas les programmes installés "
            "sur ton système. Autorise-le une fois pour toutes avec : "
            "flatpak override --user --talk-name=org.freedesktop.Flatpak org.blender.Blender"
        )
    if nature == "snap":
        return (
            "Blender tourne en Snap : son confinement l'empêche de lancer les "
            "programmes de ton système. Installe plutôt Blender depuis blender.org."
        )
    return ""

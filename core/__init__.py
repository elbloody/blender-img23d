# SPDX-License-Identifier: GPL-3.0-or-later
"""Briques internes de l'extension.

Ce paquet est volontairement coupé en deux :

* les modules sans ``bpy`` (``jobs``) sont testables hors de Blender ;
* les modules avec ``bpy`` (``imageprep``, ``scene``, ``printprep``) ne sont
  importables que depuis Blender.

Ne rien importer ici : cela casserait les tests hors-Blender.
"""

# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests de la couche backends.

Ces tests tournent sans Blender : c'est possible parce que ``backends/`` et
``core/jobs.py`` n'importent jamais ``bpy``.

    python -m unittest discover -s tests -t .
"""

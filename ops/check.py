# SPDX-License-Identifier: GPL-3.0-or-later
"""Diagnostic du backend : vérifier avant de lancer une génération de 20 minutes."""

from __future__ import annotations

from bpy.types import Operator

from ..backends import create_backend
from ..preferences import get_preferences, resolve_backend_id
from ._job_modal import JobModalMixin, is_busy


class IMG23D_OT_check_backend(JobModalMixin, Operator):
    """Vérifie que le backend sélectionné est correctement configuré"""

    bl_idname = "img23d.check_backend"
    bl_label = "Tester le backend"
    bl_options = {"REGISTER"}

    #: Libellé du backend testé, retenu pour le compte rendu.
    _label: str = ""

    @classmethod
    def poll(cls, context):
        if is_busy(context):
            cls.poll_message_set("Un travail est déjà en cours")
            return False
        return True

    def execute(self, context):
        backend_id = resolve_backend_id(context)
        preferences = get_preferences(context)

        try:
            backend = create_backend(backend_id, preferences.backend_config(backend_id))
        except KeyError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        self._label = backend.label
        state = context.window_manager.img23d_state
        state.check_result = ""
        state.status = f"Diagnostic de « {backend.label} »…"

        def run(ctx):
            ctx.report(f"Diagnostic de « {backend.label} »…", 0.3)
            return backend.check()

        return self.start_job(context, run, name=f"img23d-check-{backend_id.lower()}")

    def on_success(self, context, result, elapsed):
        state = context.window_manager.img23d_state
        state.check_ok = bool(result.ok)
        # Le libellé du backend fait partie du message : sans lui, un échec du
        # backend actif ressemble à un échec de celui qu'on vient de remplir.
        entete = f"{self._label} : {result.message}"
        state.check_result = " · ".join([entete, *result.details])

        for line in [entete, *result.details]:
            print(f"[img23d] {line}")

        self.report({"INFO"} if result.ok else {"WARNING"}, entete)
        return {"FINISHED"}

    def on_failure(self, context, error):
        state = context.window_manager.img23d_state
        state.check_ok = False
        state.check_result = f"{self._label} : {error}"


classes = (IMG23D_OT_check_backend,)

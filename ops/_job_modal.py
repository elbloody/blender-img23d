# SPDX-License-Identifier: GPL-3.0-or-later
"""Base commune aux opérateurs qui pilotent un travail en arrière-plan.

Le schéma est toujours le même : un thread fait le travail long, un timer
réveille l'opérateur modal toutes les 200 ms pour relever la progression, et
seule cette reprise — sur le thread principal — a le droit de toucher à la
scène.
"""

from __future__ import annotations

import time

import bpy

from ..core.jobs import Job, JobContext
from ..preferences import get_preferences
from ..utils import set_status, tag_redraw


#: Le travail en cours, pour que le bouton « Annuler » du panneau puisse
#: l'atteindre sans avoir de référence sur l'opérateur modal lui-même.
_ACTIVE_JOB: Job | None = None


def cancel_active_job() -> bool:
    """Demande l'arrêt du travail en cours. Rend ``True`` s'il y en avait un."""
    global _ACTIVE_JOB
    if _ACTIVE_JOB is None:
        return False
    _ACTIVE_JOB.cancel()
    return True


def has_active_job() -> bool:
    return _ACTIVE_JOB is not None


def is_busy(context) -> bool:
    """Un travail est-il réellement en cours ?

    On croise le drapeau d'interface et le travail réel. Si un opérateur modal
    n'a jamais reçu ses évènements — cela arrive dans certaines fenêtres —, le
    drapeau resterait levé et griserait les boutons définitivement. Croiser les
    deux fait que l'interface se rétablit d'elle-même.
    """
    flag = context.window_manager.img23d_state.running
    if flag and _ACTIVE_JOB is None:
        context.window_manager.img23d_state.running = False
        return False
    return flag


class JobModalMixin:
    """À mélanger avec ``bpy.types.Operator``.

    Les sous-classes implémentent :meth:`on_success` et, si besoin,
    :meth:`on_failure` ; le reste (timer, annulation, journal) est ici.
    """

    #: Période de relève de la progression, en secondes.
    poll_interval = 0.2

    _job: Job | None = None
    _timer = None
    _started: float = 0.0

    # -- cycle de vie ------------------------------------------------------
    def start_job(self, context: bpy.types.Context, function, name: str = "img23d-job"):
        state = context.window_manager.img23d_state
        state.running = True
        state.progress = 0.0
        state.last_error = ""

        global _ACTIVE_JOB
        self._job = _ACTIVE_JOB = Job(function, ctx=JobContext(), name=name).start()
        self._started = time.monotonic()

        window_manager = context.window_manager
        self._timer = window_manager.event_timer_add(self.poll_interval, window=context.window)
        window_manager.modal_handler_add(self)
        tag_redraw(context)
        return {"RUNNING_MODAL"}

    def modal(self, context: bpy.types.Context, event: bpy.types.Event):
        if self._job is None:
            return self._teardown(context, {"CANCELLED"})

        if event.type == "ESC" and event.value == "PRESS":
            if not self._job.ctx.cancelled:
                self._job.cancel()
                set_status(context, "Annulation demandée…")
                tag_redraw(context)
            return {"RUNNING_MODAL"}

        if event.type != "TIMER":
            return {"PASS_THROUGH"}

        self._pump(context)

        if not self._job.finished:
            return {"PASS_THROUGH"}

        return self._complete(context)

    # -- interne -----------------------------------------------------------
    def _pump(self, context: bpy.types.Context) -> None:
        job = self._job
        assert job is not None

        verbose = get_preferences(context).verbose
        for line in job.ctx.drain_log():
            if verbose or line.level != "DEBUG":
                print(f"[img23d] {line.message}")

        progress, message = job.ctx.snapshot()
        elapsed = int(time.monotonic() - self._started)
        set_status(context, f"{message or 'Travail en cours…'} — {elapsed} s", progress)
        tag_redraw(context)

    def _complete(self, context: bpy.types.Context):
        job = self._job
        assert job is not None

        for line in job.ctx.drain_log():
            print(f"[img23d] {line.message}")

        if job.cancelled:
            self.report({"WARNING"}, "Travail annulé")
            return self._teardown(context, {"CANCELLED"}, status="Annulé")

        if job.error is not None:
            message = f"{type(job.error).__name__}: {job.error}"
            print(job.traceback_text or message)
            context.window_manager.img23d_state.last_error = str(job.error)
            self.on_failure(context, job.error)
            self.report({"ERROR"}, str(job.error))
            return self._teardown(context, {"CANCELLED"}, status="Échec")

        try:
            result = self.on_success(context, job.result, time.monotonic() - self._started)
        except Exception as exc:  # noqa: BLE001 - remonté proprement à l'utilisateur
            import traceback

            traceback.print_exc()
            context.window_manager.img23d_state.last_error = str(exc)
            self.report({"ERROR"}, f"{type(exc).__name__}: {exc}")
            return self._teardown(context, {"CANCELLED"}, status="Échec")

        return self._teardown(context, result or {"FINISHED"}, status="Terminé")

    def _teardown(self, context: bpy.types.Context, result: set, status: str = ""):
        global _ACTIVE_JOB
        if _ACTIVE_JOB is self._job:
            _ACTIVE_JOB = None

        window_manager = context.window_manager
        if self._timer is not None:
            window_manager.event_timer_remove(self._timer)
            self._timer = None

        state = window_manager.img23d_state
        state.running = False
        state.progress = 0.0
        if status:
            state.status = status
        self._job = None
        tag_redraw(context)
        return result

    def cancel(self, context: bpy.types.Context) -> None:
        """Appelé par Blender si l'opérateur modal est interrompu de l'extérieur."""
        if self._job is not None:
            self._job.cancel()
        self._teardown(context, {"CANCELLED"}, status="Annulé")

    # -- à implémenter par les sous-classes --------------------------------
    def on_success(self, context: bpy.types.Context, result, elapsed: float):
        raise NotImplementedError

    def on_failure(self, context: bpy.types.Context, error: BaseException) -> None:
        """Crochet optionnel, appelé avant le rapport d'erreur."""

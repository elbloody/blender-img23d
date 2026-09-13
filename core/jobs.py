# SPDX-License-Identifier: GPL-3.0-or-later
"""Exécution d'un travail long dans un thread, piloté depuis un opérateur modal.

L'API Blender n'est pas thread-safe : rien de ce fichier ne touche à ``bpy``.
Le thread de travail publie sa progression dans un :class:`JobContext`, et le
thread principal (l'opérateur modal) vient la lire à intervalle régulier avant
de modifier la scène.
"""

from __future__ import annotations

import threading
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable


class JobCancelled(Exception):
    """Levée dans le thread de travail quand l'utilisateur annule."""


@dataclass
class LogLine:
    message: str
    level: str = "INFO"


class JobContext:
    """Canal de communication entre le thread de travail et l'interface.

    Le thread de travail appelle :meth:`report`, :meth:`log` et
    :meth:`raise_if_cancelled` ; le thread principal appelle :meth:`snapshot`
    et :meth:`drain_log`.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._progress = 0.0
        self._message = ""
        self._log: list[LogLine] = []

    # -- côté interface ---------------------------------------------------
    def cancel(self) -> None:
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def snapshot(self) -> tuple[float, str]:
        with self._lock:
            return self._progress, self._message

    def drain_log(self) -> list[LogLine]:
        with self._lock:
            lines, self._log = self._log, []
        return lines

    # -- côté thread de travail -------------------------------------------
    def report(self, message: str, progress: float | None = None) -> None:
        with self._lock:
            self._message = message
            if progress is not None:
                self._progress = min(max(progress, 0.0), 1.0)
            self._log.append(LogLine(message))

    def log(self, message: str, level: str = "INFO") -> None:
        with self._lock:
            self._log.append(LogLine(message, level))

    def raise_if_cancelled(self) -> None:
        if self._cancel.is_set():
            raise JobCancelled()

    def sleep(self, seconds: float) -> None:
        """Attente interruptible : rend la main dès que l'utilisateur annule."""
        if self._cancel.wait(seconds):
            raise JobCancelled()


@dataclass
class Job:
    """Enveloppe un appelable ``fn(ctx)`` exécuté dans un thread démon."""

    fn: Callable[[JobContext], Any]
    ctx: JobContext = field(default_factory=JobContext)
    name: str = "img23d-job"

    _thread: threading.Thread | None = field(default=None, init=False, repr=False)
    _result: Any = field(default=None, init=False, repr=False)
    _error: BaseException | None = field(default=None, init=False, repr=False)
    _traceback: str = field(default="", init=False, repr=False)

    def start(self) -> "Job":
        if self._thread is not None:
            raise RuntimeError("Job déjà démarré")
        self._thread = threading.Thread(target=self._run, name=self.name, daemon=True)
        self._thread.start()
        return self

    def _run(self) -> None:
        try:
            self._result = self.fn(self.ctx)
        except BaseException as exc:  # noqa: BLE001 - remonté au thread principal
            self._error = exc
            self._traceback = traceback.format_exc()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def finished(self) -> bool:
        return self._thread is not None and not self._thread.is_alive()

    @property
    def result(self) -> Any:
        return self._result

    @property
    def error(self) -> BaseException | None:
        return self._error

    @property
    def traceback_text(self) -> str:
        return self._traceback

    def cancel(self) -> None:
        self.ctx.cancel()

    @property
    def cancelled(self) -> bool:
        return isinstance(self._error, JobCancelled) or self.ctx.cancelled

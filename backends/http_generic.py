# SPDX-License-Identifier: GPL-3.0-or-later
"""Backend « HTTP générique » : branche n'importe quel serveur d'inférence.

Le protocole attendu est volontairement minimal, pour qu'un petit serveur
FastAPI maison le couvre en trente lignes (voir ``docs/http_backend.md``) :

``POST {base_url}/generate``
    Corps JSON : ``{"images": [{"name", "view", "mime", "data"}], "params": {…}}``
    (``data`` est l'image encodée en base64).

    Réponse acceptée, au choix :

    * le binaire du modèle directement (``model/gltf-binary``…) ;
    * ``{"model_b64": "…", "format": "glb"}`` ;
    * ``{"model_url": "https://…"}`` ;
    * ``{"job_id": "…"}`` — le serveur travaille en asynchrone.

``GET {base_url}/jobs/{job_id}``
    ``{"status": "pending|running|done|error", "progress": 0.0-1.0,
       "message": "…", "model_url": "…", "model_b64": "…", "error": "…"}``
"""

from __future__ import annotations

import base64
import binascii
import tempfile
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

from . import httpclient
from .base import (
    Backend,
    BackendError,
    BackendStatus,
    BackendTimeout,
    GenerationRequest,
    GenerationResult,
)

DONE_STATES = {"done", "success", "succeeded", "completed", "complete", "finished"}
ERROR_STATES = {"error", "failed", "failure", "cancelled", "canceled"}


class HttpBackend(Backend):
    id = "HTTP"
    label = "Endpoint HTTP générique"
    description = "Un serveur d'inférence que tu héberges où tu veux (maison, RunPod, Vast.ai…)."

    # -- configuration -----------------------------------------------------
    @property
    def base_url(self) -> str:
        url = self.require("url", "URL du serveur HTTP")
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            raise BackendError(f"URL invalide (http/https attendu) : {url}")
        return url if url.endswith("/") else url + "/"

    @property
    def auth_headers(self) -> dict[str, str]:
        key = str(self.cfg("api_key", "") or "")
        if not key:
            return {}
        header = str(self.cfg("auth_header", "Authorization") or "Authorization")
        scheme = str(self.cfg("auth_scheme", "Bearer") or "").strip()
        return {header: f"{scheme} {key}".strip()}

    @property
    def verify_tls(self) -> bool:
        return bool(self.cfg("verify_tls", True))

    def _url(self, path: str) -> str:
        return urljoin(self.base_url, path.lstrip("/"))

    # -- diagnostic --------------------------------------------------------
    def check(self) -> BackendStatus:
        try:
            url = self._url(str(self.cfg("health_path", "health") or "health"))
        except BackendError as exc:
            return BackendStatus.failure(str(exc))

        try:
            response = httpclient.request(
                url,
                headers=self.auth_headers,
                timeout=float(self.cfg("connect_timeout", 15.0)),
                verify=self.verify_tls,
            )
        except BackendError as exc:
            return BackendStatus.failure("Serveur injoignable", str(exc))

        details = [f"HTTP {response.status} sur {url}"]
        if response.content_type == "application/json":
            try:
                payload = response.json()
            except BackendError:
                payload = None
            if isinstance(payload, dict):
                for key in ("model", "backend", "version", "gpu"):
                    if key in payload:
                        details.append(f"{key} : {payload[key]}")
        return BackendStatus.success("Serveur joignable", *details)

    # -- génération --------------------------------------------------------
    def generate(self, request: GenerationRequest, ctx) -> GenerationResult:
        payload = {
            "images": [
                {
                    "name": image.name,
                    "view": image.view,
                    "mime": image.mime,
                    "data": base64.b64encode(image.data).decode("ascii"),
                }
                for image in request.images
            ],
            "params": {
                "prompt": request.prompt,
                "seed": request.seed,
                "steps": request.steps,
                "guidance": request.guidance,
                "octree_resolution": request.octree_resolution,
                "face_limit": request.face_limit,
                "remove_background": request.remove_background,
                "with_texture": request.with_texture,
                "symmetry": request.symmetry,
                **request.extra,
            },
        }

        ctx.report(f"Envoi au serveur ({request.summary()})…", 0.1)
        response = httpclient.request(
            self._url(str(self.cfg("generate_path", "generate") or "generate")),
            method="POST",
            json_body=payload,
            headers=self.auth_headers,
            timeout=float(self.cfg("request_timeout", 900.0)),
            verify=self.verify_tls,
        )

        # Réponse synchrone binaire : le modèle arrive directement.
        if response.content_type not in {"application/json", ""}:
            ctx.report("Modèle reçu", 0.9)
            return self._write(response.body, httpclient.guess_extension(response.content_type), ctx)

        data = response.json()
        if not isinstance(data, dict):
            raise BackendError(f"Réponse inattendue du serveur : {data!r}")

        job_id = data.get("job_id") or data.get("id") or data.get("task_id")
        if job_id and not (data.get("model_b64") or data.get("model_url")):
            data = self._wait(str(job_id), ctx)

        return self._result_from(data, ctx)

    # -- interne -----------------------------------------------------------
    def _wait(self, job_id: str, ctx) -> dict:
        status_path = str(self.cfg("status_path", "jobs/{job_id}") or "jobs/{job_id}")
        url = self._url(status_path.format(job_id=job_id))
        interval = float(self.cfg("poll_interval", 3.0))
        deadline = time.monotonic() + float(self.cfg("job_timeout", 1800.0))

        ctx.report(f"Travail {job_id} en file d'attente…", 0.15)
        while True:
            ctx.raise_if_cancelled()
            if time.monotonic() > deadline:
                raise BackendTimeout(f"Le travail {job_id} n'a pas abouti à temps.")

            payload = httpclient.request(
                url,
                headers=self.auth_headers,
                timeout=float(self.cfg("connect_timeout", 30.0)),
                verify=self.verify_tls,
                expect_json=True,
            ).json()
            if not isinstance(payload, dict):
                raise BackendError(f"Statut inattendu pour {job_id} : {payload!r}")

            state = str(payload.get("status", "")).lower()
            message = str(payload.get("message") or f"Travail {job_id} : {state or 'en cours'}")
            remote = payload.get("progress")
            fraction = None
            if isinstance(remote, (int, float)):
                # 0-1 ou 0-100 selon les serveurs : on normalise.
                fraction = 0.15 + 0.7 * min(float(remote) / (100.0 if remote > 1 else 1.0), 1.0)
            ctx.report(message, fraction)

            if state in DONE_STATES:
                return payload
            if state in ERROR_STATES:
                raise BackendError(str(payload.get("error") or message))

            ctx.sleep(interval)

    def _result_from(self, data: dict, ctx) -> GenerationResult:
        fmt = str(data.get("format") or "").lower().lstrip(".")

        if data.get("model_b64"):
            try:
                blob = base64.b64decode(str(data["model_b64"]), validate=True)
            except (binascii.Error, ValueError) as exc:
                raise BackendError("Le champ model_b64 n'est pas du base64 valide") from exc
            ctx.report("Modèle reçu", 0.9)
            return self._write(blob, f".{fmt or 'glb'}", ctx, textured=bool(data.get("textured")))

        url = data.get("model_url") or data.get("url")
        if not url:
            raise BackendError(
                "Le serveur n'a rendu ni model_b64 ni model_url. Vérifie son implémentation."
            )

        absolute = urljoin(self.base_url, str(url))
        ctx.report("Téléchargement du modèle…", 0.85)
        suffix = f".{fmt}" if fmt else httpclient.guess_extension("", absolute)
        destination = Path(tempfile.mkdtemp(prefix="img23d-http-")) / f"mesh{suffix}"
        httpclient.download(
            absolute,
            destination,
            ctx=ctx,
            headers=self.auth_headers,
            timeout=float(self.cfg("request_timeout", 900.0)),
            verify=self.verify_tls,
        )
        return GenerationResult(
            path=destination,
            backend=self.id,
            format=destination.suffix.lstrip("."),
            textured=bool(data.get("textured")),
            meta={"source": absolute},
        )

    def _write(self, blob: bytes, suffix: str, ctx, textured: bool = False) -> GenerationResult:
        if not blob:
            raise BackendError("Le serveur a rendu un fichier vide.")
        suffix = suffix if suffix.startswith(".") else f".{suffix}"
        destination = Path(tempfile.mkdtemp(prefix="img23d-http-")) / f"mesh{suffix}"
        destination.write_bytes(blob)
        ctx.report("Modèle écrit sur le disque", 0.95)
        return GenerationResult(
            path=destination,
            backend=self.id,
            format=suffix.lstrip("."),
            textured=textured,
            meta={"bytes": len(blob)},
        )

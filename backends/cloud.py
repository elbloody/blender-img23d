# SPDX-License-Identifier: GPL-3.0-or-later
"""Backend « cloud » : API SaaS payantes, en filet de sécurité.

Trois fournisseurs sont câblés (Meshy, Tripo, Rodin). Chacun vit dans son
petit adaptateur : soumettre une tâche, interroger son état, récupérer l'URL
du modèle. Ajouter un fournisseur = ajouter une classe et une entrée dans
``PROVIDERS``.

Ces API sont des produits commerciaux : leurs routes et leurs champs bougent
sans préavis. Les points d'entrée sont donc surchargeables depuis les
préférences (``api_base``), pour pouvoir corriger le tir sans patcher le code.
"""

from __future__ import annotations

import base64
import tempfile
import time
from pathlib import Path

from . import httpclient
from .base import (
    Backend,
    BackendError,
    BackendStatus,
    BackendTimeout,
    GenerationRequest,
    GenerationResult,
    SourceImage,
)

DONE_STATES = {"succeeded", "success", "done", "completed", "complete"}
ERROR_STATES = {"failed", "failure", "error", "canceled", "cancelled", "expired"}


class CloudProvider:
    """Adaptateur pour une API image-to-3D commerciale."""

    id = ""
    label = ""
    default_base = ""

    def __init__(self, api_key: str, base_url: str, options: dict) -> None:
        self.api_key = api_key
        self.base_url = (base_url or self.default_base).rstrip("/")
        self.options = options

    # Les trois méthodes que chaque fournisseur doit fournir.
    def submit(self, request: GenerationRequest, ctx) -> str:
        raise NotImplementedError

    def poll(self, task_id: str) -> tuple[str, float, str, str]:
        """Rend ``(état, progression 0-1, message, url_du_modèle)``."""
        raise NotImplementedError

    def download_headers(self) -> dict[str, str]:
        return {}

    # Utilitaires communs
    @property
    def _bearer(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    @staticmethod
    def _data_uri(image: SourceImage) -> str:
        return f"data:{image.mime};base64,{base64.b64encode(image.data).decode('ascii')}"


class MeshyProvider(CloudProvider):
    id = "MESHY"
    label = "Meshy"
    default_base = "https://api.meshy.ai/openapi/v1"

    def submit(self, request: GenerationRequest, ctx) -> str:
        payload = {
            "image_url": self._data_uri(request.primary),
            "should_texture": request.with_texture,
            "enable_pbr": bool(self.options.get("enable_pbr", False)),
            "symmetry_mode": request.symmetry.lower(),
        }
        if request.face_limit:
            payload["target_polycount"] = request.face_limit
        if self.options.get("ai_model"):
            payload["ai_model"] = self.options["ai_model"]

        data = httpclient.post_json(
            f"{self.base_url}/image-to-3d", payload, headers=self._bearer, timeout=120
        )
        task_id = _first(data, "result", "id", "task_id")
        if not task_id:
            raise BackendError(f"Meshy n'a pas rendu d'identifiant de tâche : {data!r}")
        return str(task_id)

    def poll(self, task_id: str) -> tuple[str, float, str, str]:
        data = httpclient.get_json(
            f"{self.base_url}/image-to-3d/{task_id}", headers=self._bearer, timeout=60
        )
        status = str(data.get("status", "")).lower()
        progress = float(data.get("progress") or 0) / 100.0
        error = (data.get("task_error") or {}).get("message", "") if isinstance(
            data.get("task_error"), dict
        ) else ""
        url = (data.get("model_urls") or {}).get("glb", "") if isinstance(
            data.get("model_urls"), dict
        ) else ""
        return status, progress, error or f"Meshy : {status or 'en cours'}", str(url or "")


class TripoProvider(CloudProvider):
    id = "TRIPO"
    label = "Tripo"
    default_base = "https://api.tripo3d.ai/v2/openapi"

    def submit(self, request: GenerationRequest, ctx) -> str:
        image = request.primary
        ctx.report("Envoi de l'image à Tripo…", 0.12)
        content_type, body = httpclient.encode_multipart(
            files={"file": (image.name, image.data, image.mime)}
        )
        upload = httpclient.request(
            f"{self.base_url}/upload",
            method="POST",
            data=body,
            headers={**self._bearer, "Content-Type": content_type},
            timeout=180,
            expect_json=True,
        ).json()
        token = _first(_payload(upload), "image_token", "file_token", "token")
        if not token:
            raise BackendError(f"Tripo n'a pas rendu de jeton d'image : {upload!r}")

        payload = {
            "type": "image_to_model",
            "file": {"type": image.extension.lstrip("."), "file_token": str(token)},
        }
        if request.face_limit:
            payload["face_limit"] = request.face_limit
        if not request.with_texture:
            payload["texture"] = False
        if self.options.get("model_version"):
            payload["model_version"] = self.options["model_version"]

        data = httpclient.post_json(
            f"{self.base_url}/task", payload, headers=self._bearer, timeout=120
        )
        task_id = _first(_payload(data), "task_id", "id")
        if not task_id:
            raise BackendError(f"Tripo n'a pas rendu d'identifiant de tâche : {data!r}")
        return str(task_id)

    def poll(self, task_id: str) -> tuple[str, float, str, str]:
        data = _payload(
            httpclient.get_json(f"{self.base_url}/task/{task_id}", headers=self._bearer, timeout=60)
        )
        status = str(data.get("status", "")).lower()
        progress = float(data.get("progress") or 0) / 100.0
        output = data.get("output") or {}
        url = ""
        if isinstance(output, dict):
            url = str(_first(output, "pbr_model", "model", "base_model") or "")
        return status, progress, f"Tripo : {status or 'en cours'}", url


class RodinProvider(CloudProvider):
    id = "RODIN"
    label = "Rodin (Hyper3D)"
    default_base = "https://hyperhuman.deemos.com/api/v2"

    def __init__(self, api_key: str, base_url: str, options: dict) -> None:
        super().__init__(api_key, base_url, options)
        self._subscription_key = ""
        self._task_uuid = ""

    def submit(self, request: GenerationRequest, ctx) -> str:
        # Rodin accepte plusieurs vues répétées sous le même nom de champ.
        files = [("images", image.name, image.data, image.mime) for image in request.images]
        fields = {
            "tier": str(self.options.get("tier", "Regular")),
            "geometry_file_format": "glb",
            "material": "PBR" if request.with_texture else "Shaded",
            "quality": str(self.options.get("quality", "medium")),
        }
        if request.prompt:
            fields["prompt"] = request.prompt
        content_type, body = httpclient.encode_multipart(fields=fields, files=files)

        data = httpclient.request(
            f"{self.base_url}/rodin",
            method="POST",
            data=body,
            headers={**self._bearer, "Content-Type": content_type},
            timeout=180,
            expect_json=True,
        ).json()

        self._task_uuid = str(_first(data, "uuid", "task_uuid") or "")
        jobs = data.get("jobs") or {}
        self._subscription_key = str(
            (jobs.get("subscription_key") if isinstance(jobs, dict) else "") or ""
        )
        if not self._task_uuid or not self._subscription_key:
            raise BackendError(f"Réponse Rodin inattendue : {data!r}")
        return self._task_uuid

    def poll(self, task_id: str) -> tuple[str, float, str, str]:
        data = httpclient.post_json(
            f"{self.base_url}/status",
            {"subscription_key": self._subscription_key},
            headers=self._bearer,
            timeout=60,
        )
        jobs = data.get("jobs") or []
        states = [str(job.get("status", "")).lower() for job in jobs if isinstance(job, dict)]
        if not states:
            return "running", 0.0, "Rodin : en attente", ""
        if any(state in ERROR_STATES for state in states):
            return "failed", 0.0, "Rodin : la tâche a échoué", ""
        if all(state == "done" for state in states):
            return "succeeded", 1.0, "Rodin : terminé", self._download_url()
        done = sum(1 for state in states if state == "done")
        return "running", done / len(states), f"Rodin : {done}/{len(states)} étapes", ""

    def _download_url(self) -> str:
        data = httpclient.post_json(
            f"{self.base_url}/download",
            {"task_uuid": self._task_uuid},
            headers=self._bearer,
            timeout=120,
        )
        entries = data.get("list") or data.get("files") or []
        for entry in entries:
            if isinstance(entry, dict) and str(entry.get("name", "")).lower().endswith(".glb"):
                return str(entry.get("url", ""))
        for entry in entries:
            if isinstance(entry, dict) and entry.get("url"):
                return str(entry["url"])
        raise BackendError(f"Aucun fichier téléchargeable rendu par Rodin : {data!r}")


PROVIDERS: dict[str, type[CloudProvider]] = {
    MeshyProvider.id: MeshyProvider,
    TripoProvider.id: TripoProvider,
    RodinProvider.id: RodinProvider,
}


class CloudBackend(Backend):
    id = "CLOUD"
    label = "API cloud payante"
    description = "Meshy / Tripo / Rodin — qualité immédiate, facturée au crédit."

    def _provider(self) -> CloudProvider:
        name = str(self.cfg("provider", "MESHY") or "MESHY").upper()
        provider_class = PROVIDERS.get(name)
        if provider_class is None:
            known = ", ".join(sorted(PROVIDERS))
            raise BackendError(f"Fournisseur inconnu : {name} (connus : {known})")
        api_key = self.require("api_key", f"Clé API {provider_class.label}")
        options = {
            "tier": self.cfg("tier", "Regular"),
            "quality": self.cfg("quality", "medium"),
            "ai_model": self.cfg("ai_model", ""),
            "model_version": self.cfg("model_version", ""),
            "enable_pbr": self.cfg("enable_pbr", False),
        }
        return provider_class(api_key, str(self.cfg("api_base", "") or ""), options)

    def check(self) -> BackendStatus:
        try:
            provider = self._provider()
        except BackendError as exc:
            return BackendStatus.failure(str(exc))
        return BackendStatus.success(
            f"{provider.label} configuré",
            f"Point d'entrée : {provider.base_url}",
            "La clé n'est réellement validée qu'à la première génération.",
        )

    def generate(self, request: GenerationRequest, ctx) -> GenerationResult:
        provider = self._provider()
        ctx.report(f"Soumission à {provider.label}…", 0.1)
        task_id = provider.submit(request, ctx)
        ctx.log(f"Tâche {provider.label} : {task_id}")

        interval = float(self.cfg("poll_interval", 5.0))
        deadline = time.monotonic() + float(self.cfg("timeout_minutes", 20.0)) * 60.0
        url = ""

        while True:
            ctx.raise_if_cancelled()
            if time.monotonic() > deadline:
                raise BackendTimeout(f"{provider.label} n'a pas terminé la tâche {task_id} à temps.")

            status, progress, message, candidate = provider.poll(task_id)
            ctx.report(message, 0.15 + 0.7 * min(max(progress, 0.0), 1.0))

            if status in DONE_STATES:
                url = candidate
                break
            if status in ERROR_STATES:
                raise BackendError(message or f"{provider.label} : la tâche a échoué.")

            ctx.sleep(interval)

        if not url:
            raise BackendError(f"{provider.label} n'a rendu aucune URL de modèle.")

        ctx.report("Téléchargement du modèle…", 0.9)
        destination = Path(tempfile.mkdtemp(prefix="img23d-cloud-")) / "mesh.glb"
        httpclient.download(
            url, destination, ctx=ctx, headers=provider.download_headers(), timeout=900
        )
        return GenerationResult(
            path=destination,
            backend=self.id,
            format="glb",
            textured=request.with_texture,
            meta={"provider": provider.id, "task_id": task_id},
        )


def _payload(data) -> dict:
    """Déplie les réponses enveloppées façon ``{"code": 0, "data": {...}}``."""
    if isinstance(data, dict):
        if isinstance(data.get("data"), dict):
            return data["data"]
        return data
    return {}


def _first(data, *keys):
    if not isinstance(data, dict):
        return None
    for key in keys:
        value = data.get(key)
        if value:
            return value
    return None

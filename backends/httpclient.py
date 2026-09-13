# SPDX-License-Identifier: GPL-3.0-or-later
"""Client HTTP minimal bâti sur ``urllib``.

Une extension Blender ne peut pas compter sur ``requests`` : seuls les modules
de la bibliothèque standard (ou des wheels embarqués) sont disponibles. Ce
module couvre le strict nécessaire — JSON, multipart, téléchargement avec
progression — et traduit toutes les erreurs réseau en :class:`BackendError`.
"""

from __future__ import annotations

import json
import mimetypes
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .base import BackendError, BackendTimeout

USER_AGENT = "blender-img23d/0.1 (+https://github.com/elbloody/blender-img23d)"

#: Au-delà, on refuse de charger le corps en mémoire (téléchargements exclus).
MAX_INLINE_BODY = 64 * 1024 * 1024


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes
    url: str

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";")[0].strip().lower()

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BackendError(
                f"Réponse non-JSON de {self.url} : {_snippet(self.body)}"
            ) from exc

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


def _snippet(body: bytes, limit: int = 300) -> str:
    text = body[:limit].decode("utf-8", errors="replace").replace("\n", " ")
    return text + ("…" if len(body) > limit else "")


def _ssl_context(verify: bool = True) -> ssl.SSLContext | None:
    if verify:
        return None  # urllib utilise le contexte système, qui vérifie les certificats
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def request(
    url: str,
    *,
    method: str = "GET",
    headers: Mapping[str, str] | None = None,
    data: bytes | None = None,
    json_body: Any = None,
    timeout: float = 60.0,
    verify: bool = True,
    expect_json: bool = False,
) -> Response:
    """Effectue une requête et rend la réponse complète.

    ``expect_json`` n'impose rien au serveur : il sert seulement à produire un
    message d'erreur plus clair quand la réponse n'est pas exploitable.
    """
    final_headers = {"User-Agent": USER_AGENT}
    if json_body is not None:
        if data is not None:
            raise ValueError("data et json_body sont mutuellement exclusifs")
        data = json.dumps(json_body).encode("utf-8")
        final_headers["Content-Type"] = "application/json"
    if expect_json:
        final_headers.setdefault("Accept", "application/json")
    final_headers.update({k: v for k, v in (headers or {}).items() if v is not None})

    req = urllib.request.Request(url, data=data, headers=final_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context(verify)) as resp:
            body = resp.read(MAX_INLINE_BODY + 1)
            if len(body) > MAX_INLINE_BODY:
                raise BackendError(
                    f"Réponse trop volumineuse de {url} (> {MAX_INLINE_BODY // (1024 * 1024)} Mo)"
                )
            return Response(
                status=resp.status,
                headers={k.lower(): v for k, v in resp.headers.items()},
                body=body,
                url=url,
            )
    except urllib.error.HTTPError as exc:
        detail = _snippet(exc.read() or b"")
        raise BackendError(f"HTTP {exc.code} sur {url} : {detail or exc.reason}") from exc
    except TimeoutError as exc:
        raise BackendTimeout(f"Délai dépassé sur {url} ({timeout:.0f}s)") from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, TimeoutError):
            raise BackendTimeout(f"Délai dépassé sur {url} ({timeout:.0f}s)") from exc
        raise BackendError(f"Connexion impossible à {url} : {reason}") from exc


def get_json(url: str, **kwargs: Any) -> Any:
    return request(url, method="GET", expect_json=True, **kwargs).json()


def post_json(url: str, payload: Any, **kwargs: Any) -> Any:
    return request(url, method="POST", json_body=payload, expect_json=True, **kwargs).json()


def _iter_files(files) -> list[tuple[str, str, bytes, str]]:
    if not files:
        return []
    if isinstance(files, Mapping):
        return [(name, entry[0], entry[1], entry[2]) for name, entry in files.items()]
    return [tuple(entry) for entry in files]  # type: ignore[misc]


def encode_multipart(
    fields: Mapping[str, str] | None = None,
    files: Mapping[str, tuple[str, bytes, str]]
    | Sequence[tuple[str, str, bytes, str]]
    | None = None,
) -> tuple[str, bytes]:
    """Encode un corps ``multipart/form-data``.

    ``files`` accepte deux formes :

    * un mapping ``{champ: (nom_fichier, contenu, type_mime)}`` ;
    * une séquence ``[(champ, nom_fichier, contenu, type_mime), …]``, qui
      permet plusieurs fichiers sous le *même* nom de champ (certaines API
      attendent ``images`` répété pour un turnaround multi-vues).

    Rend ``(content_type, corps)``.
    """
    boundary = f"----img23d{uuid.uuid4().hex}"
    sep = f"--{boundary}\r\n".encode()
    parts: list[bytes] = []

    for name, value in (fields or {}).items():
        parts.append(sep)
        parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        parts.append(str(value).encode("utf-8"))
        parts.append(b"\r\n")

    for name, filename, content, mime in _iter_files(files):
        parts.append(sep)
        parts.append(
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode()
        )
        parts.append(f"Content-Type: {mime}\r\n\r\n".encode())
        parts.append(content)
        parts.append(b"\r\n")

    parts.append(f"--{boundary}--\r\n".encode())
    return f"multipart/form-data; boundary={boundary}", b"".join(parts)


def download(
    url: str,
    dest: Path,
    *,
    ctx=None,
    headers: Mapping[str, str] | None = None,
    timeout: float = 300.0,
    verify: bool = True,
    label: str = "Téléchargement",
) -> Path:
    """Télécharge ``url`` vers ``dest`` en flux, avec progression et annulation."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    final_headers = {"User-Agent": USER_AGENT}
    final_headers.update({k: v for k, v in (headers or {}).items() if v is not None})
    req = urllib.request.Request(url, headers=final_headers, method="GET")

    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context(verify)) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            with open(tmp, "wb") as handle:
                while True:
                    if ctx is not None:
                        ctx.raise_if_cancelled()
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
                    done += len(chunk)
                    if ctx is not None and total:
                        ctx.report(f"{label} ({done * 100 // total} %)")
    except urllib.error.HTTPError as exc:
        tmp.unlink(missing_ok=True)
        raise BackendError(f"HTTP {exc.code} en téléchargeant {url}") from exc
    except urllib.error.URLError as exc:
        tmp.unlink(missing_ok=True)
        raise BackendError(f"Téléchargement impossible depuis {url} : {exc.reason}") from exc
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise

    os.replace(tmp, dest)
    return dest


def guess_extension(content_type: str, url: str = "", default: str = ".glb") -> str:
    """Devine l'extension d'un modèle à partir du type MIME, puis de l'URL."""
    known = {
        "model/gltf-binary": ".glb",
        "model/gltf+json": ".gltf",
        "model/obj": ".obj",
        "model/stl": ".stl",
        "application/sla": ".stl",
        "application/zip": ".zip",
    }
    content_type = (content_type or "").split(";")[0].strip().lower()
    if content_type in known:
        return known[content_type]

    path = urllib.parse.urlparse(url).path
    suffix = Path(path).suffix.lower()
    if suffix in {".glb", ".gltf", ".obj", ".ply", ".stl", ".fbx", ".usdz", ".zip"}:
        return suffix

    return mimetypes.guess_extension(content_type) or default

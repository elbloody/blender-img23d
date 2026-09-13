# Protocole du backend HTTP générique

Le backend *HTTP générique* permet de brancher n'importe quel serveur
d'inférence sur l'extension. Le contrat est volontairement minimal : deux
routes, du JSON, et trois formes de réponse acceptées.

Toutes les URL sont relatives à l'*URL du serveur* configurée dans les
préférences. Si une clé d'API est renseignée, elle est envoyée dans
`Authorization: Bearer <clé>`.

## `GET /health`

Appelée par le bouton *Tester le backend*. N'importe quelle réponse `2xx`
suffit. Si le corps est un objet JSON, les clés `model`, `backend`, `version`
et `gpu` sont affichées dans le panneau — pratique pour vérifier d'un coup
d'œil que tu parles au bon serveur.

```json
{ "backend": "hunyuan3d-2mini", "gpu": "RTX 4090", "version": "1.2.0" }
```

## `POST /generate`

Corps de la requête :

```json
{
  "images": [
    { "name": "f.png", "view": "FRONT", "mime": "image/png", "data": "<base64>" },
    { "name": "b.png", "view": "BACK",  "mime": "image/png", "data": "<base64>" }
  ],
  "params": {
    "prompt": "",
    "seed": 0,
    "steps": 30,
    "guidance": 5.5,
    "octree_resolution": 256,
    "face_limit": 0,
    "remove_background": true,
    "with_texture": false,
    "symmetry": "AUTO"
  }
}
```

`view` vaut `FRONT`, `BACK`, `LEFT`, `RIGHT`, `TOP`, `BOTTOM` ou `AUTO`. Les
images sont déjà redimensionnées et réencodées par Blender : inutile de les
retraiter.

### Trois réponses possibles

**1. Synchrone binaire** — le plus simple. Réponds avec le fichier lui-même et
un `Content-Type` de modèle (`model/gltf-binary`, `model/obj`, `model/stl`…).
L'extension devine l'extension depuis le type MIME.

**2. Synchrone en JSON**

```json
{ "model_b64": "<base64 du GLB>", "format": "glb", "textured": false }
```

ou, si le fichier est déjà accessible quelque part :

```json
{ "model_url": "/artifacts/abc.glb" }
```

L'URL peut être absolue ou relative à l'URL du serveur.

**3. Asynchrone** — recommandé dès que la génération dépasse la minute, pour
que l'extension puisse afficher une progression et rester annulable.

```json
{ "job_id": "abc123" }
```

## `GET /jobs/{job_id}`

Sondée toutes les trois secondes tant que le travail n'est pas terminé.

```json
{
  "status": "running",
  "progress": 42,
  "message": "Diffusion, étape 12/30"
}
```

- `status` : `pending`, `queued`, `running`, puis `done` ou `error`.
  Les variantes `success`, `succeeded`, `completed`, `complete`, `finished`
  sont aussi acceptées comme succès ; `failed`, `failure`, `cancelled` comme
  échec.
- `progress` : `0`–`1` ou `0`–`100`, les deux sont compris.
- `message` : affiché tel quel dans le panneau. Sois précis, c'est la seule
  chose que voit l'utilisateur pendant l'attente.

En cas de succès, ajoute `model_b64` ou `model_url` comme ci-dessus. En cas
d'échec, mets le motif dans `error` : il est remonté à l'utilisateur.

## Serveur d'exemple

Un serveur minimal et complet, en FastAPI. Remplace `generer_le_maillage` par
l'appel à ton pipeline.

```python
import base64, uuid
from pathlib import Path
from fastapi import BackgroundTasks, FastAPI
from fastapi.responses import FileResponse

app = FastAPI()
JOBS: dict[str, dict] = {}
SORTIE = Path("/tmp/img23d"); SORTIE.mkdir(exist_ok=True)


def generer_le_maillage(images: list[bytes], params: dict, destination: Path) -> None:
    """À toi de jouer : écris un .glb dans `destination`."""
    raise NotImplementedError


def travailler(job_id: str, images: list[bytes], params: dict) -> None:
    destination = SORTIE / f"{job_id}.glb"
    try:
        JOBS[job_id] |= {"status": "running", "progress": 10, "message": "Chargement du modèle"}
        generer_le_maillage(images, params, destination)
        JOBS[job_id] |= {"status": "done", "progress": 100, "model_url": f"/files/{job_id}.glb"}
    except Exception as exc:
        JOBS[job_id] |= {"status": "error", "error": f"{type(exc).__name__}: {exc}"}


@app.get("/health")
def health():
    return {"backend": "mon-serveur", "gpu": "RTX 4090"}


@app.post("/generate")
def generate(payload: dict, taches: BackgroundTasks):
    job_id = uuid.uuid4().hex
    images = [base64.b64decode(image["data"]) for image in payload["images"]]
    JOBS[job_id] = {"status": "queued", "progress": 0, "message": "En file d'attente"}
    taches.add_task(travailler, job_id, images, payload.get("params", {}))
    return {"job_id": job_id}


@app.get("/jobs/{job_id}")
def job(job_id: str):
    return JOBS.get(job_id, {"status": "error", "error": "travail inconnu"})


@app.get("/files/{nom}")
def fichier(nom: str):
    return FileResponse(SORTIE / nom, media_type="model/gltf-binary")
```

Lance-le avec `uvicorn serveur:app --host 0.0.0.0 --port 8000`, puis renseigne
`http://<ip-de-la-machine>:8000` dans les préférences de l'extension.

## Réglages avancés

Ces clés existent dans la couche backend et peuvent être utiles si ton serveur
ne suit pas exactement les chemins par défaut. Elles ne sont pas exposées dans
l'interface : elles se changent dans `preferences.backend_config()`.

| Clé | Défaut | Rôle |
|---|---|---|
| `health_path` | `health` | Chemin du diagnostic |
| `generate_path` | `generate` | Chemin de la soumission |
| `status_path` | `jobs/{job_id}` | Gabarit du chemin de sondage |
| `poll_interval` | `3.0` | Secondes entre deux sondages |
| `auth_header` | `Authorization` | En-tête portant la clé |
| `auth_scheme` | `Bearer` | Préfixe de la valeur |
| `verify_tls` | `true` | Vérification du certificat |

## Sécurité

La clé d'API est stockée dans les préférences utilisateur de Blender, jamais
dans le fichier `.blend` : un `.blend` se partage, pas une clé.

Si tu exposes ton serveur au-delà de ton réseau local, mets-le derrière HTTPS
et exige une clé. *Vérifier le certificat TLS* ne doit être décoché que pour un
serveur local en certificat auto-signé.

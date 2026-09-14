# Image → 3D pour Blender

Extension Blender 4.2+ qui transforme une ou plusieurs images en maillage
**imprimable** : génération, import, remesh, contrôle d'étanchéité, export STL
au millimètre près — sans quitter Blender.

La génération elle-même ne tourne pas dans Blender. Elle est déléguée à un
**backend interchangeable**, choisi dans les préférences. C'est le cœur du
projet : aucun compte n'est imposé, et changer de backend ne change rien au
reste de la chaîne.

| Backend | Compte requis | GPU | Pour qui |
|---|---|---|---|
| **Local** | aucun | le tien | Tu as une carte NVIDIA correcte (RTX 3060 12 Go et plus) |
| **HTTP générique** | celui de ton serveur | non géré ici | Tu as (ou veux monter) un serveur ailleurs : PC de bureau, RunPod, Vast.ai |
| **Kaggle** | compte gratuit | T4 16 Go ou P100 | Pas de GPU costaud, mais tu veux du gratuit et fiable |
| **API cloud** | clé API | sans objet | Pièce importante, qualité immédiate, budget disponible |

> **Sans compte Kaggle, ça marche quand même.** Kaggle est une option parmi
> quatre, jamais une dépendance. Installe l'extension, choisis *Local* ou
> *HTTP* dans les préférences, et c'est parti.

## Installation

1. Télécharge ou construis le `.zip` de l'extension (voir *Construire* plus bas).
2. Dans Blender : **Édition ▸ Préférences ▸ Extensions ▸ ▾ ▸ Install from Disk…**
3. Choisis le `.zip`, active l'extension.
4. Déplie ses préférences et configure le backend que tu veux utiliser.
5. Dans la vue 3D, ouvre la barre latérale (touche <kbd>N</kbd>) : onglet **Image → 3D**.

L'extension ne dépend d'aucun paquet Python externe. Elle n'utilise que la
bibliothèque standard et l'API Blender, donc rien à installer dans le Python
de Blender — ce qu'il ne faut de toute façon jamais faire.

## Démarrage rapide

1. **Images sources** — ajoute une image. Si tu en as plusieurs (face, dos,
   profils), ajoute-les toutes et assigne une vue à chacune : les modèles
   multi-vues en tirent nettement meilleur parti qu'une image seule.
2. **Génération** — choisis la résolution, puis *Tester le backend* pour
   vérifier la configuration avant de lancer un travail de vingt minutes.
   Puis *Générer le modèle 3D*.
3. **Impression 3D** — *Analyser le maillage* te dit s'il est étanche.
   *Préparer pour l'impression* enchaîne remesh, réparation et décimation.
4. **Export** — STL en millimètres, prêt pour Cura, PrusaSlicer ou Bambu Studio.

La génération tourne dans un thread : Blender reste utilisable, une barre de
progression s'affiche dans le panneau, et <kbd>Échap</kbd> (ou le bouton
*Annuler*) interrompt réellement le travail, y compris un sous-processus ou un
sondage réseau en cours.

## Configurer un backend

### Local — ton propre GPU

Il ne faut **pas** installer PyTorch dans le Python de Blender : ça casse
l'installation. L'extension lance donc un worker dans un interpréteur séparé
que tu désignes.

```bash
python -m venv ~/.img23d-env
~/.img23d-env/bin/pip install torch --index-url https://download.pytorch.org/whl/cu121
~/.img23d-env/bin/pip install huggingface_hub pillow trimesh
# puis Hunyuan3D-2 : https://github.com/Tencent-Hunyuan/Hunyuan3D-2
~/.img23d-env/bin/pip install -e /chemin/vers/Hunyuan3D-2
```

Dans les préférences, renseigne `~/.img23d-env/bin/python` comme *Interpréteur
Python*. *Tester le backend* affiche alors la version de PyTorch, le GPU
détecté et sa VRAM.

En dessous de 8 Go de VRAM, reste en résolution 128 ou 256. Les cartes Pascal
(GTX 10xx) gèrent mal le FP16 : attends-toi à des temps de génération très
supérieurs aux cartes récentes, voire à des artefacts.

### HTTP générique — ton propre serveur

Renseigne l'URL racine de ton serveur et, si besoin, une clé d'API. Le
protocole attendu tient en deux routes et est décrit dans
[`docs/http_backend.md`](docs/http_backend.md), avec un serveur FastAPI
d'exemple d'une trentaine de lignes.

C'est le backend le plus souple : il te rend indépendant de tout fournisseur,
et te permet de déménager la puissance de calcul sans toucher à l'extension.

### Kaggle — GPU gratuit

Le CLI Kaggle s'installe **hors** de Blender :

```bash
pip install --user kaggle
```

Renseigne ton nom d'utilisateur et ta clé d'API dans les préférences, ou
dépose simplement `~/.kaggle/kaggle.json` : l'extension le lit s'il existe.

À chaque génération, l'extension écrit un kernel autonome (images embarquées en
base64 + paramètres), l'envoie avec `kaggle kernels push`, suit son état, puis
récupère le GLB avec `kaggle kernels output`. Aucun tunnel, aucune session à
garder ouverte — c'est ce qui rend Kaggle plus fiable que Colab pour de
l'automatisation.

Le kernel installe lui-même Hunyuan3D en clonant son dépôt : `hy3dgen` n'est
pas publié sur PyPI. Ces commandes sont surchargeables via la clé
`setup_commands` si le dépôt amont bouge.

### API cloud — Meshy, Tripo, Rodin

Choisis le fournisseur, colle ta clé d'API. Le champ *Point d'entrée* permet de
corriger l'URL de base sans patcher le code, ce qui arrive : ce sont des
produits commerciaux dont les schémas d'API bougent sans préavis.

## Pourquoi le remesh avant l'impression

Un modèle généré n'est presque jamais imprimable tel quel : bords ouverts,
faces internes, normales retournées, sommets isolés. Un slicer confronté à ça
« répare » à sa façon, avec des résultats imprévisibles.

Le remesh voxel reconstruit une surface fermée et uniforme. C'est le seul
traitement qui rende un maillage généré fiablement étanche. Le prix à payer :
il lisse les détails plus fins que la taille de voxel.

**Choisis la taille de voxel en fonction de ta buse**, pas de ton envie de
détail : en dessous de 0,4 mm (buse standard), tu ajoutes des triangles que
l'imprimante ne saura pas restituer. Pour une figurine de 80 mm, 0,5 à 0,8 mm
est un bon compromis.

La texture, elle, ne s'imprime pas. L'option *Générer la texture* existe pour
le rendu et la prévisualisation ; pour une pièce destinée au slicer, laisse-la
décochée : c'est du temps de génération en moins.

## Construire le `.zip`

Avec Blender 4.2 ou plus récent :

```bash
blender --command extension build --source-dir . --output-dir dist
```

Le manifeste exclut déjà `tests/`, `.github/` et les `__pycache__` de
l'archive.

## Développement

```bash
# Tests de la couche backends — sans Blender, quelques secondes
python -m unittest discover -s tests -t .

# Tests d'intégration — installent l'extension dans un vrai Blender
blender --background --python tests/blender/test_integration.py
```

Les tests d'intégration couvrent la chaîne complète : préparation des images
par Blender, génération contre un serveur conforme au protocole HTTP, import du
GLB, remesh, contrôle d'étanchéité, puis relecture octet par octet du STL
produit pour vérifier qu'il est bien à l'échelle en millimètres.

### Organisation

```
backends/   les quatre backends. Aucun import de bpy : testable et
            réutilisable hors Blender (par un serveur MCP, par exemple)
core/       threads, images, import, préparation à l'impression
ops/        les opérateurs Blender, c'est-à-dire les boutons
ui/         les panneaux de la barre latérale
```

La séparation `backends/` ↔ reste du code n'est pas décorative : un test
vérifie qu'aucun module de cette couche n'importe `bpy`. C'est ce qui permet de
lancer la suite unitaire en une dizaine de secondes sans démarrer Blender.

Ajouter un backend : écrire une classe qui implémente `check()` et
`generate()`, l'inscrire dans `backends/registry.py`. L'interface, les
préférences et les opérateurs s'adaptent tout seuls.

## Ce que cette extension ne fait pas

- **Elle ne garantit pas la parité avec un SaaS haut de gamme.** Avec des vues
  multiples et la résolution maximale, l'écart se réduit beaucoup, mais aucun
  modèle libre ne bat un service commercial optimisé sur tous les cas — en
  particulier les éléments détachés et les structures très fines. Le backend
  cloud reste le filet de sécurité pour les pièces critiques.
- **Elle n'installe pas les modèles pour toi.** Les backends local et Kaggle
  supposent que tu as monté l'environnement Hunyuan3D correspondant.
- **Elle ne génère pas les vues multiples.** Si tu veux un turnaround à partir
  d'une seule image, produis-le avec l'outil de ton choix, puis donne les vues
  à l'extension.

## Licences

Le code de l'extension est publié sous **GPL-3.0-or-later** (voir
[`LICENSE`](LICENSE)), comme l'exige l'API Python de Blender.

Attention, c'est distinct : les **modèles** que tu fais tourner ont leurs
propres licences. Hunyuan3D est distribué sous *Tencent Hunyuan Community
License*, qui n'est pas une licence libre classique et pose des conditions à
l'usage commercial. Vérifie-la avant de vendre quoi que ce soit produit avec.

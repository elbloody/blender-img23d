# Reprise en local — note de passation

Ce document s'adresse à un agent (Claude Code) qui tourne **sur la machine de
l'utilisateur**, avec un accès réel au système. Il reprend un travail mené
jusqu'ici depuis un conteneur distant, sans accès à cette machine.

L'utilisateur est **débutant sur Blender et sur la ligne de commande**.
Explique ce que tu fais, en français, sans jargon, et propose des commandes
courtes : son copier-coller déforme les lignes longues (des caractères se
dupliquent en fin de ligne — cela a déjà cassé trois commandes).

---

## 1. Le projet

Extension Blender 4.2+ qui transforme une ou plusieurs images en maillage
imprimable : génération, import, remesh, contrôle d'étanchéité, export STL en
millimètres.

La génération ne tourne pas dans Blender. Elle est déléguée à un **backend
interchangeable** choisi dans les préférences : `LOCAL` (GPU de la machine),
`HTTP` (serveur d'inférence quelconque), `KAGGLE` (GPU gratuit via le CLI
officiel), `CLOUD` (Meshy / Tripo / Rodin).

```
dépôt    https://github.com/elbloody/blender-img23d
branche  claude/extension-code-akmtu0
commit   7f64adc
```

Structure : `backends/` (les quatre backends, **aucun import de `bpy`**),
`core/` (threads, images, import, impression 3D), `ops/` (opérateurs),
`ui/` (panneaux). Un test vérifie que `backends/` et `core/jobs.py` n'importent
jamais `bpy` — ne casse pas cette séparation, c'est ce qui rend la couche
testable sans Blender.

---

## 2. Où en est l'utilisateur

**Fait et vérifié :**

- Fedora, Blender **5.2.0 LTS**, utilisateur `elbloody`
- extension installée dans Blender, panneau visible
- CLI Kaggle installé dans un environnement dédié : `~/.img23d-kaggle/bin/kaggle`
  (version 2.2.4)
- session Kaggle ouverte par navigateur — vérifié dans son terminal :

  ```
  $ ~/.img23d-kaggle/bin/kaggle config view
  Configuration values from /home/elbloody/.kaggle
  - username: elbloody
  - auth_method: OAUTH
  ```

- préférences de l'extension : backend `Kaggle Kernels`, champs *Jeton d'API*,
  *Utilisateur*, *Clé (ancien système)* et *Kernel* **volontairement vides**
  (la session OAuth suffit ; un jeton renseigné passerait avant elle)

**Le blocage :** dans Blender, *Tester le backend* échoue avec

```
Kaggle Kernels : Le CLI Kaggle n'a pas abouti
  Traceback (most recent call last):
    File "/home/elbloody/.img23d-kaggle/bin/kaggle", line 3, in <module>
      from kaggle.cli import main
  ModuleNotFoundError: No module named 'kaggle'
```

Le même CLI fonctionne parfaitement dans son terminal. Le programme démarre
donc bien, mais **l'interpréteur qui l'exécute depuis Blender n'a pas accès
aux modules du venv**.

> ⚠️ Un compte Kaggle exige une **vérification par téléphone** pour que ses
> notebooks aient accès à Internet. L'extension en a besoin (le kernel
> télécharge le modèle). Ce point n'a pas été confirmé — à vérifier sur
> kaggle.com ▸ Settings ▸ Account.

---

## 3. La question à trancher en premier

Deux causes possibles, déjà traitées dans le code, mais **jamais confirmées
sur la machine** :

1. **Blender tourne dans un bac à sable** (Flatpak, très probable pour une
   5.2 sur Fedora). Il ne voit pas le même système que le terminal.
2. **Variables Python héritées** : Blender exporte `PYTHONHOME` ou
   `PYTHONPATH`, qui détournent l'interpréteur enfant.

```bash
flatpak list | grep -i blender      # une ligne → hypothèse 1
which blender && ls -l $(which blender)
```

### Si c'est Flatpak

```bash
flatpak override --user --talk-name=org.freedesktop.Flatpak org.blender.Blender
```

Puis **fermer et rouvrir Blender** — la permission n'est lue qu'au démarrage.
L'extension détecte alors le bac à sable et passe par `flatpak-spawn --host`.

Vérifie que l'évasion fonctionne réellement :

```bash
flatpak run --command=flatpak-spawn org.blender.Blender --host \
  /home/elbloody/.img23d-kaggle/bin/kaggle config view
```

### Sinon

Reproduis l'appel exact que fait l'extension, depuis le Python de Blender
(*Scripting* ▸ console) :

```python
import subprocess, os
print({k: v for k, v in os.environ.items() if "PYTHON" in k})
print(subprocess.run(["/home/elbloody/.img23d-kaggle/bin/kaggle", "config", "view"],
                     capture_output=True, text=True))
```

Compare avec le même appel depuis un terminal : la différence est la cause.

### Si le CLI reste inutilisable depuis Blender

Ce n'est pas une impasse. Le backend **HTTP** ne lance aucun programme, il ne
fait que du réseau — il traverse donc n'importe quel bac à sable. Le protocole
tient en deux routes, et `docs/http_backend.md` fournit un serveur FastAPI
d'exemple complet. Propose cette bascule plutôt que de t'acharner.

---

## 4. Objectif suivant : une vraie génération

**Personne n'a jamais fait tourner une génération de bout en bout.** C'est le
trou principal. Une fois *Tester le backend* au vert :

1. Vue 3D ▸ touche <kbd>N</kbd> ▸ onglet **Image → 3D**
2. **Images sources** ▸ **+** ▸ choisir une image
3. **Génération** ▸ **Générer le modèle 3D**

Compte **20 à 40 minutes la première fois** : le kernel Kaggle clone le dépôt
Hunyuan3D et télécharge le modèle avant de calculer. Suivi en direct sur
`https://www.kaggle.com/code/elbloody/img23d-worker`.

Ce que fait l'extension, dans l'ordre — utile pour situer une panne :

| Étape | Commande / action | Où ça casse typiquement |
|---|---|---|
| 1 | écrit un kernel autonome (images en base64 + paramètres) | images trop lourdes : garde-fou à 8 Mo |
| 2 | `kaggle kernels push -p <dossier>` | droits, quota, slug invalide |
| 3 | `kaggle kernels status <slug>` en boucle | file d'attente longue, délai de 45 min |
| 4 | `kaggle kernels output <slug> -p <dossier>` | kernel en erreur : lire ses logs |
| 5 | import du GLB, remesh, export STL | voir §6 |

Le contenu du kernel généré est dans `backends/kaggle.py`, constante
`KERNEL_TEMPLATE`, et son installation dans `DEFAULT_SETUP`. **Si le kernel
échoue côté Kaggle, c'est là qu'il faut corriger** — pas dans l'extension.
`setup_commands` permet de surcharger ces commandes sans toucher au code.

---

## 5. Bugs déjà trouvés et corrigés — ne les cherche pas

Chacun a coûté un aller-retour. Ils sont corrigés et couverts par des tests.

| Symptôme | Cause réelle |
|---|---|
| `No module named hy3dgen` sur Kaggle | `hy3dgen` n'est pas sur PyPI : le kernel clone le dépôt Hunyuan3D |
| attente jusqu'au délai max sur un kernel terminé | le CLI 2.2.x affiche `status "KernelWorkerStatus.COMPLETE"`, plus `"complete"` |
| « Aucune identification Kaggle » malgré une session valide | check() énumérait les fichiers connus ; il lance maintenant le CLI et rapporte ce qu'il dit |
| « CLI n'a pas abouti » sur un compte neuf | la sonde était `kernels list`, qui rend « Not found » sans notebook. C'est `config view` désormais |
| le test portait sur le mauvais backend | remplir une section ne la sélectionne pas ; le compte rendu nomme le backend testé |
| détail d'erreur invisible dans les Préférences | seule la vue 3D était redessinée |
| boutons grisés définitivement | drapeau « occupé » resté levé ; il est croisé avec l'existence d'un travail réel |
| chemin du CLI à ressaisir à chaque mise à jour | il est retrouvé automatiquement dans les emplacements habituels |
| Blender figé au remesh | garde-fou à 1000 voxels par côté, estimation affichée avant le clic |

---

## 6. Ce qui est vérifié, et ce qui ne l'est pas

**Vérifié** — 110 tests unitaires (sans Blender, ~10 s) et 22 tests
d'intégration qui installent l'extension dans un vrai Blender, sur **4.2 et
5.0** :

- préparation des images par Blender, import GLB, remesh, étanchéité
- export STL relu octet par octet, à la bonne échelle en millimètres
- backend HTTP complet contre un serveur conforme au protocole
- plomberie Kaggle (génération du kernel, sondage, récupération) contre un
  faux CLI reproduisant le comportement du vrai, lu dans son code source

**Jamais vérifié en conditions réelles** — c'est ta valeur ajoutée :

- une génération Kaggle de bout en bout, avec un vrai GPU
- les API **Meshy / Tripo / Rodin** : écrites d'après leurs schémas publics,
  testées seulement contre des serveurs simulés, faute de clés. Si
  l'utilisateur en a une, c'est une vérification utile — le champ *Point
  d'entrée* permet de corriger l'URL sans toucher au code
- le backend **LOCAL** : sa plomberie est testée, jamais une vraie inférence
- **Blender 5.2** précisément (5.0 et 4.2 le sont)

---

## 7. Comment travailler

```bash
git clone https://github.com/elbloody/blender-img23d
cd blender-img23d && git checkout claude/extension-code-akmtu0

# Tests rapides, sans Blender
python -m unittest discover -s tests -t .
ruff check --select F,E9,B,SIM .

# Tests d'intégration dans un vrai Blender
blender --background --python tests/blender/test_integration.py

# Construire le zip installable
blender --command extension build --source-dir . --output-dir dist
```

Sans Blender installé en ligne de commande, le module pip suffit et c'est
ainsi que tout a été validé jusqu'ici :

```bash
python -m venv /tmp/bpy5 && /tmp/bpy5/bin/pip install bpy==5.0.1
/tmp/bpy5/bin/python tests/blender/test_integration.py
```

**Installation dans Blender** : Edit ▸ Preferences ▸ Get Extensions ▸ flèche ▾
▸ *Install from Disk…* ▸ choisir le zip. Réinstaller par-dessus remplace
l'ancienne version.

### Règles de travail

- **Ne remets pas de chemin absolu** dans *Commande kaggle* : il est retrouvé
  tout seul, et le champ se vide à chaque réinstallation.
- **Toute correction vient avec un test.** Chaque bug du §5 en a un.
- **Vérifie dans Blender avant de livrer** : un import circulaire est passé au
  travers des tests unitaires et empêchait l'extension de se charger.
- Attention à `obj.dimensions` : périmé tant que le depsgraph n'a pas été
  réévalué. Pour une décision, mesurer la géométrie
  (`printprep.largest_dimension_mm`).
- Rien de coûteux dans un `draw()` : pas de `bmesh`, pas de sous-processus.
- Le travail long passe par `core/jobs.py` et un opérateur modal ; la scène ne
  se touche que depuis le thread principal.

---

## 8. Sécurité

Un jeton d'API Kaggle (`KGAT_0b5f044f5482721caa2e7a0e9ec2f943`) est apparu en
clair dans une capture d'écran partagée pendant les échanges. **Il doit être
révoqué** : kaggle.com ▸ Settings ▸ API Tokens ▸ *Generate New Token* invalide
l'ancien. La session OAuth actuelle n'en dépend pas, donc rien ne casse.

Les clés et jetons vivent dans les préférences utilisateur de Blender, jamais
dans le fichier `.blend` : un `.blend` se partage, pas une clé. Le `.gitignore`
exclut `kaggle.json`, `.env` et `*.secret`.

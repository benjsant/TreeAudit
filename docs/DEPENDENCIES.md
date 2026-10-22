# Dépendances

Commencer par le minimum, verrouiller les versions avec empreintes (`uv.lock`, D-017), et justifier ici toute nouvelle dépendance. Rien n'est installé sur l'hôte : tout s'exécute dans Docker (D-019).

## Outils (images Docker uniquement)
| Outil | Usage | Remarque |
|---|---|---|
| `python:3.14-slim` | Image de base | Épinglée par digest (D-031) |
| `ghcr.io/astral-sh/uv` | Résolution, verrouillage et installation des dépendances | Épinglée par version et digest ; absente de l'image finale |
| Python autonome (python-build-standalone) | Interpréteur embarqué dans l'AppImage (D-038) | Téléchargé et vérifié par `uv python install` (version de uv épinglée) |
| `squashfs-tools` (Debian) | Image compressée de l'AppImage | Paquet signé de l'image de construction, absent de l'AppImage |
| AppImage type2-runtime | Programme de démarrage de l'AppImage (statique, sans `libfuse2`) | Épinglé par empreinte SHA-256 : un changement arrête la construction |

## Bibliothèque standard — priorité
- `os` (`scandir`, `open` avec `dir_fd`, `O_NOFOLLOW`, `O_DIRECTORY`, `O_NOATIME`), `stat` : parcours et métadonnées.
- `pathlib` : manipulation de chemins hors parcours (configuration, affichage).
- `hashlib` : SHA-256.
- `tarfile`, `gzip`, `lzma` : archives vérifiées (D-032). Pour l'écriture, seuls `TarInfo.tobuf()` et les compresseurs servent ; la relecture se fait en mode flux, sans rien extraire.
- `re` : regex Python.
- `fnmatch` : glob de nom.
- `sqlite3` : index local.
- `tomllib` : lecture de la configuration.
- `argparse` : ligne de commande.
- `threading` : thread de travail et annulation.
- `logging`, `dataclasses`, `typing`, `enum`.
- `ctypes` (D-038), **uniquement** dans `core/sandbox.py`, pour appeler `syscall` (Landlock) et `prctl` (seccomp, `no_new_privs`) : la bibliothèque standard n'a pas d'interface pour eux. Un test vérifie qu'aucun autre module n'importe `ctypes`, et que ce module n'appelle que ces deux fonctions.

## Dépendances d'exécution
| Paquet | Usage | Phase | Remarque |
|---|---|---|---|
| `fastapi` | API locale, validation des paramètres | 1 | Apporte `starlette` et `pydantic` |
| `uvicorn` | Serveur ASGI | 1 | **Sans** l'extra `[standard]` (voir ci-dessous) |
| `jinja2` | Templates HTML | 6 | Autoéchappement obligatoire, `StrictUndefined` |

## Dépendances de développement
| Paquet | Usage |
|---|---|
| `pytest` | Tests |
| `httpx` | Requis par le `TestClient` de FastAPI |
| `ruff` | Lint et formatage |
| `mypy` | Vérification des annotations de type |
| `pyyaml`, `types-PyYAML` | Analyse de `compose.yaml` dans les tests de sécurité uniquement |

## Écartées
| Paquet | Raison | Alternative |
|---|---|---|
| `uvicorn[standard]` | Ajoute uvloop, httptools, watchfiles, websockets, python-dotenv, pyyaml : inutiles pour un usage local mono-utilisateur | `uvicorn` seul |
| `click`, `typer` | Ligne de commande simple | `argparse` |
| `sqlalchemy`, ORM | Schéma simple, contrôle fin des lots | `sqlite3` |
| `pydantic-settings`, `python-dotenv` | Configuration simple | `tomllib` + dataclasses |
| `psutil` | Inutile pour lire les montages, et peu fiable dans Docker | `/proc/self/mountinfo` |
| htmx, React, Vue… | Interface simple | HTML + JavaScript natif |
| Celery, Redis | Un seul utilisateur local | `threading` |
| `landlock`, `pyseccomp`, `python-prctl` | Une dépendance de plus, compilée ou peu suivie, pour quelques appels système | `ctypes` dans `core/sandbox.py` (D-038) |

## À évaluer plus tard, sur mesure
- `blake3` ou `xxhash` : empreintes plus rapides ; uniquement si un benchmark montre que SHA-256 est le facteur limitant (le disque l'est souvent).
- `regex` : délai maximal pour les expressions, si le ReDoS devient un problème réel.
- `pathspec` : exclusions de style `.gitignore`, si le besoin apparaît.
- `rapidfuzz` : comparaison approximative de noms, seulement si les heuristiques le justifient (phase 7).
- Ollama ou autre runtime local : phase 9, après validation du moteur sans IA.

## Politique regex
- Utiliser `re`.
- Compiler chaque motif une fois par recherche.
- Intercepter `re.error` et afficher une erreur compréhensible.
- Distinguer nom de fichier, chemin relatif, nom de dossier et contenu de fichier.
- Option de recherche insensible à la casse.
- Une regex ne sert jamais à autoriser ou interdire un chemin.
- Les motifs utilisateur ne sont jamais exécutés dans un shell.
- Prévisualiser le nombre de correspondances et quelques exemples avant d'appliquer le filtre.

## Toute nouvelle dépendance
Documenter le besoin, l'état de maintenance, la licence, les risques, l'alternative standard et la manière de la tester. Ajouter une entrée dans [DECISIONS.md](DECISIONS.md) si elle est structurante.

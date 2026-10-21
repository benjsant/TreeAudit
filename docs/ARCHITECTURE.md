# Architecture

## Vue générale
Application locale : un **moteur** Python indépendant (bibliothèque + ligne de commande), une **API web** FastAPI qui s'appuie sur ce moteur, et une base SQLite. Un seul dépôt, une seule image Docker pour l'application, utilisée par deux services (`app` et `gateway`).

Le moteur est construit et testé avant l'interface web : chaque fonctionnalité est d'abord utilisable et vérifiable en ligne de commande.

## Arborescence du dépôt

```
TreeAudit/
├── src/treeaudit/
│   ├── core/        configuration, modèles (dataclasses), erreurs, statuts de scan
│   ├── roots/       déclaration et validation des racines, lecture de mountinfo
│   ├── scanner/     parcours par descripteurs, métadonnées, erreurs, annulation, progression
│   ├── db/          schéma SQLite, migrations simples, écritures par lots
│   ├── filters/     extension, glob, regex
│   ├── duplicates/  lecture sûre, empreintes, groupes, protections, copie à garder (D-015, D-026)
│   ├── parasites/   catalogue rules.toml, détection sur l'index, commandes proposées (D-023, D-025)
│   ├── reports/     JSON, CSV, puis HTML
│   ├── web/         app.py (assemblage), api.py (JSON), pages.py + templates/ (HTML), exports.py,
│   │                jobs.py (tâches longues), queries.py, search_form.py, static/ (CSS, un JS)
│   ├── gateway.py   relais TCP entre le port publié et le conteneur app (D-020)
│   └── cli.py       point d'entrée en ligne de commande (argparse)
├── tests/
│   ├── unit/
│   ├── integration/
│   └── security/
├── docker/          Dockerfile, configuration du conteneur
├── docs/
├── compose.yaml, compose.selinux.yaml
├── Makefile         raccourcis vers docker compose (D-019)
├── pyproject.toml, uv.lock
```

Disposition `src/` : les tests s'exécutent sur le paquet installé, pas sur le dossier source, ce qui évite les imports accidentels.

Règle de dépendance entre modules : `web` et `cli` dépendent du moteur ; le moteur ne dépend jamais de `web`. Seul `db` écrit sur disque, et uniquement dans le dossier de données ; seule exception, `archive/output.py` écrit les archives vérifiées dans le dossier d'archives (D-032).

## Configuration
Fichier TOML lu avec `tomllib` (bibliothèque standard) :

```toml
data_dir = "/home/user/.local/share/treeaudit"
# archive_dir = "/mnt/Archives/treeaudit"   (optionnel, D-032)

[[roots]]
id = "backup-2019"
path = "/mnt/Data/Backup2019"
# allow_system_volume = false   (défaut)
# cross_mounts = false          (défaut)
```

En Docker, `path` vaut `/audit/roots/<id>`, `data_dir` vaut `/data` et `archive_dir` vaut `/archives` (monté seulement si `TREEAUDIT_ARCHIVE_PATH` est défini).

## Flux d'un scan
1. L'utilisateur demande un scan d'une racine par son identifiant (CLI ou API).
2. La racine est revalidée (D-009) ; un scan est créé en base avec l'état `pending`.
3. Un thread de travail passe le scan en `running` et parcourt la racine itérativement (pile de dossiers, pas de récursion Python), en ouvrant chaque dossier par descripteur avec `O_NOFOLLOW`.
4. Pour chaque entrée : métadonnées issues de `lstat` (type, tailles, dates, mode, inode, périphérique), nom en octets. Les erreurs sont enregistrées avec le chemin relatif et le code `errno`.
5. Écriture par lots dans SQLite (transactions de quelques milliers d'entrées).
6. La progression (entrées vues, octets, erreurs, dossier courant) est publiée dans un état partagé lu par l'API.
7. L'annulation (`threading.Event`) est vérifiée entre chaque dossier et chaque lot.
8. À la fin, le scan est marqué `complete`, `incomplete`, `cancelled` ou `failed` (D-012), avec ses compteurs, erreurs et exclusions.
9. Les analyses (doublons, filtres, rapports) travaillent ensuite sur l'index, pas sur le disque, sauf pour calculer les empreintes.

## Modèle de données (schéma v1, `src/treeaudit/db/schema.py`)
- `scan` : racine, sous-dossier, état (D-012), dates, options, compteurs (entrées, fichiers, dossiers, liens, autres, tailles logique et allouée, erreurs, exclusions), cause d'un échec.
- `entry` : une ligne par entrée vue. Clé `(scan_id, entry_id)`, parent, profondeur, nom et chemin relatif en octets (`BLOB`) avec leur forme affichable, type, extension (fichiers), tailles, `mtime_ns`, mode, uid, gid, périphérique, inode, nombre de liens, cible d'un lien symbolique.
- `dir_summary` : totaux du contenu de chaque dossier entièrement parcouru (D-022).
- `scan_error` : chemin, opération (`open_dir`, `list_dir`, `stat_entry`…), `errno`, message système.
- `scan_exclusion` : chemin et motif (point de montage).
- `dup_run` (schéma v3) : analyse de doublons d'un scan, état (D-012), options, fichiers et octets lus, erreurs.
- `file_hash` : empreintes partielle et complète d'un inode représentatif ; jamais de contenu.
- `dup_error` : fichiers non lus ou exclus (modifiés, remplacés, illisibles), avec l'opération et `errno`.
- Schéma v4 : `file_hash.tail` (empreinte de la fin), `dup_run.reused`, `summary_cache` (résumés des analyses terminées), index `entry_by_identity` (périphérique, inode) pour réutiliser les empreintes ; index des chemins et des extensions supprimés (D-030).

Mode WAL, clés étrangères actives, version du schéma dans `PRAGMA user_version` ; une base plus récente que le code est refusée. Les totaux finaux d'un scan sont recalculés en SQL sur ce qui a réellement été enregistré, ce qui garantit qu'un scan annulé ou incomplet n'annonce jamais plus que ce qu'il a vu. Chaque scan est un instantané indépendant.

## Interface web (D-028)
- Pages HTML rendues par Jinja2 ; un seul fichier JavaScript natif (lancement des tâches, suivi de la progression par interrogation périodique, sélections, génération et copie des commandes).
- API JSON sous `/api/`, exports sous `/export/`, pages ailleurs. Une erreur s'affiche en JSON pour l'API et les exports, en page HTML sinon.
- Pas de framework frontend, pas d'outil de build JavaScript.

## Choix techniques
- Python ≥ 3.12 (D-017).
- FastAPI + Uvicorn pour l'API locale.
- `sqlite3`, `hashlib` (SHA-256), `re`, `fnmatch`, `tomllib`, `argparse`, `logging` de la bibliothèque standard.
- `pytest`, `httpx`, `ruff`, `mypy` pour la qualité.
- Docker : `app` (données, réseau interne sans sortie) et `gateway` (port publié, aucune donnée), plus `dev` et `check` sans réseau (D-019, D-020). Un éventuel runtime LLM serait un service séparé et facultatif.

## À ne pas anticiper
- Pas de framework frontend lourd sans prototype justifié.
- Pas de base de données serveur ni de service cloud.
- Pas de file de tâches externe (Celery, Redis) : un thread suffit pour un utilisateur local.
- Pas de LLM avant la validation du moteur déterministe.
- Pas de logique Windows dans le moteur V1.

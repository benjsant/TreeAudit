# Architecture

## Vue générale
Application locale : un **moteur** Python indépendant (bibliothèque + ligne de commande), une **API web** FastAPI qui s'appuie sur ce moteur, et une base SQLite. Un seul dépôt, un seul service Docker.

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
│   ├── duplicates/  regroupement par taille, empreintes, groupes de doublons
│   ├── techdirs/    règles déclaratives des dossiers techniques (phase 6)
│   ├── reports/     JSON, CSV, puis HTML
│   ├── web/         application FastAPI, routes, templates, fichiers statiques
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

Règle de dépendance entre modules : `web` et `cli` dépendent du moteur ; le moteur ne dépend jamais de `web`. Seul `db` écrit sur disque, et uniquement dans le dossier de données.

## Configuration
Fichier TOML lu avec `tomllib` (bibliothèque standard) :

```toml
data_dir = "/home/user/.local/share/treeaudit"

[[roots]]
id = "backup-2019"
path = "/mnt/Data/Backup2019"
# allow_system_volume = false   (défaut)
# cross_mounts = false          (défaut)
```

En Docker, `path` vaut `/audit/roots/<id>` et `data_dir` vaut `/data`.

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

## Modèle de données (provisoire, précisé en phase 2)
- `root` : identifiant, chemin, options.
- `scan` : identifiant, racine, état, dates de début et de fin, compteurs, paramètres utilisés.
- `entry` : scan, parent, nom en octets (`BLOB`), nom affichable, type, taille logique, taille allouée, `mtime_ns`, mode, inode, périphérique, nombre de liens, cible du lien (texte).
- `scan_error` : scan, chemin relatif, opération, `errno`, message.
- `scan_exclusion` : scan, chemin relatif, motif (point de montage, règle d'exclusion…).
- `hash` : entrée, type (partielle ou complète), algorithme, valeur, `mtime_ns` et taille au moment du calcul.

Mode WAL, clés étrangères actives. Chaque scan est un instantané indépendant : un nouveau scan ne modifie pas les anciens.

## Interface web
- Pages HTML rendues par Jinja2 ; JavaScript natif minimal (suivi de la progression par interrogation périodique, filtres).
- Pas de framework frontend, pas d'outil de build JavaScript.

## Choix techniques
- Python ≥ 3.12 (D-017).
- FastAPI + Uvicorn pour l'API locale.
- `sqlite3`, `hashlib` (SHA-256), `re`, `fnmatch`, `tomllib`, `argparse`, `logging` de la bibliothèque standard.
- `pytest`, `httpx`, `ruff`, `mypy` pour la qualité.
- Un seul service Docker. Un éventuel runtime LLM serait un service séparé et facultatif.

## À ne pas anticiper
- Pas de framework frontend lourd sans prototype justifié.
- Pas de base de données serveur ni de service cloud.
- Pas de file de tâches externe (Celery, Redis) : un thread suffit pour un utilisateur local.
- Pas de LLM avant la validation du moteur déterministe.
- Pas de logique Windows dans le moteur V1.

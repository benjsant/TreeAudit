# TreeAudit

Application locale d'audit, d'analyse et d'aide au rangement des fichiers et dossiers, pour Linux.

TreeAudit parcourt une arborescence explicitement autorisée, indexe ses métadonnées et aide à comprendre ce qu'elle contient : répartition de l'espace, doublons exacts, dossiers techniques (`.venv`, `node_modules`, `__pycache__`…), arborescences proches. L'outil **présente des preuves et des explications ; il ne modifie jamais les données auditées**.

> **État : pré-alpha.** Implémenté en ligne de commande : validation des racines autorisées, conteneurs durcis, inventaire (parcours, tailles, extensions, erreurs, statut du scan) détection des dossiers parasites avec indice de confiance, doublons exacts et micro-nettoyage, avec commandes de nettoyage proposées, et recherche par extension, glob, regex, taille ou type. Pas encore d'interface web. Les autres fonctionnalités ci-dessous sont des objectifs.

## Pourquoi un outil de plus ?

Des outils comme Czkawka trouvent très bien les doublons. TreeAudit vise plutôt l'**audit de dossiers** : comprendre la structure d'un disque de sauvegarde, repérer les projets de développement et leurs artefacts régénérables, comparer des arborescences, et expliquer chaque résultat en distinguant les faits (doublon exact) des heuristiques (dossiers similaires).

## Objectifs de la V1

- Inventorier une ou plusieurs racines explicitement autorisées.
- Compter fichiers, dossiers, tailles (logique et allouée) et extensions.
- Signaler toutes les erreurs d'accès ; un scan partiel n'est jamais présenté comme complet.
- Filtrer par nom, extension, chemin, glob ou expression régulière Python.
- Détecter les fichiers strictement identiques (taille, puis empreinte SHA-256).
- Signaler les dossiers techniques probablement régénérables, sans jamais conclure qu'ils sont inutiles.
- Produire des rapports locaux exploitables.

## Principes

- **Lecture seule** : aucune suppression, aucun déplacement, renommage, fusion ou compression.
- **Accès confiné** : seules les racines déclarées sont analysées ; les liens symboliques ne sont pas suivis ; le volume système est refusé par défaut.
- **Local** : serveur lié à `127.0.0.1`, aucune donnée envoyée à un service distant.
- **Explicable** : chaque résultat indique pourquoi il a été produit.
- **Sans `sudo`** : l'outil ne contourne jamais les permissions.

Voir [docs/SECURITY.md](docs/SECURITY.md) pour le modèle de sécurité et ses limites.

## Plateformes

- **V1 : Linux uniquement.** Validation sur Linux Mint (base Ubuntu, AppArmor) et Fedora/Nobara (SELinux possible).
- Windows et macOS : non pris en charge. Un portage Windows pourra être étudié en V2.

## Hors périmètre V1

- Toute opération destructive ou modification des fichiers.
- Analyse de l'ensemble du système hôte.
- Exécution de commandes proposées par une IA.
- Synchronisation ou envoi de fichiers vers un service distant.

## Démarrage (Docker)

Prérequis : Docker avec le plugin Compose, et `make`. Rien d'autre n'est installé sur la machine.

```bash
make env          # crée .env avec ton UID/GID
# éditer .env : TREEAUDIT_ROOT_PATH=/chemin/absolu/du/dossier/à/auditer
make up           # vérifie le dossier sur l'hôte, puis démarre sur http://127.0.0.1:8765
```

Le dossier est monté **en lecture seule**. Sous SELinux en mode enforcing : `make up COMPOSE_SELINUX=1` (voir [D-011](docs/DECISIONS.md)).

Inventaire en ligne de commande : `make scan` (tout le dossier) ou `make scan SUBPATH=sous/dossier`, puis `make scans` pour lister les scans enregistrés.

Dossiers parasites du dernier scan : `make parasites`. Pour obtenir une commande prête à copier : `make parasites SELECT=green` (corbeille), `SELECT=1,4-6 ACTION=delete` ou `ACTION=archive`. **TreeAudit ne supprime jamais rien lui-même** : il affiche la commande, tu décides de la lancer.

Doublons exacts du dernier scan : `make dupes` (fichiers d'au moins 1 Kio) ou `make micro` (petits fichiers identiques et fichiers vides). Pour une commande : `make dupes-show SELECT=all` (les fichiers vides ne sont jamais inclus dans `all`), ou `SELECT=G3 KEEP=2 ACTION=delete`. Les copies situées dans un dépôt git, un jeu, un environnement Python ou `node_modules` ne sont jamais proposées.

Recherche dans le dernier scan : `make search ARGS="--ext jpg,png --min-size 5M"`, `ARGS="--path-glob 'projets/**/*.py'"` ou `ARGS="--regex '^IMG_\d{4}' -i"`. Voir [FILTERS_AND_REGEX.md](docs/FILTERS_AND_REGEX.md).

Développement : `make test`, `make lint`, `make typecheck`, ou `make check` pour tout lancer. `make help` liste les commandes.

## Documentation

| Document | Contenu |
|---|---|
| [PROJECT_SPEC.md](docs/PROJECT_SPEC.md) | Spécification fonctionnelle et définitions |
| [SECURITY.md](docs/SECURITY.md) | Modèle de sécurité, menaces, limites |
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | Modules, flux d'un scan, modèle de données |
| [DECISIONS.md](docs/DECISIONS.md) | Journal des décisions datées |
| [DEPENDENCIES.md](docs/DEPENDENCIES.md) | Dépendances retenues et écartées |
| [FILTERS_AND_REGEX.md](docs/FILTERS_AND_REGEX.md) | Sémantique des filtres |
| [ROADMAP.md](docs/ROADMAP.md) | Phases de développement |
| [TEST_PLAN.md](docs/TEST_PLAN.md) | Plan de tests |
| [CLAUDE_WORKFLOW.md](docs/CLAUDE_WORKFLOW.md) | Méthode de travail avec Claude Code |

## Licence

[MIT](LICENSE)

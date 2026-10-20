# TreeAudit

**Fais le tri dans tes disques, sans risque.** TreeAudit analyse un dossier ou un disque (par exemple une vieille sauvegarde) et te montre ce qui prend de la place, ce qui est en double et ce qui peut être nettoyé, en expliquant chaque résultat. Application locale pour Linux, avec une interface web et une ligne de commande.

> [!WARNING]
> **Mise en garde — à lire avant utilisation**
>
> - **Projet en pré-alpha.** Il fonctionne et il est testé, mais il est jeune : des erreurs restent possibles. Il est fourni **« tel quel », sans aucune garantie** (voir la [licence MIT](LICENSE)).
> - **TreeAudit ne supprime, ne déplace et ne modifie jamais rien.** Il lit, il analyse, et il **propose** des commandes (corbeille, suppression, archive). **Ces commandes, elles, agissent pour de vrai quand tu les lances.** Relis toujours une commande avant de l'exécuter.
> - **Préfère la corbeille** (proposée par défaut, réversible) à la suppression définitive.
> - **Aie une sauvegarde à jour** de ce qui compte avant tout nettoyage. TreeAudit n'est pas un outil de sauvegarde.
> - Les pourcentages de confiance des « dossiers à nettoyer » sont des **estimations** fondées sur des indices (présence d'un `package.json`, d'un `pyvenv.cfg`…), pas des certitudes. Un dossier vert peut exceptionnellement contenir quelque chose d'important.
> - Pour aller plus vite, une recherche de doublons réutilise les empreintes d'une analyse précédente si un fichier a le même inode, la même taille et la même date de modification (le critère de rsync). Un fichier modifié par un outil qui lui remet exactement la même date ne serait pas relu.
> - L'index local (`data/`) contient **les chemins de tes fichiers** : il reste sur ta machine, mais ne le partage pas.
> - Testé à ce jour sous **Fedora / Nobara** avec Docker, sur SSD et sur un disque USB 5 400 tours. Les autres distributions Linux ne sont pas encore vérifiées.

## Ce que fait TreeAudit

| Fonctionnalité | État |
|---|---|
| Analyse d'un dossier : tailles, nombre de fichiers, types, éléments inaccessibles signalés | ✅ disponible |
| Exploration de l'espace, dossier par dossier | ✅ disponible |
| **Dossiers et fichiers à nettoyer** : caches, `node_modules`, `.venv`, `__pycache__`, `__MACOSX`, `.DS_Store`, `Thumbs.db`… (41 règles), avec un pourcentage de confiance expliqué | ✅ disponible |
| **Doublons exacts** (empreinte SHA-256), copie à garder suggérée, copies dans les logiciels, jeux et dépôts git protégées | ✅ disponible |
| **Petits fichiers en double** et fichiers vides (« micro-nettoyage ») | ✅ disponible |
| Recherche : nom, extension, motif, regex, taille, type | ✅ disponible |
| Commandes de nettoyage prêtes à copier (corbeille, suppression, archive) | ✅ disponible |
| Exports JSON et CSV | ✅ disponible |
| Archivage intégré et vérifié avant nettoyage | 🚧 prévu |
| Dossiers presque identiques (similarité) | 🚧 prévu |
| Aide d'une IA locale pour les cas ambigus (sans jamais rien exécuter) | 🚧 envisagé |
| Windows, macOS | ❌ pas en V1 |

Des outils comme [Czkawka](https://github.com/qarmin/czkawka) trouvent très bien les doublons. TreeAudit vise plutôt l'**audit de dossiers** : comprendre un vieux disque de sauvegarde, repérer les projets de développement et ce qui s'y recrée tout seul, et toujours **expliquer pourquoi** un élément est proposé.

## Démarrage rapide

**Prérequis :** Linux, [Docker](https://docs.docker.com/engine/install/) avec le plugin Compose, et `make`. Rien d'autre n'est installé sur ta machine : tout tourne dans des conteneurs.

```bash
git clone https://github.com/benjsant/TreeAudit.git
cd TreeAudit
make env      # crée le fichier .env avec ton identifiant utilisateur
```

Ouvre `.env` et indique le dossier à analyser (chemin absolu) :

```bash
TREEAUDIT_ROOT_PATH=/media/moi/MonDisqueExterne
```

Puis :

```bash
make up       # vérifie le dossier, puis démarre TreeAudit
```

Ouvre **http://127.0.0.1:8765** dans ton navigateur. `Ctrl+C` ou `make down` arrête tout.

Le dossier est monté **en lecture seule** dans le conteneur : même en cas de bug, TreeAudit ne peut pas le modifier. Par défaut, un dossier situé sur le disque du système (par exemple dans ton dossier personnel) est refusé ; pour l'autoriser volontairement, mets `TREEAUDIT_ALLOW_SYSTEM_VOLUME=1` dans `.env` (voir D-009 dans [docs/DECISIONS.md](docs/DECISIONS.md)). Les dossiers système (`/`, `/etc`, `/home` entier…) restent toujours refusés. Sous SELinux en mode enforcing : `make up COMPOSE_SELINUX=1`.

## Utilisation

### Dans le navigateur

1. **Choisir le dossier** : navigue dans les sous-dossiers, puis « Analyser ».
2. **Analyse** : la progression s'affiche ; tu peux annuler.
3. **Bilan** : ce qui peut être nettoyé sans risque, les doublons, où va la place, et une recherche.
4. **Nettoyer** : coche ce que tu veux (les éléments sûrs le sont déjà), choisis « corbeille », copie la commande, colle-la dans un terminal et relis-la avant d'appuyer sur Entrée.

### En ligne de commande

| Commande | Effet |
|---|---|
| `make scan` | Analyse le dossier de `.env` (`SUBPATH=sous/dossier` pour une partie seulement) |
| `make scans` | Liste les analyses enregistrées |
| `make parasites` | Dossiers à nettoyer ; `SELECT=green` produit une commande « corbeille » |
| `make dupes` / `make micro` | Doublons (≥ 1 Kio) / petits fichiers en double et fichiers vides |
| `make dupes-show SELECT=all` | Commande pour les copies en trop (les fichiers vides ne sont jamais inclus) |
| `make search ARGS="--ext jpg --min-size 5M"` | Recherche ([syntaxe](docs/FILTERS_AND_REGEX.md)) |
| `make bench ARGS="--dupes"` | Mesure les performances, **en n'affichant que des chiffres** |
| `make help` | Toutes les commandes |

## Sécurité en bref

- **Lecture seule** et accès limité au dossier choisi ; les liens symboliques ne sont jamais suivis, même s'ils sont remplacés pendant l'analyse.
- **Rien ne sort de ta machine** : serveur accessible uniquement depuis `127.0.0.1`, et le conteneur qui lit tes fichiers n'a **aucun accès à Internet**.
- Conteneurs sans privilèges, sans capacités Linux, avec ton utilisateur (jamais root).
- Les noms de fichiers sont traités comme des données non fiables : l'interface et les commandes proposées sont conçues pour qu'un nom piégé ne puisse rien exécuter (échappement systématique, vérifié par des tests avec des noms comme `<img onerror=…>` ou `$(commande)`).
- TreeAudit ne demande jamais `sudo` et n'exécute aucune commande.

Détails, menaces couvertes et limites : [docs/SECURITY.md](docs/SECURITY.md).

## Performances

Pensé pour tourner sans à-coups sur une machine modeste (8 Go de RAM) et des disques externes mécaniques : lectures dans l'ordre physique du disque, lecture minimale des fichiers pour les doublons, empreintes réutilisées d'une analyse à l'autre.

| Mesure | Résultat |
|---|---|
| 1 million d'éléments (SSD) | analyse en 16 s (natif) à 23 s (Docker), moins de 80 Mio de mémoire |
| Disque USB 5 400 tours, à froid, 153 000 éléments | analyse 4,3 s, doublons 7,0 s |
| Deuxième recherche de doublons | quasi instantanée, aucun fichier relu |

Méthode et chiffres détaillés : [docs/ROADMAP.md](docs/ROADMAP.md) (phase 6 ter) et `make bench`.

## Limites connues

- Linux uniquement ; validé pour l'instant sous Fedora / Nobara.
- Les règles de « dossiers à nettoyer » et la protection des logiciels installés reposent sur des noms de dossiers connus : un logiciel installé dans un dossier au nom inattendu ne sera pas reconnu comme tel.
- Une très grande sélection produit une commande très longue.
- Interface en français uniquement.

## Documentation

| Document | Contenu |
|---|---|
| [PROJECT_SPEC.md](docs/PROJECT_SPEC.md) | Spécification fonctionnelle et définitions |
| [SECURITY.md](docs/SECURITY.md) | Modèle de sécurité, menaces, limites |
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | Modules, déroulement d'une analyse, modèle de données |
| [DECISIONS.md](docs/DECISIONS.md) | Journal des décisions datées |
| [ROADMAP.md](docs/ROADMAP.md) | Phases de développement et mesures |
| [FILTERS_AND_REGEX.md](docs/FILTERS_AND_REGEX.md) | Syntaxe de la recherche |
| [DEPENDENCIES.md](docs/DEPENDENCIES.md) | Dépendances retenues et écartées |
| [TEST_PLAN.md](docs/TEST_PLAN.md) | Plan de tests |
| [CLAUDE_WORKFLOW.md](docs/CLAUDE_WORKFLOW.md) | Méthode de travail (projet développé avec l'aide de Claude Code) |

## Contribuer

Les retours et signalements de problèmes sont bienvenus dans les [issues](https://github.com/benjsant/TreeAudit/issues). Pour le développement : `make check` lance le lint, la vérification des types et les tests, dans Docker.

## Licence

[MIT](LICENSE) — fourni « tel quel », sans garantie d'aucune sorte.

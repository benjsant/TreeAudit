# TreeAudit

**Fais le tri dans tes disques, sans risque.** TreeAudit analyse un dossier ou un disque (par exemple une vieille sauvegarde) et te montre ce qui prend de la place, ce qui est en double et ce qui peut être nettoyé, en expliquant chaque résultat. Application locale pour Linux, avec une interface web et une ligne de commande.

> [!WARNING]
> **Mise en garde — à lire avant utilisation**
>
> - **Projet en pré-alpha.** Il fonctionne et il est testé, mais il est jeune : des erreurs restent possibles. Il est fourni **« tel quel », sans aucune garantie** (voir la [licence MIT](LICENSE)).
> - **TreeAudit ne supprime, ne déplace et ne modifie jamais rien de ce qu'il analyse.** Il lit, il analyse, et il **propose** des commandes (corbeille, suppression, archive). **Ces commandes, elles, agissent pour de vrai quand tu les lances.** Relis toujours une commande avant de l'exécuter. Sa seule écriture, quand tu la demandes, est une archive vérifiée dans le dossier d'archives que tu as choisi.
> - **Préfère la corbeille** (proposée par défaut, réversible) à la suppression définitive.
> - **Aie une sauvegarde à jour** de ce qui compte avant tout nettoyage. TreeAudit n'est pas un outil de sauvegarde.
> - Les pourcentages de confiance des « dossiers à nettoyer » sont des **estimations** fondées sur des indices (présence d'un `package.json`, d'un `pyvenv.cfg`…), pas des certitudes. Un dossier vert peut exceptionnellement contenir quelque chose d'important.
> - Pour aller plus vite, une recherche de doublons réutilise les empreintes d'une analyse précédente si un fichier a le même inode, la même taille et la même date de modification (le critère de rsync). Un fichier modifié par un outil qui lui remet exactement la même date ne serait pas relu.
> - L'index local (`data/`) contient **les chemins de tes fichiers** : il reste sur ta machine, mais ne le partage pas.
> - Testé à ce jour sous **Fedora / Nobara** avec Docker, sur SSD et sur un disque USB 5 400 tours. Les autres distributions Linux ne sont pas encore vérifiées. Le choix des disques dans l'interface n'a pas encore été essayé avec un vrai disque USB.

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
| **Archive vérifiée** d'un dossier avant nettoyage (`.tar.gz`), relue et comparée fichier par fichier | ✅ disponible |
| Dossiers presque identiques (similarité) | 🚧 prévu |
| Aide d'une IA locale pour les cas ambigus (sans jamais rien exécuter) | 🚧 envisagé |
| Windows, macOS | ❌ pas en V1 |

Des outils comme [Czkawka](https://github.com/qarmin/czkawka) trouvent très bien les doublons. TreeAudit vise plutôt l'**audit de dossiers** : comprendre un vieux disque de sauvegarde, repérer les projets de développement et ce qui s'y recrée tout seul, et toujours **expliquer pourquoi** un élément est proposé.

## Démarrage rapide

**Prérequis :** Linux, [Docker](https://docs.docker.com/engine/install/) avec le plugin Compose. Rien d'autre n'est installé sur ta machine : tout tourne dans des conteneurs. **Aucun fichier de configuration à remplir.**

```bash
git clone https://github.com/benjsant/TreeAudit.git
cd TreeAudit
# branche d'abord les disques à trier
./treeaudit
```

Ouvre **http://127.0.0.1:8765** dans ton navigateur : tout se fait là. `Ctrl+C` ou `./treeaudit stop` arrête tout. SELinux en mode enforcing (Fedora par défaut) est détecté tout seul.

### Sans Docker : l'AppImage (recommandée si ton Linux est récent)

TreeAudit existe aussi en un seul fichier, sans Docker (décision D-038) :

```bash
./treeaudit appimage    # construit dist/TreeAudit-x86_64.AppImage, dans Docker
```

Double-clique sur le fichier, ou lance-le dans un terminal. Le navigateur s'ouvre ; le bouton **« ⏻ Quitter »** arrête TreeAudit.

- **Le noyau le garde en lecture seule** (Landlock et seccomp) : il ne peut écrire que dans son propre dossier (`~/.local/share/treeaudit/`), ne peut lancer aucun programme ni se connecter à quoi que ce soit. Il faut **Linux 6.7 ou plus**, sinon il refuse de démarrer : utilise alors Docker.
- Son index est séparé de celui de la version Docker.
- **Un disque branché pendant que TreeAudit tourne apparaît** dès que tu actualises la page de choix (bouton « 🔄 Actualiser ») : plus besoin de relancer, contrairement à la version Docker.

## Utilisation

### Dans le navigateur

1. **Choisir le disque** : TreeAudit propose les disques externes branchés, avec leur occupation. Navigue dans les dossiers, puis « Analyser ».
2. **Analyse** : la progression s'affiche, et tu peux annuler.
3. **Bilan** : ce qui peut être nettoyé sans risque, les doublons, où va la place, et une recherche.
4. **Explorer** : « 📂 Explorer les fichiers » se parcourt comme un gestionnaire de fichiers. Chaque élément porte ses repères (🧹 à nettoyer, 📑 en double, 🛡 protégé), et ⓘ ouvre sa fiche. Coche ce que tu veux dans n'importe quels dossiers : tout va dans le **🧺 panier**, qui prépare la commande.
5. **Ranger** : « 🗂 Ranger ce dossier » (dans l'explorateur) propose un rangement par catégories (Photos/2019, Documents, Projets…), avec un aperçu du résultat. Tu télécharges le script qui fait le rangement, et celui qui l'annule. Rien n'est écrasé, et un fichier modifié depuis l'analyse est laissé en place.
6. **Projets et logiciels** : la tuile « 📦 Projets et logiciels » du bilan liste les projets informatiques (JavaScript, Python, Rust, .NET, dépôts git…), les jeux et les logiciels, avec leur taille et la date de leur dernière modification réelle. Les dépendances réinstallées n'en tiennent pas compte. Trie par ancienneté pour retrouver les vieux projets. Leurs dossiers recréables (`node_modules`, environnements Python, compilations) sont proposés, et les sûrs sont déjà cochés.
7. **Archiver si besoin** : « 🗜 Archiver » crée une copie compressée et vérifiée du dossier. Tu la retrouves dans « 🗜 Mes archives ».
8. **Nettoyer** : coche ce que tu veux (les éléments sûrs le sont déjà), choisis « corbeille », copie la commande, colle-la dans un terminal et relis-la avant d'appuyer sur Entrée.

### Qu'est-ce qui est proposé ?

- **🏠 Ton dossier personnel**, en entier, caches cachés compris (`~/.cache`…). Les dossiers de secrets ne sont **jamais lus** : clés SSH et GPG, gestionnaires de mots de passe, profils de navigateur et de messagerie, identifiants de services en ligne.
- **💽 Les partitions de données du disque système**, comme `/mnt/Data`. Les partitions du système lui-même (`/`, `/boot`, `/home`, `/usr`, `/var`) ne sont jamais proposées.
- **🔌 Les disques externes** : ce qui est monté sous `/run/media/<toi>`, `/media/<toi>`, `/media` ou `/mnt`. TreeAudit remonte jusqu'au disque physique. En cas de doute (disque réseau, image ISO…), il ne propose pas le disque, et l'interface dit pourquoi.
- **Le dossier de TreeAudit est toujours ignoré**, même s'il se trouve sur un disque analysé : il est reconnu par son identité sur le disque, pas par son nom.
- Tout est monté **en lecture seule** : même en cas de bug, TreeAudit ne peut rien modifier.
- **Un disque doit être branché et ouvert** (monté, par exemple en l'ouvrant dans le gestionnaire de fichiers) **avant** de lancer TreeAudit. Sinon, relance-le (`Ctrl+C`, puis `./treeaudit`). Au démarrage, le terminal affiche la liste des disques trouvés.

### Les archives

Les archives sont rangées dans le dossier `archives/` de TreeAudit, sur ton ordinateur : c'est donc **une vraie sauvegarde** d'un disque externe. La page **« 🗜 Mes archives »** les liste.

TreeAudit vérifie d'abord l'espace libre, puis écrit l'archive sous un nom temporaire. Il la relit ensuite et compare chaque fichier à l'original, et ne la garde que si tout concorde : sinon, il la retire.

- **« Archive complète et vérifiée »** : l'archive contient tout le dossier.
- **`-INCOMPLET` dans le nom** : il manque des éléments (fichier illisible, fichier modifié pendant la lecture, fichier spécial…). La liste s'affiche. **Ne supprime pas l'original en te fiant à cette archive.**
- **Arrêt brutal** (coupure de courant…) : un fichier caché `.….partial` peut rester. C'est une archive inachevée ; « Mes archives » la signale, et tu peux la supprimer.
- **Restaurer** : `tar -xzf archive.tar.gz`. Pour un nom de fichier non UTF-8, GNU tar affiche un avertissement « mot clé inconnu hdrcharset » sans conséquence : le nom est restauré à l'identique.

### En ligne de commande (facultatif)

Tout passe par `./treeaudit cli …`, qui valide et monte les disques comme le navigateur. Les numéros d'analyse viennent de `scan list` et `dupes list`.

| Commande | Effet |
|---|---|
| `./treeaudit disks` | Disques proposés ou refusés, et pourquoi |
| `./treeaudit cli roots list` | Disques et dossiers analysables, avec leur identifiant (`ext-…`) |
| `./treeaudit cli scan run ext-… --path sous/dossier` | Analyse un disque (`--path` pour une partie seulement) |
| `./treeaudit cli scan list` / `scan show N` | Liste les analyses / affiche l'analyse N |
| `./treeaudit cli scan parasites N --select green` | Dossiers à nettoyer, avec une commande « corbeille » |
| `./treeaudit cli dupes run N` (`--micro`) | Doublons (≥ 1 Kio) ; `--micro` : petits fichiers en double et fichiers vides |
| `./treeaudit cli dupes show R --select all` | Commande pour les copies en trop (les fichiers vides ne sont jamais inclus) |
| `./treeaudit cli search N --ext jpg --min-size 5M` | Recherche ([syntaxe](docs/FILTERS_AND_REGEX.md)) |
| `./treeaudit cli archive run ext-… --path sous/dossier` | Archive vérifiée dans `archives/` |
| `./treeaudit cli bench ext-… --dupes` | Mesure les performances, **en n'affichant que des chiffres** |
| `./treeaudit help` | Toutes les commandes du lanceur |

### Ancien mode par fichier `.env` (expérimental, non testé)

> [!CAUTION]
> **Ancien mode, conservé à titre expérimental.** Il n'est plus testé ni recommandé : l'interface fait tout. Il reste disponible pour qui veut, par exemple, analyser un dossier du disque système ou ranger les archives ailleurs.

`./treeaudit env` crée un fichier `.env`, dont TreeAudit lit alors les variables :

| Variable | Effet |
|---|---|
| `TREEAUDIT_ROOT_PATH` | Un dossier précis à analyser (chemin absolu), monté en lecture seule et validé avant le montage. Les dossiers système (`/`, `/etc`, `/home` entier…) sont toujours refusés. |
| `TREEAUDIT_ALLOW_SYSTEM_VOLUME=1` | Autorise ce dossier même s'il est sur le disque du système (voir D-009 dans [docs/DECISIONS.md](docs/DECISIONS.md)). |
| `TREEAUDIT_ARCHIVE_PATH` | Un autre dossier d'archives, existant et dédié (par défaut : `archives/`). |
| `TREEAUDIT_EXTERNAL_DISKS=0` | Désactive la détection des disques externes et internes. |
| `TREEAUDIT_INTERNAL_DISKS=0` | Ne propose pas les partitions de données du disque système. |
| `TREEAUDIT_HOME=0` | Ne propose pas le dossier personnel. |
| `TREEAUDIT_PORT` | Autre port local (par défaut : 8765). |

Les mêmes variables peuvent être passées directement : `TREEAUDIT_ROOT_PATH=/chemin ./treeaudit`. `./treeaudit settings` affiche les réglages utilisés.

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
| 1 million d'éléments (SSD) | analyse en 17 s (natif) à 21 s (Docker), moins de 80 Mio de mémoire |
| Disque USB 5 400 tours, à froid, 153 000 éléments | analyse 4,3 s, doublons 7,0 s |
| Deuxième recherche de doublons | quasi instantanée, aucun fichier relu |

Méthode et chiffres détaillés : [docs/ROADMAP.md](docs/ROADMAP.md) (phase 6 ter) et `./treeaudit cli bench`.

## Limites connues

- Linux uniquement ; validé pour l'instant sous Fedora / Nobara.
- Les règles de « dossiers à nettoyer » et la protection des logiciels installés reposent sur des noms de dossiers connus : un logiciel installé dans un dossier au nom inattendu ne sera pas reconnu comme tel.
- Une très grande sélection produit une commande très longue.
- Archives : les dates sont gardées à la seconde ; un fichier ayant plusieurs liens physiques est copié en entier pour chaque nom ; un fichier creux occupe sa taille réelle avant compression.
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

Les retours et signalements de problèmes sont bienvenus dans les [issues](https://github.com/benjsant/TreeAudit/issues). Pour le développement : `./treeaudit check` lance le lint, la vérification des types et les tests, dans Docker.

## Licence

[MIT](LICENSE) — fourni « tel quel », sans garantie d'aucune sorte.

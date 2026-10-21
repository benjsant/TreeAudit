# Journal des décisions

Les décisions structurantes doivent être datées et motivées. Une règle de sécurité ne doit pas être modifiée silencieusement : toute révision ajoute une nouvelle entrée ou un amendement daté.

## D-001 — Linux d'abord
V1 Linux uniquement. Windows est réservé à une future V2.

*Amendement 2026-10-19* : deux environnements de validation, **Linux Mint** (base Ubuntu, AppArmor, ext4) et **Fedora/Nobara** (SELinux possiblement actif, btrfs possible). Le comportement doit être correct quel que soit le module de sécurité Linux actif (aucun, AppArmor, SELinux permissif ou enforcing). Voir D-011.

## D-002 — MVP en lecture seule
Aucune modification de fichier dans le MVP. La fiabilité des résultats est prioritaire.

## D-003 — Racine explicitement autorisée
La cible est explicitement choisie et montée en lecture seule dans Docker. La sécurité ne dépend pas seulement d'une liste d'exceptions.

## D-004 — IA non essentielle
Le moteur fonctionne sans LLM. L'IA aide à interpréter, elle ne contrôle pas les fichiers.

## D-005 — Regex comme filtre
Glob et regex filtrent noms/chemins. Les doublons sont détectés par comparaison de contenu/empreintes ; les deux mécanismes répondent à des besoins différents.

## D-006 — Interface locale
Interface web locale, liée à localhost par défaut, avec backend Python et SQLite. Un frontend lourd n'est pas retenu sans justification par prototype.

## D-007 — Mode natif et mode Docker (2026-10-19)
Le moteur fonctionne **en natif** (tests, CLI, usage direct) et **en Docker** (distribution recommandée).
- La sécurité est assurée **par l'application** : liste de racines déclarées, validation, parcours confiné. Elle ne repose pas sur Docker.
- Docker ajoute une seconde couche : seule la cible est visible, montée `:ro`.
- *Motif* : les tests tournent en natif ; une protection qui n'existerait que dans Docker ne serait jamais testée.

## D-008 — Sélection des racines à la configuration (2026-10-19)
Les racines sont déclarées dans un fichier de configuration (identifiant + chemin), pas choisies librement depuis l'interface web.
- L'API et l'interface désignent une racine par son **identifiant** ; aucun chemin absolu n'est accepté en paramètre.
- En Docker, chaque racine est montée sous `/audit/roots/<id>` en `:ro`. La validation se fait en deux temps :
  1. **Avant le montage, côté hôte** (`make check-root`, lancé automatiquement par `make up`) : le shell vérifie que le chemin existe, est un dossier et ne passe par aucun lien symbolique ; puis le contenu de `/proc/self/mountinfo` de l'hôte est transmis sur l'entrée standard à un conteneur **sans réseau ni volume**, qui applique les règles de D-009 (`treeaudit roots check --no-fs-checks --mountinfo -`).
  2. **Au démarrage du conteneur** : chaque racine doit être un dossier, sur un système de fichiers non virtuel, et son montage doit être **en lecture seule** d'après le `mountinfo` du conteneur ; sinon elle est marquée refusée.
- *Limite* : lancer `docker compose up` directement saute l'étape 1. L'étape 2 garantit tout de même la lecture seule. La documentation impose `make up`.
- *Motif* : un conteneur ne peut voir que ce qui a été monté à son démarrage ; une sélection libre depuis le navigateur supposerait un accès large au disque.

## D-009 — Validation des racines et volume système (2026-10-19)
Une racine est refusée si :
- son chemin n'est pas absolu, contient `..`, n'existe pas, n'est pas un dossier ou est un lien symbolique ;
- elle est égale à `/` ou à l'une des racines système sensibles (`/proc`, `/sys`, `/dev`, `/run`, `/boot`, `/etc`, `/usr`, `/var`, `/home`, `/root`, `/mnt`, `/media`, `/tmp`…, ou le dossier personnel de l'utilisateur en entier) ou en est un parent ;
- elle se trouve **à l'intérieur** de `/proc`, `/sys`, `/dev` ou `/run` (sauf `/run/media/`, où Fedora et GNOME montent les disques amovibles), ou sur un système de fichiers virtuel (`proc`, `sysfs`, `devtmpfs`, `cgroup2`, `debugfs`, `securityfs`…, d'après `mountinfo`) : ces arborescences exposent l'état du noyau et d'autres processus, pas des données utilisateur ;
- elle se trouve **sur le volume système** (celui qui porte `/`), sauf option explicite `allow_system_volume = true` pour cette racine, signalée dans tous les rapports ;
- elle contient le dossier de données de TreeAudit, ou est contenue dedans.

La détection du volume système lit `/proc/self/mountinfo` (source du montage et identifiant de périphérique), pas seulement `st_dev` : sur btrfs, chaque sous-volume a son propre `st_dev` alors qu'il est sur le même disque.
- *Motif* : auditer `~/Projets` sur le disque système est un besoin légitime, mais il doit être un choix explicite. La liste de chemins interdits n'est qu'un garde-fou complémentaire (règle 11 de `CLAUDE.md`).

## D-010 — Liaison réseau (2026-10-19)
- **Natif** : le serveur écoute sur `127.0.0.1` uniquement.
- **Docker** : uvicorn écoute sur `0.0.0.0` *à l'intérieur de l'espace réseau du conteneur* (nécessaire pour que le port publié réponde) ; le port est publié **uniquement** sur `127.0.0.1:8765` côté hôte. Un test vérifie le fichier Compose.
- Le serveur rejette les requêtes dont l'en-tête `Host` n'est pas `127.0.0.1:<port>` ou `localhost:<port>` (protection contre le DNS rebinding).
- *Motif* : une publication `"8765:8765"` exposerait le service sur toutes les interfaces, et Docker contourne les pare-feux de l'hôte (ufw, firewalld).

## D-011 — Docker et modules de sécurité Linux (2026-10-19)
- Les options de montage `:z` et `:Z` sont **interdites** sur les cibles : elles réétiquettent les fichiers de l'hôte (écriture d'attributs étendus), ce qui viole la lecture seule.
- Fichier Compose de base : sans option SELinux. Il fonctionne sous AppArmor (Mint) et sans SELinux actif.
- Sous SELinux enforcing, les lectures d'une cible non étiquetée sont refusées. Un fichier `compose.selinux.yaml` ajoute `security_opt: [label=disable]` à ce seul conteneur. Compromis documenté : on renonce au confinement SELinux du conteneur plutôt que de modifier les fichiers audités ; les autres protections restent actives.
- Podman et Docker rootless : non garantis en V1, à documenter s'ils sont testés.

## D-012 — Statuts de scan (2026-10-19)
- États transitoires : `pending`, `running`.
- États finaux :
  - `complete` : tout ce qui était dans le périmètre a été examiné. Les **exclusions décidées** (points de montage non traversés, filtres d'exclusion, liens non suivis) n'empêchent pas `complete` mais sont listées dans le rapport.
  - `incomplete` : au moins une **erreur subie** (permission, E/S, entrée disparue, limite atteinte) a empêché d'examiner une partie du périmètre.
  - `cancelled` : interrompu par l'utilisateur. Les résultats partiels restent consultables et marqués comme tels.
  - `failed` : arrêt sur erreur interne. Aucun résultat n'est présenté comme exploitable.
- Un scan resté `running` au redémarrage de l'application devient `failed`.

## D-013 — Points de montage et liens (2026-10-19)
- Les points de montage imbriqués ne sont **pas traversés** par défaut (comme `du -x`) ; ils sont enregistrés comme exclusions. Option par racine `cross_mounts = true`.
- Les liens symboliques ne sont jamais suivis. Ils sont enregistrés comme entrées de type lien, avec leur cible textuelle (`readlink`) à titre informatif ; cette cible n'est jamais résolue ni ouverte.
- Le parcours ouvre chaque dossier relativement à son parent avec `O_NOFOLLOW | O_DIRECTORY` (descripteurs de fichiers), ce qui empêche qu'un dossier remplacé par un lien pendant le scan fasse sortir de la racine.

## D-014 — Tailles, liens physiques et noms de fichiers (2026-10-19)
- On enregistre la taille logique (`st_size`) et la taille allouée (`st_blocks × 512`). La répartition de l'espace utilise la taille allouée.
- Un inode présent plusieurs fois (liens physiques) est compté une seule fois dans les totaux. Les liens physiques sont signalés à part et ne sont pas présentés comme des doublons qui consomment de l'espace.
- Sous Linux, un nom de fichier est une suite d'octets, pas forcément de l'UTF-8 valide. Le nom brut est stocké en `BLOB`, accompagné d'une forme affichable (caractères invalides remplacés et signalés).

## D-015 — Doublons exacts (2026-10-19)
Pipeline : regroupement par taille → empreinte partielle (SHA-256 des premiers et derniers 64 Kio) → SHA-256 complet sur les candidats restants.
- La comparaison octet à octet est **optionnelle et désactivée par défaut** : une collision SHA-256 accidentelle est négligeable, et la comparaison double les lectures.
- Un fichier dont la taille ou la date de modification change entre l'indexation et le calcul d'empreinte est marqué « modifié pendant l'analyse » et exclu des groupes.
- Les fichiers vides sont regroupés à part, sans calcul d'empreinte.

## D-016 — Langue (2026-10-19)
- Code, identifiants, commentaires, docstrings, messages de commit : **anglais**.
- Documentation (`README.md`, `docs/`) : **français**.
- Interface : français en V1 ; les textes sont centralisés pour permettre une traduction ultérieure.

## D-017 — Python et verrouillage des dépendances (2026-10-19)
- `requires-python = ">=3.12"` (Python de Linux Mint 22 ; Nobara fournit 3.14). L'intégration continue teste 3.12 et la dernière version stable.
- Image Docker : `python:3.13-slim`, épinglée par digest (remplacée par `python:3.14-slim`, voir D-031).

*Amendement 2026-10-19* : le verrouillage utilise **uv** (`uv.lock`, avec empreintes) au lieu de pip-tools. uv n'est utilisé **que dans les images Docker** : il est copié depuis l'image officielle `ghcr.io/astral-sh/uv`, épinglée par version et digest, et n'est pas présent dans l'image finale de l'application.
- *Motif* : un seul outil pour les dépendances et le verrouillage, résolution multiplateforme, reconstructions d'images rapides, aucune installation sur l'hôte (D-019).

## D-019 — Développement entièrement dans Docker (2026-10-19)
- Rien n'est installé sur la machine du développeur sans son accord explicite : ni paquets système, ni Python, ni uv, ni environnement virtuel.
- Un seul `docker/Dockerfile` multi-étapes produit deux images :
  - `dev` : outils de test et de qualité (pytest, ruff, mypy) ; le dépôt est monté en lecture-écriture dans `/app` ; aucune racine d'audit montée ; aucun accès réseau ;
  - `app` : image d'exécution minimale et durcie, sans uv ni dépendances de développement.
- Les commandes courantes passent par un `Makefile` (`make test`, `make lint`, `make up`…), qui appelle `docker compose`.
- Le mode natif (D-007) reste supporté par le code et testé, mais n'est pas installé sur la machine du développeur pour l'instant.
- *Conséquence* : sans extension *Dev Containers*, l'éditeur n'a pas d'interpréteur Python local ; ruff et mypy s'exécutent dans le conteneur.

## D-018 — Sécurité web locale (2026-10-19)
- Pas de CORS.
- Les requêtes qui modifient l'état (lancer ou annuler un scan) exigent `Content-Type: application/json` et l'en-tête `X-TreeAudit-Request: 1`, qu'un site tiers ne peut pas envoyer sans pré-requête CORS (refusée).
- Templates Jinja2 avec autoéchappement actif ; aucun `innerHTML` côté JavaScript ; en-tête `Content-Security-Policy: default-src 'self'`.
- *Motif* : un nom de fichier contrôlé par un tiers (ex. `<img onerror=…>`) sur un disque audité ne doit pas pouvoir exécuter du code dans l'interface.

## D-020 — Aucun accès sortant pour le conteneur qui lit les données (2026-10-19)
- Constat : un réseau Docker `internal: true` coupe l'accès sortant mais empêche aussi la publication du port vers l'hôte (vérifié avec Docker 29.9).
- Décision : le service `app` (qui voit les racines auditées) est placé sur un réseau interne sans sortie. Un service `gateway` distinct, **sans aucun montage de données**, publie le port sur `127.0.0.1` de l'hôte et relaie les connexions TCP vers `app`. Il est écrit avec la bibliothèque standard (`asyncio`), durci comme les autres services, et ne relaie que vers `app:8765`.
- *Motif* : l'engagement « aucun envoi distant » ne doit pas reposer uniquement sur le code. Un code compromis dans `app` ne peut plus joindre Internet ; la passerelle peut joindre le réseau mais n'a accès à aucune donnée.

## D-021 — Zone autorisée et dossier analysé (2026-10-19)
- La **racine** déclarée en configuration (et montée en lecture seule en Docker) est une **zone autorisée**, par exemple tout un disque de données.
- L'utilisateur choisit ensuite, dans la CLI (`--path`) puis dans l'interface (phase 5), **le sous-dossier à analyser** à l'intérieur de cette zone.
- Ce sous-dossier est transmis comme **chemin relatif** à la racine, jamais comme chemin absolu. Il est refusé s'il est vide de sens (`..`, `.`, composant vide, octet nul, chemin absolu), puis ouvert **composant par composant** depuis le descripteur de la racine avec `O_NOFOLLOW`. Aucun lien symbolique ne peut donc faire sortir de la zone.
- Les chemins relatifs enregistrés restent relatifs à la racine, pas au sous-dossier, pour que les scans d'une même zone soient comparables.
- *Motif* : concilier le choix du dossier depuis l'interface (souhait du mainteneur) avec un conteneur qui ne voit que ce qui a été monté à son démarrage.

## D-022 — Inventaire : règles de comptage (2026-10-19)
- Taille logique : somme des `st_size` des fichiers réguliers. Taille allouée : somme des `st_blocks × 512` de toutes les entrées (fichiers, dossiers, liens). Chaque inode n'est compté qu'une fois (couple périphérique + inode).
- Un inode à liens physiques multiples est compté **dans le premier dossier où le parcours le rencontre**. Les totaux d'un sous-dossier peuvent donc dépendre de l'ordre du parcours ; le total du scan, lui, n'en dépend pas.
- Un changement de numéro de périphérique qui ne correspond à aucun point de montage connu (sous-volume btrfs) n'est pas une exclusion : le parcours continue.
- Un point de montage n'est détecté que s'il existe au début du scan (lecture de `mountinfo`). Un montage créé pendant le scan n'est pas détecté : limite documentée.
- Les numéros d'inode et de périphérique sont des entiers non signés sur 64 bits ; ils sont convertis en entiers signés pour SQLite, sans perte.

## D-023 — Ordre des phases et dossiers parasites (2026-10-20)
- Priorité du mainteneur : repérer les dossiers « parasites » recréables (`.venv`, `__pycache__`, `node_modules`, `__MACOSX`…) et les doublons. Nouvel ordre : dossiers parasites (3), doublons exacts (4), filtres (5), interface web (6), similarité (7), validation Linux (8), IA locale (9).
- Chaque détection porte un **indicateur de confiance** en pourcentage, accompagné des règles et indices qui l'expliquent.
- TreeAudit **affiche** la commande de recréation et les options de suppression, mais **n'exécute jamais rien** : la V1 reste en lecture seule (D-002). Les chemins des commandes affichées sont échappés ; un nom qui ne peut pas être affiché sans risque (caractères de contrôle, octets invalides) n'a pas de commande proposée.
- Les détections ambiguës sont le cas d'usage visé pour l'IA locale (phase 9), qui propose sans jamais agir.
- Tests : arborescence synthétique `tools/make_dummy_tree.py`, jamais les données personnelles du mainteneur.

## D-024 — TreeAudit ne supprime jamais rien ; seule écriture future : des archives vérifiées (2026-10-20)
- TreeAudit ne supprime, ne déplace, ne renomme, ne fusionne et ne modifie **jamais** une donnée auditée, dans aucune version. Il **propose** des options de nettoyage sous forme de commandes que l'utilisateur lance lui-même (D-025).
- La seule écriture autorisée en dehors du dossier de données sera, dans une phase dédiée placée après l'interface web, la **création d'archives** (`.tar.gz` ou `.tar.xz`, bibliothèque standard) d'un dossier, à titre de sauvegarde avant nettoyage. Garde-fous obligatoires :
  1. archive écrite dans un dossier de sortie dédié, déclaré en configuration, qui ne chevauche aucune racine ; les racines restent en lecture seule ;
  2. écriture sous un nom temporaire, renommé uniquement une fois l'archive terminée ;
  3. vérification après écriture (relecture, comparaison de la taille et de l'empreinte de chaque fichier) avant tout « feu vert » ;
  4. espace disponible vérifié avant de commencer ;
  5. liens symboliques archivés comme des liens, jamais suivis ; fichiers spéciaux ignorés et signalés ;
  6. avertissement si l'archive est sur le même disque que l'original (ce n'est pas une vraie sauvegarde).
- En attendant, la commande d'archivage est seulement **affichée** (D-025).
- Cette décision amende D-002 et la règle 1 de `CLAUDE.md` ; la phase d'archivage aura sa propre revue de sécurité.

## D-025 — Commandes proposées, jamais exécutées (2026-10-20)
- Pour chaque détection, TreeAudit affiche : la commande de recréation (si connue), la mise à la corbeille (`gio trash`, réversible, proposée en premier), la suppression définitive (`rm -rf`) et l'archivage (`tar`).
- L'utilisateur peut sélectionner plusieurs détections (par numéro, ou toutes celles en vert) et obtenir **une seule commande**, écrite sur plusieurs lignes avec `\` en fin de ligne, prête à copier.
- Règles de génération :
  - chemins absolus de l'**hôte** (en Docker, le chemin hôte de la racine est transmis au conteneur uniquement pour l'affichage) ;
  - chaque chemin est échappé avec `shlex.quote` ; `--` sépare les options des chemins ; avec `tar`, les chemins relatifs sont préfixés par `./` ;
  - un chemin contenant des caractères de contrôle (retour à la ligne, tabulation…) ou de l'UTF-8 invalide ne reçoit **aucune commande**, avec une explication ;
  - TreeAudit n'exécute jamais ces commandes : aucun appel au shell n'existe dans le code (test statique).

## D-026 — Doublons : micro-nettoyage et copies protégées (2026-10-20)
- Analyse principale : fichiers d'au moins 1 Kio (`--min-size`). **Micro-nettoyage** (`--micro`) : fichiers de moins de 1 Kio et fichiers vides, triés par nombre d'éléments supprimables ; le but est de réduire aussi le nombre d'éléments.
- Une copie **protégée** n'est jamais proposée à la suppression ; elle reste affichée (🔒) avec sa raison :
  - fichier-marqueur (`__init__.py`, `.gitkeep`, `.keep`, `py.typed`, `.gitignore`, `.nojekyll`…) ;
  - fichier situé dans un dépôt git ;
  - fichier situé dans un logiciel ou des dépendances installés (`steamapps`, `compatdata`, `Program Files`, `drive_c`, `.wine`, `site-packages`, `node_modules`, `.venv`, `vendor`, `flatpak`, `snap`, dossiers `*.app`…) : les doublons y sont voulus (une copie par jeu, par environnement) et en retirer une casse le logiciel. Les dossiers de dépendances recréables se traitent en bloc avec `scan parasites`.
- **Fichiers vides** : ils ne libèrent aucune place et des programmes peuvent en avoir besoin. Ils sont regroupés par zone (les deux premiers niveaux de dossier) et ne sont **jamais** inclus dans `--select all` : chaque zone doit être désignée explicitement.
- Une copie à garder est suggérée pour chaque groupe, avec ses raisons (chemin sans « copie », « (1) », « backup », « old », « Downloads »… ; fichier le plus ancien ; chemin le plus court). Les copies protégées sont toujours gardées. L'utilisateur peut choisir une autre copie (`--keep`).
- Les liens physiques ne sont pas des doublons : ils sont comptés à part.
- Constat à l'origine de ces règles : sur l'arborescence synthétique, le micro-nettoyage proposait d'abord 149 880 fichiers vides d'une bibliothèque de type Steam et des fichiers internes à `node_modules` et `.venv`.

## D-027 — Filtres : recherche seule, regex sûres (2026-10-20)
- Les filtres cherchent dans l'index d'un scan et affichent un aperçu (nombre, taille, exemples). Ils ne génèrent **aucune** commande de suppression : une regex ou un glob trop large ne doit jamais pouvoir devenir un `rm -rf`. Les commandes restent réservées aux détections expliquées (dossiers parasites, doublons).
- Les regex à quantificateurs imbriqués ou à alternative répétée sont refusées avant toute recherche, avec une explication ; un budget de temps borne le reste (voir `FILTERS_AND_REGEX.md`).
- Sémantique des globs de chemin : `*` et `?` ne traversent pas `/`, `**` couvre zéro ou plusieurs dossiers, les fichiers cachés sont inclus.

## D-028 — Interface web (2026-10-20)
- Pages rendues côté serveur (Jinja2, autoéchappement partout, `StrictUndefined`, jamais `|safe`) et **un seul** fichier JavaScript natif, qui n'utilise que `textContent`. La CSP `default-src 'self'` interdit tout script, gestionnaire d'événement ou style en ligne ; un test analyse chaque page avec `html.parser` pour le vérifier.
- Toute action (lancer un scan, une analyse, générer une commande, annuler) est une requête `POST` JSON avec l'en-tête `X-TreeAudit-Request` ; la navigation et la recherche sont en `GET` et ne modifient rien.
- Une seule tâche longue à la fois (scan ou analyse de doublons), dans un thread, avec progression et annulation ; le verrou du dossier de données empêche aussi qu'elle tourne en même temps qu'une commande en ligne.
- Le sélecteur de dossier liste **en direct** les sous-dossiers de la zone autorisée : noms seulement, liens symboliques jamais proposés ni suivis, chemin relatif validé puis ouvert composant par composant (D-021).
- Exports JSON et CSV locaux. Dans les CSV, une cellule qui commence par `=`, `+`, `-`, `@`, une tabulation ou un retour chariot est préfixée par une apostrophe, pour qu'un nom de fichier ne puisse pas s'exécuter comme une formule dans un tableur.
- L'exploration de l'espace navigue par identifiant d'entrée de l'index (`?dir=<n>`), jamais par chemin.

## D-029 — Interface simple, en langage courant (2026-10-20)
- Retour du mainteneur : la première interface était trop technique. Elle devient un parcours guidé en trois étapes (choisir un dossier, analyse, bilan), avec de gros boutons et peu de choix par écran.
- Vocabulaire : « analyse » (et non scan), « à nettoyer » (et non parasites), « éléments », « place occupée ». Le vocabulaire technique (règles, empreintes, exclusions, erreurs, exports) reste disponible dans des sections repliées « Pourquoi ? » et « Détails techniques ».
- Le bilan d'une analyse tient en trois tuiles : ce qui peut être nettoyé sans risque, les doublons, où va la place ; plus une recherche simple « nom contient » (les critères avancés sont repliés).
- Nettoyage : les éléments sûrs (vert) sont cochés d'avance (rien n'est jamais exécuté) ; les doublons ne sont jamais cochés d'avance (ce sont des fichiers de l'utilisateur).
- Fin d'une analyse : redirection automatique vers son bilan.
- Aucune règle de sécurité ne change (D-025, D-026, D-028).

## D-030 — Optimisation pour disques mécaniques et machines modestes (2026-10-20)
Objectif du mainteneur : fonctionner sans à-coups sur un disque externe 5 400 tours et un PC de 8 Go de RAM. Sur un disque mécanique, le facteur limitant est le nombre de déplacements de la tête (10 à 15 ms chacun), pas le processeur.
- **Mesurer d'abord** : `treeaudit bench` / `make bench` affiche uniquement des chiffres (durée, processeur, mémoire maximale, octets réellement lus sur le disque, type de disque selon le noyau), jamais un nom de fichier.
- **Analyse** : les éléments d'un dossier sont examinés par paquets triés par numéro d'inode (inodes voisins = blocs voisins sur ext4 et dans la MFT NTFS).
- **Doublons** : étages successifs lisant le minimum (début 4 Kio, fin 4 Kio, puis lecture complète), dans l'**ordre physique** des fichiers sur le disque (ioctl `FIEMAP`, en lecture seule ; à défaut, ordre des inodes). Lecture complète signalée séquentielle au noyau, puis pages libérées du cache (`posix_fadvise`) pour ne pas chasser la mémoire des autres programmes. Un seul fil de lecture : en parallèle, un disque mécanique ralentit.
- **Empreintes réutilisées** d'une analyse à l'autre pour un fichier inchangé (même périphérique, inode, taille et date de modification à la nanoseconde, le critère de rsync). Une deuxième recherche ne relit rien. Option `reuse_digests=False` pour tout relire. Limite : un fichier modifié sans changer de taille avec une date remise à l'identique ne serait pas relu.
- **Résumés mis en cache** pour les analyses terminées (immuables), avec une clé incluant l'empreinte du catalogue de règles ; calculés dès la fin de l'analyse.
- **Base plus légère** (schéma v4) : index des chemins et des extensions supprimés (−24 % de taille, analyse plus rapide) ; les indices « dans le dossier » suivent les liens parent-enfant. Cache SQLite de 32 Mio et lots de 10 000 écritures.
- Mesures (SSD, cache chaud, 1 040 904 éléments) : analyse 20,3 → 15,7 s en natif avec la base en mémoire (17,3 s avec la base sur disque, voir D-031), mémoire < 80 Mio ; base 420 → 325 Mo ; doublons 0,9 → 0,5 s ; seconde recherche 0 octet lu ; détection des parasites 4,9 → 1,8 s puis instantanée. Sur un vrai disque 5 400 tours à froid : analyse −46 %, doublons −25 %, seconde recherche quasi instantanée (détails dans `ROADMAP.md`).

## D-031 — Python 3.14 dans les images, résultats « à nettoyer » mis en cache (2026-10-21)
- Images Docker en `python:3.14-slim` (épinglée par digest), comme la machine de développement. Mesuré sur 1 040 904 éléments : 21,2 s (3.13) → 20,7 s (3.14), soit −2,5 % seulement. L'hypothèse d'un gain important était fausse : l'écart restant avec le mode natif (17,3 s, base sur disque) tient au conteneur lui-même (filtrage des appels système, compilation de Python et de SQLite), pas à la version de Python.
- Correction d'une mesure de D-030 : les 15,7 s natives avaient été mesurées avec la base dans `/tmp`, qui est en mémoire (tmpfs) sur la machine de test. Avec la base sur disque, l'analyse native prend 17,3 s.
- Les résultats « à nettoyer » d'une analyse terminée sont mis en cache (évaluation des règles seulement ; filtrage, suppression des éléments inclus dans un dossier vert, tri et numérotation sont refaits à chaque fois). Les numéros affichés et ceux utilisés pour générer une commande restent donc strictement identiques au calcul direct ; un test le vérifie pour neuf seuils de confiance. Sur 1 000 000 d'éléments : 1,9 s → 0,003 s, et le cache est rempli dès la fin de l'analyse.

## D-032 — Archivage vérifié : dossier de sortie et garde-fous (2026-10-21)
Précise la mise en œuvre de D-024.
- **Dossier d'archives optionnel** : `archive_dir` dans la configuration ; en Docker, `TREEAUDIT_ARCHIVE_PATH` dans `.env`. Sans lui, l'archivage est désactivé. C'est le seul dossier hors du dossier de données où TreeAudit peut écrire, et il n'y écrit que des archives.
- **Validation**, sur le modèle des racines (D-009) :
  - chemin absolu et normalisé ;
  - jamais dans un dossier système : `/etc`, `/usr`, `/var`, `/tmp`, `/run` (sauf `/run/media`)…, ni `/`, `/home`, `/mnt`, `/media` eux-mêmes ;
  - ni le dossier personnel entier ;
  - dossier réel (sans lien symbolique sur le chemin) et accessible en écriture ;
  - système de fichiers ni virtuel, ni en mémoire (`tmpfs`), ni monté en lecture seule ;
  - **aucun chevauchement**, dans un sens comme dans l'autre, avec une racine ou avec le dossier de données (chemins réels).
  - Le volume système est accepté : une archive dans `~/Archives` est légitime. En revanche, elle est signalée si elle est sur le même disque que l'original.
- **En Docker** :
  - `make check-root` valide le dossier sur l'hôte (existence, chemin réel, droit d'écriture, puis le service `check-archive`, sans réseau ni volume, qui lit la table de montage de l'hôte) ;
  - ensuite seulement, le Makefile ajoute `compose.archive.yaml`, qui monte ce dossier en lecture-écriture sous `/archives`, dans le seul conteneur `app` ;
  - dans le conteneur, `/archives` doit être un point de montage dédié et accessible en écriture, sinon l'archivage reste désactivé, pour ne jamais écrire dans le système de fichiers du conteneur ;
  - défense en profondeur : deux montages qui exposent des dossiers qui se chevauchent sur le même périphérique (champ 4 de `mountinfo`) sont refusés.
  - Les racines restent en lecture seule.
- **Écriture** :
  - format `.tar.gz` (gzip niveau 6, le niveau 9 étant trop lent pour une machine modeste), ou `.tar.xz` en option, avec `tarfile` (bibliothèque standard) ;
  - la source est relue par descripteurs avec `O_NOFOLLOW`, sans se fier à l'index ; `tarfile.add(chemin)` n'est jamais utilisé ;
  - les liens symboliques sont archivés comme des liens ; les fichiers spéciaux sont ignorés et signalés ;
  - un fichier modifié pendant la lecture rend l'archive `incomplete`.
- **Validation finale** :
  - l'archive est d'abord écrite sous un nom temporaire `.<nom>.partial` ;
  - elle est relue, et chaque entrée est comparée (nom, type, taille, SHA-256, cible des liens) à ce qui a été lu ;
  - si tout concorde, `fsync` puis renommage sans écraser (`os.link` vers le nom final, puis retrait du nom temporaire).
  - Statuts : `complete`, `incomplete`, `cancelled`, `failed`.
- **Fichier temporaire après une annulation ou un échec** : TreeAudit le supprime lui-même. C'est la seule suppression du code. Elle ne vise que le fichier qu'il vient de créer, retiré relativement au descripteur du dossier d'archives, après vérification que l'inode est bien celui qu'il a créé. Elle ne peut jamais viser une donnée auditée.
- **Espace disque** : vérifié avant de commencer (taille non compressée plus une marge) ; s'il ne suffit pas, l'archivage est refusé.
- Mise en œuvre par étapes : (a) dossier d'archives et validation ; (b) moteur d'archivage et ligne de commande ; (c) interface web ; (d) revue de sécurité.
- **Précisions issues de l'étape (b)** :
  - **Écriture** : un seul module écrit, `archive/output.py`, ajouté seul à la liste blanche du test statique. Les en-têtes sont produits par l'API documentée `TarInfo.tobuf()` ; TreeAudit écrit lui-même le contenu, le bourrage et la fin d'archive. `TarFile` garderait en mémoire un objet par entrée (environ 500 Mo pour un million).
  - **Ordre des entrées** : chaque dossier est immédiatement suivi de son contenu, comme avec `tar` lui-même. Sinon, GNU tar restaure mal la date des dossiers : constaté sur 1 477 dossiers, puis corrigé et testé.
  - **Dates** : à la seconde, comme le format par défaut de GNU tar. Garder les nanosecondes exigerait un en-tête PAX supplémentaire par entrée, ce qui double le travail d'écriture et de vérification.
  - **Manifeste** : 16 octets par entrée (type, nom, droits, taille, cible du lien, SHA-256 du contenu), soit 16 Mo par million d'entrées.
  - **Fichiers qui changent pendant la lecture** : si un fichier rétrécit ou si une lecture échoue (secteur défectueux), les octets manquants sont remplacés par des zéros, pour que l'archive reste lisible, et le fichier est signalé. Un fichier modifié pendant sa lecture est aussi signalé (`fstat` avant et après). Dans les deux cas, l'archive est `incomplete`.
  - **Archive incomplète** : un fichier spécial, un fichier illisible ou un disque monté à l'intérieur rendent l'archive `incomplete`. Elle est vérifiée et conservée, avec `-INCOMPLET` dans son nom, mais TreeAudit dit explicitement de ne pas s'y fier pour supprimer l'original.
  - **Nom** : `<dossier>-AAAAMMJJ-HHMMSS.tar.gz`. Seuls les caractères `A-Z a-z 0-9 . _ -` sont gardés, sans point ni tiret en tête. L'heure est locale : le Makefile transmet le fuseau de l'hôte, lu dans `/etc/localtime`, au seul conteneur `app`. Les droits sont `0600`.
  - **Publication** : par `link(2)`, qui échoue si le nom existe. Sur les systèmes sans liens physiques (FAT, exFAT), TreeAudit vérifie que le nom est libre, puis renomme. Une course avec un autre programme du même utilisateur reste théoriquement possible ; c'est une limite documentée, hors du modèle de menace.
  - **Garde-fou supplémentaire** : si le parcours rencontre le dossier d'archives (même périphérique et même inode), l'archivage échoue.
  - **Liens physiques** : chaque nom est archivé comme un fichier complet (simple et toujours restaurable, mais plus volumineux). **Fichiers creux** : archivés à leur taille logique.
  - **Mesures** (SSD, 1 040 903 éléments, 1,3 Gio, dans Docker) : 168 s → 97 s, vérification comprise, et moins de 55 Mio de mémoire. À titre de comparaison, `tar czf` met 10 s, mais sans parcours sécurisé ni vérification. Le coût restant vient surtout de `tarfile`, en Python pur, entrée par entrée ; écrire soi-même les en-têtes tar ne vaut pas le risque.

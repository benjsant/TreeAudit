# Feuille de route

Chaque phase est validée (tests réellement exécutés, documentation à jour) avant de passer à la suivante. Le moteur est construit et vérifié en ligne de commande avant l'interface web.

## Phase 0 — Cadrage
- [x] Lire et consolider les documents de départ.
- [x] Choisir la version Python (D-017).
- [x] Définir les montages en lecture seule et la sélection des racines (D-008 à D-011).
- [x] Définir les statuts de scan (D-012).
- [x] Trancher les règles de parcours, tailles et doublons (D-013 à D-015).
- [x] Validation de ces documents par le mainteneur.

## Phase 1 — Socle et confinement
Aucun parcours de fichiers dans cette phase.

- [x] Paquet `treeaudit` (disposition `src/`), `pyproject.toml`, `uv.lock`.
- [x] Images Docker `dev` et `app` (D-019), `Makefile`.
- [x] Configuration TOML, erreurs typées, journalisation.
- [x] Module `roots` : validation des racines et détection du volume système (D-009).
- [x] CLI : `treeaudit roots check <chemin>` et `treeaudit roots list`.
- [x] API minimale : `GET /health`, `GET /api/roots` ; contrôle de `Host` ; en-têtes de sécurité.
- [x] Dockerfile et `compose.yaml` durcis, `compose.selinux.yaml`.
- [x] pytest, ruff, mypy.

**Critères d'acceptation**
1. `make test`, `make lint` (`ruff check`, `ruff format --check`) et `make typecheck` (`mypy`) passent dans le conteneur `dev`, sorties montrées. Aucune installation sur l'hôte.
2. `treeaudit roots check` accepte un dossier valide et refuse chaque cas de D-009 avec un message clair et un code de sortie non nul.
3. L'application refuse de démarrer si le dossier de données est dans une racine, ou une racine dans le dossier de données.
4. `GET /api/roots` renvoie identifiants et libellés, sans chemin hôte.
5. `docker compose up` démarre un conteneur non-root, cible en `:ro`, système de fichiers racine en lecture seule, sans capacités, port publié sur `127.0.0.1` seulement ; vérification manuelle documentée sous Mint et sous Fedora/Nobara.
6. La documentation d'installation (natif et Docker) suffit pour lancer le projet sur une machine vierge.

**Tests de sécurité de la phase**
- Refus de : `/`, `/home`, `/etc`, `/proc`, `/proc/1`, `/sys/kernel`, `/dev`, `/run`, dossier personnel entier, chemin relatif, chemin avec `..`, chemin inexistant, fichier, lien symbolique vers un dossier valide.
- Refus du volume système sans `allow_system_volume`, acceptation avec ; détection testée sur des exemples de `mountinfo` (ext4, btrfs avec sous-volumes, montage bind, overlay).
- Racine contenant ou contenue dans le dossier de données refusée.
- API : identifiant de racine inconnu ou ressemblant à un chemin (`../etc`, `%2Fetc`, `/etc`) → 404 ou 422, sans chemin dans la réponse.
- API : `Host: evil.example` → 400 ; aucune réponse ne contient `Access-Control-Allow-Origin`.
- Test statique (AST) : aucun `subprocess`, `os.system`, `os.popen`, `shutil.rmtree`, `os.remove`, `os.unlink`, `os.rename`, `Path.unlink`, `Path.write_*`… dans `src/` ; écritures de fichiers autorisées uniquement dans `db` et `reports`.
- Test du fichier Compose (analyse YAML) : ports préfixés par `127.0.0.1:`, cibles en `:ro`, pas de `privileged`, pas de `docker.sock`, pas de `:z`/`:Z`, présence de `cap_drop: [ALL]`, `read_only: true`, `user`, `no-new-privileges`.
- Manuel (documenté, hors CI) : `touch` dans la cible depuis le conteneur échoue avec « Read-only file system ».

## Phase 1 bis — Confinement réseau (D-020)
- [x] Service `gateway` (relais TCP stdlib, sans montage de données).
- [x] `app` sur un réseau interne sans sortie.
- [x] Tests : `app` ne joint pas Internet ; l'API reste joignable via `127.0.0.1` ; `gateway` n'a aucun volume ; analyse du fichier Compose.

## Phase 2 — Inventaire
- [x] Parcours itératif par descripteurs (`O_NOFOLLOW`), profondeur maximale.
- [x] Analyse d'un sous-dossier de la racine (`--path`, D-021).
- [x] Métadonnées, tailles logique et allouée, liens physiques comptés une fois.
- [x] Noms en octets, forme affichable.
- [x] Erreurs et exclusions enregistrées ; statuts D-012.
- [x] Annulation et progression.
- [x] Index SQLite par lots.
- [x] CLI : `treeaudit scan <id-racine>`, `treeaudit scan show <id-scan>`.
- [x] Mesure de performance (2026-10-20), sur l'arborescence synthétique `tools/make_dummy_tree.py` (152 246 entrées dont 149 942 fichiers, 2 298 dossiers, 11 Mo réels) :
  - Docker : 2,7 s, 27 Mio de mémoire maximale, statut `complete` ;
  - natif (Python 3.14 de l'hôte) : 2,1 s, 35 Mio ;
  - base SQLite : environ 42 Mo, soit environ 270 octets par entrée.
  Le mainteneur mesure lui-même sur ses données réelles (aucune donnée personnelle n'est transmise).

## Phase 3 — Dossiers parasites (D-023, D-025)
- [x] Catalogue de règles versionné (`src/treeaudit/parasites/rules.toml`, 41 règles) : Python, JavaScript, JVM, Rust, .NET, PHP, Dart, Elixir, Haskell, CMake, Terraform, restes macOS / Windows / Linux, verrous et sauvegardes d'éditeurs, fichiers auxiliaires LaTeX, bibliothèques recopiées.
- [x] Indices et contre-indices (ex. `pyvenv.cfg`, `package.json` ou `.csproj` à côté, fichier étranger dans un `__pycache__`), analyse sur l'index uniquement.
- [x] Indicateur de confiance en pourcentage (vert ≥ 80, orange ≥ 50, rouge en dessous ; seuil d'affichage 30 %) avec l'explication des indices.
- [x] Commande de recréation affichée ; corbeille, suppression et archivage proposés, **jamais exécutés**.
- [x] Sélection de plusieurs détections (numéros, intervalles, `green`) et commande unique sur plusieurs lignes (`\`), chemins échappés.
- [x] Chemin hôte affiché en Docker (`TREEAUDIT_DISPLAY_PATH_<ID>`, affichage uniquement).
- [x] Tests de faux positifs (`venv` sans `pyvenv.cfg`, `bin` de scripts personnels, lien symbolique nommé `node_modules`) et de noms hostiles dans les commandes.

## Phase 4 — Doublons exacts (D-015, D-026)
- [x] Regroupement par taille (index seul), empreinte partielle (premiers et derniers 64 Kio), SHA-256 complet.
- [x] Lecture sûre : chaque dossier ouvert depuis la racine avec `O_NOFOLLOW`, fichier ouvert avec `O_NONBLOCK`, identité (inode, taille, date) vérifiée avant et après lecture ; fichiers modifiés ou remplacés exclus et signalés.
- [x] Liens physiques distingués des doublons.
- [x] Comparaison octet à octet optionnelle (`--verify-bytes`).
- [x] Micro-nettoyage (`--micro`) : fichiers de moins de 1 Kio et fichiers vides, regroupés par zone, jamais sélectionnés en masse.
- [x] Copies protégées : fichiers-marqueurs, dépôts git, logiciels et dépendances installés ; une copie hors logiciel toujours gardée.
- [x] Copie à garder suggérée et expliquée ; commande unique (corbeille ou suppression) pour les autres copies.
- [x] Mesures (2026-10-20, arborescence synthétique de 152 970 entrées, Docker) : analyse principale 0,3 s (8,9 Mio lus) ; micro-nettoyage 0,16 s en natif après correction d'une requête SQL (2 s auparavant) ; affichage du micro-nettoyage 0,5 s et 32 Mio (159 Mio auparavant).
- [ ] Rapport JSON et CSV : reporté à la phase 6 (interface et rapports).

## Phase 5 — Filtres (D-027)
- [x] Extension, nom exact, glob de nom, glob de chemin, regex sur le nom et sur le chemin relatif, type, taille.
- [x] Option de casse (Unicode) ; correspondance entière ou partielle pour les regex.
- [x] Aperçu : nombre de résultats, taille totale (liens physiques comptés une fois), exemples triés par taille, chemin ou date.
- [x] Protections ReDoS : longueur maximale, refus des motifs dangereux, budget de temps ; annulation.
- [x] Tests : casse, Unicode NFC/NFD, fichiers cachés, regex invalides et dangereuses, aucun résultat, regex compilée une seule fois, aucune écriture.
- [x] Mesures (Docker, 152 970 entrées) : 0,01 à 0,7 s selon le critère, 22 Mio.
- Aucune commande de suppression à partir d'un filtre (recherche seule).

## Phase 6 — Interface web et rapports (D-028)
- [x] Choix du sous-dossier dans la zone autorisée (sélecteur en direct, D-021).
- [x] Lancement, progression et annulation d'un scan ou d'une analyse de doublons.
- [x] Répartition de l'espace (exploration dossier par dossier, barres proportionnelles), extensions, erreurs, exclusions.
- [x] Dossiers parasites et doublons : sélection, copie à garder modifiable, commande générée à copier.
- [x] Recherche (formulaire, aperçu, export).
- [x] Exports JSON et CSV (scan, inventaire complet, parasites, doublons, recherche), formules neutralisées.
- [x] Tests : XSS par noms piégés sur chaque page (analyse HTML), absence de code en ligne, CSRF, sélecteur (`../`, chemins absolus, liens), en-tête `Host`, CSV piégés.
- [x] Mesures (Docker, 152 970 entrées) : pages de 0,01 à 0,6 s (1,3 s pour les parasites) ; inventaire CSV complet 0,57 s (25 s avant regroupement des lignes envoyées).
- [ ] Vérification visuelle et au clavier dans un navigateur : à faire par le mainteneur.

## Phase 6 ter — Disques mécaniques et machines modestes (D-030)
- [x] Outil de mesure sans nom de fichier (`make bench`), type de disque détecté.
- [x] Arborescence synthétique d'un million d'éléments (`--scale 2.8 --heavy-mb 300`).
- [x] Analyse : métadonnées lues dans l'ordre des inodes.
- [x] Doublons : début / fin / complet, ordre physique (`FIEMAP`), `posix_fadvise`, un seul fil de lecture.
- [x] Empreintes réutilisées entre analyses (début, fin, complète).
- [x] Résumés du bilan mis en cache.
- [x] Base allégée (−24 %), cache SQLite et lots plus grands.
- [x] Résultats « à nettoyer » mis en cache (1,9 s → 0,003 s sur 1 000 000 d'éléments), numérotation identique au calcul direct (D-031).
- [x] Images en Python 3.14 (−2,5 % seulement : l'écart Docker / natif vient du conteneur, D-031).
- [ ] Plus tard, si utile : alléger le code Python de l'analyse (~20 % sur SSD, presque rien sur disque mécanique) ; ne plus stocker deux fois les chemins (base ~25 % plus petite).
- [x] Mesure sur un vrai disque 5 400 tours, à froid (2026-10-20) : disque USB 2,5 pouces ST1000LM025, ext4, dossier factice de 153 316 éléments et 527 Mo, cache du noyau vidé avant chaque mesure, une mesure par cas :
  - analyse : 8,0 s → **4,3 s (−46 %)** ;
  - doublons : 9,3 s → **7,0 s (−25 %)**, 338 → 307 Mio lus ;
  - deuxième recherche de doublons : **0,1 s, 0 octet lu** ;
  - mémoire maximale : 31 → 75 Mio (cache SQLite plus grand).
  Le dossier factice étant écrit d'un seul tenant, ses fichiers sont contigus : sur un disque de sauvegarde fragmenté, l'ordre physique devrait apporter davantage.

## Phase 7 bis — Mode natif sous bac à sable, vers une AppImage (D-038)
- [x] Prototype Landlock + seccomp, prouvé par des tests et sur l'hôte.
- [x] Étape 1 : bac à sable pour toutes les commandes natives, refus de démarrer s'il est incomplet, fichiers temporaires de SQLite dans un dossier privé.
- [x] Étape 2 : première AppImage, construite dans Docker ; zéro configuration ; bouton « Quitter » ; essai réel sur l'hôte.
- [x] Étape 3 : disques détectés à chaud (natif) ; le choix libre d'un dossier quelconque n'est pas fait (voir D-038).
- [x] Étape 4 : relecture de sécurité (deux contournements trouvés et fermés : SCTP/MPTCP, `chattr`), décision : AppImage recommandée, Docker pour les noyaux anciens et le développement.
- [ ] Publier l'AppImage (fichier à télécharger) pour qu'elle devienne le démarrage par défaut.

## Phase 6 octies — Cet ordinateur (D-037)
- [x] Partitions de données du disque système proposées, jamais celles du système (vérifiées sur l'hôte et dans le conteneur).
- [x] Dossier personnel proposé en entier, en lecture seule, secrets jamais lus.
- [x] Dossier de TreeAudit écarté par identité de toute analyse, du sélecteur et des archives ; signalé dans l'explorateur et le panier.
- [x] Messages : disques trouvés au démarrage ; « branché et ouvert ».
- [x] Essai réel : `/mnt/Data` proposé comme partition interne, `data/` reconnu par identité ; dossier personnel monté en lecture seule, `.ssh` et `.mozilla` cachés.

## Phase 10 — Ranger un dossier (D-041)
- [x] Étape A, sans IA : plan (projets et logiciels en bloc, dossiers homogènes gardés, fichiers en vrac par type et année), aperçu, script de rangement et script d'annulation, testés en les exécutant.
- [ ] Étape B : catégories plus fines proposées par l'IA (mode expert), sur banc d'essai.

## Phase 9 bis — Mode expert, LLM local (D-040)
- [x] Étape 1 : conteneur llama.cpp (Vulkan) sans Internet, téléchargement vérifié des modèles, banc d'essai ; Ministral 3 3B retenu (16/16, aucune réponse dangereuse).
- [ ] Second jeu de cas, écrit à part, pour confirmer.
- [ ] Étape 2 : « Avis de l'IA » sur les dossiers douteux, dans l'interface.

## Phase 6 septies — Explorateur et panier (D-036)
- [x] Explorateur : tri, pages, dates, repères (à nettoyer, en double, protégé).
- [x] Fiche d'un élément (autres copies, raison des repères).
- [x] Panier : sélection entre dossiers, commande construite par le serveur depuis l'index.
- [x] Essai réel sur `dummy_folder` : 30 000 éléments par dossier, 0,13 s par page.
- [ ] Aperçu des images (étape dédiée, lecture du contenu).

## Phase 6 sexies — Sans make (D-035)
- [x] Lanceur `./treeaudit` (bash) à la place du `Makefile`, mêmes validations avant montage.
- [x] `.env` lu comme des données (jamais exécuté), testé ; SELinux détecté.

## Phase 6 quinquies — Tout par l'interface (D-034)
- [x] `make up` suffit : aucun fichier de configuration ; disques externes activés et archives dans `archives/` par défaut.
- [x] Page « Mes archives ».
- [x] `.env` documenté comme ancien mode expérimental, non testé.
- [x] Essai réel : `make up` sans aucun dossier configuré ; disque système refusé ; dossier d'archives créé et validé.

## Phase 6 quater — Disques externes (D-033)
- [x] Détection des disques montés sous `/run/media/<utilisateur>`, `/media/<utilisateur>`, `/media` et `/mnt`. Exclusion par disque physique via sysfs (partitions, LUKS, LVM, RAID), refus en cas de doute.
- [x] Docker : détection sur l'hôte, montage individuel en lecture seule récursive des seuls disques acceptés, nouvelle vérification dans le conteneur ; dossier de `.env` devenu facultatif.
- [x] Interface : choix d'un disque avec son occupation, et liste des disques non proposés avec la raison.
- [x] Essais réels : `/mnt/Data` (partition du NVMe système) refusé ; un disque forgé sur le NVMe système refusé dans le conteneur ; le mode « un dossier » est inchangé.
- [ ] Essai avec un vrai disque USB (mainteneur).

## Phase 6 bis — Archivage vérifié (D-024)
- [x] Dossier de sortie dédié, déclaré et validé (D-032) : natif, hôte (`make check-root`) et conteneur.
- [x] Archive `.tar.gz` / `.tar.xz` (bibliothèque standard) écrite sous un nom temporaire, publiée sans jamais écraser, fichier temporaire retiré en cas d'échec ou d'annulation (`make archive`).
- [x] Vérification après écriture (type, nom, droits, taille et SHA-256 de chaque entrée, somme de contrôle gzip/xz) avant tout « feu vert ».
- [x] Espace disque vérifié avant de commencer ; liens archivés comme liens ; fichiers spéciaux signalés (archive `incomplete`).
- [x] Avertissement si l'archive est sur le même disque que l'original.
- [x] Mesures (SSD, 1 040 903 éléments, 1,3 Gio, Docker) : 97 s vérification comprise, moins de 55 Mio de mémoire (D-032). Extraction par GNU tar 1.35 identique à l'original (contenu, droits, dates, liens).
- [x] Interface web : bouton « Archiver » (choix du dossier et exploration de l'espace), confirmation, progression, annulation, résultat en clair. Essai réel par la passerelle Docker, annulation comprise.
- [x] Revue de sécurité dédiée (2026-10-21, `SECURITY.md`) : 3 défauts corrigés et testés, risques résiduels documentés.

## Phase 7 — Similarité (fichiers et arborescences)
- [ ] Définir les critères d'identité (mêmes chemins relatifs, mêmes empreintes).
- [ ] Définir un score de similarité explicable.
- [ ] Tester arbres renommés, sous-ensembles et fichiers manquants.
- [ ] Distinguer faits et heuristiques dans l'affichage.

## Phase 8 — Validation Linux
- [ ] Tests complets sous Linux Mint et Fedora/Nobara.
- [ ] Volumes externes (ext4, btrfs, NTFS, exFAT), noms particuliers.
- [ ] Revue de sécurité complète.
- [ ] Documentation installation, désinstallation, emplacement et suppression de la base locale.
- [ ] Aucune anomalie critique connue avant de déclarer la V1 terminée.

## Phase 9 — IA locale (optionnelle)
- [ ] Cas visés : trancher les détections ambiguës (bibliothèque recopiée ou modifiée, dossier `bin` incertain…) et expliquer les résultats.
- [ ] Comparer runtimes et modèles par benchmark ; documenter taille, RAM et performances.
- [ ] N'envoyer que les métadonnées nécessaires.
- [ ] Le modèle propose, il n'exécute jamais rien (CLAUDE.md, règle 8).
- [ ] Tester les réponses erronées.
- [ ] Vérifier que le produit fonctionne sans IA.

## Après la V1
- Visionneuse texte en lecture seule, avec taille limitée.
- Suggestions d'archivage ou de compression.
- Propositions de rangement à examiner (sans exécution).
- Éventuelle quarantaine réversible, après spécification et revue de sécurité séparées.

## V2 — Plus tard
- [ ] Évaluer Windows après stabilisation Linux.
- [ ] Ajouter protections et tests Windows avant d'annoncer le support.

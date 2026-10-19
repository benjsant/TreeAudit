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
- [ ] Parcours itératif par descripteurs (`O_NOFOLLOW`), profondeur maximale.
- [ ] Métadonnées, tailles logique et allouée, liens physiques comptés une fois.
- [ ] Noms en octets, forme affichable.
- [ ] Erreurs et exclusions enregistrées ; statuts D-012.
- [ ] Annulation et progression.
- [ ] Index SQLite par lots.
- [ ] CLI : `treeaudit scan <id-racine>`, `treeaudit scan show <id-scan>`.
- [ ] Mesure de performance documentée (nombre d'entrées, durée, mémoire) sur une arborescence réelle.

## Phase 3 — Filtres
- [ ] Extension, glob de nom, glob de chemin, regex nom et chemin relatif.
- [ ] Option de casse ; correspondance complète ou partielle.
- [ ] Prévisualisation (nombre et exemples).
- [ ] Limites anti-ReDoS.
- [ ] Tests de casse, Unicode, fichiers cachés, regex invalides.

## Phase 4 — Doublons exacts
- [ ] Regroupement par taille, empreinte partielle, SHA-256 complet (D-015).
- [ ] Vérification `fstat` avant et après lecture ; fichiers modifiés pendant l'analyse.
- [ ] Liens physiques distingués des doublons.
- [ ] Comparaison octet à octet optionnelle.
- [ ] Rapport JSON et CSV, sans action sur les fichiers.

## Phase 5 — Interface web et rapports
- [ ] Lancement, progression et annulation d'un scan.
- [ ] Répartition de l'espace, erreurs, exclusions, filtres, doublons.
- [ ] Export local.
- [ ] Tests XSS (noms piégés), CSRF, accessibilité de base.

## Phase 6 — Dossiers techniques
- [ ] Règles déclaratives (`.venv`, `venv`, `node_modules`, `__pycache__`, `bin`/`obj`, `build`, `dist`, caches connus), avec indices de confirmation (ex. `pyvenv.cfg` pour un `.venv`, `.csproj` voisin pour `bin`/`obj`).
- [ ] Explication affichée et niveau de confiance ; jamais de verdict « inutile ».
- [ ] Tests de faux positifs (`bin` contenant des scripts personnels, etc.).

## Phase 7 — Comparaison d'arborescences
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
- [ ] Démontrer un besoin que l'approche déterministe ne couvre pas.
- [ ] Comparer runtimes et modèles par benchmark ; documenter taille, RAM et performances.
- [ ] N'envoyer que les métadonnées nécessaires.
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

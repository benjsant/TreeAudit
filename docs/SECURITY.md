# Modèle de sécurité — V1 Linux

## Principes
La sécurité repose d'abord sur le **confinement des accès**, vérifié par l'application, et pas seulement sur une liste de chemins interdits. Le MVP est en lecture seule. Docker est une couche supplémentaire, pas la garantie principale (D-007).

## Ce que TreeAudit protège, et contre quoi

| Actif | Menace | Protection principale |
|---|---|---|
| Données auditées | Modification ou suppression accidentelle | Aucun code d'écriture vers les cibles ; montage `:ro` ; test statique |
| Reste du disque | Lecture hors périmètre (lien symbolique, `..`, course) | Racines déclarées ; parcours par descripteurs `O_NOFOLLOW` |
| Système hôte | Accès à `/proc`, `/sys`, socket Docker… | Refus de ces racines ; aucun montage global ; conteneur non privilégié |
| Index et rapports | Fuite des chemins (données personnelles) | Stockage local `0700/0600`, aucun envoi distant |
| Navigateur | Site tiers pilotant l'API locale, XSS par nom de fichier | Contrôle `Host`, en-tête personnalisé, autoéchappement, CSP |

**Hors du modèle de menace** : un attaquant qui a déjà les droits de l'utilisateur ou de root sur la machine. TreeAudit ne protège pas contre lui.

## Périmètre d'accès
- Le processus n'analyse que les racines déclarées et validées (D-008, D-009).
- En Docker, chaque cible est montée en lecture seule (`:ro`) sous `/audit/roots/<id>`.
- Ne jamais monter `/`, `/home` entier, `/dev`, `/proc`, `/sys` ou `/var/run/docker.sock`.
- Pas de mode Docker `privileged`. `cap_drop: [ALL]`, `no-new-privileges`, système de fichiers racine du conteneur en lecture seule, `/tmp` en `tmpfs`.
- Le conteneur s'exécute avec l'UID/GID de l'utilisateur hôte (non-root), pour lire exactement ce que l'utilisateur peut lire, ni plus ni moins.
- L'application ne demande jamais `sudo` et ne tente pas de contourner une erreur de permission : elle la signale.
- Aucun scan automatique de tous les volumes.
- Le volume système est refusé par défaut (D-009). Un disque externe contenant une ancienne installation système peut être audité en lecture seule.
- Le dossier de données (index, rapports) doit être hors de toute racine auditée ; l'application refuse de démarrer sinon.

## Traversée des chemins
- Les liens symboliques ne sont jamais suivis ; leur cible est enregistrée comme texte, jamais résolue (D-013).
- Chaque dossier est ouvert relativement à son parent (`os.open(nom, O_RDONLY | O_DIRECTORY | O_NOFOLLOW, dir_fd=parent)`, puis `os.scandir(fd)`). Un dossier remplacé par un lien entre le listage et l'ouverture provoque une erreur signalée, pas une sortie de la racine.
- Chaque fichier lu pour son empreinte est ouvert de la même manière (`O_NOFOLLOW`, relatif au parent), puis `fstat` vérifie qu'il s'agit bien du même inode, de la même taille et de la même date de modification que lors de l'indexation.
- Les points de montage imbriqués ne sont pas traversés par défaut (D-013).
- Les noms sont traités comme des octets : espaces, retours à la ligne, caractères de contrôle et UTF-8 invalide sont pris en charge et testés (D-014).
- En cas d'incertitude, refuser l'accès ou marquer le scan `incomplete`.
- **Limite** : le nombre de descripteurs ouverts croît avec la profondeur de l'arborescence. Une profondeur maximale configurable est appliquée ; la dépasser est une erreur signalée (scan `incomplete`).

## Lecture du contenu (doublons, D-015)
- Le contenu des fichiers n'est lu que pour calculer des empreintes SHA-256 ; il n'est jamais stocké ni journalisé (test : un contenu reconnaissable n'apparaît dans aucun fichier du dossier de données).
- Chaque fichier est ouvert en redescendant depuis le descripteur de la racine, dossier par dossier, avec `O_NOFOLLOW` ; un lien substitué depuis le scan provoque une erreur, jamais une lecture hors de la racine.
- Le fichier est ouvert avec `O_NONBLOCK` : une fifo substituée ne peut pas bloquer l'analyse ; il doit être un fichier régulier.
- Son identité (périphérique, inode, taille, date de modification) doit correspondre au scan avant et après la lecture ; sinon il est exclu des groupes et signalé.
- Quand le scan couvre toute la racine, la racine doit encore être le même dossier (même inode) qu'au moment du scan.
- Mémoire bornée : lecture par blocs de 1 Mio ; au plus un descripteur par niveau de dossier.

## Effets de bord de la lecture
- Sur un montage `:ro`, la lecture ne modifie rien.
- En natif, lire un fichier peut mettre à jour sa date de dernier accès (atime), selon les options de montage (`relatime` par défaut). L'application tente `O_NOATIME`, qui n'est autorisé que pour les fichiers dont l'utilisateur est propriétaire ; sinon l'atime peut changer. Cette limite est documentée pour l'utilisateur.

## Docker et modules de sécurité Linux
- Docker n'est **pas** une frontière de sécurité absolue : une faille du noyau ou du moteur Docker, une mauvaise configuration de l'utilisateur (montage trop large, port publié sur toutes les interfaces) ou l'appartenance au groupe `docker` (équivalent root) restent des risques.
- Les options `:z` et `:Z` sont interdites sur les cibles, car elles écrivent des étiquettes SELinux dans les fichiers de l'hôte (D-011).
- Sous SELinux enforcing, utiliser `compose.selinux.yaml` (`label=disable` pour ce conteneur). Compromis documenté dans D-011.
- Sous AppArmor (Mint), le profil `docker-default` s'applique sans configuration particulière.
- **Accès réseau sortant** (D-020, vérifié le 2026-10-19) : le conteneur `app`, qui voit les données, est uniquement sur un réseau Docker interne. Il ne peut ni joindre Internet (`Network is unreachable`) ni résoudre de nom externe. Le port est publié par un conteneur `gateway` distinct, qui relaie les connexions TCP vers `app` sans les interpréter. La passerelle a accès au réseau mais **aucun volume** : elle ne voit aucune donnée auditée. Les conteneurs `dev` et `check` n'ont aucun réseau.
- Vérifié sur l'image `app` (2026-10-19) : aucune capacité (`CapEff` à 0), `NoNewPrivs` actif, seccomp actif, AppArmor `docker-default`, système de fichiers racine en lecture seule, `/tmp` en `noexec`, aucun binaire setuid, au plus 256 processus, socket Docker absent ; `app` sans accès sortant ; `gateway` sans aucun montage.
- Appartenir au groupe `docker` équivaut à être root sur la machine : c'est une propriété de Docker, pas de TreeAudit, mais elle doit être connue.

## API et navigateur
- Écoute sur `127.0.0.1` en natif ; en Docker, port publié uniquement sur `127.0.0.1` (D-010).
- L'en-tête `Host` est vérifié (protection contre le DNS rebinding).
- Pas de CORS. Les requêtes qui modifient l'état exigent `Content-Type: application/json` et `X-TreeAudit-Request: 1` (D-018).
- Les routes désignent une racine par son identifiant, jamais par un chemin absolu. Les entrées sont désignées par leur identifiant dans l'index.
- Aucune route ne permet de lire un fichier arbitraire. La future visionneuse texte lira uniquement une entrée indexée d'une racine autorisée, avec une taille limitée.
- Les noms de fichiers sont des données non fiables : autoéchappement Jinja2, aucun `innerHTML`, `Content-Security-Policy: default-src 'self'`.
- Les tâches longues s'exécutent dans un thread de travail annulable ; un seul scan actif à la fois en V1.

## Filtres et expressions régulières
- Une regex ne sert jamais à décider si un chemin est autorisé.
- Les motifs utilisateur ne sont jamais passés à un shell.
- Le module `re` n'a pas de délai maximal : une regex pathologique (ReDoS) peut bloquer longtemps. Limites : longueur maximale du motif, évaluation dans une tâche annulable, avertissement à l'utilisateur. Il s'agit d'un déni de service local contre soi-même, de gravité faible.

## Données produites
- L'index SQLite et les rapports contiennent les chemins complets des fichiers : ce sont des **données personnelles**.
- Ils sont créés avec les permissions `0700` (dossier) et `0600` (fichiers), et jamais envoyés à un service distant.
- Le contenu des fichiers n'est jamais stocké dans l'index ; seules les empreintes le sont.
- Les journaux ne contiennent pas le contenu des fichiers.

## LLM (phase ultérieure)
- Aucun accès shell ou outil de modification.
- Ne pas envoyer le contenu intégral des fichiers par défaut.
- Transmettre seulement les métadonnées minimales nécessaires.
- Les réponses du modèle sont des suggestions non fiables, affichées comme telles.
- Ne jamais exécuter une sortie du modèle.
- L'application reste entièrement utile sans LLM.

## Commandes de nettoyage proposées (D-025)
- TreeAudit **affiche** des commandes (`gio trash`, `rm -rf`, `tar`, commandes de recréation) ; il ne les exécute jamais. Le code ne contient aucun appel au shell (test statique).
- Les noms de fichiers sont des données non fiables : chaque chemin est échappé avec `shlex.quote`, précédé de `--` (ou de `./` pour `tar`). Les tests vérifient, avec `shlex.split`, que des noms comme `$(touch pwned)`, `` `id` ``, `-rf` ou `a;b && c` arrivent tels quels dans la commande.
- Un chemin contenant des caractères de contrôle ou de l'UTF-8 invalide ne reçoit aucune commande : il ne pourrait pas être affiché sans ambiguïté.
- La corbeille (réversible) est proposée avant la suppression définitive ; une sélection hors du vert est signalée.
- Les modèles de commandes de recréation viennent uniquement du catalogue versionné ; leur seul paramètre (`{name}`) est échappé.

## Toute future fonction destructive est interdite
Elle exigera une spécification et une revue de sécurité séparées, un aperçu avant action, une confirmation explicite, une journalisation, une stratégie de restauration ou de quarantaine et des tests d'interruption et d'échec.

## Tests de sécurité
Voir [TEST_PLAN.md](TEST_PLAN.md), section « Sécurité ». Résumé :
- Racines refusées : `/`, racines système, intérieur de `/proc`, liens symboliques, volume système sans option, racine contenant le dossier de données.
- Liens symboliques vers `/etc` ou un chemin extérieur non suivis, y compris un lien substitué pendant le scan.
- Cible en lecture seule non modifiable.
- Aucun appel shell ni fonction d'écriture vers les cibles dans le code (test statique).
- Aucun chemin arbitraire accepté par l'API.
- `Host` étranger rejeté, absence d'en-têtes CORS, requête sans en-tête personnalisé rejetée.
- Nom de fichier contenant du HTML affiché échappé.
- Une erreur rend le scan `incomplete`, une interruption `cancelled`.
- Fichier Compose conforme (ports, `:ro`, pas de `privileged`, pas de `:z`/`:Z`, pas de socket Docker, `cap_drop`).

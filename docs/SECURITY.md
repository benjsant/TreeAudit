# Modèle de sécurité — V1 Linux

## Principes
La sécurité repose d'abord sur le **confinement des accès**, vérifié par l'application, et pas seulement sur une liste de chemins interdits. Le MVP est en lecture seule. Docker est une couche supplémentaire, pas la garantie principale (D-007).

## Ce que TreeAudit protège, et contre quoi

| Actif | Menace | Protection principale |
|---|---|---|
| Données auditées | Modification ou suppression accidentelle | Aucun code d'écriture vers les cibles ; montage `:ro` ; test statique |
| Dossier d'archives | Écriture hors de ce dossier, archive incomplète présentée comme bonne | Validation (D-032) ; nom temporaire ; vérification avant validation |
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
- Le dossier d'archives, optionnel, est le seul autre endroit où TreeAudit peut écrire, et seulement des archives vérifiées (D-032).
  - Il ne doit chevaucher ni une racine ni le dossier de données, et ne peut être ni un dossier système, ni le dossier personnel entier, ni un système de fichiers en mémoire ou en lecture seule ; sinon, l'archivage est désactivé.
  - En Docker, il est validé sur l'hôte avant d'être monté, en écriture, sous `/archives`, dans le seul conteneur `app`.

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
- Position physique des fichiers demandée avec l'ioctl `FIEMAP`, en lecture seule, sur un descripteur déjà ouvert sans suivre de lien ; aucune capacité Linux n'est nécessaire.
- Empreintes réutilisées d'une analyse à l'autre seulement si périphérique, inode, taille et date de modification (nanosecondes) sont identiques (D-030). Ce critère suppose qu'aucun outil n'a modifié un fichier en lui remettant exactement la même date ; en cas de doute, une analyse sans réutilisation relit tout.

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

## Interface web (D-028)
- Noms de fichiers rendus uniquement par des gabarits à autoéchappement ; JavaScript limité à `textContent` ; CSP sans exception (`default-src 'self'`). Test : chaque page est analysée pour vérifier l'absence de script, de gestionnaire `on…` ou de style en ligne, y compris avec des fichiers nommés `<img src=x onerror=…>` ou `"><svg onload=…>`.
- Actions en `POST` JSON avec en-tête dédié (CSRF) ; aucune action en `GET`.
- Sélecteur de dossier : noms de dossiers seulement, chemin relatif validé, ouverture sans suivre de lien ; un dossier inaccessible ou un lien renvoie une erreur, jamais le contenu d'un autre endroit.
- Exports CSV : formules neutralisées (apostrophe devant `=`, `+`, `-`, `@`, tabulation, retour chariot).
- Une seule tâche longue à la fois ; annulation possible.

## Filtres et expressions régulières
- Une regex ne sert jamais à décider si un chemin est autorisé.
- Les motifs utilisateur ne sont jamais passés à un shell.
- Le module `re` n'a pas de délai maximal : une regex pathologique (ReDoS) peut bloquer longtemps. Limites : longueur maximale du motif, évaluation dans une tâche annulable, avertissement à l'utilisateur. Il s'agit d'un déni de service local contre soi-même, de gravité faible.

## Données produites
- L'index SQLite et les rapports contiennent les chemins complets des fichiers : ce sont des **données personnelles**.
- Ils sont créés avec les permissions `0700` (dossier) et `0600` (fichiers), et jamais envoyés à un service distant.
- Le contenu des fichiers n'est jamais stocké dans l'index ; seules les empreintes le sont.
- Les journaux ne contiennent pas le contenu des fichiers.

## LLM (mode expert retiré, D-052)
Le mode expert a été retiré : TreeAudit ne contient plus d'IA. Si une IA revenait un jour, ces règles resteraient obligatoires, et elle serait locale : jamais une IA en ligne, qui recevrait les noms de fichiers.
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
- Les modèles de commandes de recréation viennent uniquement du catalogue versionné ; leur seul paramètre (`{name}`) est échappé. Le catalogue est refusé au chargement si l'un d'eux appelle un programme privilégié (`sudo`, `su`, `doas`, `pkexec`, `run0`…) ou destructeur (`rm`, `find`, `dd`, `mkfs`…).
- **Double vérification (D-045)**. Toute commande de nettoyage, d'où qu'elle vienne (nettoyage, doublons, panier, projets, ligne de commande), passe par deux contrôles indépendants :
  1. **chaque cible, avant d'être écrite**, doit :
     - être un chemin normalisé (sans `..`, `.` ni `//`) ;
     - se trouver strictement sous le dossier analysé ;
     - ne jamais être `/`, un dossier de premier niveau, une arborescence du système (`/usr`, `/etc`, `/boot`, `/root`, `/run` hors `/run/media`…), un dossier personnel entier (`/home/<nom>`), un point de montage de disque (`/media/<disque>`, `/run/media/<nom>/<disque>`, `/mnt/<partition>`), ni un disque, une racine ou le dossier d'archives que TreeAudit connaît, ni un dossier qui en contient un ;
  2. **le texte terminé est relu** comme le shell le découperait (`shlex.split`). Il doit être exactement le programme attendu, puis autant de cibles que choisies, chacune repassant le premier contrôle. Un `sudo`, un `;`, une option comme `--no-preserve-root`, un `/` ou une cible en trop font refuser toute la commande.
- Une cible refusée est **signalée avec la raison**, jamais retirée en silence. Si aucune cible ne passe, il n'y a aucune commande, et le bouton « Copier » disparaît.
- Les commandes ne contiennent jamais `sudo`. L'interface le rappelle à côté de chaque commande : ne jamais l'ajouter, et s'arrêter si le terminal demande un mot de passe.
- Les scripts de rangement (D-041) refusent d'être produits si un chemin du plan est absolu ou contient `..`. Ils ne suppriment rien : `mv -n`, et `rmdir` des seuls dossiers vides.
- **Panier de l'explorateur (D-036)** : le navigateur n'envoie que des numéros d'éléments. Le serveur relit chaque chemin dans l'index de l'analyse ; un numéro inconnu est ignoré et signalé. Le dossier analysé entier n'est jamais proposé, un élément déjà compris dans un dossier sélectionné est retiré, et un élément protégé (dépôt git, logiciel installé) est signalé. Les noms sont affichés par `textContent` uniquement.

## Aperçu des images (D-046)
- Seule lecture de contenu en dehors des doublons et des archives. Le serveur envoie les **octets d'origine**, sans rien décoder : pas de bibliothèque d'images côté serveur, c'est le navigateur, isolé, qui affiche l'image.
- Lecture par le lecteur des doublons : chaque dossier est ouvert depuis la racine avec `O_NOFOLLOW`. Le fichier doit être celui de l'index (périphérique, inode, taille, date) avant et après la lecture, sinon rien n'est envoyé. Un dossier remplacé par un lien symbolique n'est jamais suivi.
- **Type reconnu au contenu**, jamais au nom : JPEG, PNG, GIF, WebP, BMP, AVIF. Le **SVG**, qui peut contenir du script, n'est jamais servi. Un fichier `.jpg` qui contient du HTML est refusé.
- Réponse servie avec `Content-Security-Policy: default-src 'none'; sandbox` et `X-Content-Type-Options: nosniff`. Ouverte seule, elle ne peut rien exécuter. Elle n'est jamais mise en cache (`no-store`).
- Taille plafonnée à 20 Mio : l'image est lue entièrement en mémoire. La galerie montre 40 images par page, chargées à l'affichage, pour ne pas saturer la mémoire du navigateur.
- Une racine refusée, un fichier hors de l'index, un dossier privé (secrets du dossier personnel) ou une racine qui a changé depuis l'analyse ne donnent aucun aperçu.

## Archives vérifiées (D-024, D-032)
- Par défaut, le dossier d'archives est `archives/`, dans le dossier de TreeAudit (D-034). C'est un dossier dédié, que TreeAudit possède, et non un disque choisi dans l'interface : cela évite de monter un disque externe en écriture.
- C'est la seule fonction qui écrit en dehors du dossier de données. Tout passe par un descripteur du dossier d'archives, ouvert sans suivre de lien juste après sa validation. Le code est dans un seul module, `archive/output.py`, le seul ajouté à la liste blanche du test statique.
- **Création** de l'archive avec `O_CREAT | O_EXCL | O_NOFOLLOW`, droits `0600` : jamais un fichier existant, jamais à travers un lien.
- **Publication** sous le nom final sans jamais remplacer de fichier (`link(2)`). Sans liens physiques (FAT, exFAT), TreeAudit vérifie que le nom est libre, puis renomme.
- **Seule suppression du code** : celle du fichier temporaire que TreeAudit vient de créer, après un échec ou une annulation, et seulement s'il s'agit toujours du même inode. Un fichier remplacé entre-temps est laissé en place (testé).
- **Source lue** exactement comme pour les doublons : par descripteurs avec `O_NOFOLLOW`, identité du fichier vérifiée avant et après la lecture. Les liens sont archivés comme des liens, jamais suivis : un lien vers `/etc/passwd` donne un lien dans l'archive, pas le fichier (testé).
- **Noms dans l'archive** : tous relatifs, construits à partir des noms listés (jamais `..` ni `/`), sous un dossier de tête. Le nom du fichier d'archive ne garde que `A-Z a-z 0-9 . _ -`.
- **Aucune confiance avant vérification** : l'archive est relue sur le disque et comparée entrée par entrée. Une archive qui ne correspond pas est supprimée, jamais présentée comme bonne. Une archive à laquelle il manque quelque chose porte `-INCOMPLET` dans son nom.
- La relecture n'extrait rien : les entrées sont lues en mémoire, bloc par bloc.
- **Interface web** : l'archivage se lance par `POST`, avec la protection CSRF habituelle. Le dossier est désigné par la racine et un sous-chemin relatif, ou par l'analyse et le numéro d'entrée ; jamais par un chemin absolu. Le chemin de l'archive affiché est celui que l'utilisateur a choisi. Les noms des éléments manquants sont insérés avec `textContent` (testé avec des noms piégés).
- **Archives sûres à extraire** : tous les noms sont relatifs, sans `..`, et aucune entrée n'est placée sous un lien symbolique de l'archive, puisque le parcours ne descend jamais dans un lien. Extraire une archive TreeAudit ne peut donc rien écrire hors du dossier d'extraction (l'attaque classique « lien puis fichier à travers le lien » est impossible). Testé, y compris avec le filtre `data` de Python (PEP 706).

### Revue de sécurité de la phase 6 bis (2026-10-21)
Relecture critique de tout le code d'archivage, centrée sur le seul module qui écrit.

**Défauts trouvés et corrigés** (chacun a un test qui échoue avec l'ancien code) :
1. **Disque plein** : en cas d'échec d'écriture, la suppression du fichier temporaire commençait par fermer ce fichier. Or la fermeture échoue à nouveau, faute de place pour vider le tampon, si bien que le fichier temporaire restait sur le disque plein. La fermeture est désormais tentée et son échec journalisé, mais la suppression a toujours lieu.
2. **Archive lancée depuis une analyse** : elle reposait sur le chemin relatif enregistré dans l'index. Si le dossier audité avait changé entre-temps (autre dossier dans `.env`, dossier remplacé), TreeAudit archivait un autre dossier portant le même chemin. Désormais, le dossier doit avoir le même périphérique et le même inode que lors de l'analyse ; sinon, rien n'est écrit et l'utilisateur est invité à relancer l'analyse.
3. **Publication** : après `link`/`rename`, TreeAudit vérifie que le nom final désigne bien l'archive vérifiée (même inode, fichier ordinaire). Il ne retire le nom temporaire que s'il désigne encore ce fichier. Sinon, l'archivage échoue et le fichier inconnu reste en place.

**Vérifié sans défaut** :
- Aucune écriture hors du module `archive/output.py` (test statique).
- Aucun chemin absolu accepté par l'API ; protection CSRF sur les deux routes ; noms piégés affichés sans risque.
- Racines toujours en lecture seule.
- Fichier temporaire et archive en `0600`.
- Annulation et erreurs à chaque étape sans fichier résiduel.
- Mémoire bornée.

**Risques résiduels acceptés** :
- **Programme malveillant du même utilisateur** (hors du modèle de menace) : il pourrait remplacer des fichiers du dossier d'archives entre deux vérifications. Les vérifications par inode réduisent la fenêtre sans l'éliminer, faute d'appel système « supprimer ce descripteur ». De même, sans liens physiques (FAT, exFAT), le renommage final pourrait remplacer un fichier apparu à cet instant précis.
- **Le dossier d'archives est accessible en écriture au conteneur `app`.** Un conteneur compromis pourrait modifier ou supprimer les archives qui s'y trouvent, mais pas les dossiers audités. Il faut donc lui consacrer un dossier dédié, sans autre contenu.
- **Arrêt brutal** (coupure, `kill -9`) : un fichier caché `.….partial` peut rester. TreeAudit ne supprime jamais un fichier qu'il n'a pas créé pendant l'exécution en cours ; l'utilisateur le supprime lui-même (il est incomplet par définition).
- **Liens symboliques absolus** (par exemple vers `/etc/passwd`) : ils sont archivés tels quels, comme des liens. GNU tar les restaure comme des liens ; le filtre `data` de Python refuse de les extraire.
- **Limite connue** : sous SELinux en mode enforcing, écrire dans le dossier d'archives depuis le conteneur suppose `COMPOSE_SELINUX=1`, comme pour la lecture des racines. Sinon, l'écriture échoue et l'échec est signalé.

## Disques externes (D-033, activée par défaut depuis D-034)
- Désactivable avec `TREEAUDIT_EXTERNAL_DISKS=0` (ancien mode `.env`). Elle propose les disques montés sous les dossiers habituels des disques secondaires, mais **jamais un disque physique qui porte le système**. La décision repose sur le disque physique, lu dans sysfs, et non sur le nom du dossier. En cas de doute (disque introuvable, image disque, système non identifié), le disque n'est pas proposé.
- **En Docker, la détection a lieu sur l'hôte avant tout montage**, et seuls les disques acceptés sont montés, un par un et en lecture seule récursive. Le conteneur ne voit ni le disque système ni un disque refusé.
- **Nouvelle vérification dans le conteneur** : montage dédié, lecture seule, disque physique hors système. Elle a été testée en réel avec un disque forgé, refusé.
- **Le fichier Compose généré n'est jamais placé dans `data/`**, où le conteneur peut écrire. Il est régénéré avant chaque utilisation, et toutes ses valeurs sont échappées (JSON, `$` doublé).
- **Les disques branchés après le démarrage n'apparaissent pas** (pas de propagation des montages) : ils arriveraient en écriture dans le conteneur. Il faut relancer `./treeaudit`.
- **Exposition** : le conteneur voit tous les disques externes acceptés, pas seulement celui que l'on analyse. Toujours en lecture seule et sans accès à Internet.

## Disque système, dossier personnel et dossier de TreeAudit (D-037)
- **Partitions de données du disque système** : proposées seulement si elles ne partagent **aucune partition** avec un point de montage du système (`/`, `/boot`, `/boot/efi`, `/efi`, `/home`, `/usr`, `/var`). Un conteneur LUKS ou LVM partagé avec le système compte comme partagé, donc refusé. Le système de fichiers du système, remonté ailleurs, est refusé lui aussi. Comme pour les disques externes, la décision est prise sur l'hôte, puis vérifiée à nouveau dans le conteneur. Un disque annoncé comme externe et situé sur le disque système est refusé.
- **Dossier personnel** : monté en lecture seule, de façon récursive, sous `/audit/home`. Son chemin vient toujours de `$HOME` (chemin réel) et n'est jamais lu dans `.env` ou dans l'environnement. Le lanceur vérifie qu'il s'agit d'un dossier appartenant à l'utilisateur, puis le valide comme toute racine, sans la règle « dossier personnel entier ».
- **Secrets jamais lus** : une liste de chemins (`.ssh`, `.gnupg`, `.password-store`, trousseaux, profils de navigateur et de messagerie, messageries, identifiants cloud et de développement, `.netrc`, historiques de shell…, complétée par la revue V1) est écartée **avant même un `stat`**. Ces chemins sont absents de l'index et du sélecteur, et une analyse ou une archive ne peut pas commencer à l'intérieur. Cette liste ne fait que **réduire l'exposition** : la protection reste la lecture seule et l'absence de réseau (règle 11). Un secret rangé ailleurs serait lu comme un fichier ordinaire, pour les doublons par exemple, mais jamais stocké ni transmis.
- **Le dossier de TreeAudit sur un disque analysé** : `data/` et `archives/` sont reconnus par leur **identité** (périphérique, inode), relevée au moment de l'analyse, et non par leur nom. Ils sont écartés du parcours (exclusion « dossier de TreeAudit »), cachés dans le sélecteur, et une archive qui les contiendrait est refusée avant toute écriture. Dans l'explorateur et le panier, un dossier qui les contient, ou qui contient des secrets, est marqué « protégé » : le mettre à la corbeille les emporterait.
- **Exposition** : le conteneur voit en plus, en lecture seule, le dossier personnel et les partitions de données. Il n'a toujours pas d'accès à Internet, et rien n'est transmis.

## Mode natif : bac à sable du noyau (D-038)
- Sans Docker, c'est le noyau qui garde TreeAudit en lecture seule. Le point d'entrée pose, une fois pour toutes, avant toute analyse et tout fil d'exécution :
  - **Landlock** : lecture partout ; écriture, création, renommage et suppression seulement dans `data/`, `archives/` et le dossier temporaire de SQLite ; aucune exécution de programme ; écoute sur le seul port du serveur ; aucune connexion TCP sortante ; ni signal vers d'autres processus, ni socket Unix abstraite d'un autre processus (ABI 6) ;
  - **seccomp**, pour ce que Landlock ne couvre pas : seules les sockets TCP peuvent être créées (ni UDP, ni Unix, ni netlink), aucun envoi « TCP Fast Open » (D-063), pas d'`io_uring` ni d'appel x32, et aucun changement de droits, de propriétaire, de dates ou d'attributs étendus.
- **Refus de démarrer** si le réseau ne peut pas être coupé (noyau trop ancien, architecture non prise en charge) ou si d'autres fils d'exécution existent déjà.
- **Ce qui reste possible** : lire ce que l'utilisateur peut lire, y compris des secrets. Rien ne peut sortir pour autant : ni réseau, ni exécution, ni écriture ailleurs.
- `ctypes` n'est autorisé que dans `core/sandbox.py`, pour deux fonctions C (`syscall`, `prctl`) ; un test le vérifie.
- Preuves : `tests/security/test_sandbox.py`. Une sonde tente 36 actions, dont les contournements de la relecture ; l'analyse, les doublons, l'archive, le serveur web et la ligne de commande fonctionnent sous bac à sable ; un interpréteur BPF vérifie le filtre seccomp. Essais réels sur l'hôte.

### Relecture de sécurité du bac à sable (2026-10-22)
Elle a cherché à contourner le bac à sable et a tout essayé pour de vrai. Deux contournements ont été trouvés et fermés : les sockets « flux » SCTP et MPTCP, et `chattr` sur un fichier ouvert en lecture seule. Pour ce dernier, seccomp applique désormais une **liste blanche** d'`ioctl`. Une configuration trop large (`data_dir` sur le dossier personnel ou un dossier système) est refusée, et `treeaudit gateway` est refusée en natif. Le détail, piste par piste, est dans D-038.

### TCP Fast Open : un défaut du noyau, fermé par seccomp (2026-11-10, D-063)
La première construction sur GitHub a fait tourner la sonde sur un autre noyau (7.0.0-azure) : une connexion TCP sortante **passait** par `sendto(…, MSG_FASTOPEN)`. TCP Fast Open se connecte à l'intérieur de l'envoi, sans `connect()`, et Landlock ne contrôlait que `connect()` : défaut signalé en juin 2026 et corrigé dans le noyau (commit Landlock 33cb713db016), mais absent des noyaux non mis à jour. Sur ceux-là, un TreeAudit compromis aurait pu envoyer des données à l'extérieur. Le filtre seccomp refuse désormais tout envoi marqué `MSG_FASTOPEN` (`sendto`, `sendmsg`, `sendmmsg`), quel que soit le noyau ; TreeAudit ne s'en sert jamais. Les autres chemins ont été vérifiés : l'option `TCP_FASTOPEN_CONNECT` passe par `connect()` ; MPTCP et `io_uring` sont déjà refusés ; les appels 32 bits (`socketcall`) et x32 aussi. La sonde essaie les trois variantes (IPv4, IPv6, `sendmsg`) et attend `EACCES`.

## Revue de sécurité V1 (2026-11-09)
Relecture de tout ce qui a changé depuis la revue du 2026-10-21 (le bac à sable a eu la sienne le 2026-10-22) : explorateur et panier, double vérification des commandes, scripts de Ranger, aperçu des images, oubli d'une analyse et compactage, dossier personnel et partitions, pages Projets et Dossiers presque identiques, lanceur après le retrait de l'IA, journaux.

**Défauts trouvés et corrigés** (chacun a un test ; ceux de Ranger lancent réellement les scripts) :
1. **Ranger pouvait déplacer des fichiers hors du dossier choisi** (moyen) : un lien symbolique nommé comme la destination (« Rangé »), ou comme un de ses sous-dossiers, était suivi par `mkdir -p` et `mv`. Désormais, le plan prend « Rangé (2) » si « Rangé » existe sans être un vrai dossier, et refuse un nom choisi dans ce cas. Avant chaque déplacement, le script vérifie qu'**aucun dossier du chemin de la cible n'est un lien** ; sinon l'élément est ignoré et signalé. Le test du lien piégé échoue sans cette vérification.
2. **Secrets du dossier personnel : liste incomplète** (moyen) : jetons de développement (`.npmrc`, `.pypirc`, `.cargo/credentials`, `.config/rclone`), messageries (Signal, Telegram, Discord, Element, Slack, Evolution), versions Flatpak (Bitwarden, KeePassXC, 1Password, Vivaldi, Edge, Opera, LibreWolf, Epiphany) et historiques de shell et de consoles. Ils pouvaient être lus par la recherche de doublons ou archivés ; ils sont maintenant écartés avant tout `stat`.
3. **Le script d'annulation retirait tous les dossiers vides de la destination** (faible), y compris ceux de l'utilisateur : il ne retire plus que les dossiers que le rangement a pu créer, les plus profonds d'abord, s'ils sont vides.
4. **Journal d'accès du serveur** (faible) : chaque URL, donc un mot recherché ou un nom de sous-dossier tapé, était journalisée (et gardée dans les journaux Docker). Il est coupé ; les erreurs restent journalisées.
5. **Marques de sens d'écriture et contrôles C1** (faible) : « photo[U+202E]gpj.exe » s'affiche « photoexe.jpg ». Un nom qui contient ces caractères (U+202A–U+202E, U+2066–U+2069, U+200E, U+200F, U+061C, U+0080–U+009F) ne reçoit plus de commande et reste en place dans Ranger, comme un nom avec un retour à la ligne.

**Vérifié sans défaut** :
- Titre en tête des scripts : il passe par `safe_root`, aucun retour à la ligne possible.
- Commandes de nettoyage : double vérification (cibles bornées, relecture comme le shell, aucun programme privilégié).
- Aperçu des images : lecteur sûr (`O_NOFOLLOW`, même fichier avant et après), type tiré des octets, SVG refusé, en-tête `sandbox`, dossiers privés non lus.
- CSRF et en-têtes de sécurité : middleware global, y compris oublier et compacter ; aucune action en GET.
- Paramètres des pages bornés ; noms de sous-dossiers tapés filtrés puis mis entre guillemets ; identifiants des nouvelles pages vérifiés ; noms piégés échappés.
- Journaux applicatifs sans contenu de fichier ; dossier de données en `0700` ; lanceur sans accès GPU ni conteneur d'IA.
- 173 tests de sécurité, dont la sonde du bac à sable, passés sans test ignoré.

**Risques résiduels acceptés** :
- **Entre l'analyse et le lancement d'un script** : un dossier *parent d'une source* remplacé par un lien. Le script vérifie l'élément lui-même (taille, date) et le chemin de la cible, pas tous les parents de la source. Un tel changement suppose un programme malveillant du même utilisateur, hors du modèle de menace ; rien n'est jamais écrasé (`mv -n`).
- **Une page énorme** (« Tout afficher » sur 300 000 éléments) peut ralentir le serveur : un déni de service local, pour soi seulement.
- **Dépendances** : versions figées dans `uv.lock` et justifiées dans `DEPENDENCIES.md`. Audit `pip-audit` 2.10.1 fait le 2026-11-10 sur les 33 paquets de `uv.lock` (exécution et développement), bases PyPI et OSV : **aucune vulnérabilité connue**. Il n'est pas automatique (il demande le réseau) : à refaire avant chaque version publiée et après chaque `./treeaudit lock`. Méthode : `uv export --frozen --all-groups --no-emit-project` dans l'image `dev`, sans réseau ; puis `pip-audit -r … --disable-pip --no-deps` dans un conteneur jetable qui ne voit que cette liste de noms et de versions.

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

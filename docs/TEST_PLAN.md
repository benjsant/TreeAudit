# Plan de tests

## Règles
- Les tests créent leurs propres arborescences dans des répertoires temporaires (`tmp_path` de pytest). Ils n'utilisent jamais un disque réel.
- Les tests qui dépendent de l'environnement (droits root, système de fichiers particulier, Docker) sont marqués et ignorés avec un motif explicite lorsqu'ils ne peuvent pas s'exécuter. Un test ignoré n'est jamais compté comme réussi.
- Les tests de permission sont ignorés si l'on s'exécute en root (root lit tout).
- La lecture de `/proc/self/mountinfo` est isolée derrière une fonction, testée avec des exemples de contenu enregistrés (ext4, btrfs avec sous-volumes, bind, overlay, tmpfs, Docker).

## Organisation
- `tests/unit/` : fonctions pures (validation, filtres, analyse de `mountinfo`, regroupements).
- `tests/integration/` : scan réel d'arborescences temporaires, base SQLite, API via `httpx`/`TestClient`.
- `tests/security/` : tests de confinement, tests statiques du code, analyse du fichier Compose.

## Racines (phase 1)
- Racine valide acceptée.
- Refus : `/`, racines système, intérieur de `/proc`/`/sys`/`/dev`/`/run`, dossier personnel entier, chemin relatif, `..`, inexistant, fichier, lien symbolique.
- Volume système refusé sans option, accepté avec option.
- Btrfs : deux sous-volumes du disque système reconnus comme système.
- Recouvrement avec le dossier de données refusé dans les deux sens.
- Deux racines qui se chevauchent : signalées (comportement à définir en phase 2).

## Inventaire (phase 2)
- Dossier vide ; racine vide.
- Fichiers de tailles différentes ; fichier creux (taille logique ≠ taille allouée).
- Liens physiques : un inode compté une seule fois.
- Fichiers cachés, noms Unicode (NFC et NFD), espaces, retour à la ligne, caractères de contrôle.
- Nom en UTF-8 invalide (octets bruts) : stocké en octets, affiché remplacé, signalé.
- Arborescence profonde (au-delà de la profondeur maximale) : erreur signalée, scan `incomplete`.
- Dossier contenant beaucoup d'entrées.
- Dossier sans permission de lecture, fichier sans permission : erreur enregistrée, scan `incomplete`.
- Lien cassé ; lien vers l'extérieur de la racine ; lien vers un dossier parent (boucle) : enregistrés, non suivis.
- Dossier remplacé par un lien symbolique pendant le scan (simulé par crochet de test) : aucune entrée hors racine.
- Entrée supprimée entre le listage et le `lstat` : erreur signalée.
- Point de montage imbriqué (simulé par crochet sur l'identifiant de périphérique, ou réel si possible) : exclu et listé ; traversé avec `cross_mounts`.
- Annulation : état `cancelled`, résultats partiels marqués.
- Exception interne : état `failed`.
- Redémarrage pendant un scan `running` : passage à `failed`.
- Aucun contenu de fichier dans la base.

## Filtres (phase 3)
- Extension avec et sans point, casse.
- Glob de nom `*.py`, `backup-????.zip`, classes `[0-9]`.
- Glob de chemin : `*` ne traverse pas `/`, `**` traverse ; fichiers cachés inclus.
- Regex valide et invalide (message d'erreur clair, aucun plantage).
- Casse sensible ou insensible.
- Correspondance complète (`fullmatch`) ou partielle (`search`).
- Chemin relatif, noms Unicode, fichiers cachés.
- Aucun résultat.
- Motif trop long refusé ; regex pathologique interrompue par annulation.
- La regex est compilée une seule fois par recherche.

## Doublons (phase 4)
- Deux fichiers identiques avec des noms différents.
- Fichiers de même taille mais de contenu différent (début identique, fin différente ; début différent).
- Fichiers vides regroupés à part.
- Liens physiques non présentés comme doublons.
- Fichier modifié pendant le calcul d'empreinte : marqué, exclu.
- Erreur de lecture pendant le calcul : signalée.
- Comparaison octet à octet activée : résultat identique.
- Copie de référence modifiée avant la comparaison octet par octet : c'est elle qui est signalée, la suivante prend sa place, les copies intactes restent dans le groupe (audit du 2026-11-25).
- Copies protégées (logiciel, jeu, dépendances, dépôt git) toujours gardées ; programme portable reconnu à son contenu où qu'il soit rangé.
- Progression : chaque étape compte ses propres fichiers ; empreintes réutilisées quand rien n'a changé.

## Dossiers presque identiques (D-058, `test_similar.py`)
- Copie renommée : une paire, identique dans les deux sens ; sous-ensemble : le sens est dit ; fichiers modifiés, déplacés et manquants.
- Petits fichiers : une supposition, jamais comptée, devenue un fait après la recherche des petits fichiers.
- Liens physiques et fichiers vides ; paire incluse dans une paire non répétée ; dossiers sans rapport, fichiers internes de git : aucune paire.
- Rien n'est dit sans recherche de doublons ; noms piégés gardés comme données ; plusieurs copies en une famille ; analyse d'un sous-dossier.

## Photos ressemblantes (D-069)
- Regroupement (`test_similar_images.py`) : copie réduite regroupée, la plus grande gardée ; photos d'écrans différents non regroupées (corrélation) ; seuil de l'empreinte exact ; rafale qui dérive montrée, seulement en partie proposée ; l'index trouve exactement les paires d'une comparaison de toutes les paires ; corrélation de Pearson ; raison écrite = critère qui a vraiment choisi l'image gardée.
- Jamais proposées : images d'un logiciel ou d'un dépôt git ; fichier à plusieurs noms (liens physiques), compté une fois ; échecs et images trop éloignées de l'image gardée.
- Copies identiques séparées des autres versions (preuve octet par octet ou supposition) ; signatures enregistrées pendant la lecture jamais mélangées aux groupes affichés (audit du 2026-11-25).
- Web (`test_web.py`) : signatures enregistrées puis reprises pour un fichier inchangé, recalculées pour un fichier modifié ; signatures mal formées ou d'une image hors de l'analyse refusées ; image de contrôle de la page aux pixels attendus ; page avec son seul script ; commande versionnée (409 si les signatures ont changé, 422 pour un groupe ou un choix inconnu, CSRF exigé) ; paires mises en cache puis oubliées.
- Disque débranché pendant le calcul (`test_preview.py`, `test_web.py`) : dossier indisponible → 503, la page s'arrête ; fichier changé → 409, l'image est redemandée au calcul suivant.

## Vérifications dans un vrai navigateur (`tools/browser_checks/`)
Hors de la suite, avec Playwright dans un conteneur (voir son `README.md`) : tour de toutes les pages (ordinateur en thème clair, téléphone en thème sombre : erreurs de la console, débordement horizontal, requêtes vers l'extérieur), parcours jusqu'aux photos ressemblantes, calcul des signatures avec arrêt et reprise, refus d'un navigateur qui brouille les images, page des groupes, Ranger « seulement le développement ». Dernier passage : 2026-11-25, Chromium et Firefox, 77 pages et 70 téléchargements sans erreur.

## Archives vérifiées (phase 6 bis, D-032)
- Dossier d'archives refusé : dossier système, `/tmp`, dossier personnel entier, lien symbolique, non accessible en écriture, `tmpfs`, lecture seule, chevauchement avec une racine ou le dossier de données ; dans le conteneur : non monté, sous-dossier du montage, deux montages du même dossier de l'hôte.
- Archive complète en `.tar.gz` et `.tar.xz` : noms en UTF-8 invalide, avec retour à la ligne, de plus de 100 octets ; liens archivés comme liens (y compris vers `/etc/passwd`) ; droits ; dossiers vides ; chaque dossier suivi de son contenu ; source inchangée (droits, taille, date, contenu).
- Incomplète : fifo, fichier ou dossier illisible, disque monté à l'intérieur, fichier qui grandit ou rétrécit pendant la lecture (l'archive reste lisible et vérifiée).
- Annulation avant l'écriture, pendant l'écriture et pendant la vérification ; vérification qui échoue (octet modifié) ; erreur d'écriture : aucun fichier ne reste.
- Espace insuffisant, dossier non configuré, chevauchement, sous-dossier invalide : refus sans rien écrire.
- Nom déjà pris : jamais remplacé, avec ou sans liens physiques. Un fichier temporaire remplacé n'est ni supprimé ni publié.
- Vérification seule : entrée différente, en trop, manquante, archive tronquée, somme de contrôle gzip altérée.
- Web : bouton absent et route refusée (409) sans dossier d'archives ; en-tête CSRF exigé ; chemins hors racine refusés ; archive depuis le choix du dossier et depuis une analyse (nom piégé, nom non UTF-8) ; un fichier n'est pas archivable ; message clair en cas d'espace insuffisant ; pages sans code en ligne.
- Manuel : extraction par GNU tar et comparaison avec l'original (contenu, droits, dates, liens) ; archivage et annulation par la passerelle Docker.

## Disques externes (phase 6 quater, D-033)
- Faux sysfs : disque système (y compris une autre partition et un volume LUKS du système), clé USB, disque entier sans partition, LUKS externe, btrfs (numéro anonyme, résolu par la source), image ISO (`loop`), système de fichiers réseau, montage imbriqué, système de fichiers virtuel.
- Disque système impossible à identifier : aucun disque proposé. Disque contenant le dossier de données, nom contenant des caractères de contrôle : refusés.
- Transmission au conteneur : aller-retour JSON ; identifiant forgé rejeté ; fichier Compose avec uniquement les disques acceptés, en lecture seule récursive, `$` doublé.
- Conteneur : disque non monté, monté en écriture, ou devenu disque système : refusé. Racine non montée : `not_mounted`, masquée à l'accueil.
- Web : liste des disques avec leur occupation, liste des disques non proposés, nom piégé, analyse d'un disque, conseil d'activation quand l'option est désactivée.
- Réel : détection par Docker (`/mnt/Data` refusé), disque forgé refusé dans le conteneur, mode « un dossier » inchangé.
- Disque arraché, simulé (`test_disk_removed.py`) : après quelques appels, chaque lecture échoue avec `EIO`, comme sur un disque USB retiré. Arraché pendant l'analyse : `incomplete`, ce qui a été lu est gardé, erreurs `EIO` listées. Arraché avant le premier listage : `incomplete`, rien de lu. Même les dossiers déjà ouverts illisibles : `failed`, avec le message. Arraché pendant les doublons : `incomplete`, aucun fichier illisible dans un groupe.
- Réel (2026-10-29) : un disque dur USB 2,5 pouces, ext4, monté sous `/mnt`, est détecté et proposé par `./treeaudit disks` ; une analyse de 24 000 éléments et une recherche de doublons s'y terminent.

### Essai réel avec un disque USB (mainteneur)
À faire avec un disque qui ne contient **que des copies**. TreeAudit n'y écrit jamais, mais arracher un disque monté en écriture sur l'hôte peut abîmer ce que d'autres programmes y écrivaient. Rapporter seulement les états et les chiffres affichés, jamais des noms de fichiers.

**A. Disque branché après le démarrage (Docker)**
1. Disque débranché, lancer `./treeaudit` : le disque n'est pas proposé.
2. Brancher le disque, l'ouvrir dans le gestionnaire de fichiers. Attendu : toujours absent de la page (avec Docker, les disques sont lus au démarrage) ; `./treeaudit disks`, dans un autre terminal, le montre « proposé ».
3. `Ctrl+C`, puis `./treeaudit` : le disque est proposé ; une analyse se termine « terminée ».

**B. Disque arraché pendant une analyse**
1. `sync`, puis lancer l'analyse du disque entier ; attendre que le compteur d'éléments avance.
2. Débrancher le câble.
3. Attendu : l'analyse finit « incomplète » (erreurs « Input/output error » dans « Détails techniques ») ou « échouée » avec un message ; **jamais « terminée »**. La page reste utilisable.
4. Rebrancher le disque **sans redémarrer** TreeAudit et relancer l'analyse. Attendu : pas « terminée » (le conteneur garde l'ancien montage, devenu illisible).
5. `Ctrl+C`, `./treeaudit` : le disque est de nouveau proposé et une analyse se termine « terminée ».

À rapporter : l'état et le nombre d'erreurs à chaque étape, et tout ce qui a gelé la page.

La vitesse à froid sur ce type de disque est déjà mesurée (`ROADMAP.md`, phase 7, 2026-10-20).

### Clé de test : exFAT, NTFS, btrfs
Une clé USB dédiée, sans rien à garder : elle est formatée plusieurs fois.
1. Formater la clé (exFAT, puis NTFS, puis btrfs), la monter, puis y générer le dossier de test, la fiche étant écrite **hors de la clé** :
   `python3 tools/make_key_tree.py <clé>/treeaudit-test --manifest <ailleurs>/fiche-exfat.json`
   (par défaut 20 000 petits fichiers et 2,5 Go de gros fichiers aléatoires, dont des copies exactes). Puis `sync`.
2. La fiche donne ce que TreeAudit doit trouver dans ce dossier : nombres d'éléments, tailles, groupes de doublons (taille minimale par défaut), fichiers à liens physiques ; et ce que le système de fichiers a refusé (liens sur exFAT, noms interdits par Windows, noms ne différant que par la casse…). Un refus est noté, jamais une erreur : il décrit le système de fichiers.
3. Analyser le dossier `treeaudit-test` de la clé (natif et Docker) : l'analyse se termine « terminée », les chiffres du dossier et les groupes de doublons sont **exactement** ceux de la fiche.
4. Partie B ci-dessus sur cette clé, pendant l'analyse puis pendant la recherche de doublons (les gros fichiers donnent le temps de la retirer).
   - Une recherche relancée sur des fichiers inchangés **ne relit rien** (empreintes réutilisées, même sur une clé rebranchée, sauf exFAT dont les numéros d'inode changent à chaque montage). Pour la retirer en pleine lecture, générer d'abord un second dossier jamais lu (`--small-files 0`), éjecter la clé pour vider le cache, la rebrancher, analyser, puis lancer la recherche.
   - Vérifier que des octets sont vraiment lus sur la clé (`/proc/diskstats`) avant de la retirer.

`tests/integration/test_key_tree.py` vérifie que la fiche est juste : le même dossier, généré en petit, est analysé par TreeAudit, y compris quand les liens et le tube nommé sont refusés. Sous `/tmp` (tmpfs) un dossier n'occupe aucun bloc : la place du dossier de départ est vérifiée à part.

**Résultats du 2026-11-19** (clé USB 32 Go, AppImage sous bac à sable, sans fichier de configuration, disque détecté tout seul) :

| | exFAT | NTFS (ntfs-3g) | btrfs |
|---|---|---|---|
| Analyse égale à la fiche (nombres, tailles, place occupée) | oui | oui | oui |
| Doublons égaux à la fiche (24 groupes, liens physiques) | oui | oui | oui |
| Éjection logicielle : analyse gardée, disque « plus disponible » | oui | oui | oui |
| Retrait pendant la recherche : « incomplète », erreurs d'E/S notées | oui (5) | oui (8) | oui (26) |
| Aucun groupe faux après le retrait | oui | oui | oui |
| Refusés par le système de fichiers | 19 (liens, tube, noms Windows, casse, octets invalides) | 13 (noms Windows, octets invalides) | 1 (nom de plus de 255 octets) |

Sur btrfs, la clé retirée reste montée sans disque (« missing disk ») : TreeAudit ne la propose plus. Le retrait pendant l'analyse elle-même n'a pas pu être fait à la main : 20 000 fichiers sont analysés en moins de 2 s.

## Ranger (D-041)
- Plan : album gardé en entier, projet et installation Steam jamais découpés, fichiers en vrac par type et année, collisions renommées « (2) », éléments cachés, liens et dossiers vides laissés en place.
- Un dossier sans photo ni vidéo mis par l'utilisateur dans « Photos » ou « Vidéos » y va sans dossier d'année (le plan plantait, trouvé par l'audit de novembre 2026).
- Scripts exécutés pour de vrai sur une arborescence de test : rangement sans perte (empreintes identiques), annulation à l'identique, fichier modifié ignoré, place prise jamais écrasée, nom piégé sans effet.
- Catégories à ranger (D-070) : choix lu dans l'adresse, « seulement le développement et les logiciels » ne déplace ni photos ni documents, même dans un dossier mélangé, éléments non cochés laissés en place et comptés. Dossier de code sans marqueur déplacé en entier, sauf s'il contient un lien. Un `.ts` d'au moins 1 Mio est une vidéo (sauf `.d.ts`) ; un dossier renommé « (2) » garde ses points.
- Plan d'exécution de la descente dans les sous-dossiers (index des parents), page d'aperçu et téléchargements avec des noms piégés.
- Sous-dossiers par règles (D-050, `test_subfolders.py`) : mots normalisés (casse, accents, chiffres, mots vides), thèmes, sujet plus fort qu'un type de papier, thème isolé laissé aux mots communs, aucun sous-dossier qui contiendrait tout, thèmes selon la catégorie, séries d'appareil photo écartées (mais gardées parmi les documents), mots collés coupés seulement quand un morceau est un mot de thème ou un mot ailleurs (marques entières, préfixe d'appareil collé toujours écarté), lots de 30 avec « suite » (éléments déjà rangés et noms déjà pris laissés tranquilles), noms sûrs et jamais vagues, même réponse pour la même entrée, 50 000 éléments en moins de 10 s. Page : proposition sans mode expert, affichée seulement, script inchangé, catégorie inconnue ou interdite sans effet.
- Cas mis de côté (`tools/subfolder_bench/cases.py`), figés avant l'algorithme : le banc d'essai `python -m tools.subfolder_bench` (D-066) mesure les règles sur ces cas et sur ceux écrits autrefois pour l'IA ; `test_subfolders.py` exige au moins 95 % sur chaque cas mis de côté. Retrait de l'IA de Ranger (D-051) : ni bouton ni route.
- Mode expert retiré (D-052) : aucune page ne montre d'IA, la route des avis n'existe plus, aucun conteneur ne reçoit de périphérique ni de groupe, `./treeaudit expert` le dit sans rien écrire, un ancien `models/.expert` est ignoré. La comparaison qui a mené au retrait (`bench_rules.py`) reste sur la branche `legacy-expert-mode`.

## Mode natif sous bac à sable (D-038)
- Sonde dans un processus enfant : écriture, modification, suppression, création, renommage, `chmod`, `utime`, `setxattr` et `chown` hors des dossiers autorisés ; écoute sur un autre port ; TCP sortant, UDP, Unix, netlink ; signal ; `exec`. Tout est refusé ; ce qui est autorisé fonctionne.
- Analyse, doublons et archive vérifiée sous bac à sable ; serveur web et analyse par l'API ; commande `scan run` par le vrai point d'entrée.
- Filtre seccomp vérifié par un interpréteur BPF (x86_64, aarch64) ; refus sans Landlock, sans réseau (ABI < 4) ou avec d'autres fils d'exécution.
- Réel (hôte, hors Docker) : sonde, et analyse de `dummy_folder` complète.
- AppImage : entrées de construction épinglées (images, runtime, empreintes des dépendances) ; Python embarqué vérifié à la construction ; `AppRun` en Python isolé, sans commande dangereuse ; entrée de bureau sans argument ; configuration par défaut sans fichier ; bouton « Quitter » natif seulement, protégé par CSRF.
- Réel (AppImage, hôte) : analyse en ligne de commande et par l'interface sous bac à sable, « Quitter », reconnaissance d'un TreeAudit déjà ouvert.

## Cet ordinateur (D-037)
- Faux sysfs : partition de données du disque système proposée comme interne, y compris sous LUKS ; système de fichiers du système remonté ailleurs, ou LUKS du système, refusés ; sans les partitions du système, rien n'est proposé sur ce disque.
- Conteneur : partition interne acceptée, refusée si elle est annoncée comme externe, refusée si c'est une partition du système ; aller-retour JSON des nouveaux champs, valeurs forgées rejetées.
- Parcours : dossier de TreeAudit écarté par identité, même renommé ; chemins privés jamais examinés (aucun `stat`) ; analyse impossible à commencer dans l'un ou l'autre.
- Analyse : disque détecté qui contient TreeAudit (exclusions enregistrées) ; racine déclarée qui le contient toujours refusée ; dossier personnel accepté en entier, mais refusé s'il est déclaré à la main.
- Archive : dossier contenant TreeAudit ou des secrets refusé avant écriture ; un sous-dossier, accepté.
- Web : section « Sur cet ordinateur », sélecteur sans dossiers privés ni dossier de TreeAudit, dossiers qui les contiennent marqués « protégé » et signalés dans le panier.
- Lanceur : dossier personnel et partitions internes activés par défaut, désactivables ; chemin du dossier personnel toujours tiré de `$HOME` (réel) ; Compose en lecture seule récursive.
- Réel : `/mnt/Data` proposé et `data/` reconnu par identité dans le conteneur ; montages vérifiés en lecture seule par `statvfs` ; `.ssh` et `.mozilla` cachés.

## Explorateur et panier (D-036)
- Explorateur : tri par taille, nom (dossiers d'abord) et date ; tri inconnu refusé ; pages ; repères « à nettoyer », « en double » (après une recherche de doublons seulement) et « protégé » ; noms piégés échappés ; pas de case pour le dossier analysé.
- Fiche : autres copies d'un doublon, lien symbolique non suivi, élément protégé, élément ou analyse inconnus (404).
- Panier : en-tête CSRF exigé ; dossier analysé, numéro inconnu, élément compris dans un dossier sélectionné écartés et signalés ; commande corbeille et archive avec des noms piégés, jamais le disque entier ; panier vide refusé.
- Réel (Docker, `dummy_folder`) : dossier de 30 000 éléments affiché en 0,13 s par page, quel que soit le tri ; repères, fiche et commande vérifiés.

## Sécurité
- Aucun accès hors de la racine autorisée, y compris par substitution de lien pendant le scan.
- Liens symboliques extérieurs non suivis.
- Cible montée en lecture seule : aucune écriture possible (test manuel Docker documenté).
- Test statique : aucun appel shell ni fonction d'écriture ou de suppression vers les cibles.
- API : aucune route n'accepte un chemin arbitraire ; identifiants piégés rejetés.
- API : `Host` étranger rejeté ; aucun en-tête CORS ; requête sans `X-TreeAudit-Request` rejetée.
- Interface : un fichier nommé `<script>alert(1)</script>` ou `"><img src=x onerror=alert(1)>` est affiché échappé.
- Erreur → `incomplete` ; interruption → `cancelled`.
- Fichier Compose conforme (D-010, D-011).
- Permissions `0700`/`0600` sur le dossier de données et la base.

## Validation manuelle par distribution (phase 8)
- Linux Mint : natif et Docker (AppArmor).
- Fedora/Nobara : natif et Docker, SELinux désactivé puis enforcing avec `compose.selinux.yaml`.

## Critère de fin
Une phase n'est terminée que si les tests ont réellement été exécutés. Documenter les tests non lancés, les tests ignorés avec leur motif, et les limites restantes.

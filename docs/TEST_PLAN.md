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

## Ranger (D-041)
- Plan : album gardé en entier, projet et installation Steam jamais découpés, fichiers en vrac par type et année, collisions renommées « (2) », éléments cachés, liens et dossiers vides laissés en place.
- Scripts exécutés pour de vrai sur une arborescence de test : rangement sans perte (empreintes identiques), annulation à l'identique, fichier modifié ignoré, place prise jamais écrasée, nom piégé sans effet.
- Plan d'exécution de la descente dans les sous-dossiers (index des parents), page d'aperçu et téléchargements avec des noms piégés.
- Sous-dossiers par règles (D-050, `test_subfolders.py`) : mots normalisés (casse, accents, chiffres, mots vides), thèmes, sujet plus fort qu'un type de papier, thème isolé laissé aux mots communs, aucun sous-dossier qui contiendrait tout, thèmes selon la catégorie, séries d'appareil photo écartées (mais gardées parmi les documents), noms sûrs et jamais vagues, même réponse pour la même entrée, 50 000 éléments en moins de 10 s. Page : proposition sans mode expert, affichée seulement, script inchangé, catégorie inconnue ou interdite sans effet.
- Cas mis de côté (`organize/subfolder_cases.py`), figés avant l'algorithme : `treeaudit subfolders` mesure les règles sur ces cas et sur ceux écrits autrefois pour l'IA. Retrait de l'IA de Ranger (D-051) : ni bouton ni route.

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

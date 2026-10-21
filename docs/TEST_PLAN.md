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
- Manuel : extraction par GNU tar et comparaison avec l'original (contenu, droits, dates, liens).

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

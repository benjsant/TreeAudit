TreeAudit trie et explique le contenu de tes disques, **sans jamais rien supprimer ni modifier**. Il tourne sur ta machine, sans Internet.

## Nouveau dans la 1.0.0-rc.3

- **📷 Ranger date tes photos et vidéos par leur prise de vue** : un bouton lit la date écrite par l'appareil dans la photo (JPEG, HEIC d'iPhone, AVIF, TIFF, fichiers bruts des appareils, PNG, WebP) ou la vidéo (MP4, MOV, 3GP). Une copie ou une sauvegarde qui a changé toutes les dates ne mélange plus les années. Seul l'en-tête de chaque fichier est lu, en lecture seule ; une date déjà lue n'est pas relue.
- **Par mois** : en plus de l'année, `Photos/2023/2023-07/`, si tu le demandes.
- **Les photos d'une même journée** sont proposées en sous-dossiers dès le premier lot de propositions.
- **Graphismes** : les icônes, SVG et jeux d'icônes ne partent plus avec tes photos de vacances.
- **À nettoyer** : les bibliothèques web recopiées (Bootstrap, jQuery, Font Awesome, jeux d'icônes…) sont reconnues par leur nom et ce qu'elles contiennent, toujours « douteuses », jamais cochées d'avance.

## Déjà dans la 1.0.0-rc.2

- **🖼️ Photos ressemblantes** : la même photo réduite, recompressée ou convertie, et les photos d'une rafale. C'est ton navigateur qui ouvre les images : rien ne sort de ton ordinateur. Les simples copies identiques sont séparées des autres versions.
- **Ranger** : tu choisis les catégories à ranger, par exemple « seulement le développement et les logiciels », sans toucher à tes photos ni à tes documents. Une application sans marqueur de projet est déplacée en entier.
- **Doublons** : un programme portable est protégé où qu'il soit rangé ; la progression montre l'étape en cours.
- TreeAudit refuse de tourner en `root`, en Docker aussi.
- De nombreuses corrections, trouvées par deux audits complets et des essais sur de vrais disques (clé USB exFAT, NTFS, btrfs, disque dur USB, disque débranché en pleine lecture).

## Installer : rien à installer

1. Télécharge **`TreeAudit-x86_64.AppImage`** ci-dessous. Pour une machine sans Internet, copie ce fichier sur une clé USB.
2. Rends-le exécutable, une fois : clic droit sur le fichier → Propriétés → « Autoriser l'exécution » (ou `chmod +x TreeAudit-x86_64.AppImage`).
3. Double-clique dessus. Ton navigateur s'ouvre sur TreeAudit. Le bouton « ⏻ Quitter », en haut de la page, l'arrête.

Le fichier peut rester n'importe où. Ni Docker, ni Python, ni droits administrateur.

## Ce qu'il faut

- Linux sur PC (x86_64) avec un **noyau 6.7 ou plus récent** : Ubuntu 24.04, Linux Mint 22, Fedora 40 et plus récents. Sur un noyau plus ancien, TreeAudit refuse de démarrer plutôt que de tourner sans sa protection ; utilise alors la version Docker (guide, chapitre 1).
- FUSE, présent d'office sur ces systèmes. Sinon : `./TreeAudit-x86_64.AppImage --appimage-extract-and-run`.

## Vérifier le fichier (facultatif)

Dans le dossier du téléchargement, avec `SHA256SUMS` à côté :

```bash
sha256sum -c SHA256SUMS
```

La réponse doit être `TreeAudit-x86_64.AppImage: OK`.

## Documentation

Le guide complet est dans le dossier `docs/guide/` du projet.

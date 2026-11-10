TreeAudit trie et explique le contenu de tes disques, **sans jamais rien supprimer ni modifier**. Il tourne sur ta machine, sans Internet.

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

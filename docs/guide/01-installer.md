# 1. Installer et démarrer

TreeAudit existe en deux versions. Elles font la même chose et offrent la même sécurité, chacune à sa façon.

| | Docker | AppImage |
|---|---|---|
| Ce qu'il faut | Docker et son plugin Compose | Linux 6.7 ou plus récent |
| Disques | ceux branchés **avant** le démarrage | branchés à tout moment (« 🔄 Actualiser ») |
| Mode expert (IA locale) | oui | non |
| Données | `data/` et `archives/` dans le dossier du projet | `~/.local/share/treeaudit/` |

Les deux versions ont chacune leur propre index : une analyse faite avec l'une n'apparaît pas dans l'autre.

## Avec Docker

```bash
git clone https://github.com/benjsant/TreeAudit.git
cd TreeAudit
./treeaudit
```

- **Branche et ouvre tes disques avant** (en les ouvrant une fois dans le gestionnaire de fichiers, par exemple). Au démarrage, le terminal liste les disques trouvés.
- La première fois, Docker construit les images : compte quelques minutes. Les fois suivantes, le démarrage prend quelques secondes.
- Ouvre **http://127.0.0.1:8765** dans ton navigateur. Tout se fait là.
- **Arrêter** : `Ctrl+C` dans le terminal, ou `./treeaudit stop` depuis un autre.
- SELinux (Fedora en mode « enforcing ») est détecté tout seul.

Rien n'est installé sur ta machine en dehors de Docker : TreeAudit tourne dans des conteneurs, sans accès à Internet, avec ton utilisateur (jamais `root`).

## Avec l'AppImage

```bash
./treeaudit appimage        # construit dist/TreeAudit-x86_64.AppImage, dans Docker
```

Double-clique ensuite sur le fichier, ou lance-le dans un terminal. Ton navigateur s'ouvre tout seul. Le bouton **« ⏻ Quitter »**, en haut de la page, arrête TreeAudit.

- Le noyau lui-même enferme TreeAudit (Landlock et seccomp) : il ne peut écrire que dans son dossier, ne peut lancer aucun programme ni se connecter à Internet.
- Si ton noyau est trop ancien (avant 6.7), l'AppImage refuse de démarrer plutôt que de tourner sans protection. Utilise alors Docker.
- Relancer l'AppImage alors qu'elle tourne déjà rouvre simplement la page.

## Mettre à jour

```bash
git pull
./treeaudit build     # reconstruit les images avec la nouvelle version
./treeaudit
```

Pour l'AppImage, reconstruis-la avec `./treeaudit appimage` après le `git pull`.

Tes analyses sont gardées d'une version à l'autre : l'index se met à jour tout seul au démarrage.

## Changer un réglage

L'interface suffit pour l'usage normal. Quelques réglages passent par des variables, données au lancement :

```bash
TREEAUDIT_PORT=8800 ./treeaudit        # un autre port
TREEAUDIT_HOME=0 ./treeaudit           # ne pas proposer le dossier personnel
./treeaudit settings                   # affiche les réglages utilisés, sans rien lancer
```

La liste complète est dans le [README](../../README.md#ancien-mode-par-fichier-env-expérimental-non-testé).

## Désinstaller

TreeAudit n'a rien mis ailleurs que dans son dossier. Pour tout retirer :

1. `./treeaudit stop`
2. Garde tes archives si tu en as besoin : elles sont dans `archives/`.
3. Supprime le dossier `TreeAudit` (son index `data/` contient les chemins de tes fichiers : ne le partage pas).
4. Supprime les images Docker : `docker image ls | grep treeaudit`, puis `docker image rm` sur celles listées. Si tu as utilisé le mode expert, l'image `ghcr.io/ggml-org/llama.cpp` aussi.
5. Pour l'AppImage : supprime le fichier et `~/.local/share/treeaudit/`.

# 1. Installer et démarrer

TreeAudit existe en deux versions. Elles font la même chose et offrent la même sécurité, chacune à sa façon.

| | Docker | AppImage |
|---|---|---|
| Ce qu'il faut | Docker et son plugin Compose | Linux 6.7 ou plus récent |
| Disques | ceux branchés **avant** le démarrage | branchés à tout moment (« 🔄 Actualiser ») |
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

## Où sont tes données

| | Docker | AppImage |
|---|---|---|
| Index (tes analyses) | `data/treeaudit.sqlite3` dans le dossier du projet | `~/.local/share/treeaudit/index/treeaudit.sqlite3` |
| Archives faites par le bouton « Archiver » | `archives/` dans le dossier du projet | `~/.local/share/treeaudit/archives/` |
| Fichiers temporaires (grands tris de SQLite) | `data/tmp/` | `$XDG_RUNTIME_DIR/treeaudit/` (vidé à la déconnexion), sinon `~/.cache/treeaudit/` |

**Ce que contient l'index** : les chemins, tailles, dates et droits des fichiers analysés, et les empreintes calculées par la recherche de doublons. **Jamais le contenu des fichiers.** Il reste sur ta machine, lisible par toi seul, mais les chemins en disent long : ne le partage pas.

**Effacer une analyse** : sur l'accueil, « Oublier » à côté de l'analyse, puis « Compacter l'index » pour rendre la place au disque. Rien n'est touché sur tes disques.

**Tout effacer sans désinstaller** : arrête TreeAudit, puis supprime le fichier d'index (et, s'ils existent, `treeaudit.sqlite3-wal` et `treeaudit.sqlite3-shm` à côté). Un index vide est recréé au prochain démarrage.

Deux choses sont écrites ailleurs, mais seulement par **toi**, quand tu lances ce que TreeAudit propose :

- les commandes « archiver » (page *À nettoyer* ou *Panier*) écrivent dans `~/treeaudit-archives/` ;
- les scripts de Ranger que tu télécharges vont dans `~/Téléchargements/`.

## Désinstaller

**Avec Docker** :

1. `./treeaudit stop` (arrête les conteneurs et retire leur réseau).
2. Garde ce dont tu as besoin : `archives/`, et `~/treeaudit-archives/` si tu as lancé des commandes d'archive.
3. Supprime le dossier `TreeAudit` : son index `data/` part avec lui, ainsi que `models/` si tu avais essayé le mode expert.
4. Supprime les images de TreeAudit : `docker image ls | grep treeaudit`, puis `docker image rm treeaudit-app:local treeaudit-dev:local`. Si tu as utilisé le mode expert, l'image `ghcr.io/ggml-org/llama.cpp` aussi.
5. Facultatif, **avec prudence** : la construction a aussi téléchargé `python:3.14-slim` et `ghcr.io/astral-sh/uv`, et laissé un cache de construction (`docker builder prune`). D'autres projets peuvent s'en servir : ne les retire que si tu sais qu'ils ne servent à rien d'autre.

**Avec l'AppImage** :

1. Quitte TreeAudit (« ⏻ Quitter »).
2. Garde `~/.local/share/treeaudit/archives/` et `~/treeaudit-archives/` si tu en as besoin.
3. Supprime le fichier AppImage, `~/.local/share/treeaudit/` et `~/.cache/treeaudit/` (s'il existe).
4. Si tu avais créé un fichier de réglages (ancien mode) : `~/.config/treeaudit/`.

Rien d'autre n'est installé : ni service, ni tâche planifiée, ni programme hors de ces dossiers.

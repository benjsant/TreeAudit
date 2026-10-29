# 7. Projets et logiciels

La tuile **📦 Projets et logiciels** du bilan dresse l'inventaire des projets informatiques, des jeux et des logiciels du dossier analysé. Elle sert à trois choses : **savoir ce qu'il y a**, **retrouver les vieux projets**, et **faire de la place** dans leurs dépendances.

## Ce qui est reconnu

**Un projet** est un dossier qui contient un fichier caractéristique. Il reçoit tous les types dont il a les fichiers (par exemple « Dépôt git » et « Rust »).

| Type | Fichiers |
|---|---|
| JavaScript / TypeScript | `package.json`, `deno.json` |
| Python | `pyproject.toml`, `setup.py`, `setup.cfg`, `requirements.txt`, `Pipfile` |
| Rust, Go | `Cargo.toml`, `go.mod` |
| Java / Kotlin | `pom.xml`, `build.gradle`, `build.gradle.kts`, `settings.gradle` |
| C / C++ | `CMakeLists.txt`, `Makefile`, `meson.build`, `configure.ac` |
| .NET | `*.csproj`, `*.fsproj`, `*.vbproj`, `*.sln` |
| PHP, Ruby, Dart / Flutter, Elixir, Haskell, Swift | `composer.json`, `Gemfile`, `pubspec.yaml`, `mix.exs`, `stack.yaml` ou `*.cabal`, `Package.swift` ou `Podfile` |
| Godot, Unreal, Unity | `project.godot`, `*.uproject`, `Assets/` avec `ProjectSettings/` |
| Dépôt git | `.git` |

**Un logiciel ou un jeu** : une bibliothèque Steam, un jeu GOG, un préfixe Wine, une copie de `Program Files`, un programme Windows (`.exe` et `.dll` côte à côte, un `.exe` seul ne suffit pas), une application macOS (`.app`), un fichier AppImage.

**Un `requirements.txt` ou un `Makefile` seul ne suffit pas toujours.** On en trouve souvent égarés dans des dossiers qui ne sont pas des projets, comme une sauvegarde de clé USB. Ils ne font un projet que d'un dossier qui ne contient aucun autre projet ni logiciel.

**Seul le dossier le plus haut compte.** Un monorepo apparaît une fois, avec la mention « contient N autres projets ». Un logiciel reste pourtant visible même à l'intérieur d'un projet (un programme portable rangé dans un dépôt, par exemple). Rien n'est cherché dans les dépendances (un `package.json` au fond de `node_modules` n'est pas un projet). Les dossiers cachés comme `~/.cache` sont aussi écartés, sauf pour les logiciels, car Steam vit souvent dans `~/.local/share/Steam`.

## Ce que montre chaque élément

- ses types, sa taille sur le disque, son nombre de fichiers ;
- **la date de dernière modification de son vrai contenu**, avec son ancienneté (« il y a 4 ans »). Les dossiers recréables n'en tiennent pas compte : un `node_modules` réinstallé hier ne rajeunit pas un projet de 2019 ;
- un lien **« Explorer »** pour l'ouvrir dans l'explorateur.

En haut de la page : **Voir** tout, les projets seulement, ou les logiciels seulement, et **Trier** par taille ou par **ancienneté** (les plus vieux d'abord).

## Faire de la place

Sous un projet, **« ♻️ … recréables · N cochés sur M »** déplie ses dossiers recréables : dépendances, environnements Python, compilations, caches. Ce sont les mêmes que sur la page [À nettoyer](03-nettoyer.md), avec les mêmes couleurs et les mêmes numéros.

- Les verts sont **déjà cochés**. Le compteur te le rappelle même quand le volet est fermé.
- Quand une règle revient plus de trois fois (les dizaines de `__pycache__` d'un projet Python), ses dossiers sont **regroupés** sur une ligne dépliable : « Cache de bytecode Python : 34 dossiers, 2,4 Mio · 34 cochés ».
- Les chemins sont donnés à partir du projet.
- Sous chacun, quand c'est possible : la commande pour le recréer (`npm ci`, `python3 -m venv .venv`…).
- **« Dernière étape »** prépare la commande, comme pour le nettoyage. L'archive n'est pas proposée ici : archiver des dépendances n'aurait pas de sens.

Pour un vieux projet que tu gardes sans y travailler, supprimer ses dépendances libère souvent l'essentiel de sa place, sans rien perdre de ton travail.

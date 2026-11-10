# Publier une version (D-062)

Une version, c'est un fichier `TreeAudit-x86_64.AppImage` et son empreinte (`SHA256SUMS`) sur la page Releases de GitHub. GitHub les construit ; **rien n'est public avant que le mainteneur publie le brouillon à la main**.

## Avant l'étiquette, sur sa propre machine

1. `./treeaudit check` : lint, types et tests passent.
2. **La sonde du bac à sable sur l'hôte** (règle 17 de `CLAUDE.md`). Sur les machines de GitHub, elle est sautée : leur noyau n'a pas l'ABI Landlock 6. Construire l'AppImage (`./treeaudit appimage`), l'extraire dans un dossier temporaire (`--appimage-extract`), puis lancer `tests/security/sandbox_scripts/probe.py` et `workload.py` avec son Python, comme le fait `tests/security/test_sandbox.py`. Toutes les actions doivent se passer comme prévu.
3. Essai réel de l'AppImage sur l'hôte : démarrage par double-clic, analyse d'un dossier inventé, « Quitter ».
4. Audit des dépendances (`SECURITY.md`, « Dépendances ») si `uv.lock` a changé depuis le dernier.
5. Le numéro de version est le même dans `pyproject.toml`, `src/treeaudit/__init__.py` et `uv.lock` (`./treeaudit lock` après avoir changé `pyproject.toml`). Le test `tests/unit/test_release_tag.py` le vérifie.

## L'étiquette

| Version dans le code | Étiquette |
|---|---|
| `1.0.0rc1` (version d'essai) | `v1.0.0-rc.1` |
| `1.0.0` | `v1.0.0` |

```bash
git tag -a v1.0.0-rc.1 -m "TreeAudit 1.0.0rc1"
git push origin v1.0.0-rc.1
```

Toute autre forme d'étiquette est refusée avant la construction (`tools/check_release_tag.py`), comme une étiquette qui ne correspond pas à la version écrite dans le code.

## Ce que fait GitHub (`.github/workflows/release.yml`)

Vérifie l'étiquette, puis les prérequis, puis lance `./treeaudit check` et `./treeaudit appimage` et calcule `SHA256SUMS`. Il crée enfin une Release **en brouillon** (marquée « pre-release » pour une version `-rc`), avec le texte de `packaging/release-notes.md`.

Un essai sans étiquette : onglet Actions → « release » → « Run workflow ». Il fait les vérifications et la construction, sans Release.

## Publier

1. Sur la page Releases, ouvrir le brouillon.
2. Télécharger l'AppImage et `SHA256SUMS`, et vérifier avec `sha256sum -c SHA256SUMS`.
3. Lancer l'AppImage téléchargée une fois.
4. Relire le texte, puis « Publish release ».

Une version publiée n'est jamais remplacée : une correction devient une nouvelle version.

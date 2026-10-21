# Filtres, motifs et expressions régulières

## Modes de recherche
1. Extension : `.py`, `.jpg`, `.sln`.
2. Glob de nom : `*.py`, `*.jpg`, `backup-????.zip`.
3. Regex sur le nom : `.*\\.pyi?$`.
4. Regex sur le chemin relatif : `(^|/)\\.venv(/|$)`.
5. Règle de dossier : rechercher la présence d'un nom de répertoire dans l'arbre.
6. Recherche dans le contenu textuel : fonctionnalité séparée, désactivée par défaut et limitée.

## Exemples regex Python
- Fichiers Python : `.*\\.pyi?$`
- Images PNG/JPG/JPEG, avec `re.IGNORECASE` : `.*\\.(png|jpe?g)$`
- Répertoires générés connus dans un chemin relatif : `(^|/)(\\.venv|__pycache__|node_modules|build|dist)(/|$)`
- Années 2023 à 2026 dans un nom : `.*202[3-6].*`
- Nom exactement égal à `Cour` : utiliser `fullmatch("Cour", nom)` plutôt qu'une recherche partielle.

Ce sont des filtres de recherche, pas des règles de suppression.

## Interface souhaitée
- Portée : nom, chemin relatif, nom de dossier, extension ou contenu.
- Type de motif : extension, glob ou regex.
- Option de casse.
- Bouton de test sur un échantillon.
- Aperçu du nombre de résultats et de quelques exemples.
- Erreur de syntaxe affichée sans interrompre le scan.
- Motif utilisé conservé dans le rapport.

## Précision et performance
- Compiler le motif une fois, pas pour chaque fichier.
- Appliquer les filtres simples en premier si cela préserve le résultat.
- Ne pas lire les fichiers pour filtrer seulement leurs noms.
- Une regex ne détecte pas des doublons de contenu.
- Ne pas convertir automatiquement une regex en règle d'exclusion.
- Les recherches de contenu, si elles sont ajoutées, doivent avoir des limites explicites.

## Différences
- Glob (`*.txt`) : simple pour les noms.
- Regex (`.*\\.txt$`) : plus expressive, mais plus facile à mal écrire.
- `pathlib`/`glob` et `fnmatch` ont leurs propres règles de segments et de fichiers cachés ; le comportement retenu est fixé ci-dessous.

## Sémantique retenue
- **Glob de nom** : appliqué au seul nom de l'entrée (jamais au chemin), avec la syntaxe de `fnmatch` : `*`, `?`, `[abc]`, `[!abc]`.
- **Glob de chemin** : appliqué au chemin relatif à la racine, séparateur `/`.
  - `*` et `?` ne traversent pas `/` ;
  - `**` en segment complet correspond à zéro ou plusieurs dossiers (`src/**/*.py`) ;
  - le motif doit couvrir le chemin entier.
- **Fichiers cachés** : contrairement au shell, `*` correspond aussi aux noms commençant par `.`. Un audit doit voir `.venv`, `.git`, `.cache`.
- **Casse** : sensible par défaut ; option insensible pour tous les modes (`re.IGNORECASE` pour les globs et les regex, `casefold` pour le nom exact).
- **Regex** : correspondance partielle (`search`, « contient ») par défaut, entière (`fullmatch`) avec `--full-match`. Le « nom exact » est un critère distinct (`--name`). Le mode est affiché à côté du motif dans les résultats.
- **Unicode** : aucune normalisation implicite (NFC/NFD). Un nom en UTF-8 invalide est filtré sur sa forme affichable ; ce cas est signalé.
- **Limites** : longueur de motif maximale (1 000 caractères) ; évaluation annulable (voir [SECURITY.md](SECURITY.md)).

## Implémentation (phase 5)
Commande : `treeaudit search <id-scan> [critères]`, ou `./treeaudit cli search <id-scan> [critères]` en Docker. Tous les critères donnés doivent être vrais (ET).

| Option | Critère |
|---|---|
| `--ext jpg,png` | extension, sans tenir compte de la casse (les extensions sont indexées en minuscules) |
| `--name NOM` | nom exact |
| `--glob '*.py'` | glob sur le nom |
| `--path-glob 'src/**/*.py'` | glob sur le chemin relatif à la racine |
| `--regex R`, `--path-regex R` | regex Python sur le nom ou le chemin ; correspondance partielle (`search`) par défaut, entière avec `--full-match` |
| `-i` | casse ignorée (Unicode : `ÉTÉ` correspond à `été`) |
| `--kind file,dir,symlink,other` | type d'entrée |
| `--min-size 10M`, `--max-size 1G` | taille, en unités binaires ; ne concerne que les fichiers |
| `--sort size\|path\|mtime`, `--limit N` | exemples affichés |
| `--time-budget S` | durée maximale (30 s par défaut) ; au-delà, résultat marqué partiel |

Précisions :
- Les globs correspondent toujours au texte entier. `--ext` et les tailles limitent la recherche aux fichiers.
- Sans normalisation Unicode : `café` en NFC et en NFD sont deux noms différents.
- Un nom en UTF-8 invalide est comparé sous sa forme affichée (`\xNN`) ; ces résultats sont comptés et signalés.
- La recherche lit l'index seulement, n'écrit rien, et garde en mémoire uniquement les exemples affichés.
- **Aucune commande de suppression n'est générée à partir d'un filtre** (D-027).

### Expressions régulières dangereuses (ReDoS)
Le moteur `re` de Python ne peut pas être interrompu pendant une correspondance, et les sous-processus sont exclus (CLAUDE.md, règle 2). Trois protections :
1. motif limité à 1 000 caractères ;
2. **refus** des motifs dont le temps peut exploser : une répétition à l'intérieur d'une répétition (`(a+)+`, `(\w+\s?)+`, `(.*a){10}`) ou une alternative répétée (`(a|aa)*`). Le refus est volontairement prudent : `(jpg|png)+` est aussi refusé ; `.*\.(jpg|png)$` est accepté ;
3. budget de temps vérifié toutes les 2 000 entrées.

L'analyse du motif utilise le module interne `re._parser` (stable depuis Python 3.11) ; s'il disparaissait, seules les protections 1 et 3 resteraient.

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
- **Casse** : sensible par défaut, option insensible (`casefold`) pour tous les modes.
- **Regex** : `fullmatch` par défaut pour le mode « nom exact », `search` pour le mode « contient ». Le mode est affiché à côté du motif dans les résultats et les rapports.
- **Unicode** : aucune normalisation implicite (NFC/NFD). Un nom en UTF-8 invalide est filtré sur sa forme affichable ; ce cas est signalé.
- **Limites** : longueur de motif maximale (1 000 caractères) ; évaluation annulable (voir [SECURITY.md](SECURITY.md)).

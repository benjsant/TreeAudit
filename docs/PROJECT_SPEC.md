# Spécification fonctionnelle

## Vision
Aider l'utilisateur à comprendre et réduire le désordre d'un disque de sauvegarde en mettant en évidence doublons, arborescences proches, répertoires volumineux et contenus potentiellement régénérables. L'outil présente des preuves et des recommandations ; il ne décide pas seul de supprimer des données.

## Plateforme et distribution
- V1 : Linux uniquement, validée sur Linux Mint et Fedora/Nobara (D-001).
- Application locale avec interface web et backend Python.
- Deux modes d'exécution : natif (Python installé localement) et Docker Compose (recommandé) (D-007).
- Pour le mode Docker, Docker doit être installé par l'utilisateur ; ce prérequis et les ressources nécessaires seront expliqués.
- Les dossiers à auditer sont déclarés dans la configuration, validés, et montés en lecture seule en Docker (D-008, D-009).
- Windows est réservé à une future V2 après stabilisation de la V1 Linux.

## MVP
1. Inventorier une racine explicitement autorisée.
2. Parcourir fichiers et dossiers sans suivre les liens symboliques par défaut.
3. Calculer nombre de fichiers/dossiers, taille logique et extensions.
4. Afficher les erreurs et permissions empêchant un audit complet.
5. Filtrer par nom, extension, glob et regex.
6. Repérer les fichiers identiques par taille puis empreinte cryptographique.
7. Générer un rapport local lisible.
8. Afficher la progression et permettre d'interrompre proprement un scan.
9. Stocker les métadonnées d'indexation dans SQLite, sans stocker le contenu des fichiers.

## V1, après le MVP
- Heuristiques pour répertoires potentiellement régénérables (`.venv`, `venv`, `__pycache__`, `node_modules`, `build`, `dist`, `bin`, `obj`, caches connus). Chaque signalement indique la règle appliquée, les indices trouvés et un niveau de confiance. Leur présence ne prouve pas qu'ils sont inutiles.
- Comparaison d'arbres : identité et similarité expliquée.

## Après la V1
- Visionneuse texte en lecture seule, avec limites de taille et avertissement sur les données sensibles.
- LLM local pour aider à interpréter des résultats ambigus, avec contexte minimisé.
- Suggestions d'archivage/compression.
- Éventuelle quarantaine réversible après spécification et revue de sécurité séparées.

## Hors périmètre V1
- Windows/macOS.
- Nettoyage automatique.
- Analyse complète du système hôte.
- Synchronisation cloud/envoi de fichiers.
- Exécution de scripts ou commandes générés par l'IA.

## Définitions
- **Doublon exact** : mêmes octets, vérifiés par empreinte et, si nécessaire, comparaison finale.
- **Arborescence identique** : contenu relatif équivalent selon une définition documentée, indépendamment du chemin absolu.
- **Arborescence similaire** : résultat heuristique, avec critères affichés ; ce n'est pas une preuve.
- **Régénérable potentiel** : dossier correspondant à une convention connue, qui peut toutefois contenir des données utiles.
- **Doublon exact** et **lien physique** sont distincts : deux liens physiques vers le même inode ne consomment l'espace qu'une fois et ne sont pas des doublons (D-014).
- **Scan complet** : tout le périmètre a été examiné ; les exclusions décidées (points de montage non traversés, liens non suivis, règles d'exclusion) sont listées dans le rapport (D-012).
- **Scan incomplet** : des erreurs subies (permission, E/S, entrée disparue, limite atteinte) empêchent de garantir que tout le périmètre a été examiné.
- **Scan annulé** : interrompu par l'utilisateur ; résultats partiels marqués comme tels.
- **Taille logique / allouée** : taille déclarée du fichier / espace réellement occupé sur le disque (D-014).

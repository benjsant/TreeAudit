# 3. Nettoyer

La page **🧹 À nettoyer** liste ce qui se recrée tout seul ou avec une commande : dépendances de projets (`node_modules`, `.venv`), caches d'outils (`__pycache__`, `.mypy_cache`), sorties de compilation (`target`, `obj`), restes de systèmes (`__MACOSX`, `.DS_Store`, `Thumbs.db`)… TreeAudit connaît 41 règles.

## Les couleurs

Chaque élément reçoit un **pourcentage de confiance**, calculé à partir d'indices. Par exemple, un `node_modules` à côté d'un `package.json` est presque certainement recréable.

| Couleur | Confiance | Conseil |
|---|---|---|
| 🟢 **Sans risque** | 80 % et plus | **déjà coché** : recréable, sa suppression est très probablement sans conséquence |
| 🟠 **À vérifier** | de 50 à 79 % | probablement inutile, mais jette un œil avant |
| 🔴 **Douteux** | moins de 50 % | replié par défaut : ne coche que si tu es sûr |

Les éléments sous 30 % ne sont pas affichés. Le seuil se change dans « Options », en bas de la page.

**« Pourquoi ? »**, sous chaque élément, détaille le calcul : le point de départ de la règle, chaque indice trouvé ou absent avec son poids, et un éventuel plafond. Quand c'est possible, il donne aussi **la commande pour le recréer plus tard** (par exemple `npm ci` dans le dossier du projet).

Ce ne sont que des estimations. Un dossier vert peut, exceptionnellement, contenir quelque chose d'important : en cas de doute, ouvre-le dans l'explorateur.

## Préparer la commande

1. Coche ce que tu veux (les éléments verts le sont déjà). « Tout décocher » repart de zéro.
2. Dans **« Dernière étape »**, choisis l'action :
   - **Corbeille** (recommandé) : récupérable tant que tu n'as pas vidé la corbeille. Vide-la ensuite pour libérer vraiment la place.
   - **Suppression définitive** : impossible à annuler.
   - **Archive** : une copie compressée de la sélection, sans rien supprimer.
3. **« Préparer la commande »**, puis **« 📋 Copier »**.
4. Ouvre un terminal, colle (`Ctrl+Maj+V`), **relis**, puis appuie sur Entrée.

Une sélection qui contient des éléments orange ou rouges est signalée au-dessus de la commande. Les avertissements de la [double vérification](10-commandes-et-securite.md) s'affichent au même endroit.

## Avec l'IA locale

En [mode expert](09-mode-expert.md), le bouton **« 💡 Avis de l'IA »** demande à une IA locale son avis sur les éléments orange et rouges, à partir de leurs noms, types et tailles seulement. Ses avis s'affichent sous chaque élément. **Ils ne cochent ni ne décochent rien** : c'est toi qui décides.

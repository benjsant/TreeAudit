# 2. Première analyse

## Choisir ce que tu veux analyser

La page d'accueil propose ce que TreeAudit a le droit de lire :

- **🔌 les disques externes** branchés et ouverts (sous `/run/media/<toi>`, `/media` ou `/mnt`), avec leur occupation ;
- **💽 les partitions de données** du disque système, comme `/mnt/Data`. Jamais celles du système lui-même (`/`, `/boot`, `/home`, `/usr`, `/var`) ;
- **🏠 ton dossier personnel**, en entier, caches cachés compris. Les dossiers de secrets (clés SSH et GPG, mots de passe, profils de navigateur et de messagerie) ne sont **jamais lus**.

Un disque refusé est listé avec la raison : disque réseau, image ISO, ou disque dont TreeAudit ne peut pas prouver qu'il n'est pas celui du système. En cas de doute, il refuse.

Tu peux analyser un disque entier ou seulement un de ses dossiers : navigue jusqu'au dossier voulu, puis clique sur **« Analyser »**.

## Pendant l'analyse

La page montre la progression : le nombre d'éléments lus et le dossier en cours. Tu peux **annuler** à tout moment. Une seule tâche tourne à la fois (analyse, doublons, archive…) : tant qu'elle n'est pas finie ou annulée, TreeAudit refuse d'en lancer une autre, et le dit.

TreeAudit lit seulement les **noms, tailles, dates et droits**, jamais le contenu des fichiers (sauf pour les doublons, l'aperçu des images et les archives, quand tu les demandes). Il ne suit jamais les liens symboliques.

Une analyse se termine toujours dans l'un de ces états :

| État | Sens |
|---|---|
| **terminée** | tout a été lu. |
| **incomplète** | certains éléments n'ont pas pu être lus (droits, erreur du disque) : ils sont listés en bas du bilan. Les chiffres sont alors des minimums. |
| **annulée** | tu l'as arrêtée : ce qui a été lu est gardé, mais rien n'est complet. |
| **échouée** | une erreur a empêché l'analyse (disque débranché…) : le message l'explique. |

TreeAudit ne présente jamais une analyse comme complète si elle ne l'est pas.

## Lire le bilan

Le bilan est la page de départ de tout le reste. On y trouve :

| Tuile | Ce qu'elle montre | Chapitre |
|---|---|---|
| 🧹 **À nettoyer** | la place récupérable sans risque (caches, dépendances…) | [3. Nettoyer](03-nettoyer.md) |
| 📑 **Doublons** | la place prise par les copies en trop, une fois cherchées | [4. Doublons](04-doublons.md) |
| 📊 **Où va la place ?** | les plus gros dossiers, et l'accès à l'explorateur et à Ranger | [5. Explorer](05-explorer.md), [6. Ranger](06-ranger.md) |
| 📦 **Projets et logiciels** | les projets informatiques, jeux et logiciels trouvés | [7. Projets](07-projets.md) |

En dessous : une **recherche** par nom, et des **détails techniques** (types de fichiers, éléments inaccessibles, éléments écartés, exports JSON et CSV).

## Gérer les analyses

La page d'accueil liste les analyses déjà faites. Tu peux y revenir à tout moment, même après avoir arrêté TreeAudit.

- **« Oublier »** efface ce que TreeAudit a enregistré pour une analyse (index, doublons). **Jamais tes fichiers.**
- **« Compacter l'index »** rend au disque la place libérée par les analyses oubliées. Il tourne en tâche de fond.
- La taille de l'index est affichée sous la liste.

Une analyse est une photo du disque à un instant donné. Si le disque a changé depuis (fichiers ajoutés, supprimés, déplacés), **relance une analyse** avant de préparer des commandes.

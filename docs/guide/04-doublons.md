# 4. Doublons

TreeAudit trouve les **doublons exacts** : des fichiers au contenu identique, octet par octet, quels que soient leur nom et leur emplacement.

## Lancer la recherche

Depuis le bilan, tuile **📑 Doublons** :

- **« Chercher les doublons »** : les fichiers de 1 Kio et plus.
- **« Petits fichiers en double »** : les fichiers de moins de 1 Kio et les fichiers vides, séparément, parce qu'ils sont nombreux et rapportent peu de place.

C'est la seule analyse qui **lit le contenu** des fichiers. TreeAudit ne lit que les fichiers de même taille, d'abord leur début et leur fin, puis en entier seulement si nécessaire, dans l'ordre physique du disque pour ménager les disques mécaniques. Une deuxième recherche réutilise les empreintes déjà calculées pour les fichiers qui n'ont pas changé (même inode, même taille, même date) : elle est presque instantanée.

## Lire un groupe

Chaque groupe montre le nombre d'exemplaires, leur taille, et la place récupérable. **Une copie est toujours gardée**, marquée ★. TreeAudit la choisit dans cet ordre :

1. les copies **protégées** sont toujours gardées : celles qui sont dans un logiciel installé, un jeu, des dépendances ou un dépôt git. Supprimer un fichier d'un jeu pour garder sa copie dans « Téléchargements » casserait le jeu ;
2. sinon, le seul chemin **sans indice de copie** (« copie », « (1) », « backup », « old », « Downloads »…) ;
3. sinon, le fichier le plus **ancien**, probablement l'original ;
4. sinon, le chemin le plus **court**.

« Voir les copies / changer celle à garder » affiche tous les chemins et te laisse en choisir une autre.

## Préparer la commande

Coche les groupes à nettoyer (« Tout cocher » ignore les fichiers vides), puis passe par **« Dernière étape »**, comme pour le [nettoyage](03-nettoyer.md#préparer-la-commande). La commande ne vise que les copies en trop, jamais celle marquée ★ ni les copies protégées.

## Limites

- Ce ne sont que des doublons **exacts**. Une photo redimensionnée ou un document réenregistré ne sont pas des doublons pour TreeAudit.
- Les dossiers presque identiques ne sont pas encore détectés. Des outils comme [Czkawka](https://github.com/qarmin/czkawka) font très bien les images similaires.
- Si un fichier est modifié par un outil qui lui remet exactement la même date, la recherche suivante ne le relit pas. C'est le même critère que `rsync`.

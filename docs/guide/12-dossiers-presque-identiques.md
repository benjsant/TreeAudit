# 12. Dossiers presque identiques

Une sauvegarde et sa copie, un ancien et un nouveau classement, le même dossier recopié sur plusieurs disques… **« 🪞 Dossiers presque identiques »**, dans la carte *Doublons* du bilan, les retrouve et montre ce qui est pareil et ce qui diffère. **Rien n'est supprimé.**

## Avant : chercher les doublons

La comparaison s'appuie sur la recherche de doublons : c'est elle qui lit les fichiers et sait lesquels sont identiques. Si elle n'a pas été faite, la page propose de la lancer.

## La liste

Chaque bloc est une **famille** : deux dossiers, ou plus quand un dossier existe en plusieurs exemplaires. Le plus gros vient en premier ; chaque autre lui est comparé :

- **« 100 % de celui-ci se retrouve dans le premier »** : tout son contenu est aussi dans le premier ;
- le badge **« entièrement dans le premier »** ou **« contient tout le premier »** n'apparaît que si c'est un **fait** vérifié ;
- **« Comparer en détail »** ouvre la comparaison fichier par fichier.

Le pourcentage compte les octets **et** les fichiers, et retient le plus faible : quelques gros fichiers en commun ne suffisent pas.

## Le détail

| Liste | Ce que c'est |
|---|---|
| Identiques, au même endroit | même chemin dans les deux dossiers, même contenu |
| Identiques, mais ailleurs | même contenu, déplacé ou renommé |
| Modifiés | même chemin, contenu différent |
| Seulement dans A / dans B | absents de l'autre dossier |
| **Suppositions** | même chemin et même taille, mais contenu jamais lu |

Les **suppositions** sont des fichiers de moins de 1 Kio (la recherche de doublons ne les lit pas) ou illisibles. Lance **« Petits fichiers en double »** depuis le bilan pour les vérifier : ils deviennent alors des faits.

## Et ensuite ?

Si un dossier est **entièrement** dans un autre, tu peux décider de le supprimer : passe par l'**explorateur** et le **panier** (chapitre 5), qui préparent une commande que tu relis et lances toi-même. Vérifie d'abord la liste « Seulement dans… » : c'est ce que tu perdrais.

Les dates des fichiers ne servent jamais à trancher. Les dossiers vides et les liens ne sont pas comparés. La comparaison se fait à l'intérieur d'une analyse : pour comparer deux disques, analyse un dossier qui les contient tous les deux.

# Guide d'utilisation de TreeAudit

TreeAudit t'aide à faire le tri dans un disque ou un dossier : ce qui prend de la place, ce qui est en double, ce qui se recrée tout seul, ce qui peut être rangé. **Il ne modifie jamais rien lui-même** : il lit, il explique, et il te **propose** des commandes que tu relis et lances toi-même.

Ce guide suit l'ordre dans lequel on découvre l'application. Chaque chapitre se lit seul.

## Sommaire

| Chapitre | Pour… |
|---|---|
| [1. Installer et démarrer](01-installer.md) | lancer TreeAudit avec Docker ou l'AppImage, l'arrêter, le mettre à jour, le désinstaller |
| [2. Première analyse](02-premiere-analyse.md) | choisir un disque, lancer l'analyse, lire le bilan, gérer les analyses |
| [3. Nettoyer](03-nettoyer.md) | comprendre les dossiers « à nettoyer », leurs couleurs, et préparer une commande |
| [4. Doublons](04-doublons.md) | trouver les fichiers en double et choisir la copie à garder |
| [5. Explorer, chercher, panier et galerie](05-explorer.md) | parcourir les fichiers, voir les photos, tout sélectionner au même endroit |
| [6. Ranger un dossier](06-ranger.md) | proposer un rangement par catégories, l'ajuster, et obtenir les scripts |
| [7. Projets et logiciels](07-projets.md) | retrouver les projets informatiques et les jeux, leur âge, et la place recréable |
| [8. Archives](08-archives.md) | faire une copie compressée et vérifiée avant de nettoyer |
| [9. Mode expert (IA locale)](09-mode-expert.md) | demander l'avis d'une IA locale, sans qu'elle puisse rien toucher |
| [10. Commandes proposées et sécurité](10-commandes-et-securite.md) | ce que contient une commande, la double vérification, et ce qu'il ne faut jamais faire |
| [11. Dépannage](11-depannage.md) | les problèmes courants et leurs solutions |

## Les trois règles à retenir

1. **TreeAudit ne touche à rien.** Les disques sont ouverts en lecture seule. Les seules choses qu'il écrit sont son index et, si tu le demandes, une archive vérifiée.
2. **Les commandes, elles, agissent pour de vrai.** Relis toujours une commande avant de la lancer, et préfère la **corbeille** à la suppression définitive.
3. **Jamais `sudo`.** Aucune commande de TreeAudit n'en a besoin. Si un terminal te demande un mot de passe, arrête-toi (`Ctrl+C`).

## Mots utilisés dans ce guide

| Mot | Sens |
|---|---|
| **Analyse** | Une lecture complète d'un dossier, dont le résultat est gardé dans l'index. |
| **Index** | La base de données locale de TreeAudit (`data/`) : les chemins, tailles et dates de tes fichiers, jamais leur contenu. |
| **Racine** | Un disque ou un dossier que TreeAudit a le droit de lire. |
| **Recréable** | Un dossier qui se reconstruit tout seul ou avec une commande (`node_modules`, `.venv`, caches…). |
| **Place occupée** | Ce qu'un élément prend vraiment sur le disque, qui peut différer de sa taille (voir les [fichiers creux](05-explorer.md#fichiers-creux)). |
| **Commande** | Le texte que TreeAudit prépare pour toi, à copier dans un terminal. Il ne la lance jamais. |

Pour les détails techniques : [ARCHITECTURE.md](../ARCHITECTURE.md), [SECURITY.md](../SECURITY.md), et le journal des choix dans [DECISIONS.md](../DECISIONS.md).

# 9. Pourquoi pas d'IA

TreeAudit a eu un **mode expert** : une IA locale, sur la carte graphique, donnait son avis sur les dossiers à vérifier et proposait des sous-dossiers pour ranger. Il a été **retiré** après avoir été mesuré (décisions D-050 à D-052).

## Ce que les mesures ont montré

- **Pour ranger**, des règles fixes font mieux que l'IA, en un instant et pour tout le monde : elles regroupent d'après les mots des noms, et disent pourquoi.
- **Pour nettoyer**, les règles seules ne cochent jamais un dossier à garder. Sur 14 cas où l'IA pouvait donner son avis, elle a donné une réponse dangereuse : « nettoyable » pour une bibliothèque dont un site a besoin. Ses alertes « à garder » portaient sur des dossiers que les règles mettaient déjà en rouge.

## Pourquoi pas une IA plus puissante

- **Sur ta machine**, un modèle nettement meilleur demanderait une carte graphique bien plus grosse, et la ferait tourner à pleine charge.
- **En ligne** (OpenAI, DeepSeek…), il faudrait envoyer les noms de tes fichiers et de tes dossiers à un service extérieur. TreeAudit ne le fera pas : rien de ce qu'il voit ne quitte ta machine.

## Si tu l'avais activé

- `./treeaudit expert` ne fait plus que le dire, et TreeAudit démarre sans IA.
- Le modèle téléchargé reste dans le dossier `models/` du projet : **supprime ce dossier** pour libérer environ 2 Gio.
- Tu peux aussi retirer l'image Docker de l'IA : `docker image ls | grep llama.cpp`, puis `docker image rm` sur celle listée.

Le code du mode expert est conservé sur la branche `legacy-expert-mode` du dépôt.

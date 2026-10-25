# 10. Commandes proposées et sécurité

TreeAudit ne supprime, ne déplace et ne modifie jamais rien de lui-même. Il te **propose** des commandes. Ce chapitre explique ce qu'elles contiennent et comment elles sont protégées, parce que **ces commandes, elles, agissent pour de vrai**.

## À quoi ressemble une commande

```bash
gio trash -- \
  '/run/media/moi/Disque/projets/site/node_modules' \
  '/run/media/moi/Disque/projets/api/.venv'
```

- **`gio trash`** met à la corbeille, **`rm -rf`** supprime définitivement, **`tar`** archive. Il n'y a jamais d'autre programme.
- Chaque chemin est entre apostrophes, et précédé de `--` : un nom de fichier piégé comme `$(commande)`, `-rf` ou `a; b` reste un simple nom, jamais une instruction.
- Un chemin qui contient des caractères invisibles (retour à la ligne, tabulation) ou un encodage invalide ne reçoit **aucune commande** : il ne pourrait pas être affiché sans ambiguïté. Il est signalé.

## La double vérification

Avant d'être affichée, chaque commande passe deux contrôles indépendants.

1. **Chaque cible, avant d'être écrite**, doit se trouver strictement à l'intérieur du dossier analysé. Elle ne peut **jamais** être :
   - `/`, ni un dossier de premier niveau (`/data`…) ;
   - un dossier du système (`/usr`, `/etc`, `/boot`, `/root`…) ;
   - un dossier personnel entier (`/home/toi`) ;
   - un disque entier (`/media/disque`, `/run/media/toi/disque`, `/mnt/Data`) ;
   - un disque, une racine ou le dossier d'archives que TreeAudit connaît ;
   - le dossier analysé lui-même, ni un dossier qui contient l'un d'eux.
2. **Le texte terminé est relu** comme le terminal le lirait. Il doit être exactement le programme prévu suivi des cibles choisies, en même nombre. Un `sudo`, un `;`, une option comme `--no-preserve-root`, un `/` ou une cible en trop font refuser **toute** la commande.

Une cible refusée est **affichée avec sa raison**, jamais retirée en silence. S'il ne reste rien, il n'y a pas de commande et le bouton « Copier » disparaît.

Les commandes de recréation (`npm ci`…) et les scripts de [rangement](06-ranger.md) sont contrôlés de la même façon : jamais de programme privilégié ni destructeur, jamais de chemin absolu ou contenant `..`.

## Ce qu'il ne faut jamais faire

- **Ne jamais ajouter `sudo`** devant une commande. Aucune n'en a besoin. Si le terminal te demande un mot de passe, **arrête-toi** avec `Ctrl+C` : quelque chose ne va pas.
- **Ne pas lancer une commande préparée sur une vieille analyse** si le disque a changé depuis. Relance une analyse d'abord.
- **Ne pas modifier une commande** sans comprendre ce que tu changes.
- **Ne pas supprimer l'original** en te fiant à une archive marquée `-INCOMPLET`.

## Ce que TreeAudit garantit de son côté

- Les disques sont ouverts **en lecture seule** ; les liens symboliques ne sont **jamais suivis**, même s'ils apparaissent pendant l'analyse.
- **Rien ne sort de ta machine** : le serveur n'est joignable que depuis ton ordinateur (`127.0.0.1`), et ce qui lit tes fichiers n'a pas accès à Internet.
- TreeAudit tourne avec **ton utilisateur**, jamais `root`, sans aucun privilège. L'AppImage est en plus enfermée par le noyau.
- Les dossiers de secrets de ton dossier personnel ne sont **jamais lus**.
- L'index (`data/`) contient les **chemins** de tes fichiers, jamais leur contenu. Il reste sur ta machine, mais ne le partage pas.

Le modèle de sécurité complet, avec les menaces couvertes et les limites, est dans [SECURITY.md](../SECURITY.md).

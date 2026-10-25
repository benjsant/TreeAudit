# 8. Archives

Avant de nettoyer un dossier dont tu n'es pas sûr, tu peux en faire une **archive vérifiée** : une copie compressée (`.tar.gz`), relue et comparée à l'original fichier par fichier. C'est la **seule chose que TreeAudit écrit** en dehors de son index.

## Créer une archive

**« 🗜️ Archiver ce dossier »**, dans l'explorateur ou sur la fiche d'un dossier.

1. TreeAudit vérifie d'abord qu'il y a assez de place.
2. Il écrit l'archive sous un nom temporaire.
3. Il la **relit** et compare chaque fichier à l'original.
4. Il ne la garde que si tout concorde. Sinon, il la retire.

L'archive va dans le dossier `archives/` de TreeAudit, **sur ton ordinateur**. C'est donc une vraie sauvegarde d'un disque externe.

Tu peux aussi demander « Commande pour tout archiver » dans « Dernière étape » : c'est alors une commande `tar` que tu lances toi-même, sans la vérification ci-dessus.

## Mes archives

La page **« 🗜️ Mes archives »** les liste avec leur taille et leur date.

| Ce que tu vois | Sens |
|---|---|
| **Archive complète et vérifiée** | elle contient tout le dossier. |
| **`-INCOMPLET`** dans le nom | il manque des éléments (fichier illisible, modifié pendant la lecture, fichier spécial). La liste s'affiche. **Ne supprime pas l'original en te fiant à elle.** |
| un fichier caché `.….partial` | une archive inachevée, après une coupure de courant par exemple. Tu peux la supprimer. |

## Restaurer

```bash
tar -xzf archive.tar.gz
```

Pour un nom de fichier qui n'est pas en UTF-8, GNU tar affiche un avertissement « mot clé inconnu hdrcharset » sans conséquence : le nom est restauré à l'identique.

## Limites

- Les dates sont gardées à la seconde près.
- Un fichier qui a plusieurs liens physiques est copié en entier pour chaque nom.
- Un [fichier creux](05-explorer.md#fichiers-creux) est archivé à sa taille apparente avant compression.
- Une archive sur le même disque que l'original n'est pas une vraie sauvegarde : si le disque meurt, les deux partent.

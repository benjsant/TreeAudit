# 11. Dépannage

## Mon disque n'apparaît pas

- **Avec Docker**, les disques sont détectés **au démarrage**. Branche le disque, **ouvre-le** une fois dans le gestionnaire de fichiers (pour qu'il soit monté), puis relance : `Ctrl+C`, puis `./treeaudit`.
- **Avec l'AppImage**, clique sur **« 🔄 Actualiser »** sur la page de choix.
- `./treeaudit disks` liste les disques proposés **et refusés**, avec la raison. Un disque réseau, une image ISO, ou un disque dont TreeAudit ne peut pas prouver qu'il n'est pas celui du système, sont refusés volontairement.

## L'analyse est « incomplète »

Certains éléments n'ont pas pu être lus, le plus souvent faute de droits. Ils sont listés en bas du bilan, dans « Détails techniques ». Les chiffres affichés sont alors des minimums. TreeAudit ne demandera jamais `sudo` pour les lire.

## Une commande ne vise pas tout ce que j'ai coché

Regarde les avertissements au-dessus de la commande :

- **« Ignoré (nom non affichable sans risque) »** : le nom contient des caractères invisibles ou un encodage invalide.
- **« Refusé par la double vérification »** : la cible est un disque entier, un dossier du système, le dossier analysé lui-même… La raison est donnée. Voir [10. Commandes et sécurité](10-commandes-et-securite.md#la-double-vérification).

## « Aperçu impossible » dans la galerie

L'image a été modifiée ou déplacée depuis l'analyse, son contenu n'est pas une image reconnue (malgré son extension), ou elle dépasse 20 Mio. Relance une analyse si le dossier a changé.

## Une taille me paraît fausse

- Un dossier montre la **place qu'il occupe** sur le disque, un fichier sa **taille**. Un petit fichier de 32 o occupe pourtant 4 Kio : c'est la taille d'un bloc du disque.
- Un fichier marqué **creux** montre la place qu'il occupe vraiment, bien inférieure à sa taille apparente. Voir les [fichiers creux](05-explorer.md#fichiers-creux).
- Un dossier marqué « taille inconnue » n'a pas pu être lu entièrement.

## TreeAudit est lent

- La première analyse d'un disque mécanique (USB, 5 400 tours) est plus lente : le disque doit tout lire. Les suivantes profitent du cache du système.
- Les doublons sont l'étape la plus longue, car ils lisent le contenu. Une deuxième recherche réutilise les empreintes et ne relit que ce qui a changé.
- Un très gros index peut se réduire : « Oublier » les analyses inutiles, puis « Compacter l'index », sur la page d'accueil.

## L'AppImage refuse de démarrer

Elle exige Linux 6.7 ou plus récent, pour pouvoir s'enfermer elle-même (Landlock). Sur un noyau plus ancien, elle refuse plutôt que de tourner sans protection. Utilise Docker. `uname -r` affiche la version de ton noyau.

## Le mode expert ne démarre pas

- Relance `./treeaudit expert` : il dit ce qui manque (carte graphique, place disque, modèle) et le corrige quand il le peut.
- « Mode expert ignoré : models/.expert ne désigne pas un modèle téléchargé » : le modèle a été supprimé. `./treeaudit expert` le télécharge à nouveau.
- Carte NVIDIA, ou pas assez de mémoire vidéo : voir [9. Mode expert](09-mode-expert.md#ce-quil-faut).

## Signaler un problème

Ouvre une [issue](https://github.com/benjsant/TreeAudit/issues) en décrivant ce que tu as fait, ce que tu attendais et ce qui s'est passé. **N'y colle pas tes chemins de fichiers ni ton index** s'ils sont personnels : un exemple reconstruit suffit.

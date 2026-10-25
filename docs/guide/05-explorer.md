# 5. Explorer, chercher, panier et galerie

## L'explorateur

**« 📂 Explorer les fichiers »**, depuis le bilan, se parcourt comme un gestionnaire de fichiers : clique sur un dossier pour y entrer, et sur le fil d'Ariane, en haut, pour remonter.

- **Tri** par taille, nom ou date. En tri par taille, une barre montre la part de chaque élément dans le dossier.
- **200 éléments par page** : les dossiers immenses restent rapides.
- **Repères** à côté des noms :

| Repère | Sens |
|---|---|
| 🧹 | à nettoyer (voir [3. Nettoyer](03-nettoyer.md)) |
| 📑 | en double (une fois les doublons cherchés) |
| 🛡️ | protégé : dans un projet, un logiciel ou un dépôt git |
| `creux` | [fichier creux](#fichiers-creux) |

- **ⓘ** ouvre la **fiche** d'un élément : chemin complet, taille, place occupée, date, droits, raison de ses repères, et les autres copies s'il est en double.

Rien de ce que tu fais dans l'explorateur ne modifie le disque.

## Fichiers creux

Un fichier, en principe, montre sa **taille** et un dossier la **place qu'il occupe** sur le disque.

Un **fichier creux** (une image disque, une machine virtuelle, une base de données préallouée) peut paraître énorme sans rien occuper : 1 Gio qui ne prend que 0 o. TreeAudit montre alors la place réellement occupée, avec le repère **creux**. La fiche donne les deux : « Il paraît faire 1,0 Gio mais n'occupe que 0 o. Le supprimer ne libérerait que 0 o. »

## Le panier

Coche des éléments dans n'importe quels dossiers : ils vont tous dans le **🧺 panier**, affiché en haut de chaque page. « Voir le panier » les liste avec leur taille totale et prépare la commande.

- Le panier se souvient de ta sélection pendant que tu navigues.
- Le dossier analysé lui-même n'est jamais proposé.
- Un élément déjà compris dans un dossier sélectionné est retiré, pour ne pas le compter deux fois.
- Un élément protégé (projet, logiciel) est signalé.

Le navigateur n'envoie que des numéros : c'est le serveur qui relit chaque chemin dans l'index pour construire la commande.

## La galerie

Un dossier qui contient des images propose **« 🖼️ Galerie »** : les images s'affichent en vignettes, 40 par page, chacune avec sa case pour le panier. La fiche d'une image la montre aussi en grand.

- Formats affichés : JPEG, PNG, GIF, WebP, BMP, AVIF. Jamais le SVG, qui peut contenir du code.
- Le type est reconnu au **contenu** du fichier, pas à son nom : un `.jpg` qui n'est pas une image affiche « Aperçu impossible ».
- L'image est lue sur le disque au moment où tu la regardes, telle que l'analyse l'a vue. Si elle a été modifiée ou déplacée depuis, elle n'est pas montrée.
- Les images de plus de 20 Mio ne sont pas affichées.
- Aucune copie de tes photos n'est gardée par TreeAudit.
- Les formats HEIC (iPhone) et RAW ne s'affichent pas : ton navigateur ne sait pas les lire.

## Chercher

La recherche du bilan trouve les éléments dont le **nom contient** un mot. **« Recherche avancée »** ajoute l'extension, la taille minimale ou maximale, le type, le nom exact, un motif de nom (`*.py`) ou de chemin (`projets/**/*.py`), et une expression régulière sur le nom ou le chemin. Les résultats se trient par taille, nom ou date.

Les résultats s'exportent en CSV. La syntaxe complète est dans [FILTERS_AND_REGEX.md](../FILTERS_AND_REGEX.md).

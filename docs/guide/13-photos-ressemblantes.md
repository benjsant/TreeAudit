# 13. Photos ressemblantes

Les [doublons](04-doublons.md) ne trouvent que des fichiers identiques octet par octet. Une même photo **réduite**, **recompressée**, **convertie** (JPEG ↔ WebP) ou dont les **métadonnées** ont changé n'en est pas un. La page **🖼️ Photos ressemblantes** les retrouve, ainsi que les photos d'une **rafale**.

On y arrive depuis le bilan, par le lien **« 🖼️ Photos ressemblantes »** de la tuile Doublons.

## 1. Calculer les signatures

Clique sur **« Calculer les signatures »**. Ton navigateur ouvre chaque image de l'analyse (JPEG, PNG, GIF, WebP, BMP, AVIF, jusqu'à 20 Mio) et en garde une **signature** : une empreinte de 256 bits et une vignette de 32 × 32 points en gris.

- **Rien ne sort de ton ordinateur.** C'est ton navigateur qui ouvre les images, comme pour l'aperçu. TreeAudit lui-même n'en décode aucune et ne garde jamais l'image : seulement la signature.
- **Garde l'onglet ouvert.** Compte une à deux minutes pour 1 000 photos, plus le temps de lecture du disque. Tu peux **arrêter** et reprendre plus tard : ce qui est fait est gardé.
- **Une image déjà traitée n'est pas recalculée**, même dans une nouvelle analyse, tant qu'elle n'a pas changé (même fichier, même taille, même date).
- Une image que le navigateur ne sait pas ouvrir (fichier abîmé, faux `.jpg`) est comptée comme **illisible** et laissée de côté. Une image modifiée depuis l'analyse, ou qui n'a pas pu être lue sur le disque, est laissée de côté elle aussi, mais elle est **redemandée** au calcul suivant.
- **Disque débranché pendant le calcul** : la page s'arrête aussitôt (« Calcul interrompu : dossier indisponible »), sans marquer les images restantes comme illisibles. Rebranche le disque (avec Docker, relance aussi TreeAudit, voir [Dépannage](11-depannage.md#jai-débranché-le-disque-pendant-une-analyse)), puis clique de nouveau sur « Calculer les signatures » : ce qui est fait est gardé.

**Si TreeAudit refuse de calculer** en parlant de « protection contre le pistage » : ton navigateur brouille volontairement les images que lit une page (Brave, LibreWolf, Tor Browser, Firefox en protection renforcée). Les signatures seraient fausses. TreeAudit le vérifie avant tout calcul avec une petite image de contrôle. Ouvre TreeAudit dans un autre navigateur (Firefox ou Chrome par défaut), ou désactive cette protection pour `127.0.0.1`.

## 2. Lire les groupes

Une fois les signatures calculées, la page montre les **groupes** : des images qui se ressemblent, avec leurs aperçus côte à côte, leur résolution, leur poids et leur date.

Deux images se ressemblent si leurs empreintes sont proches **et** si leurs vignettes se superposent presque parfaitement. Le second test écarte les photos d'écrans ou de documents différents, que l'empreinte seule confond (une page claire avec du petit texte).

**L'image gardée (★)** est choisie dans cet ordre :

1. la plus grande **résolution** (le plus de pixels) ;
2. à résolution égale, le **fichier le plus lourd** ;
3. sinon, la plus **ancienne**, probablement l'originale.

Clique sur **« garder celle-ci »** sous une autre image pour la choisir à la place.

**Copies identiques.** Un groupe mélange souvent deux choses : d'autres versions ou d'autres prises d'une photo, et de simples **copies** (la même photo sauvegardée à plusieurs endroits). Sous chaque copie, la page écrit « = copie identique de… » :

- après une [recherche de doublons](04-doublons.md), c'est un fait : les deux fichiers ont été comparés octet par octet ;
- sans elle, TreeAudit écrit « sans doute une copie identique » : même taille et même signature, mais les octets n'ont pas été comparés.

Le total est séparé : ce que libèrent les copies identiques, sans aucune perte, et ce que libèrent les autres versions ou prises, qui sont un vrai choix. Sur une sauvegarde de téléphone copiée deux fois, presque tout est souvent fait de copies : **commence alors par la page Doublons**, puis reviens ici pour les rafales.

**Ce qui n'est jamais proposé** :

- l'image gardée ;
- une image qui ne ressemble pas **directement** à l'image gardée. Dans une rafale, la 1ʳᵉ photo peut ressembler à la 2ᵉ et la 2ᵉ à la 3ᵉ, sans que la 1ʳᵉ ressemble à la 3ᵉ. La 3ᵉ reste montrée, avec la mention « pas assez proche de l'image gardée » ;
- une image dans un logiciel, un jeu ou un dépôt git (🔒), comme pour les doublons ;
- un fichier qui a **plusieurs noms** sur le disque (liens physiques, 🔒) : en supprimer un nom ne libère rien.

## 3. Préparer la commande

Coche les groupes à nettoyer, puis passe par **« Dernière étape »**, comme pour les [doublons](04-doublons.md#préparer-la-commande) : corbeille (recommandé) ou suppression définitive. TreeAudit ne lance rien.

> ⚠ **Ressemblantes ne veut pas dire identiques.** Les photos d'une rafale sont des prises différentes : un sourire, un regard, un flou. **Regarde les aperçus avant de cocher un groupe**, et préfère la corbeille.

Si de nouvelles images ont été traitées entre l'affichage de la page et la préparation de la commande, TreeAudit refuse et te demande de recharger la page : les groupes ont pu changer.

## Limites

- **Recadrage, rotation, miroir** : une photo recadrée, tournée d'un quart de tour ou retournée n'est pas reconnue.
- **Gros filigrane** : une photo avec un texte qui en couvre une grande partie peut ne plus être reconnue.
- **HEIC** (photos d'iPhone) : Firefox et Chrome ne savent pas l'ouvrir. Ces images sont comptées comme illisibles.
- **Images de plus de 20 Mio** : ignorées, comme pour l'aperçu.
- **Un même logo recoloré** est regroupé : la comparaison se fait en gris.
- Les réglages ont été mesurés sur des images de test et sur de vraies photos de téléphone ([D-069](../DECISIONS.md)). Sur ce qui a été vérifié à l'œil, il n'y a eu aucune fausse ressemblance, mais l'échantillon reste petit : regarde toujours les aperçus.

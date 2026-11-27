# 6. Ranger un dossier

**« 🗂️ Ranger ce dossier »**, dans l'explorateur ou sur le bilan, propose de ranger le dossier affiché par catégories. Tu vois d'abord **un aperçu du résultat**. Rien ne bouge tant que tu ne lances pas toi-même le script.

## Ce que fait le plan

Tout irait dans un nouveau dossier, **`Rangé/`** par défaut, à l'intérieur du dossier choisi (« Rangé (2) » si un fichier ou un lien porte déjà ce nom) :

| Catégorie | Contenu |
|---|---|
| Photos, Vidéos | classées **par année** : `Rangé/Photos/2019/`, ou par mois si tu le demandes : `Rangé/Photos/2019/2019-07/` (voir plus bas) |
| Musique, Documents, Archives, Installateurs, Code, Polices | par type de fichier |
| **Projets** | les projets informatiques, **déplacés en entier** (un dossier avec `package.json`, `.git`…). Un `requirements.txt` ou un `Makefile` seul ne compte que si le dossier ne contient pas d'autre projet. Un **dossier de code** sans marqueur (des fichiers de code à son niveau, des sous-dossiers, et au moins trois quarts de fichiers de code) est déplacé en entier lui aussi : une application n'est jamais dispersée en `Code/pages`, `Code/config`… Sauf s'il contient un lien symbolique (une fois déplacé, il pointerait ailleurs), un fichier spécial ou un nom qu'on ne peut pas écrire sans ambiguïté dans un script : ce dossier est alors réparti comme un dossier mélangé. |
| **Logiciels** | les jeux et logiciels, **déplacés en entier** (Steam, préfixe Wine, programme Windows…) |
| Autres | le reste |

- **L'année d'une photo**, du plus sûr au moins sûr :
  1. sa **date de prise de vue**, écrite dans la photo par l'appareil (voir « Lire les dates des photos » ci-dessous) ;
  2. une date complète écrite dans son nom (`IMG_20190714_…`, `Capture d'écran 2024-01-02…`) ;
  3. sa date de modification.

  Une copie (sauvegarde, disque USB) donne souvent à tous les fichiers la date du jour de la copie : la date de prise de vue et le nom sont alors plus fiables. Une date de prise de vue ou du nom antérieure à 1990, ou plus récente que la modification, n'est pas crue ; une date de prise de vue au 1er janvier à minuit pile non plus (l'horloge d'un appareil jamais réglé). La ligne de l'élément dit d'où vient l'année : « date de prise de vue » ou « année tirée du nom ».
- **Par mois** : coche « Photos et vidéos aussi par mois », puis « 🔄 Mettre à jour l'aperçu ». Chaque photo va alors dans `Photos/2019/2019-07/`. Un album garde son nom et va dans son année, sans mois. Les scripts reprennent ce choix.
- Un **album** (un dossier qui ne contient qu'un seul type de fichiers) garde son nom et est déplacé tel quel.
- Un dossier mélangé est réparti fichier par fichier.
- Restent toujours en place : les fichiers cachés, les liens symboliques, les dossiers vides, et ce qu'on ne peut pas écrire sans ambiguïté dans un script.
- Deux éléments du même nom au même endroit : le second reçoit un numéro, « (2) » (avant l'extension d'un fichier, à la fin du nom d'un dossier).
- Un fichier **`.ts`** est du code TypeScript ou une vidéo (enregistrement télé) : à partir de 1 Mio, c'est une vidéo, sauf un fichier `.d.ts`.
- Si tu as cherché les doublons, un fichier qui existe en plusieurs exemplaires porte la marque **« 📑 en double »** ; clique dessus pour voir où sont les autres. Ranger ne l'écarte pas : à toi de décider.

## Lire les dates des photos

L'analyse ne lit que les noms, tailles et dates des fichiers, jamais leur contenu. La date de prise de vue est écrite *dans* la photo (ses métadonnées EXIF) : il faut ouvrir chaque photo pour la lire. C'est une étape à part, que tu lances toi-même.

En haut de l'aperçu, la ligne **« 📷 Dates de prise de vue »** dit pour combien de photos du dossier elle a déjà été lue. **« 📷 Lire les dates des photos »** les lit, puis revient sur cet aperçu, avec tes ajustements.

- TreeAudit ouvre chaque photo **en lecture seule** et n'en lit que l'en-tête : 256 Kio au plus, jamais l'image elle-même. Il ne modifie rien, pas même la date d'accès quand le système le permet.
- Formats lus : JPEG, HEIC/HEIF (iPhone), AVIF, TIFF et les fichiers bruts des appareils (DNG, NEF, CR2, ARW, ORF, RW2, PEF…), PNG et WebP. Les vidéos ne sont pas datées ainsi : elles gardent la date de leur nom ou de modification.
- Seules les photos du dossier affiché et de ses sous-dossiers sont lues. Une date lue est gardée : elle sert aux autres dossiers de l'analyse et aux analyses suivantes, tant que le fichier ne change pas. Après une nouvelle analyse, la ligne dit combien de dates sont **déjà lues** : le bouton les reprend en quelques secondes, sans rouvrir les photos.
- Beaucoup de photos n'ont pas de date : captures d'écran, images enregistrées depuis le web ou une messagerie qui efface les métadonnées. Elles gardent la date de leur nom ou de modification.
- Une photo **qui n'a pas pu être lue** (droits, disque débranché, fichier modifié depuis l'analyse) est comptée à part et redemandée la fois suivante.
- Tu peux annuler la lecture : ce qui est déjà lu est gardé.

## Que ranger ?

En haut de l'aperçu, coche les catégories à ranger : toutes le sont par défaut. Ce qui n'est pas coché **reste en place**, et la page dit combien d'éléments de chaque catégorie restent. Un dossier mélangé n'envoie dans `Rangé/` que ses fichiers des catégories cochées ; un album reste entier.

Le bouton **« 💻 Seulement le développement et les logiciels »** coche Code, Installateurs, Projets et Logiciels : les scripts, installateurs et projets éparpillés sont rangés, tes photos, vidéos, musique et documents ne bougent pas. Les archives n'y sont pas, parce qu'elles contiennent souvent des photos ou des sauvegardes : coche-les toi-même si tu veux. **« Tout ranger »** revient au plan complet. Les scripts téléchargés reprennent ce choix.

## Ajuster le plan

Dans l'aperçu, tu peux :

- **renommer** le dossier `Rangé` et chaque catégorie ;
- pour chaque élément : **le laisser en place**, **changer sa catégorie**, ou lui donner un **sous-dossier** ;
- cliquer sur **« 🔄 Mettre à jour l'aperçu »** pour voir le résultat (les catégories dépliées le restent), et sur **« Annuler mes ajustements »** pour repartir du plan proposé.

### Sous-dossiers proposés

Sur chaque catégorie d'au moins trois éléments, **« 📂 Proposer des sous-dossiers »** regroupe ses éléments d'après leurs noms, sans IA et sans carte graphique :

- un **mot connu** donne un thème : « facture » ou « invoice » vont dans *Factures*, « avis d'imposition » dans *Impôts*, « fiche de paie » dans *Salaires*, « capture d'écran » dans *Captures d'écran*… Un sujet l'emporte sur un type de papier : « facture-garage-vidange » va dans *Voiture* ;
- parmi le reste, les noms qui **partagent un mot** sont regroupés sous ce mot : « TD1_maths », « examen_maths » dans *Maths* ; « Daft Punk - … » dans *Daft Punk*.

- enfin, parmi les **photos et vidéos** qui restent (souvent nommées par l'appareil, `IMG_20190714_…`), celles **prises le même jour** sont regroupées, dès qu'elles sont au moins cinq : *2019-07-14*. Renomme le sous-dossier à ton idée (« 2019-07-14 Plage »). La date est la date de prise de vue si elle a été lue, sinon celle écrite dans le nom, sinon la date de modification, sauf si elle ressemble à celle d'une copie (beaucoup de fichiers dans la même seconde). Les albums ne sont jamais répartis.

Les mots collés sont coupés quand c'est utile : « FactureEDF » rejoint *Factures*, « CompteRendu » rejoint les « compte rendu ». Une marque comme « WhatsApp » ou « YouTube » reste entière.

Un sous-dossier regroupe au moins deux éléments, et jamais tous (il ne rangerait rien). Au plus 30 sous-dossiers sont proposés à la fois, les plus fournis d'abord. S'il en reste, accepte ce qui te convient, puis clique sur **« ➡ Proposer la suite »** : la suite est cherchée parmi les éléments qui n'ont pas encore de sous-dossier, sans toucher à ceux que tu as acceptés ou tapés. Une proposition que tu n'as pas acceptée peut revenir. Les éléments d'un même sous-dossier sont affichés les uns à la suite des autres, et la raison de chaque proposition est écrite sous l'élément. Clique sur une proposition pour l'accepter, ou sur « Accepter toutes les suggestions », puis sur « 🔄 Mettre à jour l'aperçu ». **Rien ne change tant que tu ne les acceptes pas.**

Les photos et les vidéos sont déjà rangées par année : les sous-dossiers se placent à l'intérieur. Les noms donnés par un appareil ou une application (un préfixe suivi d'une date ou d'un long numéro) ne forment pas de groupe : ils disent d'où vient la photo, pas ce qu'elle montre.

## Lancer le rangement

1. Télécharge les **deux scripts** : celui qui range, et celui qui **annule**. Ils reprennent tes ajustements.
2. Ouvre-les dans un éditeur de texte et **relis-les**.
3. Dans un terminal : `bash ~/Téléchargements/treeaudit-ranger-….sh`

Le script est prudent :

- avant chaque déplacement, il vérifie que l'élément est toujours là, et qu'un fichier a encore **la taille et la date** vues par l'analyse ;
- il **n'écrase jamais rien** (`mv -n`) : si la place est prise, il passe ;
- il ne passe **jamais par un lien** : si un dossier du chemin d'arrivée est un lien symbolique, l'élément reste en place ;
- tout élément ignoré est affiché avec sa raison, et un bilan s'affiche à la fin.

**Garde le script d'annulation** : il remet chaque élément à sa place d'origine, puis retire seulement les dossiers que le rangement a pu créer, s'ils sont vides. Tes propres dossiers vides restent.

Au-delà de 300 000 éléments, le plan serait trop long : choisis plutôt un sous-dossier.

# 6. Ranger un dossier

**« 🗂️ Ranger ce dossier »**, dans l'explorateur ou sur le bilan, propose de ranger le dossier affiché par catégories. Tu vois d'abord **un aperçu du résultat**. Rien ne bouge tant que tu ne lances pas toi-même le script.

## Ce que fait le plan

Tout irait dans un nouveau dossier, **`Rangé/`** par défaut, à l'intérieur du dossier choisi :

| Catégorie | Contenu |
|---|---|
| Photos, Vidéos | classées **par année** (date de modification) : `Rangé/Photos/2019/` |
| Musique, Documents, Archives, Installateurs, Code, Polices | par type de fichier |
| **Projets** | les projets informatiques, **déplacés en entier** (un dossier avec `package.json`, `.git`…). Un `requirements.txt` ou un `Makefile` seul ne compte que si le dossier ne contient pas d'autre projet. |
| **Logiciels** | les jeux et logiciels, **déplacés en entier** (Steam, préfixe Wine, programme Windows…) |
| Autres | le reste |

- Un **album** (un dossier qui ne contient qu'un seul type de fichiers) garde son nom et est déplacé tel quel.
- Un dossier mélangé est réparti fichier par fichier.
- Restent toujours en place : les fichiers cachés, les liens symboliques, les dossiers vides, et ce qu'on ne peut pas écrire sans ambiguïté dans un script.
- Deux éléments du même nom au même endroit : le second reçoit un numéro, « (2) ».

## Ajuster le plan

Dans l'aperçu, tu peux :

- **renommer** le dossier `Rangé` et chaque catégorie ;
- pour chaque élément : **le laisser en place**, **changer sa catégorie**, ou lui donner un **sous-dossier** ;
- cliquer sur **« 🔄 Mettre à jour l'aperçu »** pour voir le résultat, et sur **« Annuler mes ajustements »** pour repartir du plan proposé.

### Sous-dossiers proposés

Sur chaque catégorie, **« 📂 Proposer des sous-dossiers »** regroupe ses éléments d'après leurs noms, sans IA et sans carte graphique :

- un **mot connu** donne un thème : « facture » ou « invoice » vont dans *Factures*, « avis d'imposition » dans *Impôts*, « fiche de paie » dans *Salaires*, « capture d'écran » dans *Captures d'écran*… Un sujet l'emporte sur un type de papier : « facture-garage-vidange » va dans *Voiture* ;
- parmi le reste, les noms qui **partagent un mot** sont regroupés sous ce mot : « TD1_maths », « examen_maths » dans *Maths* ; « Daft Punk - … » dans *Daft Punk*.

Un sous-dossier regroupe au moins deux éléments, et jamais tous (il ne rangerait rien). Survole une proposition pour voir pourquoi elle est faite. Clique dessus pour l'accepter, ou sur « Accepter toutes les suggestions », puis sur « 🔄 Mettre à jour l'aperçu ». **Rien ne change tant que tu ne les acceptes pas.**

Les photos et les vidéos sont déjà rangées par année : les sous-dossiers se placent à l'intérieur. Les noms donnés par un appareil ou une application (un préfixe suivi d'une date ou d'un long numéro) ne forment pas de groupe : ils disent d'où vient la photo, pas ce qu'elle montre.

## Lancer le rangement

1. Télécharge les **deux scripts** : celui qui range, et celui qui **annule**. Ils reprennent tes ajustements.
2. Ouvre-les dans un éditeur de texte et **relis-les**.
3. Dans un terminal : `bash ~/Téléchargements/treeaudit-ranger-….sh`

Le script est prudent :

- avant chaque déplacement, il vérifie que l'élément est toujours là, et qu'un fichier a encore **la taille et la date** vues par l'analyse ;
- il **n'écrase jamais rien** (`mv -n`) : si la place est prise, il passe ;
- tout élément ignoré est affiché avec sa raison, et un bilan s'affiche à la fin.

**Garde le script d'annulation** : il remet chaque élément à sa place d'origine, puis retire seulement les dossiers que le rangement a laissés vides.

Au-delà de 300 000 éléments, le plan serait trop long : choisis plutôt un sous-dossier.

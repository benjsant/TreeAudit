# Vérifications dans un vrai navigateur

Ces scripts ne sont pas livrés avec TreeAudit. Ils pilotent un vrai navigateur avec Playwright, là où la suite de tests (`./treeaudit check`) ne va pas : le JavaScript des pages, l'affichage sur téléphone, les parcours complets par des clics.

Ils ne cliquent jamais sur un bouton qui modifie un fichier, et ne lancent jamais une commande proposée.

| Script | Ce qu'il vérifie |
|---|---|
| `tour.cjs` | Toutes les pages accessibles par des liens (3 au plus par type de page), sur ordinateur en thème clair et sur téléphone en thème sombre : code de réponse, erreurs de la console, débordement horizontal, requêtes vers l'extérieur. Les exports sont téléchargés, pas ouverts. |
| `journey.cjs` | Le parcours d'un utilisateur jusqu'aux photos ressemblantes : accueil, analyse, bilan, signatures, groupes, commande préparée. |
| `signatures.cjs` | Le calcul des signatures d'images, avec arrêt et reprise, et le refus d'un navigateur qui brouille les images (`firefox-rfp`). |
| `groups.cjs` | La page des groupes : changer l'image gardée, préparer la commande, captures d'écran. |
| `ranger.cjs` | Ranger : le raccourci « seulement le développement et les logiciels », et le script téléchargé. |
| `photo_dates.cjs` | Ranger et les dates de prise de vue (D-071) : lecture par le bouton, retour sur Ranger, option « par mois » ; compte les destinations des photos par année et par mois dans le script téléchargé, sans jamais afficher un nom. |

## Lancer

1. Démarre un TreeAudit de test, avec un index à part, par exemple l'AppImage avec `XDG_CONFIG_HOME`, `XDG_DATA_HOME` et `TREEAUDIT_CONFIG` qui pointent vers un dossier temporaire, et un port libre.
2. Lance un script dans l'image Playwright, avec le réseau de l'hôte pour joindre `127.0.0.1` :

```
docker run --rm --network host -u "$(id -u):$(id -g)" -e HOME=/tmp \
  --mount type=bind,src="$PWD/tools/browser_checks/tour.cjs",dst=/work/tour.cjs,readonly \
  -w /app --entrypoint node <image playwright> /work/tour.cjs chromium http://127.0.0.1:8767
```

Pour donner un dossier à un conteneur, utilise toujours `--mount … ,readonly` et jamais `-v` : avec `-v`, Docker crée le dossier s'il n'existe pas, en tant que `root`, par exemple sous le point de montage d'un disque débranché.

Le prototype des signatures d'images (D-069) est à part, dans `tools/similar_images_proto/`.

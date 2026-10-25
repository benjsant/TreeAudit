# 9. Mode expert (IA locale)

Le mode expert ajoute une **IA locale** qui donne son avis. Elle tourne sur ta carte graphique, dans un conteneur sans accès à Internet, et **ne peut rien faire d'autre que répondre** : elle ne lit aucun fichier, ne lance aucune commande, ne coche aucune case.

## Ce qu'elle fait

| Où | Ce qu'elle propose |
|---|---|
| [À nettoyer](03-nettoyer.md) | un avis sur les éléments orange et rouges (au plus 50 à la fois) : « à garder », « incertain », ou « ne voit pas de raison de garder » |
| [Ranger](06-ranger.md) | des sous-dossiers pour une catégorie (« Factures », « Impôts »…), à accepter ou non |

**Ce qu'elle voit** : des noms, des types, des tailles, des années. **Jamais le contenu de tes fichiers**, jamais un chemin sur lequel agir.

**Comment lire ses avis** : une alerte (« L'IA conseille de garder ce dossier ») mérite que tu regardes. Un avis favorable n'est pas une recommandation : l'IA peut se tromper, et un garde-fou corrige ses réponses les plus risquées. C'est toi qui décides.

## Ce qu'il faut

- **Docker** : le mode expert ne fonctionne pas avec l'AppImage.
- **Une carte graphique AMD** (ou Intel), utilisée par Vulkan. Les cartes NVIDIA ne sont pas prises en charge pour l'instant.
- **Environ 4 Gio de mémoire vidéo** : mesuré sur une Radeon RX 6600, le modèle en prend 3,8 Gio (2,0 Gio pour le modèle, 1,6 Gio pour son contexte, 0,1 Gio de calcul), plus 0,4 Gio de mémoire vive. Une carte de 4 Gio suffit tout juste, et 6 Gio ou plus sont confortables.
- **2 Gio d'espace disque** pour le modèle.

Le modèle retenu est **Ministral 3 3B** (Mistral AI, licence Apache 2.0). Il a donné 16 bonnes réponses sur 16 aux cas d'essai, sans aucune réponse dangereuse.

## L'activer

```bash
./treeaudit llm-download ministral      # une seule fois : télécharge et vérifie le modèle (2 Gio)
TREEAUDIT_EXPERT=1 TREEAUDIT_LLM_MODEL=Ministral-3-3B-Instruct-2512-Q4_K_M.gguf ./treeaudit
```

- Le téléchargement est vérifié par son empreinte SHA-256 : un fichier altéré est écarté.
- Le modèle est rangé dans `models/`, en lecture seule pour le conteneur.
- Le conteneur de l'IA n'a accès qu'à la puce de calcul de la carte graphique (`/dev/dri/renderD128`), jamais à l'écran ni au reste de `/dev`.
- Sans les deux variables, TreeAudit démarre sans IA. Les boutons « 💡 » n'apparaissent alors pas.

**Pour la retirer** : supprime le fichier dans `models/`, puis l'image Docker `ghcr.io/ggml-org/llama.cpp`.

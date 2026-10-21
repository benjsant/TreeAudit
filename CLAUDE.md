# Instructions permanentes pour Claude Code — TreeAudit

## Mission
Construire TreeAudit, une application locale d'audit de fichiers et d'arborescences, Linux uniquement en V1. Le produit doit être fiable, explicable, testé et non destructif.

## Règles non négociables
1. **Jamais de suppression, déplacement, renommage, fusion ou modification d'une donnée auditée** (D-024). TreeAudit propose des commandes de nettoyage que l'utilisateur lance lui-même ; il ne les exécute jamais (D-025). Seule écriture hors du dossier de données : des archives vérifiées, dans le dossier d'archives déclaré (D-032).
2. Ne jamais utiliser `sudo`, `subprocess`, `os.system`, `rm`, `shutil.rmtree`, des commandes shell destructrices ou des commandes fournies par l'utilisateur ou un LLM.
3. Ne jamais monter `/`, `/home` entier, `/dev`, `/proc`, `/sys`, le socket Docker ou un volume hôte général. Jamais d'option `:z`/`:Z` sur une cible (D-011).
4. Les racines sont déclarées dans la configuration et validées (D-008, D-009). En Docker, chacune est montée en lecture seule sous `/audit/roots/<id>`. Seule exception, en option : les disques externes détectés (D-033). Ils sont validés sur l'hôte (jamais un disque physique du système, en cas de doute refusés), puis montés un par un en lecture seule sous `/audit/disks/<id>`, et revérifiés dans le conteneur.
5. Le serveur n'est joignable que depuis `127.0.0.1` : écoute sur `127.0.0.1` en natif ; en Docker, écoute sur `0.0.0.0` dans le conteneur mais port publié uniquement sur `127.0.0.1` (D-010). Toute autre exposition exige une décision documentée et des tests.
6. Aucun conteneur privilégié, aucune capacité Linux ajoutée (`cap_drop: [ALL]`), aucun accès au socket Docker.
7. Le conteneur s'exécute avec un utilisateur non-root (UID/GID de l'utilisateur hôte).
8. Le LLM n'a pas accès à des outils système ou fonctions d'écriture. Il ne reçoit que des métadonnées minimales.
9. Une erreur de permission/E/S ou une interruption doit être signalée ; ne jamais présenter un scan comme complet s'il ne l'est pas (D-012).
10. Ne jamais suivre les liens symboliques. Parcourir par descripteurs avec `O_NOFOLLOW` (D-013). Ne jamais permettre à un chemin de sortir d'une racine autorisée.
11. La sécurité ne doit jamais dépendre uniquement d'une liste de noms de dossiers ou de regex.
12. Les seules écritures autorisées vont dans le dossier de données de TreeAudit et, pour les archives vérifiées uniquement, dans le dossier d'archives (D-032). Aucun des deux ne doit chevaucher une racine auditée, ni l'autre.
13. Ne pas implémenter Windows dans la V1. Documenter le portage comme une future V2.
14. Toute dépendance tierce doit être justifiée dans `docs/DEPENDENCIES.md`.
15. Ne pas intégrer de LLM avant que l'inventaire, les filtres et les rapports déterministes soient testés.
16. La sécurité ne repose pas sur Docker : chaque protection existe aussi en mode natif (D-007).

## Langue (D-016)
- Code, identifiants, commentaires, docstrings, messages de commit : **anglais**.
- Documentation (`README.md`, `docs/`) : **français**.
- Échanges avec le mainteneur : français.

## Méthode
- Lire `docs/PROJECT_SPEC.md`, `docs/SECURITY.md`, `docs/ARCHITECTURE.md`, `docs/DECISIONS.md`, `docs/DEPENDENCIES.md` et `docs/ROADMAP.md` avant de coder.
- Avancer par petites étapes, une phase à la fois, avec validation du mainteneur entre les phases.
- Avant chaque étape, annoncer l'objectif, les fichiers touchés et les risques.
- Écrire ou adapter les tests avec chaque fonctionnalité.
- Exécuter les tests pertinents, le lint et mypy ; ne jamais prétendre qu'ils ont réussi sans les avoir exécutés.
- Si une demande contredit une règle de sécurité, expliquer le conflit et ne pas la contourner.
- Ne pas générer toute l'application en une seule passe et ne pas ajouter de fonctions non demandées.
- Toute décision structurante est ajoutée, datée, à `docs/DECISIONS.md`.

## Qualité
- Type hints dans le code de production ; `mypy` sans erreur.
- Fonctions à responsabilité claire.
- Gestion explicite des erreurs et journalisation sans divulguer le contenu des fichiers.
- Pas de `except Exception: pass`.
- Parcours itératif ; ne pas charger toute l'arborescence en mémoire.
- Noms de fichiers traités comme des octets ; stockés en `BLOB` (D-014).
- SQLite via la bibliothèque standard.
- Compiler les regex une fois par recherche et gérer `re.error`.
- Une regex ne sert jamais à décider si un chemin est autorisé.
- Chaque scan se termine en `complete`, `incomplete`, `cancelled` ou `failed` (D-012).

## Compte rendu à la fin d'une tâche
- Résumé des changements et fichiers touchés.
- Tests réellement exécutés et résultats (y compris tests ignorés et leur motif).
- Limites connues.
- Prochaine petite étape proposée.

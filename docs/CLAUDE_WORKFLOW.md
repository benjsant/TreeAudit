# Workflow conseillé pour les sessions Claude Code

## Première session
1. Lire `CLAUDE.md` et tous les documents `docs/`.
2. Ne pas coder immédiatement.
3. Résumer l'architecture et signaler les contradictions ou risques.
4. Proposer un plan de phase 0 avec critères d'acceptation.
5. Attendre validation avant de créer l'application.

## Chaque tâche
- Demander une tâche limitée et un résultat vérifiable.
- Faire examiner le plan avant les changements qui touchent la sécurité, Docker ou les chemins.
- Ajouter les tests en même temps que le code.
- Montrer les commandes de test exécutées et leurs résultats.
- Ne pas faire de refactorisation large non demandée.

## Prompt initial à copier
« Lis `CLAUDE.md` et les documents du dossier `docs/`. Pour cette première session, ne crée pas encore de code applicatif. Fais un audit de cohérence des spécifications, identifie les décisions qui manquent pour le MVP Linux en lecture seule, puis propose une Phase 0 courte avec critères d'acceptation et tests de sécurité. Respecte strictement les règles de sécurité. N'ajoute aucune fonctionnalité non demandée. »

## Prompt pour chaque incrément
« Implémente uniquement l'étape validée. Commence par résumer les fichiers concernés et les risques. Ajoute les tests correspondants, exécute-les réellement et rapporte les résultats. Ne modifie pas les règles de sécurité sans demander validation. »

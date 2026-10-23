# iphone-duo-watcher — consignes pour Claude Code

Service Python (asyncio, httpx) en Docker qui surveille le stock de l'iPhone Duo
(Apple Store + revendeurs FR + emails d'alerte) et notifie par Telegram, appel
CallMeBot et Discord. Voir README.md pour le fonctionnement, et
docs/DEPLOIEMENT_CLAUDE_CODE.md pour la procédure de mise en place et de test.

## Règles non négociables
- **Aucun contournement anti-bot** : pas de résolution de CAPTCHA, pas de
  plugins « stealth », pas de rotation de proxys/IP, pas de réutilisation de
  cookies de session, pas d'usurpation d'API mobile. Un site qui bloque reste
  désactivé (`enabled: false`) et passe en « email uniquement ».
- **Aucun achat, ajout au panier ou réservation automatique.**
- **Secrets** : uniquement dans `.env` (ignoré par git). Ne jamais afficher,
  logger, committer ni recopier leurs valeurs. Pour vérifier qu'une variable
  est remplie, n'afficher que son nom.
- Ne jamais committer `.env`, `config.yaml` (local) ni le dossier `data/`.
- Respecter les fréquences : Apple jamais sous 60 s ; lors des tests manuels,
  quelques requêtes seulement par site, espacées.

## Conventions
- Code, commentaires, logs et messages en français, style du code existant.
- Un site = une classe `@register("nom")` dans `watcher/connectors/` ; ajuster
  d'abord les motifs dans `config.yaml` avant de modifier le code.
- Toute correction de parseur s'accompagne d'une fixture dans `tests/fixtures/`
  (page/réponse réelle, sans données personnelles) et d'un test.
- Avant de committer : `.venv/bin/python -m pytest -q` (ou `./scripts/verify.sh`).

## Commandes
```bash
./scripts/verify.sh                                  # vérification hors-ligne complète
docker compose run --rm watcher test-notify          # message sur chaque canal + appel
docker compose run --rm watcher check-sites [sites]  # état réel extrait, sans notifier
docker compose run --rm watcher simulate <site> <fixture> [--model id] [--send] [--db /data/x.db]
docker compose run --rm watcher status               # tableau de bord enregistré
docker compose up -d && docker compose logs -f
```

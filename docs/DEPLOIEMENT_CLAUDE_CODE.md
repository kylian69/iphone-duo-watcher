# Procédure de déploiement et de vérification (pour Claude Code sur la VM)

Ce document est destiné à Claude Code, lancé sur la VM du homelab dans le
dossier du dépôt. Exécute les phases **dans l'ordre**. Après chaque phase,
résume en 2-3 lignes ce qui est OK et ce qui ne l'est pas. Arrête-toi et
pose la question à l'utilisateur quand c'est indiqué **[DEMANDER]**.

Relis d'abord `CLAUDE.md`. Ses règles s'appliquent partout : aucun
contournement anti-bot, aucun achat automatique, aucun secret affiché ou
committé.

Contexte : le code a été écrit et testé sur des données simulées, depuis un
environnement qui n'avait **pas accès** à apple.com ni aux sites marchands.
Trois éléments n'ont donc jamais été confrontés au réel, et sont le cœur de
cette procédure :

1. le format réel de la réponse JSON Apple `fulfillment-messages` ;
2. l'extraction de l'état sur les vraies pages des revendeurs ;
3. certaines références et URL marquées `À VÉRIFIER` dans
   `config.yaml.example`.

---

## Phase 0 — Prérequis

```bash
docker --version && docker compose version
git log --oneline -3
```

- Docker et Compose v2 doivent être installés. S'ils manquent, propose de
  les installer (dépôt officiel Docker) **[DEMANDER]** avant d'installer quoi
  que ce soit.
- Vérifie l'heure et le fuseau : `timedatectl`. Le conteneur utilise
  `TZ=Europe/Paris` via `.env`.

## Phase 1 — Configuration

```bash
[ -f config.yaml ] || cp config.yaml.example config.yaml
[ -f .env ] || cp .env.example .env
```

Vérifie quelles variables sont vides **sans jamais afficher les valeurs** :

```bash
grep -E '^(TELEGRAM_BOT_TOKEN|TELEGRAM_CHAT_ID|CALLMEBOT_TELEGRAM_USER|DISCORD_WEBHOOK_URL|DISCORD_USER_ID|IMAP_HOST|IMAP_USER|IMAP_PASSWORD)=' .env \
  | awk -F= '{print $1": "($2==""?"VIDE":"renseigné")}'
```

Si des secrets de notification sont vides : **[DEMANDER]** à l'utilisateur de
remplir `.env` lui-même (ne lui demande pas de te les coller dans la
conversation). Les phases 2 à 4 peuvent se dérouler sans secrets. Un IMAP
vide est normal : le connecteur email est alors désactivé.

## Phase 2 — Vérification hors-ligne

```bash
./scripts/verify.sh
```

Le script fait :

- le build de l'image ;
- la vérification non-root ;
- pytest dans l'image ;
- les simulations : Apple, revendeur prioritaire/secondaire, prix opérateur,
  email traité une seule fois, newsletter ignorée, CAPTCHA, IMAP vide.

Attendu : `0 échec(s)`. En cas d'échec, diagnostique et corrige avant de
continuer.

## Phase 3 — Valider le connecteur Apple sur le vrai endpoint

C'est la source principale, donc la phase la plus importante. Fais **au plus
3 ou 4 requêtes**, espacées d'au moins 60 s.

1. Lance le diagnostic intégré (environ 4 requêtes espacées, réponses brutes
   enregistrées dans `/data`) :

   ```bash
   docker compose run --rm watcher probe-apple
   ```

   La commande interroge l'endpoint principal (`fulfillment-messages`) et
   l'endpoint de secours (`retail/pickup-message`). Elle affiche :
   - les clés de la réponse ;
   - les magasins trouvés (Part-Dieu, Confluence…) ;
   - l'état extrait par modèle ;
   - pour chacune des 8 références, ✅ ou ❌ « absente de la réponse ».

   Attendu : `✅ connecteur Apple opérationnel`. Pour lire une réponse brute :
   `docker compose run --rm --entrypoint cat watcher /data/apple_probe_principal_0.json | python3 -m json.tool | head -150`.
2. Si le format n'est pas reconnu, compare la réponse brute avec ce
   qu'attend `watcher/connectors/apple.py` (docstring en tête de fichier) :
   - `body.content.pickupMessage.stores[]` avec `storeNumber`, `storeName`,
     `partsAvailability[<réf>].pickupDisplay` et `pickupSearchQuote` ;
   - `body.content.deliveryMessage[<réf>].regular` avec `isBuyable` et
     `deliveryOptions[].date` / `displayName`.
3. Lance le connecteur réel :

   ```bash
   docker compose run --rm watcher check-sites apple
   ```

   Attendu : une ligne par modèle, pour la livraison et pour chaque magasin
   (Part-Dieu, Confluence…), avec un statut et un délai cohérents avec la
   réponse brute.
4. Si le format diffère, ou si des statuts sortent `inconnu` ou faux :
   - adapte `parse()` dans `apple.py`, en restant tolérant ;
   - enregistre la réponse réelle comme fixture
     (`tests/fixtures/apple_real_<date>.json`) ;
   - ajoute un test dans `tests/test_acceptance.py` ;
   - relance `./scripts/verify.sh`.
5. Valide les références des 8 modèles avec `probe-apple`, en particulier **`MK2F4F/A`**
   (512 Go Blanc stellaire), qui a été **déduite** et non confirmée.
   - Une référence correcte apparaît dans `deliveryMessage` / `partsAvailability`.
   - Si une référence est absente ou rejetée, cherche la bonne dans le source
     de la page produit :

     ```bash
     curl -sS -A "Mozilla/5.0" "https://www.apple.com/fr/shop/buy-iphone/iphone-duo" | grep -oE 'MK[0-9A-Z]{3}F/A' | sort -u
     ```

     Puis corrige `config.yaml` **et** `config.yaml.example`.
6. Si Apple répond 403, 429 ou 541 : n'insiste pas. Attends 10 min et
   réessaie une fois. Si le blocage persiste, signale-le **[DEMANDER]**.

## Phase 4 — Revendeurs et opérateurs

```bash
docker compose run --rm watcher check-sites boulanger sfr bouygues orange
docker compose run --rm watcher check-sites fnac darty cdiscount amazon
```

`check-sites` teste aussi les sites désactivés. Pour **chaque site**, classe le
résultat :

| Résultat | Action |
|---|---|
| état cohérent avec la page | ✅ site validé : `enabled: true` |
| `BlockedError` (403, 429, CAPTCHA, page anti-bot) | ⛔ `enabled: false`, alertes email uniquement. **Aucune tentative de contournement.** |
| `FormatError` / statut faux | 🔧 voir ci-dessous |
| HTTP 404 | URL fausse : chercher la bonne fiche produit |

Pour vérifier qu'un état est cohérent, récupère la page une seule fois et
regarde le JSON-LD :

```bash
curl -sS -A "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36" -H "Accept-Language: fr-FR" "<URL>" -o /tmp/page.html
grep -o '"availability"[^,}]*' /tmp/page.html | head; grep -oiE '(livr|exp[ée]di|retrait)[^<]{0,80}' /tmp/page.html | head
```

En cas de 🔧, ajuste dans cet ordre :

1. les motifs dans `config.yaml` (`connectors.<site>.available_patterns`,
   `unavailable_patterns`, `delivery_patterns`, `price_with_plan_patterns`,
   `price_without_plan_patterns`) ;
2. sinon, une sous-classe dédiée dans `watcher/connectors/sites.py`.

Dans les deux cas, enregistre la page réelle en fixture et ajoute un test.

Points d'attention :

- **URL `À VÉRIFIER` / `À RENSEIGNER`** dans `config.yaml` :
  - Boulanger 256 Go Blanc (`1243118`) et 2 To Ciel nocturne (manquant) ;
  - RED by SFR (variantes déduites) ;
  - Orange, Fnac, Cdiscount, Amazon (fiches produit non trouvées) ;
  - Bouygues (page générique pour tous les coloris).
  
  Vérifie que le titre de chaque page correspond bien au modèle et au coloris
  exacts. Si tu ne trouves pas une URL, laisse le champ vide et liste-le dans
  le rapport final.
- **Opérateurs** (Orange, Bouygues, SFR) : vérifie que le prix sans forfait
  et le prix avec forfait extraits sont corrects.
- Si un site n'affiche le stock qu'avec JavaScript, propose
  `fetcher: playwright` (README, section 7) **[DEMANDER]**. Ce n'est pas un
  contournement : si le site bloque aussi le navigateur, il passe en email
  uniquement.

## Phase 5 — Notifications (secrets nécessaires)

1. Test des canaux :

   ```bash
   docker compose run --rm watcher test-notify
   ```

   Attendu : `telegram envoyé`, `discord envoyé`, `callmebot appel lancé`.
   **[DEMANDER]** à l'utilisateur de confirmer qu'il a bien reçu :
   - le message Telegram, qui sonne ;
   - la notification Discord qui le **mentionne** ;
   - l'**appel Telegram**.
2. Canal volontairement mal configuré : les autres doivent continuer.

   ```bash
   docker compose run --rm -e DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/0/invalide watcher test-notify
   ```

   Attendu : Discord en `échec`, Telegram et l'appel OK.
   Respecte la limite CallMeBot : attends 5 min après l'appel précédent.
3. Bout en bout, avec un état isolé, en envoi réel :

   ```bash
   docker compose run --rm watcher simulate apple tests/fixtures/apple_pickup_available.json --send --db /data/e2e.db
   docker compose run --rm watcher simulate apple tests/fixtures/apple_pickup_available.json --send --db /data/e2e.db
   ```

   Attendu : le 1er lancement envoie une alerte urgente (appel + Telegram +
   Discord), le 2e affiche « aucune notification ».
4. IMAP, si l'utilisateur a rempli `IMAP_*` :
   - ajoute **temporairement** dans `config.yaml` une règle en tête de
     `connectors.email.rules` :
     `{site: Test, from: ["<adresse de l'utilisateur>"], keywords: ["disponible"], require_product: 'iphone\s*duo'}` ;
   - **[DEMANDER]** à l'utilisateur d'envoyer depuis cette adresse, vers la
     boîte dédiée, l'objet « iPhone Duo 256 Go Ciel nocturne de nouveau
     disponible » ;
   - lance le service (phase 6) et vérifie : alerte urgente reçue, email
     marqué comme lu et déplacé dans `Traités`, pas de seconde alerte ;
   - **retire ensuite la règle de test.**

## Phase 6 — Mise en service

```bash
docker compose up -d --build
docker compose logs --tail 60 watcher
```

Vérifie dans les logs :

- `Démarrage : N connecteurs actifs` ;
- l'état des canaux ;
- le tableau de bord (✅ / ⚠️ / ⏸ par connecteur, modèles prioritaires) ;
- aucune exception.

L'utilisateur doit avoir reçu « Surveillance iPhone Duo démarrée ».

Mode debug, environ 10 min :

1. passe `LOG_LEVEL=DEBUG` dans `.env`, puis `docker compose up -d` ;
2. vérifie que, pour chaque site, l'état et la date de livraison de chaque
   modèle sont affichés ;
3. repasse `LOG_LEVEL=INFO`, puis `docker compose up -d`.

## Phase 7 — Robustesse

```bash
docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' iphone-duo-watcher   # unless-stopped
docker compose restart watcher && sleep 20 && docker compose logs --since 1m watcher
docker volume ls | grep watcher-data
docker compose run --rm watcher status
```

Attendu :

- après redémarrage, **seule** la notification de démarrage est reçue (aucune
  alerte rejouée : l'état est persistant) ;
- le tableau de bord conserve les dernières vérifications.

Vérifie aussi que le service redémarre avec la VM : le service Docker doit
être activé (`systemctl is-enabled docker`).

## Phase 8 — Enregistrer les ajustements

Si tu as modifié du code, des fixtures ou `config.yaml.example` :

```bash
git checkout -b ajustements-homelab
./scripts/verify.sh
git add -A && git status   # vérifier : ni .env, ni config.yaml, ni data/
git commit -m "Ajustements après validation sur le homelab"
```

Pousse la branche **[DEMANDER]** avant tout push.

## Rapport final attendu

Termine par un rapport court :

1. tableau **site → état** : ✅ validé / ⛔ bloqué → email uniquement /
   🔧 corrigé / ❓ URL manquante ;
2. références Apple : validées, corrigées (en particulier `MK2F4F/A`) ;
3. critères d'acceptation : chacun ✅ ou ❌, avec la preuve (commande et
   sortie) ;
4. ce qui reste à faire par l'utilisateur :
   - inscription aux alertes email des sites ;
   - Keepa ;
   - URL manquantes ;
   - secrets non remplis.

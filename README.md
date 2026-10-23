# iPhone Duo — surveillance de stock (Apple + revendeurs FR)

Service Docker qui interroge en continu l'Apple Store (retrait en magasin autour
de 69530 et livraison), les opérateurs et les revendeurs français, ainsi qu'une
boîte email dédiée aux alertes « retour en stock ». Il vous prévient par
**appel Telegram** (CallMeBot), **message Telegram** et **Discord avec
@mention** dès qu'un changement utile apparaît.

Le service ne fait **aucun achat, ajout au panier ni réservation**, et
**aucun contournement de CAPTCHA ou de protection anti-bot** : un site qui
bloque passe en « dégradé », est réessayé de plus en plus rarement (jusqu'à
6 h), et ne produit qu'une seule alerte technique.

---

## Sommaire

1. [Démarrage rapide](#1-démarrage-rapide)
2. [Ce qui déclenche une notification](#2-ce-qui-déclenche-une-notification)
3. [Configurer les notifications](#3-configurer-les-notifications)
4. [Références Apple et URL produit](#4-références-apple-et-url-produit)
5. [Boîte email dédiée, alertes revendeurs et Keepa](#5-boîte-email-dédiée-alertes-revendeurs-et-keepa)
6. [Tester sans attendre un vrai stock](#6-tester-sans-attendre-un-vrai-stock)
7. [Exploitation : logs, debug, tableau de bord](#7-exploitation--logs-debug-tableau-de-bord)
8. [Ajouter un connecteur](#8-ajouter-un-connecteur)
9. [Architecture](#9-architecture)

---

## 1. Démarrage rapide

Sur le homelab (VM Proxmox, ou conteneur LXC avec `nesting=1` et Docker installé) :

```bash
git clone <ce dépôt> iphone-duo-watcher && cd iphone-duo-watcher
cp .env.example .env               # secrets : Telegram, CallMeBot, Discord, IMAP
cp config.yaml.example config.yaml # modèles, URL, fréquences, règles
nano .env

docker compose build
docker compose run --rm watcher test-notify   # 1 message par canal + 1 appel
docker compose run --rm watcher check-sites   # fiabilité réelle de chaque site
docker compose up -d                          # lancement (redémarrage auto)
docker compose logs -f
```

Au démarrage, vous recevez « Surveillance iPhone Duo démarrée » sur Telegram et
Discord, avec l'état de chaque connecteur.

L'état (dernier statut de chaque modèle sur chaque site, santé des
connecteurs, emails déjà traités) est stocké en SQLite dans le volume Docker
`watcher-data`. Il survit aux redémarrages et aux mises à jour de l'image.

Mise à jour : `git pull && docker compose up -d --build`.

---

## 2. Ce qui déclenche une notification

Le service notifie **uniquement les changements**, jamais à chaque vérification.

| Événement | Appel Telegram | Message Telegram | Discord (@mention) |
|---|:---:|:---:|:---:|
| **Priorité haute** : retrait Apple disponible, livraison Apple ≤ 30 j, revendeur disponible ≤ 7 j, email d'alerte | ✅ | ✅ | ✅ |
| Autre changement d'état (date qui bouge, précommande, retour en rupture, modèle secondaire dispo…) | ❌ | ✅ | ✅ |
| Alerte technique / rétablissement | ❌ | ✅ | ✅ |
| « Toujours actif » (toutes les 12 h) | ❌ | ✅ | ✅ |
| Démarrage du service | ❌ | ✅ | ✅ |

Règles de priorité par modèle (`config.yaml`) :

- `priority: high` : modèle prioritaire (256 Go Ciel nocturne / Blanc stellaire).
- `allow_call` : autorise l'appel. Il vaut `true` par défaut pour un modèle
  `high` et `false` pour un modèle `low`. Une disponibilité rapide d'un modèle
  sans appel donne seulement un message (c'est le cas du 2 To).
- Seuils de délai (`alerts.urgent_max_days`) : `apple: 30`, `default: 7`
  (revendeurs). Un site peut avoir son propre seuil, par exemple `boulanger: 10`.
- Disponible sans délai lisible : traité comme urgent par prudence
  (`urgent_if_delay_unknown`).
- Email d'alerte avec un modèle non identifié : urgent par prudence.

Appels CallMeBot : au maximum un appel toutes les
`CALLMEBOT_MIN_INTERVAL_MINUTES` minutes (5 par défaut). Les disponibilités
qui arrivent en même temps, ou pendant l'attente, sont regroupées en **un seul
appel**. Si l'appel échoue, les messages Telegram et Discord partent quand même
et l'échec est loggé.

Pour les opérateurs, les messages affichent le **prix sans forfait** et le prix
**avec forfait (« à partir de »)**.

---

## 3. Configurer les notifications

Tous les canaux sont gratuits. Chacun s'active ou se désactive dans `.env`
(`*_ENABLED`). Un canal non configuré est ignoré et n'empêche jamais les autres
d'envoyer.

### Telegram : bot de messages

1. Dans Telegram, ouvrez **@BotFather**, envoyez `/newbot`, et suivez les
   instructions. Copiez le token dans `TELEGRAM_BOT_TOKEN`.
2. Ouvrez la conversation avec votre bot et envoyez-lui n'importe quel message.
3. Ouvrez `https://api.telegram.org/bot<TOKEN>/getUpdates` dans un navigateur.
   Copiez la valeur `"chat":{"id": …}` dans `TELEGRAM_CHAT_ID`.
4. Les messages sont envoyés avec `disable_notification=false`, donc ils sonnent
   toujours. Vérifiez seulement que la conversation avec le bot n'est pas
   en sourdine dans Telegram.

### Telegram : appel vocal via CallMeBot

1. Il vous faut un **nom d'utilisateur Telegram** (Réglages → Nom d'utilisateur).
2. Ouvrez une conversation avec **@CallMeBot_txtbot**, envoyez `/start`, et
   acceptez l'autorisation d'appel. La procédure est décrite sur
   <https://www.callmebot.com/telegram-call-api/>.
3. Mettez `CALLMEBOT_TELEGRAM_USER=@votre_username`.
4. Vérifiez que Telegram a le droit de sonner (appels autorisés, pas de mode
   « Ne pas déranger » qui bloque Telegram).
5. Lancez `docker compose run --rm watcher test-notify` : vous devez recevoir
   un appel qui lit « Test de la surveillance iPhone Duo… ».

CallMeBot est un service gratuit réservé à un usage personnel. Respectez sa
limite d'appels avec `CALLMEBOT_MIN_INTERVAL_MINUTES`. La voix se règle avec
`CALLMEBOT_LANG` (`fr-FR-Standard-A` par défaut).

### Discord : webhook avec @mention

1. Sur votre serveur Discord : **Paramètres du salon → Intégrations →
   Webhooks → Nouveau webhook**. Copiez l'URL dans `DISCORD_WEBHOOK_URL`.
2. Pour récupérer votre ID utilisateur : **Paramètres utilisateur → Avancés →
   Mode développeur** (activé), puis clic droit sur votre pseudo → **Copier
   l'identifiant utilisateur**. Collez-le dans `DISCORD_USER_ID`.
3. Chaque message commence par `<@ID>` avec `allowed_mentions` : la mention
   déclenche une notification push même si le salon est en mode « mentions
   uniquement ».

---

## 4. Références Apple et URL produit

`config.yaml.example` est **pré-rempli** avec ce qui a pu être trouvé par
recherche web le 23/10/2026. Les valeurs marquées `À VÉRIFIER` ou
`À RENSEIGNER` sont à contrôler ou à compléter.

> Les références françaises de l'iPhone Duo sont au format **`MK2xxF/A`**
> (et non `ZD/A`). Exemple : `MK2E4F/A` = 256 Go Ciel nocturne.

### Récupérer une référence Apple (DevTools)

1. Ouvrez la page d'achat du modèle sur apple.com/fr, par exemple
   `https://www.apple.com/fr/shop/buy-iphone/iphone-duo`, puis choisissez la
   capacité et le coloris.
2. Ouvrez les DevTools (F12) → onglet **Réseau (Network)**, et filtrez sur
   `fulfillment`.
3. Cliquez sur « Vérifier la disponibilité en magasin » ou modifiez le code
   postal. Une requête `fulfillment-messages?...parts.0=MK2E4F/A...` apparaît.
4. La valeur de `parts.0` est la référence. Vous pouvez aussi lire la réponse
   JSON (onglet **Réponse**) pour vérifier le format attendu par le connecteur
   (voir le commentaire en tête de `watcher/connectors/apple.py`).

Autre méthode : la référence figure aussi dans le code source de la page
(Ctrl+U, puis cherchez `F/A`).

### Récupérer l'URL produit d'un revendeur

Ouvrez la fiche du **modèle exact** (capacité et coloris) et copiez l'URL dans
`models[].urls.<site>`. Lorsqu'un site n'a qu'une page pour toutes les
variantes, choisissez la variante, puis copiez l'URL si elle change (paramètre
`?couleur=`, etc.).

Vérifiez ensuite depuis le homelab :

```bash
docker compose run --rm watcher check-sites boulanger sfr
```

Pour chaque modèle, la commande affiche l'état extrait (statut, délai, prix),
ou l'erreur rencontrée : `BlockedError` (anti-bot), `FormatError` (page non
reconnue ou URL fausse), erreur réseau.

### Sites « faible fiabilité » (Fnac, Darty, Cdiscount, Amazon)

Ces sites sont désactivés par défaut (`enabled: false`). `check-sites` les
teste quand même. Si un site répond sans blocage depuis votre connexion,
passez-le à `enabled: true`. Sinon, gardez-le **en email uniquement**
(section 5).

---

## 5. Boîte email dédiée, alertes revendeurs et Keepa

### Créer et configurer la boîte

Utilisez une adresse **dédiée**, car le service déplace les emails qu'il
traite.

| Fournisseur | IMAP_HOST | Mot de passe |
|---|---|---|
| iCloud | `imap.mail.me.com` | mot de passe pour app : appleid.apple.com → Connexion et sécurité → Mots de passe pour app. `IMAP_USER` = adresse principale iCloud. |
| Gmail | `imap.gmail.com` | Validation en 2 étapes activée, puis myaccount.google.com → Sécurité → Mots de passe des applications |
| Outlook | `outlook.office365.com` | mot de passe d'application (compte Microsoft → Sécurité) |

Remplissez `IMAP_HOST`, `IMAP_USER` et `IMAP_PASSWORD` dans `.env`. **Tant
que ces champs sont vides, le connecteur email est désactivé
automatiquement** (affiché « DÉSACTIVÉ » et non en erreur).

Fonctionnement :

- Le service lit les emails **non lus** du dossier `IMAP_FOLDER`.
- Un email d'alerte reconnu (expéditeur et mot-clé, voir
  `connectors.email.rules`) est marqué comme lu, puis déplacé dans
  `IMAP_PROCESSED_FOLDER` (`Traités`, créé si besoin).
- Chaque Message-ID est mémorisé : un email n'est jamais notifié deux fois.
- Les autres emails (newsletters…) ne sont pas touchés.

### S'inscrire aux alertes « retour en stock »

Avec l'adresse dédiée, sur la fiche de chaque modèle :

- **Fnac** : bouton « M'alerter » / « Être alerté de la disponibilité ».
- **Darty** : « M'avertir de la disponibilité ».
- **Cdiscount** : « Alertez-moi » (selon l'offre).
- **Boulanger** : « M'alerter du retour en stock ».
- **Amazon via Keepa** : créez un compte sur <https://keepa.com>, ajoutez
  l'URL Amazon.fr du produit dans **Suivi de produit**, cochez l'alerte de
  disponibilité **Amazon** (vendu par Amazon) et choisissez la notification
  par **email**, avec l'adresse dédiée.

Après la première alerte reçue, vérifiez l'expéditeur réel et ajustez au besoin
les règles `from` / `keywords` dans `config.yaml`. Testez avec un email
enregistré (`.eml`, voir section 6).

---

## 6. Tester sans attendre un vrai stock

### Commande de test des canaux

```bash
docker compose run --rm watcher test-notify
```

Elle envoie un message Telegram, un message Discord qui vous mentionne, et
lance un **appel Telegram**. Le résultat de chaque canal s'affiche
(`envoyé`, `ignoré (raison)`, `échec: …`).

### Données simulées par connecteur

Le dossier `tests/fixtures/` contient :

| Fichier | Contenu |
|---|---|
| `apple_pickup_available.json` | réponse Apple : 256 Go Ciel nocturne en retrait à Part-Dieu |
| `apple_unavailable.json` | réponse Apple : rien en magasin, livraison fin janvier |
| `boulanger_available.html` | fiche revendeur « en stock, livré sous 3 jours » |
| `boulanger_unavailable.html` | fiche revendeur en rupture |
| `orange_available.html` | fiche opérateur avec prix avec et sans forfait |
| `fnac_blocked.html` | page de CAPTCHA (DataDome) |
| `email_fnac_alert.eml` | email d'alerte Fnac (modèle identifié) |
| `email_keepa_unknown.eml` | alerte Keepa (modèle non identifié) |
| `email_newsletter.eml` | newsletter (doit être ignorée) |

`simulate` rejoue une donnée avec un **état isolé** (`/data/simulation.db`).
Par défaut, les notifications sont affichées sans être envoyées. Ajoutez
`--send` pour les envoyer réellement.

```bash
# Alerte urgente Apple ; relancer la même commande → plus aucune alerte
docker compose run --rm watcher simulate apple tests/fixtures/apple_pickup_available.json
# Revendeur : prioritaire → urgent ; secondaire → message seulement
docker compose run --rm watcher simulate boulanger tests/fixtures/boulanger_available.html --model duo-256-ciel
docker compose run --rm watcher simulate boulanger tests/fixtures/boulanger_available.html --model duo-2to-blanc
# Email d'alerte (traité une seule fois)
docker compose run --rm watcher simulate email tests/fixtures/email_fnac_alert.eml
# Envoi réel sur Telegram/Discord + appel
docker compose run --rm watcher simulate apple tests/fixtures/apple_pickup_available.json --send --db /data/sim2.db
```

Pour utiliser vos propres captures, enregistrez une page (Ctrl+S) ou une
réponse JSON des DevTools, puis montez-la dans le conteneur
(`-v ./capture.html:/tmp/capture.html`).

### Tests automatisés

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

Les tests couvrent les critères d'acceptation :

- alerte urgente unique ;
- prioritaire / secondaire ;
- email traité une seule fois ;
- canal mal configuré ou en échec ;
- connecteur bloqué : une seule alerte technique, attente progressive,
  rétablissement, Apple non impacté ;
- regroupement des appels ;
- commande de test ;
- tableau de bord.

---

## 7. Exploitation : logs, debug, tableau de bord

```bash
docker compose logs -f watcher
docker compose run --rm watcher status    # tableau de bord enregistré
```

- Au démarrage et à chaque heartbeat, les logs affichent l'état de chaque
  connecteur (✅ OK / ⚠️ DÉGRADÉ / ⏸ DÉSACTIVÉ), la dernière vérification
  réussie et l'état des modèles prioritaires sur chaque site.
- `LOG_LEVEL=DEBUG` (dans `.env`) affiche, à chaque vérification et pour chaque
  site, l'état, la date de livraison ou de retrait et le prix de chaque modèle
  suivi.
- Fréquences par défaut :
  - Apple : 2 à 3 min, plus un délai aléatoire, jamais sous 60 s ;
  - revendeurs : 10 à 15 min, plus un délai aléatoire ;
  - email : 1 à 2 min.
- Erreurs :
  - erreur temporaire : attente progressive jusqu'à 30 min ;
  - blocage (403, 429, CAPTCHA) : attente progressive jusqu'à 6 h ;
  - après 3 échecs consécutifs : **une** alerte technique, puis **une** alerte
    de rétablissement.

### Navigateur headless (optionnel)

Il ne sert que si un site n'affiche le stock qu'avec JavaScript. Ajoutez
`fetcher: playwright` (et éventuellement `wait_selector`) au connecteur
concerné dans `config.yaml`, puis lancez la variante :

```bash
docker compose stop watcher
docker compose --profile playwright up -d --build watcher-playwright
```

L'image par défaut ne contient pas de navigateur.

---

## 8. Ajouter un connecteur

### Site « page produit » (cas courant)

1. Ajoutez une classe dans `watcher/connectors/sites.py`, ou dans un nouveau
   module du dossier `watcher/connectors/` (découverte automatique) :

   ```python
   @register("ldlc")
   class LdlcConnector(RetailConnector):
       label = "LDLC"
       # is_operator = True          # si le site vend avec/sans forfait
       # unavailable_patterns = [...] # motifs HTML spécifiques (optionnel)
   ```

2. Ajoutez un bloc `connectors.ldlc` dans `config.yaml` (`enabled`,
   `interval`, `jitter`, et éventuellement les motifs
   `available_patterns`, `unavailable_patterns`, `delivery_patterns`,
   `price_patterns`…).
3. Ajoutez `ldlc: <url>` dans `urls` de chaque modèle.
4. Testez : `check-sites ldlc`, puis `simulate ldlc page.html --model …`.

Le connecteur générique lit d'abord le JSON-LD `schema.org/Product` →
`offers.availability`/`price`, puis, à défaut, les motifs HTML.

### Site avec une API ou un format particulier

Héritez de `Connector` et implémentez `async def check(self) ->
list[ItemState]`. Exemple complet : `watcher/connectors/apple.py`. Levez :

- `BlockedError` en cas d'anti-bot ;
- `FormatError` si la page n'est pas reconnue ;
- `ConnectorError` pour une erreur temporaire.

Le planificateur gère la fréquence, les attentes progressives, la santé et les
alertes.

---

## 9. Architecture

```
watcher/
  __main__.py          CLI : run | test-notify | check-sites | simulate | status
  config.py            .env (Settings) + config.yaml (modèles, connecteurs, règles)
  state.py             SQLite : états, santé, emails traités, dernier appel
  models.py            ItemState normalisé (statut, délai, magasin, prix, forfait), Event
  dates.py             « Livraison : 12/11 », « sous 3 jours », « Aujourd'hui »… → jours
  events.py            ancien état vs nouveau → événement (haute / moyenne)
  service.py           une boucle asyncio indépendante par connecteur, santé, heartbeat
  notify/              Telegram, CallMeBot (limite + regroupement), Discord (@mention)
  connectors/
    apple.py           endpoint JSON fulfillment-messages (retrait + livraison)
    retail.py          générique page produit : JSON-LD → HTML, anti-bot → dégradé
    sites.py           Orange, Bouygues, SFR, Boulanger, Fnac, Darty, Cdiscount, Amazon
    email_imap.py      alertes email (IMAP), Keepa
  playwright_fetcher.py  optionnel
```

Une exception dans un connecteur est capturée dans sa propre boucle. Elle
n'affecte ni Apple ni les autres sites, ni l'envoi des notifications.

#!/usr/bin/env bash
# Vérification automatique du watcher, sans secrets ni accès aux sites.
#
#   ./scripts/verify.sh            build + tests + simulations
#   SKIP_BUILD=1 ./scripts/verify.sh
#
# Variables utiles :
#   IMAGE            image à tester (défaut : iphone-duo-watcher:latest)
#   DOCKER_RUN_ARGS  options supplémentaires pour `docker run` (proxy, CA…)
set -uo pipefail
cd "$(dirname "$0")/.."

IMAGE="${IMAGE:-iphone-duo-watcher:latest}"
CONFIG="config.yaml"
[ -f "$CONFIG" ] || CONFIG="config.yaml.example"
PASS=0
FAIL=0

ok()   { echo "  ✅ $1"; PASS=$((PASS + 1)); }
ko()   { echo "  ❌ $1"; FAIL=$((FAIL + 1)); }
step() { echo; echo "== $1"; }

# Lance une commande shell dans l'image, config montée, état éphémère.
# Les notifications ne partent pas : `simulate` sans --send est un dry-run.
in_image() {
  # shellcheck disable=SC2086
  docker run --rm ${DOCKER_RUN_ARGS:-} \
    -v "$PWD/$CONFIG:/config/config.yaml:ro" \
    -e DATA_DIR=/tmp/verify -e LOG_LEVEL=INFO \
    --entrypoint sh "$IMAGE" -c "$1" 2>&1
}

step "docker compose config"
if [ -f .env ]; then
  docker compose config -q && ok "docker-compose.yml valide" || ko "docker-compose.yml invalide"
else
  # env_file: .env doit exister : copie temporaire de l'exemple, supprimée aussitôt
  cp .env.example .env
  docker compose config -q && ok "docker-compose.yml valide" || ko "docker-compose.yml invalide"
  rm -f .env
fi

if [ "${SKIP_BUILD:-0}" != "1" ]; then
  step "Build de l'image"
  docker build --target default -t "$IMAGE" . >/tmp/watcher-build.log 2>&1 \
    && ok "image $IMAGE construite" || { ko "build en échec (voir /tmp/watcher-build.log)"; exit 1; }
fi

step "Sécurité de l'image"
uid=$(docker run --rm --entrypoint id "$IMAGE" -u)
[ "$uid" != "0" ] && ok "conteneur non-root (uid $uid)" || ko "le conteneur tourne en root"

step "Tests automatisés (pytest dans l'image)"
out=$(docker run --rm ${DOCKER_RUN_ARGS:-} -v "$PWD:/src:ro" -w /src --entrypoint sh "$IMAGE" -c \
  "pip install --quiet --user pytest==9.1.1 >/dev/null 2>&1; cd /tmp && cp -r /src/watcher /src/tests /src/config.yaml.example . && python -m pytest -q -p no:cacheprovider tests" 2>&1)
echo "$out" | tail -3 | sed 's/^/     /'
echo "$out" | grep -qE "[0-9]+ passed" && ! echo "$out" | grep -qE "failed|error" \
  && ok "pytest" || ko "pytest"

step "Simulation Apple : alerte urgente, une seule fois"
out=$(in_image "python -m watcher simulate apple tests/fixtures/apple_pickup_available.json --db /tmp/a.db; \
               echo '---SECOND---'; \
               python -m watcher simulate apple tests/fixtures/apple_pickup_available.json --db /tmp/a.db")
first=${out%%---SECOND---*}
second=${out#*---SECOND---}
echo "$first" | grep -q "\[HIGH\]" && echo "$first" | grep -q "APPEL" \
  && ok "retrait Apple → alerte HAUTE avec appel" || ko "retrait Apple sans alerte haute"
echo "$second" | grep -q "aucune notification" \
  && ok "même état → aucune nouvelle alerte" || ko "alerte répétée sur état identique"

step "Simulation revendeur : prioritaire vs secondaire"
out=$(in_image "python -m watcher simulate boulanger tests/fixtures/boulanger_available.html --model duo-256-ciel --db /tmp/b1.db")
echo "$out" | grep -q "\[HIGH\]" && ok "modèle prioritaire → alerte HAUTE" || ko "prioritaire : pas d'alerte haute"
out=$(in_image "python -m watcher simulate boulanger tests/fixtures/boulanger_available.html --model duo-2to-blanc --db /tmp/b2.db")
echo "$out" | grep -q "\[MEDIUM\]" && ! echo "$out" | grep -q "APPEL" \
  && ok "modèle secondaire (2 To) → message seul, sans appel" || ko "secondaire : comportement inattendu"

step "Simulation opérateur : prix avec / sans forfait"
out=$(in_image "python -m watcher simulate orange tests/fixtures/orange_available.html --model duo-256-ciel --db /tmp/o.db")
echo "$out" | grep -q "Prix sans forfait : 2 339,00" && echo "$out" | grep -q "avec forfait : à partir de 1 469,00" \
  && ok "prix sans et avec forfait affichés" || ko "prix opérateur non extraits"

step "Simulation email : urgent, traité une seule fois"
out=$(in_image "python -m watcher simulate email tests/fixtures/email_fnac_alert.eml --db /tmp/e.db; \
               echo '---SECOND---'; \
               python -m watcher simulate email tests/fixtures/email_fnac_alert.eml --db /tmp/e.db")
first=${out%%---SECOND---*}
second=${out#*---SECOND---}
echo "$first" | grep -q "\[HIGH\]" && ok "email d'alerte → alerte HAUTE" || ko "email d'alerte non détecté"
echo "$second" | grep -q "aucune notification" && ok "email non retraité" || ko "email traité deux fois"
out=$(in_image "python -m watcher simulate email tests/fixtures/email_newsletter.eml --db /tmp/n.db")
echo "$out" | grep -q "aucune notification" && ok "newsletter ignorée" || ko "newsletter notifiée"

step "Simulation blocage anti-bot"
out=$(in_image "python -m watcher simulate fnac tests/fixtures/fnac_blocked.html --model duo-256-ciel --db /tmp/f.db")
echo "$out" | grep -q "bloqué" && ok "CAPTCHA détecté comme blocage (pas de contournement)" || ko "blocage non détecté"

step "Connecteur email sans identifiants IMAP"
out=$(in_image "IMAP_HOST= IMAP_USER= IMAP_PASSWORD= python -m watcher status")
echo "$out" | grep -q "Emails d'alerte — DÉSACTIVÉ" && ok "désactivé automatiquement" || ko "email non désactivé"

echo
echo "Résultat : $PASS OK, $FAIL échec(s)"
[ "$FAIL" -eq 0 ]

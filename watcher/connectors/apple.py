"""Apple Store France — endpoint JSON `fulfillment-messages` (retrait + livraison).

Endpoint non documenté, utilisé par apple.com/fr et l'app Apple Store :
  https://www.apple.com/fr/shop/fulfillment-messages
    ?pl=true&mts.0=regular&mts.1=compact&parts.0=MK2D4F/A&parts.1=...
    &searchNearby=true&location=69530

Structure attendue de la réponse (champs utilisés uniquement) :
  body.content.pickupMessage.stores[]           → magasins proches
      .storeNumber / .storeName / .storedistance
      .partsAvailability[<réf>].pickupDisplay     → "available" | "unavailable" | "ineligible"
      .partsAvailability[<réf>].pickupSearchQuote → « Aujourd'hui », « Indisponible »…
  body.content.deliveryMessage[<réf>].regular
      .isBuyable
      .deliveryOptions[].date / .displayName      → « Livraison : 12/11 – 19/11 »…

Le parseur est tolérant (plusieurs variantes de champs) ; si rien n'est reconnu il lève
FormatError → alerte technique « format non reconnu ».

Secours : si l'endpoint principal ne répond plus au format attendu, l'ancien endpoint
`/fr/shop/retail/pickup-message` (retrait uniquement, `body.stores[]`) est essayé ; une
alerte technique signale la bascule. Une référence absente de la réponse (référence
fausse ou retirée par Apple) déclenche aussi une alerte technique, une fois par démarrage.
La dernière réponse brute est enregistrée dans DATA_DIR/apple_last.json (diagnostic).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..config import ModelConfig
from ..dates import parse_delay
from ..models import Channel, Event, ItemState, Priority, Status
from .base import Connector, FormatError, register

DEFAULT_BUY_URL = "https://www.apple.com/fr/shop/buy-iphone/iphone-duo"
PRIMARY_URL = "https://www.apple.com/fr/shop/fulfillment-messages"
FALLBACK_URL = "https://www.apple.com/fr/shop/retail/pickup-message"


@register("apple")
class AppleConnector(Connector):
    label = "Apple Store"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._events: list[Event] = []
        self._warned: set[str] = set()  # alertes déjà émises depuis le démarrage
        self.last_raw: list[str] = []

    def tracked_models(self) -> list[ModelConfig]:
        return [m for m in self.app.models if m.apple_part]

    def extra_events(self) -> list[Event]:
        ev, self._events = self._events, []
        return ev

    def _warn_once(self, key: str, title: str, body: str) -> None:
        self.log.warning("%s — %s", title, body.replace("\n", " | "))
        if key not in self._warned:
            self._warned.add(key)
            self._events.append(Event(Priority.TECH, title, body, kind="tech"))

    def _buy_url(self, m: ModelConfig) -> str:
        return m.urls.get("apple") or self.opts.get("buy_url") or DEFAULT_BUY_URL

    async def check(self) -> list[ItemState]:
        models = self.tracked_models()
        if not models:
            return []
        fixture = self.fixture()
        if fixture is not None:  # simulation : une seule réponse pour tous les modèles
            items = self._parse_raw(fixture, models)
        else:
            self.last_raw = []
            items = []
            batch = max(1, int(self.opts.get("batch_size", 4)))
            for i in range(0, len(models), batch):
                items.extend(await self._check_group(models[i : i + batch]))
            self._dump_raw()
        self._check_missing_parts(models, items)
        return items

    async def _check_group(self, group: list[ModelConfig]) -> list[ItemState]:
        try:
            return self._parse_raw(await self._request(group, self.opts.get("base_url", PRIMARY_URL)), group)
        except FormatError as primary_exc:
            fallback = self.opts.get("fallback_url", FALLBACK_URL)
            if not fallback:
                raise
            try:
                items = self._parse_raw(await self._request(group, fallback), group)
            except FormatError:
                raise primary_exc from None
            self._warn_once(
                "fallback", "Apple : endpoint de secours utilisé",
                f"fulfillment-messages ne répond plus au format attendu ({primary_exc}).\n"
                "Le retrait en magasin reste surveillé via pickup-message ; la livraison n'est plus suivie. "
                "Lancez `probe-apple` pour diagnostiquer.")
            return items

    def _check_missing_parts(self, models: list[ModelConfig], items: list[ItemState]) -> None:
        seen = {i.model_id for i in items}
        for m in models:
            if m.id not in seen:
                self._warn_once(
                    f"missing:{m.apple_part}", f"Apple : référence {m.apple_part} introuvable",
                    f"{m.name} n'apparaît pas dans la réponse Apple : la référence est sans doute fausse "
                    "et ce modèle n'est PAS surveillé chez Apple. Corrigez apple_part dans config.yaml "
                    "(voir README, « Récupérer une référence Apple »).")

    def _parse_raw(self, raw: str, group: list[ModelConfig]) -> list[ItemState]:
        self.last_raw.append(raw)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise FormatError(f"Réponse Apple non JSON : {exc}") from exc
        return self.parse(data, group)

    def _dump_raw(self) -> None:
        try:
            path = Path(self.settings.data_dir) / "apple_last.json"
            path.write_text("[\n" + ",\n".join(self.last_raw) + "\n]", encoding="utf-8")
        except OSError:
            pass

    async def _request(self, group: list[ModelConfig], base: str) -> str:
        params = {"fae": "true", "pl": "true", "mts.0": "regular", "mts.1": "compact",
                  "searchNearby": "true", "location": str(self.opts.get("postal_code") or self.settings.postal_code)}
        for idx, m in enumerate(group):
            params[f"parts.{idx}"] = m.apple_part
        r = await self.fetch(base, params=params, headers={
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Referer": DEFAULT_BUY_URL,
            "X-Requested-With": "XMLHttpRequest",
        })
        return r.text

    # --- parsing ------------------------------------------------------------
    def _store_allowed(self, store: dict) -> bool:
        wanted = [str(s).lower() for s in self.opts.get("stores") or []]
        if wanted:
            hay = f"{store.get('storeNumber', '')} {store.get('storeName', '')} {store.get('city', '')}".lower()
            if not any(w in hay for w in wanted):
                return False
        max_km = self.opts.get("max_distance_km")
        if max_km is not None:
            try:
                if float(store.get("storedistance", 0)) > float(max_km):
                    return False
            except (TypeError, ValueError):
                pass
        return True

    def parse(self, data: dict, group: list[ModelConfig]) -> list[ItemState]:
        body = data.get("body", data)
        content = body.get("content", body) if isinstance(body, dict) else {}
        pickup = content.get("pickupMessage") or {}
        if not pickup and isinstance(content.get("stores"), list):  # ancien format pickup-message
            pickup = content
        delivery = content.get("deliveryMessage") or {}
        if not pickup and not delivery:
            keys = ", ".join(list(content)[:8]) if isinstance(content, dict) else type(content).__name__
            raise FormatError(f"Réponse Apple sans pickupMessage ni deliveryMessage (clés : {keys})")

        by_part = {m.apple_part: m for m in group}
        items: list[ItemState] = []

        stores = [s for s in (pickup.get("stores") or []) if self._store_allowed(s)]
        stores = stores[: int(self.opts.get("max_stores", 6))]
        for store in stores:
            name = store.get("storeName") or store.get("address", {}).get("address") or store.get("storeNumber")
            pa = store.get("partsAvailability") or {}
            for part, m in by_part.items():
                info = pa.get(part)
                if info is None:
                    continue
                disp = str(info.get("pickupDisplay", "")).lower()
                quote = (info.get("pickupSearchQuote") or info.get("storePickupQuote")
                         or (info.get("messageTypes", {}).get("regular", {}) or {}).get("storePickupQuote")
                         or "")
                quote = _strip_html(quote)
                if disp == "available":
                    status, delay = Status.AVAILABLE, parse_delay(quote)
                    delay = 0 if delay is None else delay
                elif disp in ("unavailable", "ineligible"):
                    status, delay = Status.UNAVAILABLE, None
                else:
                    status, delay = Status.UNKNOWN, None
                items.append(ItemState(
                    connector=self.name, model_id=m.id, channel=Channel.PICKUP, status=status,
                    delay_days=delay, date_text=quote or None, store=f"Apple {name}",
                    store_id=str(store.get("storeNumber") or name), url=self._buy_url(m),
                    extra={"distance": store.get("storeDistanceWithUnit") or store.get("storedistance")},
                ))

        for part, m in by_part.items():
            dm = delivery.get(part)
            if not isinstance(dm, dict):
                continue
            reg = dm.get("regular") or dm.get("compact") or dm
            texts = []
            for opt in reg.get("deliveryOptions") or []:
                texts.append(_strip_html(opt.get("date") or opt.get("displayName") or ""))
            for opt in reg.get("deliveryOptionMessages") or []:
                texts.append(_strip_html(opt.get("displayName") or ""))
            texts = [t for t in texts if t]
            buyable = reg.get("isBuyable", dm.get("isBuyable"))
            if buyable is None and "buyability" in reg:
                buyable = reg["buyability"].get("isBuyable")
            text = " / ".join(texts) or None
            low = (text or "").lower()
            if buyable is False or "indisponible" in low or "épuisé" in low:
                status, delay = Status.UNAVAILABLE, None
            elif texts:
                delay = parse_delay(text)
                status = Status.AVAILABLE
            elif buyable:
                status, delay = Status.AVAILABLE, None
            else:
                status, delay = Status.UNKNOWN, None
            items.append(ItemState(
                connector=self.name, model_id=m.id, channel=Channel.DELIVERY, status=status,
                delay_days=delay, date_text=text, url=self._buy_url(m),
            ))

        if not items:
            raise FormatError("Aucune référence suivie trouvée dans la réponse Apple")
        return items


def _strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "").replace("\xa0", " ").strip()

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
"""

from __future__ import annotations

import json
import re

from ..config import ModelConfig
from ..dates import parse_delay
from ..models import Channel, ItemState, Status
from .base import Connector, FormatError, register

DEFAULT_BUY_URL = "https://www.apple.com/fr/shop/buy-iphone/iphone-duo"


@register("apple")
class AppleConnector(Connector):
    label = "Apple Store"

    def tracked_models(self) -> list[ModelConfig]:
        return [m for m in self.app.models if m.apple_part]

    def _buy_url(self, m: ModelConfig) -> str:
        return m.urls.get("apple") or self.opts.get("buy_url") or DEFAULT_BUY_URL

    async def check(self) -> list[ItemState]:
        models = self.tracked_models()
        if not models:
            return []
        fixture = self.fixture()
        if fixture is not None:  # simulation : une seule réponse pour tous les modèles
            return self._parse_raw(fixture, models)
        items: list[ItemState] = []
        batch = max(1, int(self.opts.get("batch_size", 4)))
        for i in range(0, len(models), batch):
            group = models[i : i + batch]
            items.extend(self._parse_raw(await self._request(group), group))
        return items

    def _parse_raw(self, raw: str, group: list[ModelConfig]) -> list[ItemState]:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise FormatError(f"Réponse Apple non JSON : {exc}") from exc
        return self.parse(data, group)

    async def _request(self, group: list[ModelConfig]) -> str:
        base = self.opts.get("base_url", "https://www.apple.com/fr/shop/fulfillment-messages")
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
        delivery = content.get("deliveryMessage") or {}
        if not pickup and not delivery:
            raise FormatError("Réponse Apple sans pickupMessage ni deliveryMessage")

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
                quote = (info.get("pickupSearchQuote")
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

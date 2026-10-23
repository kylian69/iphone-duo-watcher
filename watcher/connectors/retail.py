"""Connecteur générique « page produit » pour revendeurs et opérateurs.

Ordre d'extraction :
  1. détection de blocage (CAPTCHA / anti-bot) → BlockedError, jamais contourné ;
  2. données structurées JSON-LD schema.org/Product → offers.availability, price ;
  3. sinon motifs HTML configurables (disponible / rupture / précommande) ;
  4. délai : motifs de texte de livraison/retrait → parse_delay().

Chaque site est une petite sous-classe qui fixe ses motifs par défaut ; tout est
surchargeable dans config.yaml (`connectors.<site>.*`).
"""

from __future__ import annotations

import asyncio
import html as htmllib
import json
import random
import re
from typing import ClassVar

from ..config import ModelConfig
from ..dates import parse_delay, parse_price
from ..models import Channel, ItemState, Status
from .base import BlockedError, Connector, FormatError

BLOCK_MARKERS = [
    r"captcha", r"datadome", r"geo\.captcha-delivery\.com", r"cf-chl", r"challenge-platform",
    r"px-captcha", r"perimeterx", r"access denied", r"request unsuccessful",
    r"pardon our interruption", r"robot or human", r"vous [eê]tes un robot",
    r"activit[ée] inhabituelle", r"incapsula",
]

UNAVAILABLE_DEFAULT = [
    r"rupture de stock", r"temporairement indisponible", r"momentan[ée]ment indisponible",
    r"produit indisponible", r"article indisponible", r"[ée]puis[ée]", r"plus disponible",
    r"m['’]alerter", r"me pr[ée]venir", r"alertez[- ]moi", r"[êe]tre (?:alert|pr[ée]venu)",
]
PREORDER_DEFAULT = [r"pr[ée][- ]?command", r"pre-?order", r"r[ée]servez[- ]le"]
AVAILABLE_DEFAULT = [r"ajouter au panier", r"en stock", r"disponible en livraison", r"commander maintenant",
                     r"je (?:le )?commande", r"acheter maintenant"]
DELIVERY_DEFAULT = [
    r"(?:livr[ée]e?s?|livraison|exp[ée]di[ée]e?s?|exp[ée]dition|retrait|disponible)"
    r"[^.<\n]{0,25}?(?:d[èe]s|le|du|à partir du|a partir du|sous|en|avant le|entre le)\s[^.<\n]{1,40}",
]

AVAIL_MAP = [
    ("preorder", Status.PREORDER), ("backorder", Status.PREORDER),
    ("outofstock", Status.UNAVAILABLE), ("soldout", Status.UNAVAILABLE), ("discontinued", Status.UNAVAILABLE),
    ("instock", Status.AVAILABLE), ("limitedavailability", Status.AVAILABLE),
    ("onlineonly", Status.AVAILABLE), ("instoreonly", Status.AVAILABLE),
]

_LD_RE = re.compile(r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", re.S | re.I)
_TAG_RE = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>|<[^>]+>", re.S | re.I)


def visible_text(page: str) -> str:
    text = _TAG_RE.sub(" ", page)
    text = htmllib.unescape(text).replace("\xa0", " ").replace(" ", " ")
    return re.sub(r"\s+", " ", text)


def _walk(node):
    if isinstance(node, list):
        for n in node:
            yield from _walk(n)
    elif isinstance(node, dict):
        yield node
        for k in ("@graph", "hasVariant", "isVariantOf", "itemListElement", "item", "mainEntity"):
            if k in node:
                yield from _walk(node[k])


def json_ld_products(page: str) -> list[dict]:
    out = []
    for block in _LD_RE.findall(page):
        try:
            data = json.loads(block.strip())
        except json.JSONDecodeError:
            continue
        for n in _walk(data):
            t = n.get("@type")
            types = t if isinstance(t, list) else [t]
            if any(str(x).lower() in ("product", "productgroup") for x in types) and "offers" in n:
                out.append(n)
    return out


def _offers(product: dict) -> list[dict]:
    o = product.get("offers")
    if isinstance(o, dict) and isinstance(o.get("offers"), list):
        return [o] + o["offers"]
    return o if isinstance(o, list) else ([o] if isinstance(o, dict) else [])


def availability_status(value: str | None) -> Status:
    v = (value or "").lower().rsplit("/", 1)[-1].replace("_", "")
    for key, st in AVAIL_MAP:
        if key in v:
            return st
    return Status.UNKNOWN


class RetailConnector(Connector):
    """Sous-classes : fixer `label`, éventuellement `is_operator` et les motifs par défaut."""

    is_operator: ClassVar[bool] = False
    unavailable_patterns: ClassVar[list[str]] = UNAVAILABLE_DEFAULT
    preorder_patterns: ClassVar[list[str]] = PREORDER_DEFAULT
    available_patterns: ClassVar[list[str]] = AVAILABLE_DEFAULT
    delivery_patterns: ClassVar[list[str]] = DELIVERY_DEFAULT
    price_with_plan_patterns: ClassVar[list[str]] = [
        r"avec (?:un )?forfait[^€]{0,80}?(\d[\d  .]*(?:,\d{2})?)\s?€",
        r"(\d[\d  .]*(?:,\d{2})?)\s?€[^€]{0,40}?avec (?:un )?forfait",
    ]
    price_without_plan_patterns: ClassVar[list[str]] = [
        r"sans forfait[^€]{0,80}?(\d[\d  .]*(?:,\d{2})?)\s?€",
        r"(\d[\d  .]*(?:,\d{2})?)\s?€[^€]{0,40}?sans forfait",
        r"(?:t[ée]l[ée]phone|mobile) seul[^€]{0,60}?(\d[\d  .]*(?:,\d{2})?)\s?€",
    ]

    def _pats(self, key: str, default: list[str]) -> list[re.Pattern]:
        return [re.compile(p, re.I) for p in (self.opts.get(key) or default)]

    async def check(self) -> list[ItemState]:
        cache: dict[str, str] = {}
        items, errors = [], []
        for m in self.tracked_models():
            url = m.urls[self.name]
            try:
                page = self.fixture(m.id)
                if page is None:
                    if url not in cache:
                        if cache:  # espace les requêtes vers un même site
                            await asyncio.sleep(random.uniform(*self.opts.get("request_spacing", (3, 8))))
                        cache[url] = await self.get_page(url)
                    page = cache[url]
                items.append(self.parse(page, m, url))
            except FormatError as exc:
                # URL cassée ou page d'un seul modèle illisible : les autres modèles continuent.
                # Blocage et erreurs réseau concernent tout le site → remontés tels quels.
                errors.append(exc)
                self.log.warning("%s : %s", m.id, exc)
        if errors and not items:
            raise errors[0]
        return items

    async def get_page(self, url: str) -> str:
        if self.opts.get("fetcher") == "playwright":
            from ..playwright_fetcher import fetch_with_browser

            return await fetch_with_browser(url, wait_selector=self.opts.get("wait_selector"))
        return (await self.fetch(url)).text

    # --- parsing ------------------------------------------------------------
    def detect_block(self, page: str) -> None:
        head = page[:20000].lower()
        markers = self.opts.get("block_markers") or BLOCK_MARKERS
        # Une vraie page produit est volumineuse ; une page de challenge est courte
        for mk in markers:
            if re.search(mk, head) and (len(page) < 60000 or "captcha-delivery" in head or "px-captcha" in head):
                raise BlockedError(f"Page anti-bot détectée (« {mk} »)")

    def _pick_product(self, products: list[dict], m: ModelConfig) -> dict | None:
        if len(products) <= 1:
            return products[0] if products else None
        for p in products:
            name = f"{p.get('name', '')} {p.get('sku', '')} {p.get('color', '')}"
            if m.apple_part and m.apple_part.split("/")[0].lower() in name.lower():
                return p
            if (m.email_capacity and m.email_capacity.search(name)) and (m.email_color and m.email_color.search(name)):
                return p
        return None

    def parse(self, page: str, m: ModelConfig, url: str) -> ItemState:
        self.detect_block(page)
        text = visible_text(page)
        status = Status.UNKNOWN
        price = None
        prices: list[float] = []

        product = self._pick_product(json_ld_products(page), m)
        if product:
            for off in _offers(product):
                st = availability_status(off.get("availability"))
                if st is not Status.UNKNOWN and (status is Status.UNKNOWN or st is Status.AVAILABLE):
                    status = st
                for k in ("price", "lowPrice", "highPrice"):
                    p = parse_price(off.get(k)) if off.get(k) not in (None, "") else None
                    if p:
                        prices.append(p)
            price = min(prices) if prices else None

        if status is Status.UNKNOWN:
            low = text.lower()
            if any(p.search(low) for p in self._pats("unavailable_patterns", self.unavailable_patterns)):
                status = Status.UNAVAILABLE
            elif any(p.search(low) for p in self._pats("preorder_patterns", self.preorder_patterns)):
                status = Status.PREORDER
            elif any(p.search(low) for p in self._pats("available_patterns", self.available_patterns)):
                status = Status.AVAILABLE

        if status is Status.UNKNOWN:
            raise FormatError(f"État de stock introuvable sur la page ({m.id})")

        delay, date_text = None, None
        if status in (Status.AVAILABLE, Status.PREORDER):
            snippets = []
            for p in self._pats("delivery_patterns", self.delivery_patterns):
                snippets += [mt.group(0).strip() for mt in p.finditer(text)]
            parsed = [(parse_delay(s), s) for s in snippets]
            parsed = [(d, s) for d, s in parsed if d is not None]
            if parsed:
                delay, date_text = min(parsed)

        price_plan = None
        if self.is_operator:
            price_plan = self._regex_price(text, "price_with_plan_patterns", self.price_with_plan_patterns, minimum=0.5)
            no_plan = self._regex_price(text, "price_without_plan_patterns", self.price_without_plan_patterns)
            if no_plan:
                price = no_plan
            elif len(prices) >= 2:  # AggregateOffer : bas = avec forfait, haut = sans forfait
                price, price_plan = max(prices), price_plan or min(prices)
            if price_plan and price and price_plan >= price:
                price_plan = None
        elif price is None:
            price = self._regex_price(text, "price_patterns", [r"(\d[\d  .]{2,}(?:,\d{2})?)\s?€"])

        return ItemState(
            connector=self.name, model_id=m.id, channel=Channel.DELIVERY, status=status,
            delay_days=delay, date_text=date_text, price=price, price_with_plan=price_plan, url=url,
            extra={"source": "json-ld" if product else "html"},
        )

    def _regex_price(self, text: str, key: str, default: list[str], minimum: float = 50) -> float | None:
        for p in self._pats(key, default):
            for mt in p.finditer(text):
                v = parse_price(mt.group(1))
                if v and v >= minimum:  # sans forfait : écarte mensualités et frais symboliques
                    return v
        return None

"""Socle commun des connecteurs.

Un connecteur = une classe décorée par @register("nom") dans un module du paquet
`watcher.connectors`. Il est découvert automatiquement : ajouter un site ne demande
de toucher à rien d'autre (hors config.yaml).
"""

from __future__ import annotations

import logging
import random
from pathlib import Path
from typing import ClassVar

import httpx

from ..config import AppConfig, ConnectorConfig, ModelConfig, Settings
from ..models import Event, ItemState

REGISTRY: dict[str, type["Connector"]] = {}

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.6",
    "Cache-Control": "no-cache",
}


def register(name: str):
    def deco(cls):
        cls.name = name
        REGISTRY[name] = cls
        return cls

    return deco


class ConnectorError(Exception):
    """Erreur temporaire (réseau, 5xx…)."""


class BlockedError(ConnectorError):
    """Le site bloque (403, 429, CAPTCHA, page anti-bot). Jamais contourné."""


class FormatError(ConnectorError):
    """La page / la réponse ne correspond plus au format attendu."""


class Connector:
    name: ClassVar[str] = "base"
    label: ClassVar[str] = "Base"
    kind: ClassVar[str] = "http"  # http | email

    def __init__(self, cfg: ConnectorConfig, app: AppConfig, settings: Settings,
                 client: httpx.AsyncClient | None = None):
        self.cfg = cfg
        self.app = app
        self.settings = settings
        self.opts = cfg.options
        self.client = client
        self.log = logging.getLogger(f"connector.{self.name}")

    # --- à implémenter ------------------------------------------------------
    async def check(self) -> list[ItemState]:
        """Interroge le site et renvoie l'état normalisé de chaque modèle suivi."""
        raise NotImplementedError

    def disabled_reason(self) -> str | None:
        """Raison de désactivation automatique (ex. identifiants manquants)."""
        if self.kind == "http" and not self.tracked_models():
            return "aucun modèle configuré pour ce site (URL produit / référence manquante)"
        return None

    def extra_events(self) -> list[Event]:
        """Événements produits directement par le connecteur (ex. emails d'alerte)."""
        return []

    # --- utilitaires --------------------------------------------------------
    def next_delay(self) -> float:
        lo, hi = self.cfg.interval
        return random.uniform(lo, hi) + random.uniform(0, self.cfg.jitter)

    def tracked_models(self) -> list[ModelConfig]:
        return [m for m in self.app.models if self.name in m.urls]

    def fixture(self, key: str | None = None) -> str | None:
        """Mode simulation : `fixture:` (ou `fixtures: {modèle: fichier}`) dans config.yaml."""
        fx = self.opts.get("fixtures") or {}
        path = fx.get(key) if key and isinstance(fx, dict) else None
        path = path or self.opts.get("fixture")
        return Path(path).read_text(encoding="utf-8") if path else None

    async def fetch(self, url: str, **kw) -> httpx.Response:
        assert self.client is not None
        headers = {**BROWSER_HEADERS, **kw.pop("headers", {})}
        try:
            r = await self.client.get(url, headers=headers, follow_redirects=True, **kw)
        except httpx.HTTPError as exc:
            raise ConnectorError(f"{type(exc).__name__}: {exc}") from exc
        if r.status_code in (401, 403, 429, 541):
            raise BlockedError(f"HTTP {r.status_code} sur {url}")
        if r.status_code >= 500:
            raise ConnectorError(f"HTTP {r.status_code} sur {url}")
        if r.status_code >= 400:
            raise FormatError(f"HTTP {r.status_code} sur {url} (URL produit à vérifier)")
        return r

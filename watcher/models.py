"""Types partagés : état normalisé renvoyé par les connecteurs, événements."""

from __future__ import annotations

import enum
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime


class Status(str, enum.Enum):
    AVAILABLE = "disponible"
    UNAVAILABLE = "indisponible"
    PREORDER = "précommande"
    UNKNOWN = "inconnu"


class Priority(enum.IntEnum):
    LOW = 1  # heartbeat, démarrage
    TECH = 2  # alerte technique / rétablissement
    MEDIUM = 3  # changement d'état
    HIGH = 4  # disponibilité urgente → appel


class Channel(str, enum.Enum):
    """Mode d'obtention : livraison ou retrait (le magasin est dans ItemState.store)."""

    DELIVERY = "livraison"
    PICKUP = "retrait"


@dataclass
class ItemState:
    """État normalisé d'un modèle sur un site (et un magasin pour le retrait)."""

    connector: str
    model_id: str
    channel: Channel = Channel.DELIVERY
    status: Status = Status.UNKNOWN
    delay_days: int | None = None  # délai estimé avant livraison/retrait
    date_text: str | None = None  # texte brut annoncé par le site
    store: str | None = None  # nom du magasin (retrait)
    store_id: str | None = None
    price: float | None = None  # prix sans forfait
    price_with_plan: float | None = None  # opérateurs : prix avec forfait
    url: str | None = None
    extra: dict = field(default_factory=dict)
    checked_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    @property
    def key(self) -> str:
        suffix = f":{self.store_id or self.store}" if self.channel is Channel.PICKUP else ""
        return f"{self.connector}:{self.model_id}:{self.channel.value}{suffix}"

    def signature(self) -> tuple:
        """Champs dont la variation constitue un « changement d'état »."""
        return (self.status.value, self.delay_days, (self.date_text or "").strip().lower())

    def to_json(self) -> str:
        d = asdict(self)
        d["channel"] = self.channel.value
        d["status"] = self.status.value
        return json.dumps(d, ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> "ItemState":
        d = json.loads(raw)
        d["channel"] = Channel(d["channel"])
        d["status"] = Status(d["status"])
        return cls(**d)


@dataclass
class Event:
    priority: Priority
    title: str
    body: str
    url: str | None = None
    call_text: str | None = None  # texte lu par l'appel (priorité haute uniquement)
    kind: str = "change"  # change | urgent | tech | recovery | heartbeat | startup | test

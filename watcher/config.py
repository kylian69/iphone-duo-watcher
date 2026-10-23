"""Chargement de la configuration : secrets/réglages globaux (.env) + config.yaml."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

from .models import Priority


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_bool(name: str, default: bool = True) -> bool:
    v = _env(name)
    if not v:
        return default
    return v.lower() in ("1", "true", "yes", "oui", "on")


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name) or default)
    except ValueError:
        return default


@dataclass
class Settings:
    """Réglages issus du .env."""

    telegram_enabled: bool = True
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    callmebot_enabled: bool = True
    callmebot_user: str = ""  # @username Telegram
    callmebot_lang: str = "fr-FR-Standard-A"
    callmebot_min_interval_min: float = 5.0
    callmebot_repeat: int = 2

    discord_enabled: bool = True
    discord_webhook_url: str = ""
    discord_user_id: str = ""

    imap_host: str = ""
    imap_port: int = 993
    imap_user: str = ""
    imap_password: str = ""
    imap_folder: str = "INBOX"
    imap_processed_folder: str = "Traités"

    postal_code: str = "69530"
    log_level: str = "INFO"
    heartbeat_hours: float = 12.0
    config_path: str = "/config/config.yaml"
    data_dir: str = "/data"

    @property
    def debug(self) -> bool:
        return self.log_level.upper() == "DEBUG"

    @property
    def imap_configured(self) -> bool:
        return bool(self.imap_host and self.imap_user and self.imap_password)

    @classmethod
    def from_env(cls, env_file: str | None = None) -> "Settings":
        load_dotenv(env_file or ".env", override=False)
        return cls(
            telegram_enabled=_env_bool("TELEGRAM_ENABLED"),
            telegram_bot_token=_env("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=_env("TELEGRAM_CHAT_ID"),
            callmebot_enabled=_env_bool("CALLMEBOT_ENABLED"),
            callmebot_user=_env("CALLMEBOT_TELEGRAM_USER"),
            callmebot_lang=_env("CALLMEBOT_LANG", "fr-FR-Standard-A"),
            callmebot_min_interval_min=_env_float("CALLMEBOT_MIN_INTERVAL_MINUTES", 5),
            callmebot_repeat=int(_env_float("CALLMEBOT_REPEAT", 2)),
            discord_enabled=_env_bool("DISCORD_ENABLED"),
            discord_webhook_url=_env("DISCORD_WEBHOOK_URL"),
            discord_user_id=_env("DISCORD_USER_ID"),
            imap_host=_env("IMAP_HOST"),
            imap_port=int(_env_float("IMAP_PORT", 993)),
            imap_user=_env("IMAP_USER"),
            imap_password=_env("IMAP_PASSWORD"),
            imap_folder=_env("IMAP_FOLDER", "INBOX"),
            imap_processed_folder=_env("IMAP_PROCESSED_FOLDER", "Traités"),
            postal_code=_env("POSTAL_CODE", "69530"),
            log_level=_env("LOG_LEVEL", "INFO").upper(),
            heartbeat_hours=_env_float("HEARTBEAT_HOURS", 12),
            config_path=_env("CONFIG_PATH", "/config/config.yaml"),
            data_dir=_env("DATA_DIR", "/data"),
        )


@dataclass
class ModelConfig:
    id: str
    name: str  # « iPhone Duo 256 Go Ciel nocturne »
    short: str  # « 256 Go Ciel nocturne » (messages vocaux)
    apple_part: str | None
    priority: str = "high"  # high | low
    allow_call: bool = True
    urls: dict[str, str] = field(default_factory=dict)
    email_capacity: re.Pattern | None = None
    email_color: re.Pattern | None = None

    @property
    def is_priority(self) -> bool:
        return self.priority == "high"


@dataclass
class ConnectorConfig:
    name: str
    enabled: bool
    interval: tuple[float, float]  # secondes (min, max)
    jitter: float
    options: dict


@dataclass
class AppConfig:
    models: list[ModelConfig]
    connectors: dict[str, ConnectorConfig]
    thresholds: dict[str, int]  # délai max (jours) pour une alerte urgente, par connecteur
    failure_threshold: int
    max_backoff_s: float
    max_backoff_blocked_s: float
    urgent_if_delay_unknown: bool
    raw: dict

    def model(self, model_id: str) -> ModelConfig | None:
        return next((m for m in self.models if m.id == model_id), None)

    def threshold_for(self, connector: str) -> int:
        return int(self.thresholds.get(connector, self.thresholds.get("default", 7)))


# Fréquences minimales imposées quelle que soit la config
MIN_INTERVAL = {"apple": 60.0}
DEFAULT_INTERVALS = {"apple": (120, 180), "email": (60, 120)}
DEFAULT_RETAILER_INTERVAL = (600, 900)


def _interval(raw, name: str) -> tuple[float, float]:
    if raw is None:
        lo, hi = DEFAULT_INTERVALS.get(name, DEFAULT_RETAILER_INTERVAL)
    elif isinstance(raw, (int, float)):
        lo = hi = float(raw)
    else:
        lo, hi = float(raw[0]), float(raw[-1])
    floor = MIN_INTERVAL.get(name, 30.0)
    lo, hi = max(lo, floor), max(hi, floor)
    return (min(lo, hi), max(lo, hi))


def load_config(path: str | Path) -> AppConfig:
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    models = []
    for m in raw.get("models", []):
        prio = str(m.get("priority", "high")).lower()
        if prio not in ("high", "low"):
            raise ValueError(f"Modèle {m.get('id')}: priority doit valoir high ou low")
        em = m.get("email_match") or {}
        models.append(
            ModelConfig(
                id=m["id"],
                name=m.get("name", m["id"]),
                short=m.get("short", m.get("name", m["id"])),
                apple_part=m.get("apple_part"),
                priority=prio,
                allow_call=bool(m.get("allow_call", prio == "high")),
                urls={k: v for k, v in (m.get("urls") or {}).items() if v},
                email_capacity=re.compile(em["capacity"], re.I) if em.get("capacity") else None,
                email_color=re.compile(em["color"], re.I) if em.get("color") else None,
            )
        )
    if len({m.id for m in models}) != len(models):
        raise ValueError("Identifiants de modèles en double dans config.yaml")

    connectors = {}
    for name, c in (raw.get("connectors") or {}).items():
        c = dict(c or {})
        connectors[name] = ConnectorConfig(
            name=name,
            enabled=bool(c.pop("enabled", True)),
            interval=_interval(c.pop("interval", None), name),
            jitter=float(c.pop("jitter", 30)),
            options=c,
        )

    alerts = raw.get("alerts") or {}
    thresholds = {"default": 7, "apple": 30}
    thresholds.update(alerts.get("urgent_max_days") or {})
    return AppConfig(
        models=models,
        connectors=connectors,
        thresholds=thresholds,
        failure_threshold=int(alerts.get("failure_threshold", 3)),
        max_backoff_s=float(alerts.get("max_backoff_minutes", 30)) * 60,
        max_backoff_blocked_s=float(alerts.get("max_backoff_blocked_hours", 6)) * 3600,
        urgent_if_delay_unknown=bool(alerts.get("urgent_if_delay_unknown", True)),
        raw=raw,
    )


def priority_for_available(model: ModelConfig) -> Priority:
    """Priorité d'une disponibilité « rapide » : appel seulement si le modèle l'autorise."""
    return Priority.HIGH if model.allow_call else Priority.MEDIUM

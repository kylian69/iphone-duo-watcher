"""Canaux de notification : Telegram (messages), CallMeBot (appel Telegram), Discord (webhook)."""

from __future__ import annotations

import html
import logging

import httpx

from ..config import Settings
from ..models import Event, Priority

log = logging.getLogger("notify")

ICONS = {
    Priority.HIGH: "🚨",
    Priority.MEDIUM: "🔔",
    Priority.TECH: "⚠️",
    Priority.LOW: "💤",
}


def _icon(event: Event) -> str:
    if event.kind == "recovery":
        return "✅"
    if event.kind == "startup":
        return "🚀"
    if event.kind == "test":
        return "🧪"
    return ICONS.get(event.priority, "ℹ️")


def _chunks(text: str, size: int) -> list[str]:
    out = []
    while len(text) > size:
        cut = text.rfind("\n", 0, size)
        cut = cut if cut > 0 else size
        out.append(text[:cut])
        text = text[cut:].lstrip("\n")
    out.append(text)
    return out


class ChannelError(Exception):
    pass


class TelegramChannel:
    name = "telegram"

    def __init__(self, s: Settings, client: httpx.AsyncClient):
        self.s, self.client = s, client

    @property
    def problem(self) -> str | None:
        if not self.s.telegram_enabled:
            return "désactivé"
        if not (self.s.telegram_bot_token and self.s.telegram_chat_id):
            return "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID manquant"
        return None

    def format(self, e: Event) -> str:
        text = f"{_icon(e)} <b>{html.escape(e.title)}</b>\n{html.escape(e.body)}"
        if e.url:
            text += f'\n\n👉 <a href="{html.escape(e.url, quote=True)}">{html.escape(e.url)}</a>'
        return text

    async def send(self, e: Event) -> None:
        url = f"https://api.telegram.org/bot{self.s.telegram_bot_token}/sendMessage"
        for part in _chunks(self.format(e), 4000):
            r = await self.client.post(
                url,
                json={
                    "chat_id": self.s.telegram_chat_id,
                    "text": part,
                    "parse_mode": "HTML",
                    "disable_notification": False,  # toujours sonore
                    "link_preview_options": {"is_disabled": True},
                },
            )
            if r.status_code != 200 or not r.json().get("ok"):
                raise ChannelError(f"Telegram HTTP {r.status_code}: {r.text[:200]}")


class DiscordChannel:
    name = "discord"

    def __init__(self, s: Settings, client: httpx.AsyncClient):
        self.s, self.client = s, client

    @property
    def problem(self) -> str | None:
        if not self.s.discord_enabled:
            return "désactivé"
        if not self.s.discord_webhook_url:
            return "DISCORD_WEBHOOK_URL manquant"
        if not self.s.discord_user_id:
            return "DISCORD_USER_ID manquant (la mention est obligatoire)"
        return None

    def format(self, e: Event) -> str:
        text = f"<@{self.s.discord_user_id}> {_icon(e)} **{e.title}**\n{e.body}"
        if e.url:
            text += f"\n👉 <{e.url}>"
        return text

    async def send(self, e: Event) -> None:
        for i, part in enumerate(_chunks(self.format(e), 1900)):
            if i:  # chaque morceau doit aussi mentionner l'utilisateur
                part = f"<@{self.s.discord_user_id}> {part}"
            r = await self.client.post(
                self.s.discord_webhook_url,
                json={"content": part, "allowed_mentions": {"users": [self.s.discord_user_id]}},
            )
            if r.status_code not in (200, 204):
                raise ChannelError(f"Discord HTTP {r.status_code}: {r.text[:200]}")


class CallMeBotChannel:
    """Appel vocal Telegram via CallMeBot (https://www.callmebot.com/telegram-call-api/)."""

    name = "callmebot"
    API = "https://api.callmebot.com/start.php"

    def __init__(self, s: Settings, client: httpx.AsyncClient):
        self.s, self.client = s, client

    @property
    def problem(self) -> str | None:
        if not self.s.callmebot_enabled:
            return "désactivé"
        if not self.s.callmebot_user:
            return "CALLMEBOT_TELEGRAM_USER manquant"
        return None

    async def call(self, text: str) -> None:
        user = self.s.callmebot_user
        if not user.startswith("@") and not user.lstrip("+").isdigit():
            user = "@" + user
        r = await self.client.get(
            self.API,
            params={
                "source": "web",
                "user": user,
                "text": text[:250],
                "lang": self.s.callmebot_lang,
                "rpt": str(self.s.callmebot_repeat),
            },
            timeout=60,
        )
        body = r.text.lower()
        if r.status_code != 200 or "error" in body or "not authorized" in body or "invalid" in body:
            raise ChannelError(f"CallMeBot HTTP {r.status_code}: {r.text[:300]}")

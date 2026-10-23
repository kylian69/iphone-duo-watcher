"""Routage des événements vers les canaux, limitation et regroupement des appels.

| Événement         | Appel | Telegram | Discord |
| priorité haute    |  oui  |   oui    |   oui   |
| tout le reste     |  non  |   oui    |   oui   |

L'échec d'un canal est loggé et n'empêche jamais les autres d'envoyer.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from ..config import Settings
from ..models import Event, Priority
from ..state import StateStore
from .channels import CallMeBotChannel, DiscordChannel, TelegramChannel

log = logging.getLogger("notify")


class Notifier:
    def __init__(self, settings: Settings, store: StateStore, client: httpx.AsyncClient | None = None,
                 group_window_s: float = 5.0):
        self.s = settings
        self.store = store
        self.client = client or httpx.AsyncClient(timeout=20)
        self.telegram = TelegramChannel(settings, self.client)
        self.discord = DiscordChannel(settings, self.client)
        self.callmebot = CallMeBotChannel(settings, self.client)
        self.group_window_s = group_window_s
        self._pending_calls: list[str] = []
        self._call_task: asyncio.Task | None = None
        self.sent: list[tuple[str, Event | str]] = []  # journal (tests / simulation)

    @property
    def message_channels(self):
        return [self.telegram, self.discord]

    def describe(self) -> list[str]:
        out = []
        for ch in (self.telegram, self.callmebot, self.discord):
            out.append(f"{ch.name}: {'OK' if ch.problem is None else ch.problem}")
        return out

    async def dispatch(self, event: Event) -> dict[str, str]:
        """Envoie un événement ; renvoie le résultat par canal."""
        results: dict[str, str] = {}
        if event.priority is Priority.LOW:  # démarrage / heartbeat : le tableau de bord est loggé à part
            log.info("[%s] %s", event.priority.name, event.title)
        else:
            log.info("[%s] %s — %s", event.priority.name, event.title, event.body.replace("\n", " | "))

        async def _send(ch):
            if ch.problem:
                results[ch.name] = f"ignoré ({ch.problem})"
                return
            try:
                await ch.send(event)
                results[ch.name] = "envoyé"
                self.sent.append((ch.name, event))
            except Exception as exc:  # un canal ne bloque jamais les autres
                results[ch.name] = f"échec: {exc}"
                log.error("Échec d'envoi %s: %s", ch.name, exc)

        await asyncio.gather(*(_send(ch) for ch in self.message_channels))

        if event.priority is Priority.HIGH and event.call_text:
            results["callmebot"] = self.queue_call(event.call_text)
        return results

    # --- appels -------------------------------------------------------------
    def queue_call(self, text: str) -> str:
        if self.callmebot.problem:
            return f"ignoré ({self.callmebot.problem})"
        self._pending_calls.append(text)
        if self._call_task is None or self._call_task.done():
            self._call_task = asyncio.create_task(self._flush_calls())
        return "programmé"

    def _cooldown_left(self) -> float:
        last = float(self.store.get_meta("last_call_ts") or 0)
        return max(0.0, last + self.s.callmebot_min_interval_min * 60 - time.time())

    async def _flush_calls(self) -> None:
        while self._pending_calls:
            # Laisse le temps aux disponibilités simultanées d'arriver → un seul appel groupé
            await asyncio.sleep(self.group_window_s)
            wait = self._cooldown_left()
            if wait > 0:
                log.info("Appel différé de %.0f s (limite CallMeBot)", wait)
                await asyncio.sleep(wait)
            texts, self._pending_calls = self._pending_calls, []
            text = self.group_call_text(texts)
            self.store.set_meta("last_call_ts", str(time.time()))
            try:
                await self.callmebot.call(text)
                self.sent.append(("callmebot", text))
                log.info("Appel Telegram lancé : %s", text)
            except Exception as exc:
                log.error("Échec de l'appel CallMeBot : %s", exc)

    @staticmethod
    def group_call_text(texts: list[str]) -> str:
        uniq = list(dict.fromkeys(texts))
        if len(uniq) == 1:
            return uniq[0]
        return f"{len(uniq)} alertes iPhone Duo. " + ". ".join(uniq)

    async def wait_calls(self) -> None:
        if self._call_task:
            await self._call_task

    async def test_all(self) -> dict[str, str]:
        """Commande de test : un message sur chaque canal + un appel immédiat."""
        ev = Event(Priority.MEDIUM, "Test des notifications",
                   "Si vous lisez ceci, le canal fonctionne. Un appel Telegram de test suit.",
                   kind="test")
        results = await self.dispatch(ev)
        if self.callmebot.problem:
            results["callmebot"] = f"ignoré ({self.callmebot.problem})"
        else:
            try:
                await self.callmebot.call("Test de la surveillance iPhone Duo. Les appels fonctionnent.")
                self.store.set_meta("last_call_ts", str(time.time()))
                results["callmebot"] = "appel lancé"
            except Exception as exc:
                results["callmebot"] = f"échec: {exc}"
        return results

    async def aclose(self) -> None:
        if self._call_task and not self._call_task.done():
            try:
                await asyncio.wait_for(self._call_task, timeout=30)
            except asyncio.TimeoutError:
                self._call_task.cancel()
        await self.client.aclose()

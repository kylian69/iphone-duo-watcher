"""Planificateur : une boucle indépendante par connecteur, santé, alertes techniques,
heartbeat et tableau de bord dans les logs."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

import httpx

from .config import AppConfig, Settings
from .connectors import REGISTRY, BlockedError, Connector, FormatError
from .events import diff, fmt_price, site_label
from .models import Event, ItemState, Priority
from .notify import Notifier
from .state import Health, StateStore

log = logging.getLogger("service")

STATE_ICON = {"OK": "✅", "DÉGRADÉ": "⚠️", "DÉSACTIVÉ": "⏸", "EN ATTENTE": "⏳"}


def _hm(ts: str | None) -> str:
    if not ts:
        return "jamais"
    try:
        return datetime.fromisoformat(ts).strftime("%d/%m %H:%M:%S")
    except ValueError:
        return ts


def format_items(items: list[ItemState]) -> str:
    lines = []
    for it in sorted(items, key=lambda i: (i.model_id, i.channel.value, i.store or "")):
        where = it.store or it.channel.value
        delay = f"{it.delay_days} j" if it.delay_days is not None else "-"
        price = f" {fmt_price(it.price)}" if it.price else ""
        plan = f" / forfait {fmt_price(it.price_with_plan)}" if it.price_with_plan else ""
        lines.append(f"  {it.model_id:<16} {where:<24} {it.status.value:<13} délai {delay:<5}"
                     f" {it.date_text or ''}{price}{plan}")
    return "\n".join(lines)


class Service:
    def __init__(self, settings: Settings, app: AppConfig, store: StateStore, notifier: Notifier,
                 client: httpx.AsyncClient | None = None):
        self.s, self.app, self.store, self.notifier = settings, app, store, notifier
        self.client = client or httpx.AsyncClient(timeout=30, http2=False)
        self.connectors: list[Connector] = []
        self.disabled: dict[str, str] = {}
        for name, cfg in app.connectors.items():
            cls = REGISTRY.get(name)
            if cls is None:
                log.warning("Connecteur inconnu dans config.yaml : %s (ignoré)", name)
                continue
            conn = cls(cfg, app, settings, self.client)
            if hasattr(conn, "store"):
                conn.store = store
            reason = "désactivé dans config.yaml" if not cfg.enabled else conn.disabled_reason()
            if reason:
                self.disabled[name] = reason
                self.store.put_health(Health(name, "DÉSACTIVÉ", last_error=reason))
            else:
                self.connectors.append(conn)

    # --- une vérification -----------------------------------------------------
    async def run_once(self, conn: Connector) -> float:
        """Exécute une vérification ; renvoie le délai avant la suivante (secondes)."""
        h = self.store.get_health(conn.name)
        try:
            items = await asyncio.wait_for(conn.check(), timeout=float(conn.opts.get("timeout", 120)))
        except Exception as exc:  # une erreur ne sort jamais du connecteur
            return await self._on_failure(conn, h, exc)

        operator = bool(getattr(conn, "is_operator", False))
        for item in items:
            old = self.store.get_item(item.key)
            ev = diff(old, item, self.app, operator)
            self.store.put_item(item)
            if ev:
                await self.notifier.dispatch(ev)
        for ev in conn.extra_events():
            await self.notifier.dispatch(ev)

        if self.s.debug:
            log.debug("[%s] état complet :\n%s", conn.name, format_items(items) or "  (aucun modèle)")
        else:
            log.info("[%s] vérification OK (%d entrées)", conn.name, len(items))

        recovered = h.incident_open
        h.state, h.consecutive_failures, h.incident_open = "OK", 0, False
        h.last_success, h.last_error = datetime.now().isoformat(timespec="seconds"), None
        self.store.put_health(h)
        if recovered:
            await self.notifier.dispatch(Event(
                Priority.TECH, f"Rétablissement : {site_label(conn.name)}",
                "Le connecteur fonctionne de nouveau normalement.", kind="recovery"))
        return conn.next_delay()

    async def _on_failure(self, conn: Connector, h: Health, exc: Exception) -> float:
        blocked = isinstance(exc, BlockedError)
        kind = "bloqué" if blocked else ("format non reconnu" if isinstance(exc, FormatError) else "erreur")
        msg = f"{type(exc).__name__}: {exc}" if not str(exc) or kind == "erreur" else str(exc)
        h.consecutive_failures += 1
        h.last_error = f"{kind} — {msg}"
        log.warning("[%s] échec %d (%s) : %s", conn.name, h.consecutive_failures, kind, msg)

        base = conn.next_delay()
        cap = self.app.max_backoff_blocked_s if blocked else self.app.max_backoff_s
        delay = min(base * (2 ** (h.consecutive_failures - (0 if blocked else 1))), max(cap, base))

        if h.consecutive_failures >= self.app.failure_threshold:
            h.state = "DÉGRADÉ"
            if not h.incident_open:
                h.incident_open = True
                self.store.put_health(h)
                await self.notifier.dispatch(Event(
                    Priority.TECH, f"Alerte technique : {site_label(conn.name)} ({kind})",
                    f"{h.consecutive_failures} échecs consécutifs.\nDernière erreur : {msg}\n"
                    f"Nouvelle tentative dans {delay / 60:.0f} min (attente progressive"
                    f"{' jusqu’à ' + str(int(cap / 3600)) + ' h' if blocked else ''}). "
                    "Aucun contournement n'est tenté.",
                    kind="tech"))
        self.store.put_health(h)
        return delay

    # --- boucles -------------------------------------------------------------
    async def loop(self, conn: Connector, first_delay: float = 0) -> None:
        await asyncio.sleep(first_delay)
        while True:
            try:
                delay = await self.run_once(conn)
            except Exception:  # filet de sécurité : la boucle ne meurt jamais
                log.exception("[%s] erreur inattendue", conn.name)
                delay = 300
            log.debug("[%s] prochaine vérification dans %.0f s", conn.name, delay)
            await asyncio.sleep(delay)

    async def heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self.s.heartbeat_hours * 3600)
            await self.notifier.dispatch(Event(Priority.LOW, "Toujours actif", self.dashboard(), kind="heartbeat"))
            log.info("Tableau de bord :\n%s", self.dashboard())

    async def run(self) -> None:
        log.info("Démarrage : %d connecteurs actifs, %d désactivés", len(self.connectors), len(self.disabled))
        for line in self.notifier.describe():
            log.info("Canal %s", line)
        # Premier passage en parallèle pour que la notification de démarrage contienne l'état réel
        delays = await asyncio.gather(*(self.run_once(c) for c in self.connectors))
        board = self.dashboard()
        log.info("Tableau de bord :\n%s", board)
        await self.notifier.dispatch(Event(Priority.LOW, "Surveillance iPhone Duo démarrée", board, kind="startup"))
        tasks = [asyncio.create_task(self.loop(c, d)) for c, d in zip(self.connectors, delays)]
        tasks.append(asyncio.create_task(self.heartbeat_loop()))
        await asyncio.gather(*tasks)

    # --- affichage ------------------------------------------------------------
    def dashboard(self) -> str:
        lines = ["Connecteurs :"]
        names = [c.name for c in self.connectors] + list(self.disabled)
        for name in names:
            h = self.store.get_health(name)
            state = h.state
            detail = self.disabled.get(name) or (h.last_error if state != "OK" else "")
            lines.append(f"{STATE_ICON.get(state, '•')} {site_label(name)} — {state} — "
                         f"dernière vérif. réussie : {_hm(h.last_success)}" + (f" — {detail}" if detail else ""))
        lines.append("")
        lines.append("Modèles prioritaires :")
        for m in [m for m in self.app.models if m.is_priority]:
            parts = []
            for it in self.store.items():
                if it.model_id != m.id:
                    continue
                where = it.store.replace("Apple ", "") if it.store else site_label(it.connector)
                d = f" {it.delay_days} j" if it.delay_days is not None else ""
                parts.append(f"{where} : {it.status.value}{d}")
            lines.append(f"• {m.short} : " + ("; ".join(parts) if parts else "aucune donnée"))
        return "\n".join(lines)

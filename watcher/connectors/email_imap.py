"""Alertes « retour en stock » reçues par email (Fnac, Darty, Cdiscount, Boulanger, Keepa…).

Lecture IMAP de la boîte dédiée. Un email d'alerte reconnu (expéditeur + mot-clé) :
  - déclenche une alerte urgente (appel + messages) si le modèle est prioritaire
    ou non identifié (par prudence) ;
  - est marqué comme lu puis déplacé dans le dossier « traités ».
Chaque Message-ID est mémorisé : un email n'est jamais traité deux fois.
Connecteur désactivé automatiquement tant que les identifiants IMAP sont vides.
"""

from __future__ import annotations

import asyncio
import base64
import email
import email.policy
import imaplib
import re
from email.message import EmailMessage

from ..config import ModelConfig, priority_for_available
from ..models import Event, ItemState, Priority
from .base import Connector, ConnectorError, register
from .retail import visible_text

URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.I)
URL_EXCLUDE = re.compile(r"unsubscribe|desinscri|désinscri|desabonn|preferences|privacy|confidentialit|"
                         r"\.(?:png|jpe?g|gif)(?:\?|$)|/open\?|pixel|mailto:", re.I)


def imap_utf7(name: str) -> str:
    """Encodage « modified UTF-7 » des noms de dossiers IMAP (RFC 3501) : Traités → Trait&AOk-s."""
    out, buf = [], ""

    def flush():
        nonlocal buf
        if buf:
            b64 = base64.b64encode(buf.encode("utf-16-be")).decode().rstrip("=").replace("/", ",")
            out.append(f"&{b64}-")
            buf = ""

    for ch in name:
        if 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            buf += ch
    flush()
    return "".join(out)


def _quote(folder: str) -> str:
    return '"' + imap_utf7(folder).replace("\\", "\\\\").replace('"', '\\"') + '"'


@register("email")
class EmailConnector(Connector):
    label = "Emails d'alerte"
    kind = "email"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._events: list[Event] = []
        self.store = None  # injecté par le planificateur (déduplication des Message-ID)

    def disabled_reason(self) -> str | None:
        if self.opts.get("fixture"):
            return None
        if not self.settings.imap_configured:
            return "identifiants IMAP absents (.env)"
        return None

    def extra_events(self) -> list[Event]:
        ev, self._events = self._events, []
        return ev

    async def check(self) -> list[ItemState]:
        fx = self.opts.get("fixture")
        if fx:
            with open(fx, "rb") as f:
                msg = email.message_from_bytes(f.read(), policy=email.policy.default)
            self._handle(msg)
            return []
        try:
            await asyncio.to_thread(self._poll_imap)
        except (imaplib.IMAP4.error, OSError) as exc:
            raise ConnectorError(f"IMAP : {exc}") from exc
        return []

    # --- IMAP ---------------------------------------------------------------
    def _poll_imap(self) -> None:
        s = self.settings
        with imaplib.IMAP4_SSL(s.imap_host, s.imap_port, timeout=60) as imap:
            imap.login(s.imap_user, s.imap_password)
            typ, _ = imap.select(_quote(s.imap_folder))
            if typ != "OK":
                raise ConnectorError(f"Dossier IMAP introuvable : {s.imap_folder}")
            self._ensure_folder(imap, s.imap_processed_folder)
            typ, data = imap.uid("SEARCH", None, "UNSEEN")
            uids = data[0].split() if typ == "OK" and data and data[0] else []
            for uid in uids:
                typ, parts = imap.uid("FETCH", uid, "(BODY.PEEK[])")
                raw = next((p[1] for p in parts if isinstance(p, tuple)), None)
                if not raw:
                    continue
                msg = email.message_from_bytes(raw, policy=email.policy.default)
                if self._handle(msg):
                    imap.uid("STORE", uid, "+FLAGS", r"(\Seen)")
                    self._move(imap, uid, s.imap_processed_folder)

    def _ensure_folder(self, imap: imaplib.IMAP4, folder: str) -> None:
        typ, data = imap.list('""', _quote(folder))
        if typ != "OK" or not data or data == [None]:
            imap.create(_quote(folder))

    def _move(self, imap: imaplib.IMAP4, uid: bytes, folder: str) -> None:
        caps = {c.decode() if isinstance(c, bytes) else c for c in imap.capabilities}
        if "MOVE" in caps:
            imap.uid("MOVE", uid, _quote(folder))
            return
        typ, _ = imap.uid("COPY", uid, _quote(folder))
        if typ == "OK":
            imap.uid("STORE", uid, "+FLAGS", r"(\Deleted)")
            if "UIDPLUS" in caps:
                imap.uid("EXPUNGE", uid)
            else:
                imap.expunge()

    # --- analyse ------------------------------------------------------------
    def _handle(self, msg: EmailMessage) -> bool:
        """Renvoie True si l'email est une alerte (à déplacer dans « traités »)."""
        mid = str(msg.get("Message-ID") or f"{msg.get('From')}|{msg.get('Date')}|{msg.get('Subject')}").strip()
        seen = self.store.email_result(mid) if self.store else None
        if seen is not None:  # déjà traité : on ne renotifie pas, on range seulement
            return seen == "alert"
        sender = str(msg.get("From") or "").lower()
        subject = str(msg.get("Subject") or "")
        body_html, body_text = self._bodies(msg)
        text = f"{subject}\n{body_text or visible_text(body_html)}"

        rule = self.match_rule(sender, text)
        if rule is None:
            if self.store:
                self.store.mark_email(mid, "ignored")
            self.log.debug("Email ignoré : %s — %s", sender, subject)
            return False

        model = self.identify_model(text)
        link = self.extract_link(body_html + "\n" + body_text, rule)
        self._events.append(self.build_event(rule, model, subject, link))
        if self.store:
            self.store.mark_email(mid, "alert")
        return True

    @staticmethod
    def _bodies(msg: EmailMessage) -> tuple[str, str]:
        html_part = msg.get_body(preferencelist=("html",))
        text_part = msg.get_body(preferencelist=("plain",))
        h = html_part.get_content() if html_part else ""
        t = text_part.get_content() if text_part else ""
        return h, t

    def match_rule(self, sender: str, text: str) -> dict | None:
        low = text.lower()
        for rule in self.opts.get("rules") or []:
            senders = [s.lower() for s in rule.get("from") or []]
            if senders and not any(s in sender for s in senders):
                continue
            kws = rule.get("keywords") or []
            if kws and not any(re.search(k, low, re.I) for k in kws):
                continue
            if rule.get("require_product") and not re.search(rule["require_product"], text, re.I):
                continue
            return rule
        return None

    def identify_model(self, text: str) -> ModelConfig | None:
        both = [m for m in self.app.models
                if m.email_capacity and m.email_color and m.email_capacity.search(text) and m.email_color.search(text)]
        return both[0] if len(both) == 1 else None

    @staticmethod
    def extract_link(body: str, rule: dict) -> str | None:
        urls = [u.rstrip(").,;") for u in URL_RE.findall(body) if not URL_EXCLUDE.search(u)]
        prefer = [d.lower() for d in rule.get("link_domains") or []]
        for u in urls:
            if any(d in u.lower() for d in prefer):
                return u
        return urls[0] if urls else None

    def build_event(self, rule: dict, model: ModelConfig | None, subject: str, link: str | None) -> Event:
        site = rule.get("site", "email")
        if model is None:
            prio, what = Priority.HIGH, "modèle non identifié"
        else:
            prio, what = priority_for_available(model), model.name
        call = f"Alerte stock iPhone Duo chez {site}" + (f", {model.short}" if model else "")
        return Event(
            priority=prio, kind="urgent" if prio is Priority.HIGH else "change",
            title=f"Email d'alerte {site} : iPhone Duo de retour en stock",
            body=f"Site : {site}\nModèle : {what}\nObjet : {subject}",
            url=link, call_text=call,
        )

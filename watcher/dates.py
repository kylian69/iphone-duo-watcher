"""Extraction d'un délai (en jours) depuis les textes de livraison/retrait en français.

Le délai retenu est toujours le plus court annoncé (borne basse d'une plage) :
mieux vaut une alerte de trop qu'une disponibilité manquée.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, timedelta

MONTHS = {
    "janv": 1, "janvier": 1, "jan": 1,
    "fevr": 2, "fevrier": 2, "fev": 2, "feb": 2,
    "mars": 3, "mar": 3,
    "avr": 4, "avril": 4, "apr": 4,
    "mai": 5, "may": 5,
    "juin": 6, "jun": 6,
    "juil": 7, "juillet": 7, "jul": 7,
    "aout": 8, "aug": 8,
    "sept": 9, "septembre": 9, "sep": 9,
    "oct": 10, "octobre": 10,
    "nov": 11, "novembre": 11,
    "dec": 12, "decembre": 12,
}
_MONTH_RE = "|".join(sorted(MONTHS, key=len, reverse=True))


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return text.lower().replace("’", "'").replace("\xa0", " ").replace(" ", " ")


def _roll_year(d: date, today: date) -> date:
    # « 12/01 » vu en novembre = janvier de l'année suivante
    if d < today - timedelta(days=60):
        try:
            return d.replace(year=d.year + 1)
        except ValueError:
            return d
    return d


def parse_delay(text: str | None, today: date | None = None) -> int | None:
    """Renvoie le nombre de jours avant la date la plus proche annoncée, ou None."""
    if not text:
        return None
    today = today or date.today()
    t = _norm(text)
    candidates: list[int] = []

    if re.search(r"aujourd'?hui|today|dans l'heure", t):
        candidates.append(0)
    if re.search(r"apres[- ]demain", t):
        candidates.append(2)
    elif re.search(r"\bdemain\b|tomorrow", t):
        candidates.append(1)

    for m in re.finditer(r"(?:sous|en|dans)?\s*(\d{1,3})\s*(?:a|-|–|à)?\s*(\d{1,3})?\s*(h\b|heures?|jours?(?: ouvres)?|j\b|semaines?|sem\b|mois)", t):
        n = int(m.group(1))
        unit = m.group(3)
        if unit.startswith("h"):
            candidates.append(max(0, (n + 23) // 24))
        elif unit.startswith("j"):
            candidates.append(n)
        elif unit.startswith("sem"):
            candidates.append(n * 7)
        elif unit.startswith("mois"):
            candidates.append(n * 30)

    for m in re.finditer(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", t):
        day, month = int(m.group(1)), int(m.group(2))
        year = int(m.group(3)) if m.group(3) else today.year
        if year < 100:
            year += 2000
        try:
            d = date(year, month, day)
        except ValueError:
            continue
        if not m.group(3):
            d = _roll_year(d, today)
        candidates.append((d - today).days)

    for m in re.finditer(rf"\b(\d{{1,2}})(?:er)?\s+({_MONTH_RE})\.?\w*(?:\s+(\d{{4}}))?", t):
        day, month = int(m.group(1)), MONTHS[m.group(2)]
        year = int(m.group(3)) if m.group(3) else today.year
        try:
            d = date(year, month, day)
        except ValueError:
            continue
        if not m.group(3):
            d = _roll_year(d, today)
        candidates.append((d - today).days)

    if not candidates:
        # « Expédition : janvier » → début du mois
        m = re.search(rf"\b({_MONTH_RE})\w*\.?(?:\s+(\d{{4}}))?", t)
        if m and re.search(r"livr|exped|dispo|retrait|envoi", t):
            month = MONTHS[m.group(1)]
            year = int(m.group(2)) if m.group(2) else today.year
            d = _roll_year(date(year, month, 1), today)
            candidates.append(max(0, (d - today).days))

    candidates = [c for c in candidates if c >= 0]
    return min(candidates) if candidates else None


def parse_price(text: str | None) -> float | None:
    """« 2 339,00 € » → 2339.0"""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    t = str(text).replace("\xa0", " ").replace(" ", " ")
    m = re.search(r"(\d{1,3}(?:[ .]\d{3})+(?!\d)|\d+)(?:[,.](\d{1,2})(?!\d))?", t)
    if not m:
        return None
    whole = re.sub(r"[ .]", "", m.group(1))
    cents = m.group(2) or "0"
    try:
        return float(f"{whole}.{cents}")
    except ValueError:
        return None

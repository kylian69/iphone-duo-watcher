"""Comparaison ancien / nouvel état → événements à notifier (uniquement sur changement)."""

from __future__ import annotations

from .config import AppConfig, ModelConfig, priority_for_available
from .connectors import REGISTRY
from .models import Channel, Event, ItemState, Priority, Status


def site_label(connector: str) -> str:
    cls = REGISTRY.get(connector)
    return cls.label if cls else connector.capitalize()


def is_urgent(item: ItemState, app: AppConfig) -> bool:
    """Retrait disponible, ou livraison/précommande sous le seuil du connecteur."""
    if item.status not in (Status.AVAILABLE, Status.PREORDER):
        return False
    if item.channel is Channel.PICKUP:
        return item.status is Status.AVAILABLE
    if item.delay_days is None:
        return item.status is Status.AVAILABLE and app.urgent_if_delay_unknown
    return item.delay_days <= app.threshold_for(item.connector)


def fmt_price(v: float | None) -> str:
    return f"{v:,.2f} €".replace(",", " ").replace(".", ",") if v else "?"


def describe(item: ItemState, model: ModelConfig | None, operator: bool = False) -> str:
    lines = [f"Site : {site_label(item.connector)}"]
    if item.store:
        lines.append(f"Magasin : {item.store}")
    lines.append(f"Modèle : {model.name if model else item.model_id}")
    lines.append(f"État : {item.status.value} ({item.channel.value})")
    delay = f"{item.delay_days} j" if item.delay_days is not None else "non précisé"
    lines.append(f"Délai : {delay}" + (f" — « {item.date_text} »" if item.date_text else ""))
    if operator or item.price_with_plan:
        lines.append(f"Prix sans forfait : {fmt_price(item.price)}")
        lines.append(f"Prix avec forfait : {'à partir de ' + fmt_price(item.price_with_plan) if item.price_with_plan else '?'}")
    elif item.price:
        lines.append(f"Prix : {fmt_price(item.price)}")
    return "\n".join(lines)


def call_text(item: ItemState, model: ModelConfig | None) -> str:
    what = f"iPhone Duo {model.short}" if model else "iPhone Duo"
    if item.channel is Channel.PICKUP:
        store = (item.store or "").replace("Apple ", "")
        return f"{what} disponible en retrait à {store}"
    return f"{what} disponible chez {site_label(item.connector)}"


def diff(old: ItemState | None, new: ItemState, app: AppConfig, operator: bool = False) -> Event | None:
    model = app.model(new.model_id)
    if new.status is Status.UNKNOWN:
        return None  # pas assez d'information : on ne notifie pas
    new_urgent = is_urgent(new, app)
    old_urgent = old is not None and old.status is not Status.UNKNOWN and is_urgent(old, app)

    if old is None or old.status is Status.UNKNOWN:
        # Premier relevé : silencieux, sauf disponibilité urgente
        if not new_urgent:
            return None
    elif old.signature() == new.signature():
        return None

    body = describe(new, model, operator)
    if old is not None and old.status is not Status.UNKNOWN:
        prev = f"{old.status.value}" + (f", {old.delay_days} j" if old.delay_days is not None else "")
        body += f"\nAvant : {prev}"

    if new_urgent and not old_urgent and model is not None:
        prio = priority_for_available(model)
        where = f"en retrait à {new.store}" if new.channel is Channel.PICKUP else f"chez {site_label(new.connector)}"
        return Event(
            priority=prio, kind="urgent" if prio is Priority.HIGH else "change",
            title=f"{model.name} disponible {where}", body=body, url=new.url,
            call_text=call_text(new, model) if prio is Priority.HIGH else None,
        )

    name = model.name if model else new.model_id
    title = f"{site_label(new.connector)} : {name} → {new.status.value}"
    if new.store:
        title += f" ({new.store})"
    return Event(priority=Priority.MEDIUM, title=title, body=body, url=new.url)

"""Point d'entrée.

  python -m watcher run            lance la surveillance (défaut)
  python -m watcher test-notify    envoie un message sur chaque canal + un appel Telegram
  python -m watcher check-sites    une requête par site, affiche l'état extrait (aucune notification)
  python -m watcher probe-apple    diagnostic de l'endpoint Apple réel (format, références, magasins)
  python -m watcher simulate ...   rejoue des données simulées (réponse Apple, page, email)
  python -m watcher status         affiche le tableau de bord enregistré
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import logging
import sys
from pathlib import Path

from .config import ConnectorConfig, Settings, load_config
from .connectors import REGISTRY
from .notify import Notifier
from .service import Service, format_items
from .state import StateStore


def setup_logging(level: str) -> None:
    logging.basicConfig(
        stream=sys.stdout,
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


class DryRunNotifier(Notifier):
    """Affiche les notifications au lieu de les envoyer (simulation)."""

    async def dispatch(self, event):
        print(f"\n>>> [{event.priority.name}] {event.title}\n{event.body}"
              + (f"\nLien : {event.url}" if event.url else "")
              + (f"\nAPPEL : « {event.call_text} »" if event.call_text and event.priority.name == "HIGH" else ""))
        self.sent.append(("dry-run", event))
        return {"dry-run": "affiché"}


async def cmd_run(settings, app, store):
    notifier = Notifier(settings, store)
    try:
        await Service(settings, app, store, notifier).run()
    finally:
        await notifier.aclose()


async def cmd_test_notify(settings, store):
    notifier = Notifier(settings, store)
    try:
        results = await notifier.test_all()
    finally:
        await notifier.aclose()
    print("Résultat du test des notifications :")
    for ch in ("telegram", "discord", "callmebot"):
        print(f"  {ch:<10} {results.get(ch, 'ignoré')}")
    return 0 if any(v.startswith(("envoyé", "appel")) for v in results.values()) else 1


async def cmd_check_sites(settings, app, store, only):
    """Évalue la fiabilité réelle de chaque site depuis la machine courante."""
    import httpx

    async with httpx.AsyncClient(timeout=30) as client:
        for name, cfg in app.connectors.items():
            if only and name not in only:
                continue
            cls = REGISTRY.get(name)
            if not cls:
                continue
            conn = cls(cfg, app, settings, client)
            reason = conn.disabled_reason()
            print(f"\n=== {cls.label} ({name}) ===")
            if conn.kind == "email":
                print("  ignoré (lire la boîte déplacerait les emails ; utilisez `simulate email <fichier.eml>`)")
                continue
            if reason:
                print(f"  désactivé : {reason}")
                continue
            try:
                items = await conn.check()
            except Exception as exc:
                print(f"  ÉCHEC : {type(exc).__name__}: {exc}")
                continue
            if not items:
                print("  aucun modèle configuré (URL produit manquantes ?)")
            print(format_items(items))
            for ev in conn.extra_events():
                print(f"  ⚠️ {ev.title} — {ev.body.splitlines()[0]}")


async def cmd_probe_apple(settings, app):
    """Une requête sur chaque endpoint Apple ; vérifie format, références et magasins."""
    import json

    import httpx

    from .connectors.apple import FALLBACK_URL, PRIMARY_URL

    cfg = app.connectors.get("apple") or ConnectorConfig("apple", True, (120, 180), 0, {})
    out_dir = Path(settings.data_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ok = True
    async with httpx.AsyncClient(timeout=30) as client:
        conn = REGISTRY["apple"](cfg, app, settings, client)
        models = conn.tracked_models()
        for label, url in (("principal", PRIMARY_URL), ("secours", FALLBACK_URL)):
            print(f"\n=== Endpoint {label} : {url}")
            seen = set()
            for i in range(0, len(models), 4):
                group = models[i : i + 4]
                if i:
                    await asyncio.sleep(5)  # requêtes espacées
                try:
                    raw = await conn._request(group, url)
                except Exception as exc:
                    print(f"  ÉCHEC requête : {type(exc).__name__}: {exc}")
                    ok = ok and label != "principal"
                    break
                dump = out_dir / f"apple_probe_{label}_{i // 4}.json"
                dump.write_text(raw, encoding="utf-8")
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    print(f"  Réponse non JSON (enregistrée dans {dump})")
                    ok = ok and label != "principal"
                    break
                body = data.get("body", data)
                content = body.get("content", body) if isinstance(body, dict) else {}
                print(f"  Réponse enregistrée : {dump}")
                print(f"  Clés : body.{'content.' if 'content' in body else ''}"
                      f"{{{', '.join(list(content)[:10])}}}")
                try:
                    items = conn.parse(data, group)
                except Exception as exc:
                    print(f"  ❌ format non reconnu : {exc}")
                    ok = ok and label != "principal"
                    break
                stores = sorted({it.store for it in items if it.store})
                print(f"  Magasins : {', '.join(stores) or 'aucun'}")
                print(format_items(items))
                seen |= {it.model_id for it in items}
            if not seen:  # aucune réponse exploitable : impossible de juger les références
                continue
            for m in models:
                flag = "✅" if m.id in seen else "❌ absente de la réponse (référence fausse ?)"
                print(f"  {m.apple_part:<10} {m.short:<24} {flag}")
                if label == "principal" and m.id not in seen:
                    ok = False
            await asyncio.sleep(5)
    print("\nRésultat :", "✅ connecteur Apple opérationnel" if ok else "❌ à corriger (voir ci-dessus)")
    return 0 if ok else 1


async def cmd_simulate(settings, app, args):
    """Rejoue une donnée simulée avec un état isolé (data/simulation.db par défaut)."""
    name = args.connector
    if name not in REGISTRY:
        print(f"Connecteur inconnu : {name}. Disponibles : {', '.join(sorted(REGISTRY))}")
        return 2
    app = copy.deepcopy(app)
    base = app.connectors.get(name) or ConnectorConfig(name, True, (60, 60), 0, {})
    opts = dict(base.options)
    if args.model:
        opts["fixtures"] = {args.model: args.fixture}
        opts.pop("fixture", None)
        for m in app.models:  # seul le modèle visé est évalué
            if m.id == args.model:
                m.urls.setdefault(name, f"https://exemple.invalid/{name}/{m.id}")
            else:
                m.urls.pop(name, None)
    else:
        opts["fixture"] = args.fixture
    app.connectors = {name: ConnectorConfig(name, True, base.interval, 0, opts)}
    store = StateStore(args.db)
    notifier = Notifier(settings, store) if args.send else DryRunNotifier(settings, store)
    svc = Service(settings, app, store, notifier)
    if not svc.connectors:
        print(f"Connecteur {name} désactivé : {svc.disabled.get(name)}")
        return 1
    await svc.run_once(svc.connectors[0])
    await notifier.wait_calls()
    await notifier.aclose()
    if not notifier.sent:
        print("\n(aucune notification : pas de changement par rapport à l'état enregistré)")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="watcher", description="Surveillance de stock iPhone Duo")
    p.add_argument("--env", default=None, help="fichier .env (défaut : ./.env)")
    p.add_argument("--config", default=None, help="config.yaml (défaut : $CONFIG_PATH)")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("run")
    sub.add_parser("test-notify")
    sub.add_parser("status")
    sub.add_parser("probe-apple")
    cs = sub.add_parser("check-sites")
    cs.add_argument("connectors", nargs="*")
    sim = sub.add_parser("simulate")
    sim.add_argument("connector")
    sim.add_argument("fixture")
    sim.add_argument("--model", help="modèle visé (connecteurs revendeurs)")
    sim.add_argument("--db", default=None, help="base d'état de simulation")
    sim.add_argument("--send", action="store_true", help="envoyer réellement les notifications")
    args = p.parse_args(argv)

    settings = Settings.from_env(args.env)
    setup_logging(settings.log_level)
    cfg_path = args.config or settings.config_path
    if not Path(cfg_path).exists() and Path("config.yaml").exists():
        cfg_path = "config.yaml"
    app = load_config(cfg_path)
    data = Path(settings.data_dir)
    cmd = args.cmd or "run"

    if cmd == "probe-apple":
        return asyncio.run(cmd_probe_apple(settings, app))
    if cmd == "simulate":
        args.db = args.db or str(data / "simulation.db")
        return asyncio.run(cmd_simulate(settings, app, args))
    store = StateStore(data / "state.db")
    if cmd == "test-notify":
        return asyncio.run(cmd_test_notify(settings, store))
    if cmd == "check-sites":
        asyncio.run(cmd_check_sites(settings, app, store, args.connectors))
        return 0
    if cmd == "status":
        svc = Service(settings, app, store, Notifier(settings, store))
        print(svc.dashboard())
        return 0
    try:
        asyncio.run(cmd_run(settings, app, store))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

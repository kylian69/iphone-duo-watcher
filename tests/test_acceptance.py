"""Critères d'acceptation du cahier des charges, sur données simulées."""

from watcher.models import Priority
from watcher.notify import Notifier

from .conftest import FIX, make_settings, run


def _sent_priorities(svc):
    return [ev.priority for ch, ev in svc.notifier.sent if ch == "telegram"]


async def _once(svc, name):
    conn = next(c for c in svc.connectors if c.name == name)
    await svc.run_once(conn)
    await svc.notifier.wait_calls()


def test_apple_pickup_urgent_once(env):
    svc, apis, store = env({"apple": {"fixture": str(FIX / "apple_pickup_available.json")}})
    run(_once(svc, "apple"))
    assert _sent_priorities(svc) == [Priority.HIGH]
    assert len(apis.by("telegram")) == 1 and len(apis.by("discord")) == 1 and len(apis.by("callmebot")) == 1
    call = apis.by("callmebot")[0].url.params["text"]
    assert "256 Go Ciel nocturne" in call and "Part-Dieu" in call
    assert "<@999>" in apis.bodies("discord")[0]["content"]
    # même état → aucune nouvelle alerte
    run(_once(svc, "apple"))
    assert len(apis.by("telegram")) == 1 and len(apis.by("callmebot")) == 1


def test_apple_unavailable_is_silent_then_change_is_urgent(env):
    svc, apis, store = env({"apple": {"fixture": str(FIX / "apple_unavailable.json")}})
    run(_once(svc, "apple"))
    assert apis.requests == []
    svc.connectors[0].opts["fixture"] = str(FIX / "apple_pickup_available.json")
    run(_once(svc, "apple"))
    assert _sent_priorities(svc) == [Priority.HIGH]
    # retour en rupture → message simple, pas d'appel
    svc.connectors[0].opts["fixture"] = str(FIX / "apple_unavailable.json")
    run(_once(svc, "apple"))
    assert _sent_priorities(svc) == [Priority.HIGH, Priority.MEDIUM]
    assert len(apis.by("callmebot")) == 1


def _only(site, ids, url="https://exemple.invalid"):
    """Ne garde l'URL `site` que pour les modèles `ids`."""

    def patch(app):
        for m in app.models:
            if m.id in ids:
                m.urls[site] = url
            else:
                m.urls.pop(site, None)

    return patch


def test_retailer_priority_vs_secondary(env):
    page = str(FIX / "boulanger_available.html")
    svc, apis, _ = env({"boulanger": {"fixtures": {"duo-256-ciel": page, "duo-2to-blanc": page}}},
                       models_patch=_only("boulanger", ("duo-256-ciel", "duo-2to-blanc")))
    # même fiche simulée pour les deux : on vérifie la règle de priorité par modèle
    run(_once(svc, "boulanger"))
    prios = sorted(_sent_priorities(svc))
    assert prios == [Priority.MEDIUM, Priority.HIGH]
    assert len(apis.by("callmebot")) == 1  # un seul appel, pour le prioritaire
    titles = [ev.title for ch, ev in svc.notifier.sent if ch == "telegram"]
    assert any("256 Go Ciel nocturne" in t and "Boulanger" in t for t in titles)


def test_one_broken_url_does_not_hide_other_models(env):
    svc, apis, store = env({"boulanger": {"fixtures": {"duo-256-ciel": str(FIX / "boulanger_available.html")}}},
                           models_patch=_only("boulanger", ("duo-256-ciel", "duo-256-blanc")))
    run(_once(svc, "boulanger"))  # duo-256-blanc → HTTP 404 simulé
    assert [i.model_id for i in store.items("boulanger")] == ["duo-256-ciel"]
    assert store.get_health("boulanger").state == "OK"


def test_operator_prices(env):
    svc, apis, store = env({"orange": {"fixtures": {"duo-256-ciel": str(FIX / "orange_available.html")}}},
                           models_patch=_only("orange", ("duo-256-ciel",)))
    run(_once(svc, "orange"))
    item = store.items("orange")[0]
    assert item.price == 2339.0 and item.price_with_plan == 1469.0 and item.delay_days == 2
    body = apis.bodies("telegram")[0]["text"]
    assert "sans forfait" in body and "avec forfait" in body


def test_email_alert_processed_once(env):
    svc, apis, store = env({"email": {"fixture": str(FIX / "email_fnac_alert.eml"),
                                       "rules": [{"site": "Fnac", "from": ["fnac.com"],
                                                  "keywords": ["disponible", "de retour"],
                                                  "require_product": "iphone\\s*duo", "link_domains": ["fnac.com"]}]}})
    run(_once(svc, "email"))
    run(_once(svc, "email"))
    assert _sent_priorities(svc) == [Priority.HIGH]
    assert len(apis.by("callmebot")) == 1
    ev = svc.notifier.sent[0][1]
    assert "256 Go Ciel nocturne" in ev.body and ev.url.startswith("https://www.fnac.com/a21000000")


def test_email_unknown_model_is_urgent_and_newsletter_ignored(env):
    rules = [{"site": "Amazon (Keepa)", "from": ["keepa.com"], "keywords": ["in stock"],
              "require_product": "iphone\\s*duo", "link_domains": ["amazon.fr"]},
             {"site": "Fnac", "from": ["fnac.com"], "keywords": ["disponible"], "require_product": "iphone\\s*duo"}]
    svc, apis, _ = env({"email": {"fixture": str(FIX / "email_newsletter.eml"), "rules": rules}})
    run(_once(svc, "email"))
    assert apis.requests == []
    svc.connectors[0].opts["fixture"] = str(FIX / "email_keepa_unknown.eml")
    run(_once(svc, "email"))
    assert _sent_priorities(svc) == [Priority.HIGH]
    ev = svc.notifier.sent[0][1]
    assert "non identifié" in ev.body and ev.url == "https://www.amazon.fr/dp/B0EXAMPLE1"


def test_email_disabled_without_imap(env):
    svc, _, store = env({"email": {"rules": []}})
    assert svc.connectors == [] and "IMAP" in svc.disabled["email"]
    assert store.get_health("email").state == "DÉSACTIVÉ"


def test_broken_channel_does_not_block_others(env):
    svc, apis, _ = env({"apple": {"fixture": str(FIX / "apple_pickup_available.json")}}, fail=("telegram",))
    run(_once(svc, "apple"))
    assert len(apis.by("discord")) == 1 and len(apis.by("callmebot")) == 1


def test_callmebot_failure_does_not_block_messages(env):
    svc, apis, _ = env({"apple": {"fixture": str(FIX / "apple_pickup_available.json")}}, fail=("callmebot",))
    run(_once(svc, "apple"))
    assert len(apis.by("telegram")) == 1 and len(apis.by("discord")) == 1


def test_misconfigured_channel_skipped(env):
    svc, apis, _ = env({"apple": {"fixture": str(FIX / "apple_pickup_available.json")}},
                       settings=make_settings(discord_webhook_url=""))
    run(_once(svc, "apple"))
    assert apis.by("discord") == [] and len(apis.by("telegram")) == 1


def test_blocked_connector_single_alert_and_isolation(env):
    svc, apis, store = env({
        "fnac": {"fixtures": {"duo-256-ciel": str(FIX / "fnac_blocked.html")}},
        "apple": {"fixture": str(FIX / "apple_pickup_available.json")},
    }, models_patch=_only("fnac", ("duo-256-ciel",)))

    async def scenario():
        fnac = next(c for c in svc.connectors if c.name == "fnac")
        apple = next(c for c in svc.connectors if c.name == "apple")
        delays = [await svc.run_once(fnac) for _ in range(6)]
        await svc.run_once(apple)
        await svc.notifier.wait_calls()
        return delays

    delays = run(scenario())
    tech = [ev for ch, ev in svc.notifier.sent if ch == "telegram" and ev.kind == "tech"]
    assert len(tech) == 1 and "bloqué" in tech[0].title
    assert store.get_health("fnac").state == "DÉGRADÉ"
    assert delays[-1] > delays[0] * 4  # attente progressive
    assert any(ev.priority is Priority.HIGH for ch, ev in svc.notifier.sent)  # Apple non impacté

    # rétablissement : une seule alerte
    svc.connectors[0].opts["fixtures"] = {"duo-256-ciel": str(FIX / "boulanger_unavailable.html")}
    run(_once(svc, "fnac"))
    run(_once(svc, "fnac"))
    rec = [ev for ch, ev in svc.notifier.sent if ch == "telegram" and ev.kind == "recovery"]
    assert len(rec) == 1
    assert store.get_health("fnac").state == "OK"


def test_call_grouping_and_rate_limit(env):
    svc, apis, store = env({})
    n: Notifier = svc.notifier

    async def scenario():
        n.queue_call("iPhone Duo 256 Go disponible en retrait à Part-Dieu")
        n.queue_call("iPhone Duo 256 Go disponible chez Boulanger")
        await n.wait_calls()

    run(scenario())
    calls = apis.by("callmebot")
    assert len(calls) == 1
    assert "Part-Dieu" in calls[0].url.params["text"] and "Boulanger" in calls[0].url.params["text"]
    assert n._cooldown_left() > 250  # prochain appel différé (limite 5 min)


def test_test_command_hits_every_channel(env):
    svc, apis, _ = env({})
    results = run(svc.notifier.test_all())
    assert results["telegram"] == "envoyé" and results["discord"] == "envoyé" and results["callmebot"] == "appel lancé"
    d = apis.bodies("discord")[0]
    assert d["content"].startswith("<@999>") and d["allowed_mentions"] == {"users": ["999"]}
    assert apis.bodies("telegram")[0]["disable_notification"] is False


def test_startup_dashboard(env):
    svc, apis, _ = env({"apple": {"fixture": str(FIX / "apple_unavailable.json")}, "email": {"rules": []}})

    async def startup():
        for c in svc.connectors:
            await svc.run_once(c)
        return svc.dashboard()

    board = run(startup())
    assert "Apple Store — OK" in board and "Emails d'alerte — DÉSACTIVÉ" in board
    assert "256 Go Ciel nocturne" in board and "Part-Dieu : indisponible" in board

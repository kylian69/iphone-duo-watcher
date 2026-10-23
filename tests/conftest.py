import asyncio
import json
from pathlib import Path

import httpx
import pytest

from watcher.config import Settings, load_config
from watcher.notify import Notifier
from watcher.service import Service
from watcher.state import StateStore

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures"


class FakeApis:
    """Simule Telegram / Discord / CallMeBot ; `fail` = noms de canaux à faire échouer."""

    def __init__(self, fail=()):
        self.fail = set(fail)
        self.requests: list[tuple[str, httpx.Request]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        name = {"api.telegram.org": "telegram", "discord.com": "discord", "api.callmebot.com": "callmebot"}.get(host, host)
        self.requests.append((name, request))
        if name in self.fail:
            return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})
        if name == "telegram":
            return httpx.Response(200, json={"ok": True, "result": {}})
        if name == "discord":
            return httpx.Response(204)
        if name == "callmebot":
            return httpx.Response(200, text="<p>Call queued</p>")
        return httpx.Response(404)

    def by(self, name):
        return [r for n, r in self.requests if n == name]

    def bodies(self, name):
        return [json.loads(r.content) for r in self.by(name)]


def make_settings(**kw) -> Settings:
    base = dict(
        telegram_bot_token="123:abc", telegram_chat_id="42",
        callmebot_user="@moi", callmebot_min_interval_min=5,
        discord_webhook_url="https://discord.com/api/webhooks/1/x", discord_user_id="999",
        log_level="INFO",
    )
    base.update(kw)
    return Settings(**base)


@pytest.fixture
def env(tmp_path):
    """Fabrique (service, apis, store) à partir de config.yaml.example + surcharges."""

    def build(connectors: dict, fail=(), settings=None, models_patch=None):
        app = load_config(ROOT / "config.yaml.example")
        from watcher.config import ConnectorConfig

        app.connectors = {n: ConnectorConfig(n, True, (60, 60), 0, opts) for n, opts in connectors.items()}
        if models_patch:
            models_patch(app)
        store = StateStore(tmp_path / "state.db")
        apis = FakeApis(fail)
        client = httpx.AsyncClient(transport=httpx.MockTransport(apis.handler))
        s = settings or make_settings()
        notifier = Notifier(s, store, client=client, group_window_s=0)
        svc = Service(s, app, store, notifier, client=client)
        return svc, apis, store

    return build


def run(coro):
    return asyncio.run(coro)

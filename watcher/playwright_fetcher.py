"""Récupération via navigateur headless (optionnel, image Docker `playwright`).

Utilisé uniquement pour un site qui impose JavaScript pour afficher le stock
(`fetcher: playwright` dans config.yaml). Un seul navigateur, un seul onglet à la fois.
Aucune tentative de contournement anti-bot : la page est simplement rendue.
"""

from __future__ import annotations

import asyncio

from .connectors.base import BROWSER_HEADERS, BlockedError, ConnectorError

_lock = asyncio.Lock()
_browser = None
_pw = None


async def fetch_with_browser(url: str, wait_selector: str | None = None, timeout_ms: int = 45000) -> str:
    global _browser, _pw
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise ConnectorError(
            "Playwright non installé : utilisez l'image `playwright` (voir README) ou retirez `fetcher: playwright`"
        ) from exc

    async with _lock:
        if _browser is None:
            _pw = await async_playwright().start()
            _browser = await _pw.chromium.launch(headless=True)
        ctx = await _browser.new_context(user_agent=BROWSER_HEADERS["User-Agent"], locale="fr-FR")
        page = await ctx.new_page()
        try:
            resp = await page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
            if resp and resp.status in (401, 403, 429):
                raise BlockedError(f"HTTP {resp.status} sur {url}")
            if wait_selector:
                await page.wait_for_selector(wait_selector, timeout=timeout_ms)
            else:
                await page.wait_for_timeout(3000)
            return await page.content()
        finally:
            await ctx.close()

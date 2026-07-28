"""Page rendering and extraction.

Playwright renders the page in a real browser, which is what makes this work
against sites that build their galleries in JavaScript or sit behind bot
protection. One browser is launched per batch and reused across rows: a
fresh Chromium per URL costs seconds each time and throws away the
Cloudflare cookies earned on the previous row of the same supplier.

Extraction deliberately takes every image on the page. Junk filtering by
filename proved unreliable, so the sort order (biggest original first) and
the human on the review screen do the choosing instead.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urljoin

from playwright.async_api import BrowserContext, async_playwright
from selectolax.parser import HTMLParser

from .config import SETTINGS

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

LAZY_ATTRS = ("data-large_image", "data-src", "data-lazy-src",
              "data-original", "data-zoom-image", "src")

# Containers that usually hold the real product copy, most specific first.
DESCRIPTION_SELECTORS = (
    '[itemprop="description"]',
    "#tab-description",
    ".woocommerce-Tabs-panel--description",
    ".product-description",
    '[class*="product-desc"]',
    "#description",
    '[id*="description"]',
    '[class*="description"]',
    "article",
    "main",
)


class BrowserSession:
    """One Chromium for the whole batch; a fresh context per row."""

    def __init__(self) -> None:
        self._playwright = None
        self._browser = None

    async def start(self) -> None:
        self._playwright = await async_playwright().start()
        # channel="chromium" runs full Chromium in its new headless mode.
        # The default headless shell is the old headless build, which bot
        # protection (Akamai, Cloudflare) recognises immediately.
        launch_args = ["--disable-blink-features=AutomationControlled"]
        try:
            self._browser = await self._playwright.chromium.launch(
                headless=True, channel="chromium", args=launch_args)
        except Exception:
            self._browser = await self._playwright.chromium.launch(
                headless=True, args=launch_args)

    async def close(self) -> None:
        if self._browser is not None:
            await self._browser.close()
        if self._playwright is not None:
            await self._playwright.stop()
        self._browser = None
        self._playwright = None

    async def render(self, url: str) -> tuple[str, BrowserContext]:
        """Render the page and return its HTML plus the live context.

        The context is returned open on purpose: image downloads can then go
        through context.request with the page's own cookies, which is the
        fallback for CDNs that refuse a bare HTTP client. The caller closes it.
        """
        assert self._browser is not None, "BrowserSession not started"
        context = await self._browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1440, "height": 2400},
            locale="en-GB",
            extra_http_headers={"Accept-Language": "en-GB,en;q=0.9"},
        )
        # navigator.webdriver is the first thing bot detection reads.
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', "
            "{get: () => undefined})")
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded",
                            timeout=SETTINGS.request_timeout * 1000)
            # Lazy-loaded galleries need a scroll before they populate src.
            await page.evaluate(
                "window.scrollTo(0, document.body.scrollHeight)")
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass  # Some pages poll forever; the DOM is usually ready.
            html = await page.content()
        except Exception:
            await context.close()
            raise
        return html, context


def _largest_from_srcset(srcset: str, base: str) -> str | None:
    """Pick the highest resolution candidate from a srcset attribute."""
    best, best_width = None, -1
    for part in srcset.split(","):
        bits = part.strip().split()
        if not bits:
            continue
        candidate = urljoin(base, bits[0])
        width = 0
        if len(bits) > 1 and bits[1].endswith("w"):
            try:
                width = int(bits[1][:-1])
            except ValueError:
                width = 0
        if width > best_width:
            best, best_width = candidate, width
    return best


def _json_ld_products(tree: HTMLParser) -> list[dict]:
    blocks: list[dict] = []
    for node in tree.css('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.text())
        except (json.JSONDecodeError, TypeError):
            continue
        items = data if isinstance(data, list) else [data]
        if isinstance(data, dict) and "@graph" in data:
            items = data["@graph"]
        for item in items:
            if isinstance(item, dict) and item.get("@type") in ("Product", ["Product"]):
                blocks.append(item)
    return blocks


def extract_all_image_urls(html: str, base_url: str) -> list[tuple[str, str]]:
    """Every candidate image on the page as (url, alt) pairs.

    No name-based junk filtering: payment badges and logos are cheap to
    download and trivial to drop on the review screen, whereas a filter that
    guesses wrong silently loses a product shot.
    """
    tree = HTMLParser(html)
    seen: set[str] = set()
    results: list[tuple[str, str]] = []

    def add(url: str, alt: str = "") -> None:
        if not url or url.startswith("data:"):
            return
        clean = urljoin(base_url, url).split("#")[0]
        if clean in seen:
            return
        seen.add(clean)
        results.append((clean, (alt or "").strip()))

    for block in _json_ld_products(tree):
        raw = block.get("image") or []
        raw = [raw] if isinstance(raw, (str, dict)) else raw
        for item in raw:
            if isinstance(item, str):
                add(item)
            elif isinstance(item, dict) and item.get("url"):
                add(item["url"])

    og = tree.css_first('meta[property="og:image"]')
    if og:
        add(og.attributes.get("content") or "")

    for node in tree.css("img"):
        attrs = node.attributes
        src = ""
        if attrs.get("srcset"):
            src = _largest_from_srcset(attrs["srcset"], base_url) or ""
        if not src:
            for key in LAZY_ATTRS:
                if attrs.get(key):
                    src = attrs[key]
                    break
        add(src, attrs.get("alt") or "")

    # <a href> wrappers around gallery thumbnails often point at the
    # full-resolution original.
    for node in tree.css("a[href]"):
        href = node.attributes.get("href") or ""
        if re.search(r"\.(jpe?g|png|webp|avif)(\?|$)", href, re.I):
            add(href)

    return results


def extract_description(html: str) -> dict[str, str]:
    """Three description candidates, best-effort each.

    None of them is trusted alone: the review screen shows all three next to
    an editable draft, and the human decides.
    """
    tree = HTMLParser(html)

    json_ld = ""
    for block in _json_ld_products(tree):
        if block.get("description"):
            json_ld = str(block["description"]).strip()
            break

    meta = ""
    node = tree.css_first('meta[name="description"]')
    if node:
        meta = (node.attributes.get("content") or "").strip()
    if not meta:
        node = tree.css_first('meta[property="og:description"]')
        if node:
            meta = (node.attributes.get("content") or "").strip()

    main_block = ""
    for selector in DESCRIPTION_SELECTORS:
        for candidate in tree.css(selector):
            text = candidate.text(separator="\n", strip=True)
            # A hit above 20k characters is a page-level wrapper, not the
            # product copy; matching it would drag in menus and footers.
            if len(text) > 20000:
                continue
            if len(text) > len(main_block):
                main_block = text
        if len(main_block) > 200:
            break
    if len(main_block) < 200:
        paragraphs = [p.text(strip=True) for p in tree.css("p")]
        joined = "\n".join(p for p in paragraphs if len(p) > 60)
        if len(joined) > len(main_block):
            main_block = joined

    main_block = re.sub(r"\n{3,}", "\n\n", main_block)[:8000]

    return {"json_ld": json_ld, "meta": meta, "main_block": main_block}


def page_title(html: str) -> str:
    node = HTMLParser(html).css_first("title")
    return node.text(strip=True) if node else ""


def best_description(candidates: dict[str, str]) -> str:
    """The draft for the editor: the fullest candidate wins."""
    return max(
        (candidates.get("json_ld", ""), candidates.get("main_block", ""),
         candidates.get("meta", "")),
        key=len,
    )

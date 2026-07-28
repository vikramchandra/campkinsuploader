"""Image download and processing.

Every image is converted to WebP within a size ceiling before it touches
disk, so nothing heavy ever reaches the review screen or the site. The
original byte size is recorded first, because it is the sort key: the
biggest source file is usually the hero shot.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import re
import unicodedata
from dataclasses import dataclass, asdict
from pathlib import Path

import httpx
from PIL import Image

from .config import SETTINGS
from .scraper import USER_AGENT

Image.MAX_IMAGE_PIXELS = 120_000_000  # Guard against decompression bombs.

# Floors that drop tracking pixels and favicons without guessing from
# filenames: nothing this small is a product shot.
MIN_ORIG_BYTES = 10 * 1024
MIN_EDGE_PX = 200

DOWNLOAD_CONCURRENCY = 6


@dataclass
class Candidate:
    sha256: str
    path: str
    orig_bytes: int
    webp_bytes: int
    width: int
    height: int
    source_url: str
    source_alt: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def slugify(value: str) -> str:
    """Lowercase hyphenated slug, ASCII only, suitable for a filename."""
    value = unicodedata.normalize("NFKD", value)
    value = value.encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^\w\s-]", "", value).strip().lower()
    value = re.sub(r"[-\s_]+", "-", value)
    return value.strip("-") or "product"


def build_filename(product_name: str, suffix: str, position: int,
                   total: int) -> str:
    """{product-name-slug}-{user-string}-{NN}.webp, zero-padded."""
    width = max(2, len(str(total)))
    parts = [slugify(product_name)]
    if suffix.strip():
        parts.append(slugify(suffix))
    parts.append(str(position).zfill(width))
    return "-".join(parts) + ".webp"


def build_alt_text(product_name: str, position: int, source_alt: str = "") -> str:
    """Alt text describes the image; the product name anchors it.

    The supplier's own alt text is used as a descriptor when it says
    something beyond the product name, since 'front view' or 'with 24-105mm
    lens' is genuinely useful and better than a numbered repetition.
    """
    descriptor = (source_alt or "").strip()
    if descriptor and descriptor.lower() not in product_name.lower():
        descriptor = re.sub(r"\s+", " ", descriptor)[:60]
        return f"{product_name} - {descriptor}"
    return product_name if position == 1 else f"{product_name} - view {position}"


def process_bytes(raw: bytes, out_path: Path) -> tuple[int, int, int]:
    """Convert to WebP within the size ceiling. Returns width, height, bytes."""
    with Image.open(io.BytesIO(raw)) as img:
        img = img.convert("RGBA" if img.mode in ("RGBA", "LA", "P") else "RGB")
        if img.mode == "RGBA":
            # Flatten onto white; product shots on transparency upload badly.
            flat = Image.new("RGB", img.size, (255, 255, 255))
            flat.paste(img, mask=img.split()[-1])
            img = flat
        ceiling = SETTINGS.max_dimension
        if max(img.size) > ceiling:
            img.thumbnail((ceiling, ceiling), Image.LANCZOS)
        img.save(out_path, "WEBP", quality=SETTINGS.webp_quality, method=6)
        width, height = img.size
    return width, height, out_path.stat().st_size


async def _fetch(client: httpx.AsyncClient, context, url: str,
                 referer: str) -> bytes:
    """Plain HTTP first; the page's own browser context as fallback.

    Hotlink-protected and Cloudflare-gated CDNs refuse a bare client but
    accept a request carrying the cookies the page render just earned.
    """
    try:
        response = await client.get(
            url,
            headers={"User-Agent": USER_AGENT, "Referer": referer},
            follow_redirects=True,
            timeout=SETTINGS.request_timeout,
        )
        response.raise_for_status()
        return response.content
    except Exception:
        if context is None:
            raise
        response = await context.request.get(
            url, headers={"Referer": referer},
            timeout=SETTINGS.request_timeout * 1000)
        if not response.ok:
            raise RuntimeError(f"HTTP {response.status}")
        return await response.body()


async def collect_all(items: list[tuple[str, str]], referer: str,
                      images_dir: Path,
                      context=None) -> tuple[list[Candidate], list[dict]]:
    """Download every candidate, dedupe by content, convert, sort by size.

    Returns candidates sorted descending by original byte size, plus a list
    of failures with reasons so nothing is silently dropped.
    """
    images_dir.mkdir(parents=True, exist_ok=True)
    failures: list[dict] = []
    semaphore = asyncio.Semaphore(DOWNLOAD_CONCURRENCY)

    async with httpx.AsyncClient() as client:
        async def grab(url: str, alt: str):
            async with semaphore:
                try:
                    raw = await _fetch(client, context, url, referer)
                    return url, alt, raw, ""
                except Exception as exc:
                    return url, alt, b"", f"Download failed: {exc}"

        downloads = await asyncio.gather(
            *(grab(url, alt) for url, alt in items))

    results: list[Candidate] = []
    seen: set[str] = set()
    for url, alt, raw, error in downloads:
        if error:
            failures.append({"url": url, "reason": error})
            continue
        if len(raw) < MIN_ORIG_BYTES:
            continue  # Pixels, favicons; not worth reporting individually.

        digest = hashlib.sha256(raw).hexdigest()
        if digest in seen:
            continue  # Same file served from two paths.
        seen.add(digest)

        out_path = images_dir / f"{digest[:16]}.webp"
        try:
            width, height, webp_size = process_bytes(raw, out_path)
        except Exception as exc:
            failures.append({"url": url, "reason": f"Not a usable image: {exc}"})
            continue

        if width < MIN_EDGE_PX or height < MIN_EDGE_PX:
            out_path.unlink(missing_ok=True)
            continue

        results.append(Candidate(
            sha256=digest,
            path=str(out_path),
            orig_bytes=len(raw),
            webp_bytes=webp_size,
            width=width,
            height=height,
            source_url=url,
            source_alt=alt,
        ))

    # Biggest original first: the closest available proxy for "the shot the
    # supplier cared about". The review screen corrects the exceptions.
    results.sort(key=lambda c: c.orig_bytes, reverse=True)
    return results, failures

"""WordPress and WooCommerce API clients.

These are two different APIs with two different credentials, which is the
detail that trips most people up. Media goes to WordPress core using an
Application Password. Products go to WooCommerce using consumer keys. A
WooCommerce key will be rejected by the media endpoint.

None of this had been executed against the live site when it was written.
Assume the first real run finds problems here.
"""

from __future__ import annotations

import html
import mimetypes
import re
from pathlib import Path
from typing import Any

import httpx

from .config import SETTINGS


class ApiError(RuntimeError):
    """Raised with a message suitable for showing in the interface."""


def _explain(response: httpx.Response) -> str:
    try:
        body = response.json()
        detail = body.get("message") or body.get("code") or response.text[:300]
    except Exception:
        detail = response.text[:300]
    if response.status_code in (401, 403):
        return (f"Rejected by the site ({response.status_code}). Check the "
                f"credentials in Settings. Site said: {detail}")
    return f"Request failed ({response.status_code}). Site said: {detail}"


def upload_media(path: Path, filename: str, alt_text: str,
                 title: str) -> dict[str, Any]:
    """Upload one file to the media library and set its alt text.

    The filename the site stores comes from the Content-Disposition header,
    not from the local file, which is how the SEO filename gets applied.
    """
    mime = mimetypes.guess_type(filename)[0] or "image/webp"
    auth = (SETTINGS.wp_user, SETTINGS.wp_app_password)

    with httpx.Client(timeout=SETTINGS.request_timeout * 2) as client:
        response = client.post(
            SETTINGS.media_endpoint,
            auth=auth,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Type": mime,
            },
            content=path.read_bytes(),
        )
        if response.status_code not in (200, 201):
            raise ApiError(_explain(response))
        media = response.json()

        # Alt text has to be set in a second call; the upload ignores it.
        patch = client.post(
            f"{SETTINGS.media_endpoint}/{media['id']}",
            auth=auth,
            json={"alt_text": alt_text, "title": title},
        )
        if patch.status_code not in (200, 201):
            raise ApiError(_explain(patch))

    return {
        "id": media["id"],
        "source_url": media.get("source_url", ""),
        "slug": media.get("slug", ""),
    }


def find_by_sku(sku: str) -> dict[str, Any] | None:
    """Check whether a product with this SKU already exists.

    Uploading the same sheet twice is a matter of time; catching the
    duplicate here turns a raw 400 into a choice the user can make.
    """
    if not sku:
        return None
    with httpx.Client(timeout=SETTINGS.request_timeout) as client:
        response = client.get(
            SETTINGS.products_endpoint,
            params={"sku": sku, "status": "any"},
            auth=(SETTINGS.woo_key, SETTINGS.woo_secret),
        )
    if response.status_code != 200:
        raise ApiError(_explain(response))
    products = response.json()
    if not products:
        return None
    product = products[0]
    return {
        "id": product["id"],
        "name": product.get("name", ""),
        "status": product.get("status", ""),
        "permalink": product.get("permalink", ""),
        "edit_url": _edit_url(product["id"]),
    }


def _same_term(api_name: str, wanted: str) -> bool:
    # The API returns names HTML-encoded ("Scopes &amp; Monoculars"), so a
    # sheet value with a plain ampersand never matched before this decode.
    return html.unescape(api_name or "").strip().lower() == wanted.strip().lower()


def _search_term(client: httpx.Client, endpoint: str,
                 name: str) -> dict[str, Any] | None:
    """Find one term by exact name, or None.

    The search parameter is a substring match that punctuation breaks: a
    query containing "&" returns nothing. Searching on the longest word
    keeps the candidate list small without tripping on that. If that still
    finds nothing, every term is paged through, since a site has hundreds
    of categories at most.
    """
    auth = (SETTINGS.woo_key, SETTINGS.woo_secret)
    words = [w for w in re.split(r"[^\w]+", name) if w]
    query = max(words, key=len) if words else name
    response = client.get(endpoint, params={"search": query, "per_page": 100},
                          auth=auth)
    if response.status_code != 200:
        raise ApiError(_explain(response))
    for term in response.json():
        if _same_term(term.get("name", ""), name):
            return term

    page = 1
    while True:
        response = client.get(endpoint, params={"per_page": 100, "page": page},
                              auth=auth)
        if response.status_code != 200:
            raise ApiError(_explain(response))
        terms = response.json()
        for term in terms:
            if _same_term(term.get("name", ""), name):
                return term
        if len(terms) < 100:
            return None
        page += 1


def _create_term(client: httpx.Client, endpoint: str, name: str) -> int:
    response = client.post(endpoint, json={"name": name},
                           auth=(SETTINGS.woo_key, SETTINGS.woo_secret))
    if response.status_code in (200, 201):
        return int(response.json()["id"])
    # Two rows in one batch can race to create the same category; the
    # second attempt comes back as term_exists with the winner's ID.
    try:
        body = response.json()
    except Exception:
        body = {}
    if body.get("code") == "term_exists":
        existing = (body.get("data") or {}).get("resource_id")
        if existing:
            return int(existing)
    raise ApiError(_explain(response))


def _find_term_ids(client: httpx.Client, endpoint: str, names: list[str],
                   create: bool = False) -> tuple[list[int], list[str]]:
    """Match term names (categories, brands) to their IDs.

    The product API takes IDs only. With create=True a name that is not on
    the site is created there first, so the product lands in the category
    the sheet names rather than the site's default one. Without it the
    name is left off with a warning.
    """
    ids: list[int] = []
    warnings: list[str] = []
    for name in names:
        try:
            match = _search_term(client, endpoint, name)
        except ApiError as exc:
            warnings.append(f"Could not look up '{name}': {exc}")
            continue
        if match:
            ids.append(int(match["id"]))
            continue
        if not create:
            warnings.append(f"'{name}' does not exist on the site; left off "
                            "the product.")
            continue
        try:
            ids.append(_create_term(client, endpoint, name))
            warnings.append(f"Created the category '{name}' on the site.")
        except ApiError as exc:
            warnings.append(f"Could not create '{name}': {exc}")
    return ids, warnings


def create_product(name: str, slug: str, sku: str, regular_price: str,
                   description: str, media_ids: list[int],
                   meta_data: list[dict] | None = None,
                   status: str = "draft", ean: str = "", brand: str = "",
                   categories: list[str] | None = None, weight: str = "",
                   dimensions: dict[str, str] | None = None) -> dict[str, Any]:
    """Create the product. Draft by default, deliberately.

    The first media ID becomes the featured image and the rest form the
    gallery, which is the standard WooCommerce behaviour the theme reads.
    Thumbnail choice is therefore an ordering decision made by the caller.

    Weight and dimensions must arrive already converted to the store's
    units; the caller owns that (see the unit settings in config).
    """
    payload: dict[str, Any] = {
        "name": name,
        "slug": slug or None,
        "type": "simple",
        "status": status,
        "sku": sku or None,
        "regular_price": regular_price or None,
        "description": description,
        "images": [{"id": mid} for mid in media_ids],
        # EAN lands in the GTIN field WooCommerce 9.2 added. Older stores
        # ignore unknown fields, so this is safe to send blind.
        "global_unique_id": ean or None,
        "weight": weight or None,
        "dimensions": dimensions or None,
    }
    if meta_data:
        payload["meta_data"] = meta_data

    warnings: list[str] = []
    with httpx.Client(timeout=SETTINGS.request_timeout) as client:
        if categories:
            ids, category_warnings = _find_term_ids(
                client, f"{SETTINGS.products_endpoint}/categories", categories,
                create=True)
            warnings.extend(category_warnings)
            if ids:
                payload["categories"] = [{"id": i} for i in ids]
        if brand:
            # The brands endpoint only exists on WooCommerce 9.6+ (or with a
            # brands plugin); on anything else the lookup warns and moves on.
            ids, brand_warnings = _find_term_ids(
                client, f"{SETTINGS.products_endpoint}/brands", [brand])
            warnings.extend(brand_warnings)
            if ids:
                payload["brands"] = [{"id": i} for i in ids]

        payload = {k: v for k, v in payload.items() if v is not None}
        response = client.post(
            SETTINGS.products_endpoint,
            auth=(SETTINGS.woo_key, SETTINGS.woo_secret),
            json=payload,
        )
    if response.status_code not in (200, 201):
        raise ApiError(_explain(response))

    product = response.json()
    return {
        "id": product["id"],
        "permalink": product.get("permalink", ""),
        "edit_url": _edit_url(product["id"]),
        "warnings": warnings,
    }


def attach_to_product(product_id: int, media_ids: list[int]) -> dict[str, Any]:
    """Replace the gallery on an existing product."""
    with httpx.Client(timeout=SETTINGS.request_timeout) as client:
        response = client.put(
            f"{SETTINGS.products_endpoint}/{product_id}",
            auth=(SETTINGS.woo_key, SETTINGS.woo_secret),
            json={"images": [{"id": mid} for mid in media_ids]},
        )
    if response.status_code not in (200, 201):
        raise ApiError(_explain(response))
    product = response.json()
    return {"id": product["id"], "edit_url": _edit_url(product["id"])}


def check_connection() -> dict[str, Any]:
    """Verify both credential sets separately, so a failure names the one
    that is wrong."""
    result: dict[str, Any] = {"wordpress": False, "woocommerce": False,
                              "errors": []}
    if not SETTINGS.configured:
        result["errors"].append("Site credentials are incomplete. Fill in "
                                "Settings first.")
        return result

    with httpx.Client(timeout=20) as client:
        try:
            r = client.get(f"{SETTINGS.media_endpoint}?per_page=1",
                           auth=(SETTINGS.wp_user, SETTINGS.wp_app_password))
            result["wordpress"] = r.status_code == 200
            if r.status_code != 200:
                result["errors"].append(f"WordPress media: {_explain(r)}")
        except Exception as exc:
            result["errors"].append(f"WordPress media: {exc}")

        try:
            r = client.get(f"{SETTINGS.products_endpoint}?per_page=1",
                           auth=(SETTINGS.woo_key, SETTINGS.woo_secret))
            result["woocommerce"] = r.status_code == 200
            if r.status_code != 200:
                result["errors"].append(f"WooCommerce: {_explain(r)}")
        except Exception as exc:
            result["errors"].append(f"WooCommerce: {exc}")

    return result


def _edit_url(product_id: int) -> str:
    return (f"{SETTINGS.site_url.rstrip('/')}/wp-admin/post.php"
            f"?post={product_id}&action=edit")

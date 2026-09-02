"""API routes and the background scrape task.

A batch scrape of a 50-row sheet takes tens of minutes, so it cannot live
inside one request-response: the scrape runs as an asyncio task that writes
per-row status to the database, and the interface polls a progress route.
One scrape at a time; the browser is shared across the whole batch.
"""

from __future__ import annotations

import asyncio
import html
import json
import re
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, images, llm, sheet, woo
from . import scraper
from .config import EDITABLE_FIELDS, SETTINGS, save_settings
from .sanitise import sanitise_html, text_only

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="Campkins Batch Uploader")
db.init()

# One scrape at a time. The task writes progress to the database, which is
# what the polling route reads, so nothing here needs to be shared state
# beyond the handle itself.
_scrape_task: asyncio.Task | None = None
_scrape_run_id: int | None = None


def _scrape_active() -> bool:
    return _scrape_task is not None and not _scrape_task.done()


# --- settings and status --------------------------------------------------

@app.get("/api/settings")
def get_settings() -> dict:
    data = {key: getattr(SETTINGS, key) for key in EDITABLE_FIELDS}
    data["configured"] = SETTINGS.configured
    data["llm_configured"] = SETTINGS.llm_configured
    data["default_model"] = llm.DEFAULT_MODEL
    data["output_dir_resolved"] = str(SETTINGS.output_dir)
    return data


@app.post("/api/settings")
def post_settings(updates: dict) -> dict:
    save_settings(updates)
    return get_settings()


@app.get("/api/connection")
async def connection() -> dict:
    return await asyncio.to_thread(woo.check_connection)


# --- runs -----------------------------------------------------------------

@app.post("/api/run")
async def create_run(file: UploadFile) -> dict:
    data = await file.read()
    try:
        rows, problems = sheet.parse_sheet(data, file.filename or "sheet.csv")
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    run_dir = SETTINGS.output_dir / time.strftime("run-%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    run_id = db.create_run(str(run_dir), file.filename or "")
    for index, row in enumerate(rows, start=1):
        # A description from the sheet wins; the scraper only fills the gap
        # when this is empty (see _scrape_one).
        db.add_row(run_id, index, row.name, row.url, row.cost, row.slug,
                   row.sku, ean=row.ean, brand=row.brand,
                   categories=row.categories, weight=row.weight,
                   dimensions=row.dimensions,
                   description_html=_text_to_html(row.description),
                   meta_title=row.meta_title,
                   meta_description=row.meta_description)
    return {"run_id": run_id, "rows": db.get_rows(run_id),
            "problems": problems}


@app.get("/api/runs")
def list_runs() -> dict:
    return {"runs": db.list_runs()}


@app.get("/api/run/{run_id}")
def get_run(run_id: int) -> dict:
    run = db.get_run(run_id)
    if not run:
        raise HTTPException(404, "No such run.")
    return {"run": run, "rows": db.get_rows(run_id),
            "scraping": _scrape_active() and _scrape_run_id == run_id}


@app.get("/api/run/{run_id}/progress")
def progress(run_id: int) -> dict:
    rows = db.get_rows(run_id)
    return {
        "rows": [{"row_index": r["row_index"], "name": r["name"],
                  "status": r["status"], "error": r["error"],
                  "image_count": r["image_count"],
                  "kept_count": r["kept_count"]} for r in rows],
        "scraping": _scrape_active() and _scrape_run_id == run_id,
    }


# --- scraping -------------------------------------------------------------

@app.post("/api/run/{run_id}/scrape")
async def start_scrape(run_id: int) -> dict:
    # Async on purpose: the task must be created on the main event loop,
    # and a sync route runs in a worker thread that has none.
    if not db.get_run(run_id):
        raise HTTPException(404, "No such run.")
    return _spawn_scrape(run_id)


@app.post("/api/run/{run_id}/row/{row_index}/rescrape")
async def rescrape_row(run_id: int, row_index: int) -> dict:
    row = db.get_row(run_id, row_index)
    if not row:
        raise HTTPException(404, "No such row.")
    if _scrape_active():
        raise HTTPException(409, "A scrape is already running. Wait for it "
                                 "to finish.")
    db.set_row_status(row["id"], "pending")
    return _spawn_scrape(run_id, only_index=row_index)


def _spawn_scrape(run_id: int, only_index: int | None = None) -> dict:
    global _scrape_task, _scrape_run_id
    if _scrape_active():
        raise HTTPException(409, "A scrape is already running. Wait for it "
                                 "to finish.")
    _scrape_run_id = run_id
    _scrape_task = asyncio.get_running_loop().create_task(
        _scrape_run(run_id, only_index))
    return {"started": True}


async def _scrape_run(run_id: int, only_index: int | None = None) -> None:
    run = db.get_run(run_id)
    if not run:
        return
    session = scraper.BrowserSession()
    try:
        await session.start()
    except Exception as exc:
        # Chromium missing entirely: fail every pending row with the reason
        # rather than leaving them stuck on 'pending' forever.
        for row in db.get_rows(run_id):
            if row["status"] == "pending":
                db.set_row_status(row["id"], "failed",
                                  f"Browser did not start: {exc}")
        return
    try:
        for row in db.get_rows(run_id):
            if only_index is not None and row["row_index"] != only_index:
                continue
            if row["status"] != "pending":
                continue
            await _scrape_one(session, run, row)
    finally:
        await session.close()


async def _scrape_one(session: scraper.BrowserSession, run: dict,
                      row: dict) -> None:
    db.set_row_status(row["id"], "scraping")
    try:
        page_html, context = await session.render(row["url"])
    except Exception as exc:
        db.set_row_status(row["id"], "failed", f"Could not load the page: {exc}")
        return

    try:
        candidates_meta = scraper.extract_description(page_html)
        image_urls = scraper.extract_all_image_urls(page_html, row["url"])

        product_dir = (Path(run["dir"]) /
                       f"{row['row_index']:02d}-{images.slugify(row['name'])}")
        found, failures = await images.collect_all(
            image_urls, row["url"], product_dir / "images", context)
    except Exception as exc:
        db.set_row_status(row["id"], "failed", f"Extraction failed: {exc}")
        return
    finally:
        await context.close()

    (product_dir / "extracted.json").write_text(
        json.dumps({"candidates": candidates_meta, "failures": failures},
                   ensure_ascii=False, indent=2),
        encoding="utf-8")

    db.replace_row_images(row["id"], [c.to_dict() for c in found])

    updates: dict = {"dir": str(product_dir), "status": "scraped", "error": ""}
    if not found:
        # The sheet carries the description and meta, so a page with no
        # usable images still gets a review screen; the note says why the
        # grid is empty. Bot protection lands here.
        title = scraper.page_title(page_html)
        hint = f" The page's title was '{title}'." if title else ""
        updates["error"] = ("No usable images came back from the page."
                            + hint + " The site may be blocking automated "
                            "browsers. You can Retry, or review and upload "
                            "without images.")
    # Never clobber a description the user has already edited on a rescrape.
    if not row.get("description_html"):
        draft = scraper.best_description(candidates_meta)
        updates["description_html"] = _text_to_html(draft)
    db.update_row(row["id"], **updates)


def _text_to_html(text: str) -> str:
    if not text:
        return ""
    if "<" in text and ">" in text:
        # JSON-LD descriptions are sometimes HTML already.
        return sanitise_html(text)
    paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    return "".join(f"<p>{html.escape(p)}</p>" for p in paragraphs)


# --- per-product review ---------------------------------------------------

@app.get("/api/run/{run_id}/product/{row_index}")
def product_detail(run_id: int, row_index: int) -> dict:
    row = db.get_row(run_id, row_index)
    if not row:
        raise HTTPException(404, "No such row.")
    row_images = db.get_row_images(row["id"])
    for image in row_images:
        image["url"] = f"/media/{row['id']}/{image['sha256']}"
        image.pop("path", None)

    extracted = {}
    if row["dir"]:
        extracted_path = Path(row["dir"]) / "extracted.json"
        if extracted_path.exists():
            try:
                extracted = json.loads(extracted_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                extracted = {}

    total = len(db.get_rows(run_id))
    return {"row": row, "images": row_images,
            "extracted": extracted.get("candidates", {}),
            "failures": extracted.get("failures", []),
            "total_rows": total}


class SelectionImage(BaseModel):
    sha256: str
    keep: bool = False
    position: int = 0
    filename: str = ""
    alt_text: str = ""


class SelectionRequest(BaseModel):
    suffix: str = ""
    description_html: str = ""
    thumbnail_sha: str = ""
    images: list[SelectionImage] = []


@app.post("/api/run/{run_id}/product/{row_index}/selection")
def save_selection(run_id: int, row_index: int,
                   body: SelectionRequest) -> dict:
    row = db.get_row(run_id, row_index)
    if not row:
        raise HTTPException(404, "No such row.")
    db.save_selection(
        row["id"],
        suffix=body.suffix.strip(),
        description_html=sanitise_html(body.description_html),
        thumbnail_sha=body.thumbnail_sha,
        images=[img.model_dump() for img in body.images],
    )
    return {"saved": True}


@app.post("/api/run/{run_id}/product/{row_index}/meta")
async def generate_meta(run_id: int, row_index: int) -> dict:
    row = db.get_row(run_id, row_index)
    if not row:
        raise HTTPException(404, "No such row.")
    description = text_only(row["description_html"])
    if not description:
        raise HTTPException(400, "Write or paste a description first; the "
                                 "meta tags are generated from it.")
    try:
        result = await asyncio.to_thread(
            llm.generate_meta, row["name"], description, row["cost"], SETTINGS)
    except llm.LlmError as exc:
        raise HTTPException(502, str(exc))
    db.update_row(row["id"], meta_title=result["title"],
                  meta_description=result["meta_description"])
    return result


# --- upload ---------------------------------------------------------------

def _split_categories(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


# The sheet always uses grams and millimetres; the site reads whatever unit
# it is set to under WooCommerce > Settings > Products, chosen in Settings.
GRAMS_PER_UNIT = {"g": 1, "kg": 1000, "lbs": 453.59237, "oz": 28.349523125}
MM_PER_UNIT = {"mm": 1, "cm": 10, "m": 1000, "in": 25.4, "yd": 914.4}


def _convert(value: str, per_unit: dict[str, float], unit: str) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    converted = number / per_unit.get(unit, 1)
    return f"{converted:.4f}".rstrip("0").rstrip(".")


def _weight_for_store(grams: str) -> str:
    return _convert(grams, GRAMS_PER_UNIT, SETTINGS.store_weight_unit)


def _dimensions_for_store(value: str) -> dict[str, str]:
    """'122x96x75.1' (LxWxH in mm from the sheet) to Woo's three fields."""
    parts = [p.strip() for p in re.split(r"[x×]", value or "", flags=re.I)]
    if len(parts) != 3 or not all(parts):
        return {}
    unit = SETTINGS.store_dimension_unit
    length, width, height = (_convert(p, MM_PER_UNIT, unit) for p in parts)
    if not all([length, width, height]):
        return {}
    return {"length": length, "width": width, "height": height}

class UploadRequest(BaseModel):
    name: str = ""
    slug: str = ""
    sku: str = ""
    price: str = ""
    ean: str = ""
    brand: str = ""
    categories: str = ""
    weight: str = ""
    dimensions: str = ""
    meta_title: str = ""
    meta_description: str = ""
    # create: normal path. skip: do nothing, mark the row. replace_images:
    # the product already exists, refresh its gallery only.
    mode: str = "create"


def _persist_confirm_edits(row: dict, body: UploadRequest) -> None:
    """The confirmation screen edits these; persist whatever it sent."""
    db.update_row(row["id"],
                  name=body.name.strip() or row["name"],
                  slug=body.slug.strip(),
                  sku=body.sku.strip(),
                  cost=body.price.strip(),
                  ean=body.ean.strip(),
                  brand=body.brand.strip(),
                  categories=body.categories.strip(),
                  weight=body.weight.strip(),
                  dimensions=body.dimensions.strip(),
                  meta_title=body.meta_title.strip(),
                  meta_description=body.meta_description.strip())


@app.post("/api/run/{run_id}/product/{row_index}/upload")
async def upload_product(run_id: int, row_index: int,
                         body: UploadRequest) -> dict:
    row = db.get_row(run_id, row_index)
    if not row:
        raise HTTPException(404, "No such row.")
    if not SETTINGS.configured:
        raise HTTPException(400, "Site credentials are incomplete. Fill in "
                                 "Settings first.")

    _persist_confirm_edits(row, body)
    row = db.get_row(run_id, row_index)

    if body.mode == "skip":
        db.set_row_status(row["id"], "skipped")
        return {"skipped": True}

    return await _upload_row_now(row, body.mode)


async def _upload_row_now(row: dict, mode: str) -> dict:
    """The upload itself, shared by the single and batch routes.

    Raises HTTPException on failure; returns {'exists': ...} instead of
    creating when the SKU is already on the site, so the caller decides.
    """
    # A product can go up without images; the sheet data stands on its own.
    kept = [i for i in db.get_row_images(row["id"]) if i["keep"]]
    names = [i["filename"] for i in kept]
    if any(not n for n in names):
        raise HTTPException(400, "Every kept image needs a filename.")
    if len(set(names)) != len(names):
        raise HTTPException(400, "Two kept images share a filename. Make "
                                 "them unique, or WordPress will rename one "
                                 "silently.")

    # Thumbnail first; WooCommerce makes the first image the featured one.
    thumb_sha = row["thumbnail_sha"]
    if thumb_sha not in {i["sha256"] for i in kept}:
        thumb_sha = ""
    kept.sort(key=lambda i: (0 if i["sha256"] == thumb_sha else 1,
                             i["position"] or 999, -i["orig_bytes"]))

    # Duplicate SKUs come back as a raw 400 from WooCommerce; catching it
    # first turns it into a decision instead of an error.
    if mode == "create" and row["sku"]:
        try:
            existing = await asyncio.to_thread(woo.find_by_sku, row["sku"])
        except woo.ApiError as exc:
            raise HTTPException(502, str(exc))
        if existing:
            return {"exists": True, "product": existing}

    uploaded, failures = [], []
    for image in kept:
        # The row's own media_id is not trusted here: it may point at a file
        # uploaded under an earlier name. The upload log is keyed on bytes
        # and name together, so a rename forces a fresh upload.
        media_id = db.find_existing_media(image["sha256"], image["filename"])
        if media_id:
            uploaded.append({"filename": image["filename"], "id": media_id,
                             "reused": True})
            db.set_image_media_id(row["id"], image["sha256"], media_id)
            continue
        try:
            media = await asyncio.to_thread(
                woo.upload_media, Path(image["path"]), image["filename"],
                image["alt_text"] or row["name"], row["name"])
        except woo.ApiError as exc:
            failures.append({"filename": image["filename"],
                             "reason": str(exc)})
            continue
        db.record_upload(image["sha256"], image["filename"], media["id"])
        db.set_image_media_id(row["id"], image["sha256"], media["id"])
        uploaded.append({"filename": image["filename"], "id": media["id"],
                         "reused": False})

    if failures:
        # Do not create a half-galleried product: the media IDs that made it
        # are recorded, so a retry only uploads the stragglers.
        db.set_row_status(row["id"], "upload_failed",
                          "; ".join(f["reason"] for f in failures)[:500])
        raise HTTPException(502, "Some images failed to upload: "
                            + "; ".join(f"{f['filename']} ({f['reason']})"
                                        for f in failures))

    media_ids = [u["id"] for u in uploaded]
    description = sanitise_html(row["description_html"])

    meta_data = []
    if SETTINGS.seo_title_key and row["meta_title"]:
        meta_data.append({"key": SETTINGS.seo_title_key,
                          "value": row["meta_title"]})
    if SETTINGS.seo_desc_key and row["meta_description"]:
        meta_data.append({"key": SETTINGS.seo_desc_key,
                          "value": row["meta_description"]})

    try:
        if mode == "replace_images" and row["sku"]:
            if not media_ids:
                raise HTTPException(400, "No images are selected, and "
                                         "replacing a gallery with nothing "
                                         "would empty it.")
            existing = await asyncio.to_thread(woo.find_by_sku, row["sku"])
            if not existing:
                raise HTTPException(400, "No existing product with that SKU "
                                         "to attach images to.")
            product = await asyncio.to_thread(
                woo.attach_to_product, existing["id"], media_ids)
        else:
            product = await asyncio.to_thread(
                woo.create_product, row["name"], row["slug"], row["sku"],
                row["cost"], description, media_ids, meta_data,
                ean=row["ean"], brand=row["brand"],
                categories=_split_categories(row["categories"]),
                weight=_weight_for_store(row["weight"]),
                dimensions=_dimensions_for_store(row["dimensions"]))
    except woo.ApiError as exc:
        db.set_row_status(row["id"], "upload_failed", str(exc)[:500])
        raise HTTPException(502, f"Images uploaded but the product step "
                                 f"failed: {exc}")

    db.update_row(row["id"], status="uploaded", error="", ready=0,
                  product_id=product["id"],
                  edit_url=product.get("edit_url", ""))
    return {"uploaded": uploaded, "product": product}


class ReadyRequest(UploadRequest):
    ready: bool = True


@app.post("/api/run/{run_id}/product/{row_index}/ready")
async def mark_ready(run_id: int, row_index: int, body: ReadyRequest) -> dict:
    """Save the confirmation edits and flag the row for the batch upload."""
    row = db.get_row(run_id, row_index)
    if not row:
        raise HTTPException(404, "No such row.")
    _persist_confirm_edits(row, body)
    db.update_row(row["id"], ready=1 if body.ready else 0)
    return {"ready": body.ready}


@app.post("/api/run/{run_id}/upload-ready")
async def upload_all_ready(run_id: int) -> dict:
    """Upload every row marked ready, one after another.

    One bad row must not sink the batch: failures are reported per row and
    the loop carries on.
    """
    if not SETTINGS.configured:
        raise HTTPException(400, "Site credentials are incomplete. Fill in "
                                 "Settings first.")
    targets = [r for r in db.get_rows(run_id)
               if r["ready"] and r["status"] not in ("uploaded", "skipped")]
    results = []
    for row in targets:
        entry: dict = {"row_index": row["row_index"], "name": row["name"]}
        try:
            result = await _upload_row_now(row, "create")
        except HTTPException as exc:
            entry.update(ok=False, error=str(exc.detail))
        else:
            if result.get("exists"):
                entry.update(ok=False,
                             error=f"A product with SKU '{row['sku']}' "
                                   "already exists on the site; left alone.")
            else:
                product = result["product"]
                entry.update(ok=True, product_id=product["id"],
                             edit_url=product.get("edit_url", ""),
                             warnings=product.get("warnings", []))
        results.append(entry)
    return {"attempted": len(targets), "results": results}


# --- staged image serving -------------------------------------------------

@app.get("/media/{row_id}/{sha256}", response_model=None)
def media(row_id: int, sha256: str):
    """Serves a staged image looked up from the database, never from a
    caller-supplied path, so there is nothing to traverse."""
    image = db.get_row_image(row_id, sha256.removesuffix(".webp"))
    if not image or not Path(image["path"]).exists():
        raise HTTPException(404, "No such image.")
    return FileResponse(image["path"], media_type="image/webp")


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")

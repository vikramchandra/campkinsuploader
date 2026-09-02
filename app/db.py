"""SQLite state: runs, rows, images and the upload log.

The database is the single source of truth for everything mutable in a run.
Only the image files themselves and extracted.json live on disk. That is what
makes a half-reviewed run survive an app restart, and it avoids a write race
between the background scrape task and the review screen.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

from .config import SETTINGS

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dir TEXT NOT NULL,
    sheet_name TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS rows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    row_index INTEGER NOT NULL,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    cost TEXT DEFAULT '',
    slug TEXT DEFAULT '',
    sku TEXT DEFAULT '',
    ean TEXT DEFAULT '',
    brand TEXT DEFAULT '',
    categories TEXT DEFAULT '',
    weight TEXT DEFAULT '',
    dimensions TEXT DEFAULT '',
    dir TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT DEFAULT '',
    suffix TEXT DEFAULT '',
    description_html TEXT DEFAULT '',
    thumbnail_sha TEXT DEFAULT '',
    meta_title TEXT DEFAULT '',
    meta_description TEXT DEFAULT '',
    product_id INTEGER,
    edit_url TEXT DEFAULT '',
    ready INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_rows_run ON rows (run_id);

CREATE TABLE IF NOT EXISTS row_images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    row_id INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    filename TEXT DEFAULT '',
    alt_text TEXT DEFAULT '',
    custom_name TEXT DEFAULT '',
    suffix TEXT DEFAULT '',
    alt_custom INTEGER NOT NULL DEFAULT 0,
    keep INTEGER NOT NULL DEFAULT 0,
    position INTEGER NOT NULL DEFAULT 0,
    orig_bytes INTEGER NOT NULL DEFAULT 0,
    webp_bytes INTEGER NOT NULL DEFAULT 0,
    width INTEGER NOT NULL DEFAULT 0,
    height INTEGER NOT NULL DEFAULT 0,
    path TEXT NOT NULL,
    source_url TEXT DEFAULT '',
    source_alt TEXT DEFAULT '',
    media_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_row_images_row ON row_images (row_id);

CREATE TABLE IF NOT EXISTS uploads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256 TEXT NOT NULL,
    filename TEXT NOT NULL,
    media_id INTEGER NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_uploads_sha ON uploads (sha256);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(SETTINGS.db_path, timeout=15)
    conn.row_factory = sqlite3.Row
    # WAL lets the background scrape task and request handlers write without
    # tripping over each other's locks.
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        # Databases created before the sheet format grew extra columns.
        existing = {r["name"] for r in conn.execute("PRAGMA table_info(rows)")}
        for column in ("ean", "brand", "categories", "weight", "dimensions"):
            if column not in existing:
                conn.execute(
                    f"ALTER TABLE rows ADD COLUMN {column} TEXT DEFAULT ''")
        if "ready" not in existing:
            conn.execute(
                "ALTER TABLE rows ADD COLUMN ready INTEGER NOT NULL DEFAULT 0")
        # Per-image naming controls added after the first release.
        existing = {r["name"]
                    for r in conn.execute("PRAGMA table_info(row_images)")}
        for column, definition in (("custom_name", "TEXT DEFAULT ''"),
                                   ("suffix", "TEXT DEFAULT ''"),
                                   ("alt_custom",
                                    "INTEGER NOT NULL DEFAULT 0")):
            if column not in existing:
                conn.execute(
                    f"ALTER TABLE row_images ADD COLUMN {column} {definition}")


# --- runs -----------------------------------------------------------------

def create_run(run_dir: str, sheet_name: str) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO runs (dir, sheet_name, created_at) VALUES (?, ?, ?)",
            (run_dir, sheet_name, time.time()),
        )
        return int(cur.lastrowid)


def get_run(run_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return dict(row) if row else None


def list_runs(limit: int = 12) -> list[dict]:
    with connect() as conn:
        runs = conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        out = []
        for run in runs:
            counts = conn.execute(
                "SELECT status, COUNT(*) AS n FROM rows WHERE run_id = ? "
                "GROUP BY status", (run["id"],)
            ).fetchall()
            item = dict(run)
            item["counts"] = {c["status"]: c["n"] for c in counts}
            item["total_rows"] = sum(c["n"] for c in counts)
            out.append(item)
        return out


# --- rows -----------------------------------------------------------------

def add_row(run_id: int, row_index: int, name: str, url: str, cost: str,
            slug: str, sku: str, ean: str = "", brand: str = "",
            categories: str = "", weight: str = "", dimensions: str = "",
            description_html: str = "", meta_title: str = "",
            meta_description: str = "") -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO rows (run_id, row_index, name, url, cost, slug, sku, "
            "ean, brand, categories, weight, dimensions, description_html, "
            "meta_title, meta_description) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, row_index, name, url, cost, slug, sku, ean, brand,
             categories, weight, dimensions, description_html, meta_title,
             meta_description),
        )
        return int(cur.lastrowid)


def get_rows(run_id: int) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT r.*, "
            "  (SELECT COUNT(*) FROM row_images i WHERE i.row_id = r.id) AS image_count, "
            "  (SELECT COUNT(*) FROM row_images i WHERE i.row_id = r.id AND i.keep = 1) AS kept_count "
            "FROM rows r WHERE r.run_id = ? ORDER BY r.row_index",
            (run_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_row(run_id: int, row_index: int) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM rows WHERE run_id = ? AND row_index = ?",
            (run_id, row_index),
        ).fetchone()
        return dict(row) if row else None


def get_row_by_id(row_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM rows WHERE id = ?", (row_id,)).fetchone()
        return dict(row) if row else None


def update_row(row_id: int, **fields: Any) -> None:
    if not fields:
        return
    keys = ", ".join(f"{k} = ?" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE rows SET {keys} WHERE id = ?",
                     (*fields.values(), row_id))


def set_row_status(row_id: int, status: str, error: str = "") -> None:
    update_row(row_id, status=status, error=error)


# --- images ---------------------------------------------------------------

def replace_row_images(row_id: int, images: list[dict]) -> None:
    """Used after a scrape or rescrape: the candidate set is replaced whole."""
    with connect() as conn:
        conn.execute("DELETE FROM row_images WHERE row_id = ?", (row_id,))
        conn.executemany(
            "INSERT INTO row_images (row_id, sha256, orig_bytes, webp_bytes, "
            "width, height, path, source_url, source_alt) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(row_id, i["sha256"], i["orig_bytes"], i["webp_bytes"], i["width"],
              i["height"], i["path"], i["source_url"], i["source_alt"])
             for i in images],
        )


def get_row_images(row_id: int) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM row_images WHERE row_id = ? ORDER BY orig_bytes DESC",
            (row_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_row_image(row_id: int, sha256: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM row_images WHERE row_id = ? AND sha256 = ?",
            (row_id, sha256),
        ).fetchone()
        return dict(row) if row else None


def save_selection(row_id: int, suffix: str, description_html: str,
                   thumbnail_sha: str, images: list[dict]) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE rows SET suffix = ?, description_html = ?, thumbnail_sha = ?, "
            "status = CASE WHEN status IN ('uploaded') THEN status ELSE 'reviewed' END "
            "WHERE id = ?",
            (suffix, description_html, thumbnail_sha, row_id),
        )
        for img in images:
            conn.execute(
                "UPDATE row_images SET keep = ?, position = ?, filename = ?, "
                "alt_text = ?, custom_name = ?, suffix = ?, alt_custom = ? "
                "WHERE row_id = ? AND sha256 = ?",
                (1 if img.get("keep") else 0, int(img.get("position") or 0),
                 img.get("filename") or "", img.get("alt_text") or "",
                 img.get("custom_name") or "", img.get("suffix") or "",
                 1 if img.get("alt_custom") else 0,
                 row_id, img["sha256"]),
            )


def set_image_media_id(row_id: int, sha256: str, media_id: int) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE row_images SET media_id = ? WHERE row_id = ? AND sha256 = ?",
            (media_id, row_id, sha256),
        )


# --- upload log (cross-run dedup) ----------------------------------------

def find_existing_media(sha256: str, filename: str) -> int | None:
    """The same photo uploaded before under the same name is reused.

    The name is part of the match on purpose. The site stores the file
    under the name it was uploaded with, so a renamed image has to go up
    again or the new name never reaches the site.
    """
    with connect() as conn:
        row = conn.execute(
            "SELECT media_id FROM uploads WHERE sha256 = ? AND filename = ? "
            "ORDER BY id DESC LIMIT 1", (sha256, filename)
        ).fetchone()
        return int(row["media_id"]) if row else None


def record_upload(sha256: str, filename: str, media_id: int) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO uploads (sha256, filename, media_id, created_at) "
            "VALUES (?, ?, ?, ?)",
            (sha256, filename, media_id, time.time()),
        )


def to_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)

"""Reading the product sheet.

One row per product. Required: product name and manufacturer URL. Optional:
SKU, price, slug, EAN, brand, categories, weight, dimensions, description,
meta title and meta description. Headers are matched loosely (case, spaces
and punctuation are ignored) so the sheet does not have to be typed exactly.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

# Aliases are checked as substrings of the normalised header. Order matters:
# the first field whose alias matches an unclaimed header wins, so the more
# specific names sit before the generic ones.
FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "slug": ("slug",),
    "url": ("producturl", "manufacturalurl", "manufacturerurl", "url",
            "link", "webpage"),
    "cost": ("cost", "price"),
    "sku": ("uniqueproductidentifier", "identifier", "sku", "productid", "uniqueid"),
    "ean": ("ean", "barcode", "gtin", "upc"),
    "brand": ("brand", "make"),
    "categories": ("categor",),
    "weight": ("weight",),
    "dimensions": ("dimension",),
    "meta_title": ("metatitle", "seotitle"),
    "meta_description": ("metadescription", "metadesc", "seodescription"),
    "name": ("productname", "name", "product", "title"),
    "description": ("description",),
}


@dataclass
class SheetRow:
    name: str
    url: str
    cost: str
    slug: str
    sku: str
    ean: str = ""
    brand: str = ""
    categories: str = ""
    weight: str = ""
    dimensions: str = ""
    description: str = ""
    meta_title: str = ""
    meta_description: str = ""


def _normalise(header: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (header or "").lower())


def _map_headers(headers: list[str]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    normalised = [_normalise(h) for h in headers]
    for field, aliases in FIELD_ALIASES.items():
        for alias in aliases:
            found = None
            for idx, header in enumerate(normalised):
                if idx in mapping.values() or not header:
                    continue
                if alias in header:
                    found = idx
                    break
            if found is not None:
                mapping[field] = found
                break
    return mapping


def _normalise_cost(value: str) -> tuple[str, str]:
    """Return (cost, problem). openpyxl hands numbers over as floats, and
    '199.99000000000001' must never reach the shop as a price."""
    raw = (value or "").strip().replace("£", "").replace(",", "")
    if not raw:
        return "", ""
    try:
        return str(Decimal(raw).quantize(Decimal("0.01"))), ""
    except InvalidOperation:
        return "", f"Cost '{value}' is not a number"


def _rows_from_csv(data: bytes) -> list[list[str]]:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError("Could not read the file as text.")
    return [row for row in csv.reader(io.StringIO(text))]


def _cell_to_str(cell) -> str:
    if cell is None:
        return ""
    # Excel stores every number as a float, so SKUs and EANs arrive as
    # 149409.0 and 8809298889562.0. Whole numbers go back to being whole.
    if isinstance(cell, float) and cell.is_integer():
        return str(int(cell))
    return str(cell)


def _rows_from_xlsx(data: bytes) -> list[list[str]]:
    from openpyxl import load_workbook
    book = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    sheet = book.worksheets[0]
    out = []
    for row in sheet.iter_rows(values_only=True):
        out.append([_cell_to_str(cell) for cell in row])
    book.close()
    return out


def parse_sheet(data: bytes, filename: str) -> tuple[list[SheetRow], list[str]]:
    """Returns rows plus human-readable problems.

    Rows missing a name or a usable URL are dropped and reported. Everything
    else is kept; cost, slug and SKU stay editable before upload.
    """
    if filename.lower().endswith((".xlsx", ".xlsm")):
        raw = _rows_from_xlsx(data)
    elif filename.lower().endswith(".csv"):
        raw = _rows_from_csv(data)
    else:
        raise ValueError("Use a .csv or .xlsx file.")

    raw = [r for r in raw if any(cell.strip() for cell in r)]
    if len(raw) < 2:
        raise ValueError("The sheet needs a header row and at least one product row.")

    headers, body = raw[0], raw[1:]
    mapping = _map_headers(headers)
    missing = [f for f in ("name", "url") if f not in mapping]
    if missing:
        raise ValueError(
            "Could not find these columns in the header row: "
            + ", ".join(missing)
            + ". Found headers: " + ", ".join(h for h in headers if h.strip())
        )

    def cell(row: list[str], field: str) -> str:
        idx = mapping.get(field)
        if idx is None or idx >= len(row):
            return ""
        return (row[idx] or "").strip()

    rows: list[SheetRow] = []
    problems: list[str] = []
    for number, row in enumerate(body, start=2):
        name = cell(row, "name")
        url = cell(row, "url")
        if not name and not url:
            continue
        if not name:
            problems.append(f"Row {number}: no product name, row skipped.")
            continue
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            problems.append(f"Row {number} ({name}): '{url}' is not a usable "
                            "URL, row skipped.")
            continue

        cost, cost_problem = _normalise_cost(cell(row, "cost"))
        if cost_problem:
            problems.append(f"Row {number} ({name}): {cost_problem}. Set the "
                            "price before upload.")

        rows.append(SheetRow(
            name=name,
            url=url,
            cost=cost,
            slug=cell(row, "slug"),
            sku=cell(row, "sku"),
            ean=cell(row, "ean"),
            brand=cell(row, "brand"),
            categories=cell(row, "categories"),
            weight=cell(row, "weight"),
            dimensions=cell(row, "dimensions"),
            description=cell(row, "description"),
            meta_title=cell(row, "meta_title"),
            meta_description=cell(row, "meta_description"),
        ))

    if not rows:
        raise ValueError("No usable rows in the sheet.")
    return rows, problems

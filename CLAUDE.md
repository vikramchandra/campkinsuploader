# Project instructions

Campkins Batch Uploader: a local Windows desktop app that takes a CSV/XLSX
sheet of products (name, manufacturer URL, cost, slug, SKU), scrapes every
image from each URL, lets a human review and select per product, generates
SEO meta with an LLM, and creates draft products on campkinscameras.com.

The build plan and all decisions are in
`C:\Users\NIDHI\.claude\plans\ok-so-here-is-smooth-clover.md`. The older
single-product prototype this reuses code from sits in
`campkins-uploader\campkins-uploader\` (its HANDOFF.md documents the
WordPress/WooCommerce API gotchas; do not relitigate them).

## Current state

Written but the WordPress and WooCommerce calls in `app/woo.py` have never
been executed against the live site. Assume that is where things break.

## Environment

Windows, native. Python 3.11 or 3.12 in `.venv`. Build with `build.bat`
(PyInstaller does not cross-compile). Run in development with
`python run.py` after `pip install -r requirements.txt` and
`playwright install chromium`.

## Conventions

- British English in all documentation, comments and interface copy.
- No em dashes.
- Image filenames: `{product-name-slug}-{user-string}-{NN}.webp`, zero-padded.
- Comments explain why, not what. Skip them where the code is already clear.
- Interface copy is plain and active. "Upload to Campkins", not "Submit".
- SQLite (`uploader.db`) is the single source of truth for run state; only
  images and extracted.json live on disk under the output folder.

## Do not

- Commit `config.json`, `output/` or `uploader.db`.
- Change products from draft to published status.
- Add a frontend framework. The interface is deliberately three static files.
- Use `--onefile` for PyInstaller (slow start, blocked by some AV).
- Launch a browser per URL in the scraper; one browser per run, one context
  per row.

## Files that change most

`app/scraper.py` is where supplier-specific extraction problems get fixed.
`app/proxy.py` is where proxy providers are added: one `ProxyProvider`
subclass per provider plus one entry in the `PROVIDERS` tuple, nothing else.
Everything else should be stable.

Unit tests live in `tests/`. Install `requirements-dev.txt` and run
`python -m pytest tests`.

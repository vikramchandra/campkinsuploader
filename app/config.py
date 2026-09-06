"""Configuration loading and saving.

Credentials live in config.json next to the executable, never in the code.
Two separate sets are needed for the site and they are not interchangeable:

  wp_user / wp_app_password  -> WordPress Application Password, used for
                                media uploads (/wp-json/wp/v2/media)
  woo_key / woo_secret       -> WooCommerce REST keys, used for products
                                (/wp-json/wc/v3/products)

Settings are edited from the interface, so save_settings() must mutate the
module-level SETTINGS object in place: every other module binds it once at
import, and writing the file alone would change nothing until restart.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_SYSTEM_PROMPT = (
    "You write SEO metadata for product pages on Campkins Cameras "
    "(campkinscameras.com), a camera shop in Cambridge, England. Given a "
    "product name, price and description, reply with JSON only, in the form "
    '{"title": "...", "meta_description": "..."}.\n'
    "- title: a meta title of at most 60 characters. It must include the "
    "product name. Plain and specific, no superlatives.\n"
    "- meta_description: at most 155 characters, British English, active "
    "voice. Say what the product is and one genuine selling point. No "
    "exclamation marks, no em dashes.\n"
    "Return nothing but the JSON object."
)

# Fields the Settings screen may change. Tunables such as max_dimension stay
# file-only so a stray edit in the interface cannot quietly degrade images.
EDITABLE_FIELDS = (
    "site_url", "wp_user", "wp_app_password", "woo_key", "woo_secret",
    "output_root", "llm_api_key", "llm_model",
    "llm_system_prompt", "seo_title_key", "seo_desc_key",
    "store_weight_unit", "store_dimension_unit",
    "proxy_provider", "proxy_username", "proxy_password",
)


def app_dir() -> Path:
    """Directory to read config from and keep the database in.

    Installed builds live in Program Files, which is read-only, so their
    data goes to LOCALAPPDATA. A config.json beside the .exe marks the old
    portable zip layout and keeps working where it always did.
    """
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).parent
        if (exe_dir / "config.json").exists():
            return exe_dir
        data = Path(os.environ.get("LOCALAPPDATA",
                                   str(Path.home()))) / "CampkinsUploader"
        data.mkdir(parents=True, exist_ok=True)
        return data
    return Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    site_url: str = ""
    wp_user: str = ""
    wp_app_password: str = ""
    woo_key: str = ""
    woo_secret: str = ""

    output_root: str = ""
    # All meta generation goes through OpenRouter, whatever the model.
    llm_api_key: str = ""
    llm_model: str = ""
    llm_system_prompt: str = DEFAULT_SYSTEM_PROMPT
    seo_title_key: str = ""
    seo_desc_key: str = ""

    # The units the store is set to under WooCommerce > Settings > Products.
    # The sheet always uses grams and millimetres; uploads convert to these.
    store_weight_unit: str = "kg"
    store_dimension_unit: str = "cm"

    # A key from proxy.PROVIDERS. "none" scrapes from this computer's IP.
    proxy_provider: str = "none"
    proxy_username: str = ""
    proxy_password: str = ""

    max_dimension: int = 1600
    webp_quality: int = 82
    request_timeout: int = 45

    db_path: Path = field(default_factory=lambda: app_dir() / "uploader.db")

    @property
    def configured(self) -> bool:
        return all([self.site_url, self.wp_user, self.wp_app_password,
                    self.woo_key, self.woo_secret])

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_api_key)

    @property
    def media_endpoint(self) -> str:
        return f"{self.site_url.rstrip('/')}/wp-json/wp/v2/media"

    @property
    def products_endpoint(self) -> str:
        return f"{self.site_url.rstrip('/')}/wp-json/wc/v3/products"

    @property
    def output_dir(self) -> Path:
        root = Path(self.output_root) if self.output_root else app_dir() / "output"
        return root


def _config_path() -> Path:
    return app_dir() / "config.json"


def load_settings() -> Settings:
    data: dict = {}
    path = _config_path()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}

    # Environment overrides make it easy to run without a config file.
    def pick(key: str, default: str = "") -> str:
        return os.environ.get(f"CAMPKINS_{key.upper()}", data.get(key) or default)

    settings = Settings(
        site_url=pick("site_url"),
        wp_user=pick("wp_user"),
        wp_app_password=pick("wp_app_password"),
        woo_key=pick("woo_key"),
        woo_secret=pick("woo_secret"),
        output_root=pick("output_root"),
        llm_api_key=pick("llm_api_key"),
        llm_model=pick("llm_model"),
        llm_system_prompt=pick("llm_system_prompt", DEFAULT_SYSTEM_PROMPT),
        seo_title_key=pick("seo_title_key"),
        seo_desc_key=pick("seo_desc_key"),
        store_weight_unit=pick("store_weight_unit", "kg"),
        store_dimension_unit=pick("store_dimension_unit", "cm"),
        proxy_provider=pick("proxy_provider", "none"),
        proxy_username=pick("proxy_username"),
        proxy_password=pick("proxy_password"),
        max_dimension=int(data.get("max_dimension", 1600)),
        webp_quality=int(data.get("webp_quality", 82)),
        request_timeout=int(data.get("request_timeout", 45)),
    )
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    return settings


SETTINGS = load_settings()


def save_settings(updates: dict) -> None:
    """Apply interface edits to the live object, then persist them."""
    for key in EDITABLE_FIELDS:
        if key in updates and updates[key] is not None:
            value = str(updates[key])
            if key != "llm_system_prompt":
                value = value.strip()
            setattr(SETTINGS, key, value)

    path = _config_path()
    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
    for key in EDITABLE_FIELDS:
        data[key] = getattr(SETTINGS, key)
    data.setdefault("max_dimension", SETTINGS.max_dimension)
    data.setdefault("webp_quality", SETTINGS.webp_quality)
    data.setdefault("request_timeout", SETTINGS.request_timeout)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    SETTINGS.output_dir.mkdir(parents=True, exist_ok=True)

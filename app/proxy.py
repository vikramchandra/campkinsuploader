"""Proxy providers for the scraper.

Supplier sites behind Cloudflare or Akamai sometimes refuse the scraper's
home IP. Routing the page render and the image downloads through a
residential proxy is the fix. This module is the only place that knows
about proxy providers.

Patterns, named because the module is meant to grow:

  Strategy      ProxyProvider subclasses. Each provider differs only in how a
                login and password become a gateway address, a decorated
                username and provider-specific error text.
  Null Object   NoProxyProvider and NO_PROXY. Callers always hold a
                ProxyEndpoint and spread its kwargs; they never test whether
                a proxy is configured.
  Registry      PROVIDERS, built from an explicit tuple. Adding a provider is
                one class plus one entry in that tuple. The tuple order is
                the dropdown order.
  Facade        active_proxy(), check_proxy() and explain() are the only
                functions the rest of the app calls.

Only http:// servers are allowed. Playwright refuses SOCKS5 when the proxy
needs a username and password, which every residential provider does.
"""

from __future__ import annotations

import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

import httpx

from .config import SETTINGS


class ProxyConfigError(Exception):
    """The proxy settings are incomplete or name an unknown provider."""


class ProxyFailure(Exception):
    """The proxy failed during a run. Nothing else will load, so stop."""


@dataclass(frozen=True)
class ProxyEndpoint:
    """A resolved proxy. Empty server means no proxy."""

    server: str = ""
    username: str = ""
    password: str = ""

    def __bool__(self) -> bool:
        return bool(self.server)

    def launch_kwargs(self) -> dict:
        """Keyword arguments for playwright chromium.launch()."""
        if not self.server:
            return {}
        return {"proxy": {"server": self.server,
                          "username": self.username,
                          "password": self.password}}

    def client_kwargs(self) -> dict:
        """Keyword arguments for httpx.AsyncClient().

        Credentials go in as a tuple, never inside the URL, so the ';' and
        '.' that providers put in usernames need no encoding.
        """
        if not self.server:
            return {}
        auth = (self.username, self.password) if self.username else None
        return {"proxy": httpx.Proxy(self.server, auth=auth)}


NO_PROXY = ProxyEndpoint()


# --- providers ------------------------------------------------------------

class ProxyProvider(ABC):
    key: str = ""
    label: str = ""
    username_label: str = "User id"
    username_placeholder: str = ""
    password_label: str = "Password"
    hint: str = ""

    def validate(self, username: str, password: str) -> list[str]:
        problems = []
        if not username.strip():
            problems.append(f"{self.username_label} is blank.")
        if not password.strip():
            problems.append(f"{self.password_label} is blank.")
        return problems

    @abstractmethod
    def build(self, username: str, password: str,
              session_id: str) -> ProxyEndpoint:
        """Turn the credentials into an endpoint for one session."""

    def explain_reason(self, text: str) -> str | None:
        """Plain English for a provider-specific error, or None."""
        return None

    def describe(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "username_label": self.username_label,
            "username_placeholder": self.username_placeholder,
            "password_label": self.password_label,
            "hint": self.hint,
            "needs_credentials": self.key != "none",
        }


class NoProxyProvider(ProxyProvider):
    key = "none"
    label = "None"
    hint = "Pages and images are fetched from this computer's own connection."

    def validate(self, username: str, password: str) -> list[str]:
        return []

    def build(self, username: str, password: str,
              session_id: str) -> ProxyEndpoint:
        return NO_PROXY


class DataImpulseProvider(ProxyProvider):
    """dataimpulse.com residential proxies.

    Sticky session for the whole run, not a new IP per request. Within a
    row the page render and the image downloads must come from the same IP,
    because Cloudflare and Akamai bind their clearance cookies to it. Port
    10000 selects the sticky pool, sessid names the session and sessttl
    stops the IP rotating during a long batch.
    """

    key = "dataimpulse"
    label = "DataImpulse"
    username_label = "DataImpulse login"
    username_placeholder = "login from the DataImpulse dashboard"
    password_label = "DataImpulse password"
    hint = ("Residential proxy. Each run uses one UK IP for the whole batch. "
            "Find the login and password under Proxies in the DataImpulse "
            "dashboard. Enter the plain login; the app adds the country "
            "and session parameters itself.")

    server = "http://gw.dataimpulse.com:10000"
    country = "gb"
    session_minutes = 120

    # Tokens DataImpulse puts in the 407 status line.
    REASONS = {
        "NO_USER": ("The proxy rejected the DataImpulse login or password. "
                    "Check them under Settings > Proxy."),
        "TRAFFIC_EXHAUSTED": ("The DataImpulse plan has no traffic left. Top "
                              "it up in the DataImpulse dashboard."),
        "THREADS_EXHAUSTED": ("Too many connections are open on the "
                              "DataImpulse plan. Wait a minute and try again."),
        "PORT_NOT_ALLOWED": ("The DataImpulse plan does not allow sticky "
                             "session ports. Contact DataImpulse support."),
        "USER_BLOCKED": ("The DataImpulse account is blocked. Contact "
                         "DataImpulse support."),
    }

    def build(self, username: str, password: str,
              session_id: str) -> ProxyEndpoint:
        login = username.strip()
        session = re.sub(r"[^A-Za-z0-9]", "", session_id) or "0"
        decorated = (f"{login}__cr.{self.country};sessid.{session};"
                     f"sessttl.{self.session_minutes}")
        return ProxyEndpoint(self.server, decorated, password.strip())

    def explain_reason(self, text: str) -> str | None:
        for token, message in self.REASONS.items():
            if token in text:
                return message
        return None


# Dropdown order. Add a provider here and nowhere else.
PROVIDERS: dict[str, ProxyProvider] = {
    provider.key: provider
    for provider in (NoProxyProvider(), DataImpulseProvider())
}


def is_known_provider(key: str) -> bool:
    return key in PROVIDERS


def get_provider(key: str | None) -> ProxyProvider:
    """Unknown or blank keys fall back to no proxy."""
    return PROVIDERS.get((key or "none").strip().lower(), PROVIDERS["none"])


def provider_catalogue() -> list[dict]:
    return [provider.describe() for provider in PROVIDERS.values()]


# --- facade ---------------------------------------------------------------

def build_proxy(key: str, username: str, password: str,
                session_id: str) -> ProxyEndpoint:
    if not is_known_provider(key):
        raise ProxyConfigError(
            f"Unknown proxy provider '{key}'. Choose one under Settings > Proxy.")
    provider = PROVIDERS[key]
    problems = provider.validate(username, password)
    if problems:
        raise ProxyConfigError(
            " ".join(problems) + " Fill them in under Settings > Proxy, or "
            "set the provider to None.")
    return provider.build(username, password, session_id)


def active_proxy(session_id: str) -> ProxyEndpoint:
    """The endpoint for the saved settings, or NO_PROXY."""
    return build_proxy(SETTINGS.proxy_provider, SETTINGS.proxy_username,
                       SETTINGS.proxy_password, session_id)


# Chromium's own proxy errors, most specific first. A 407 status line is
# handled separately because providers put their reason token in it.
_CHROMIUM_ERRORS = (
    ("ERR_INVALID_AUTH_CREDENTIALS",
     "The proxy rejected the login or password. Check them under "
     "Settings > Proxy."),
    ("ERR_TUNNEL_CONNECTION_FAILED",
     "The proxy refused the connection. Check the login and password under "
     "Settings > Proxy."),
    ("ERR_PROXY_CONNECTION_FAILED",
     "Could not connect to the proxy server. Check the internet connection, "
     "or set the provider to None."),
    ("ERR_PROXY_AUTH_UNSUPPORTED",
     "The proxy asked for a kind of authentication the browser does not "
     "support."),
)

_STATUS_407 = re.compile(r"\b407\b")


def explain(error: BaseException | str,
            provider: ProxyProvider | None = None) -> str | None:
    """Plain English for a proxy error, or None when it is not one.

    Only call this when a proxy is in use: '407' can appear in an ordinary
    URL, and the Chromium tokens only occur with a proxy anyway.
    """
    text = str(error)
    if provider is None:
        provider = get_provider(SETTINGS.proxy_provider)
    reason = provider.explain_reason(text)
    if reason:
        return reason
    for token, message in _CHROMIUM_ERRORS:
        if token in text:
            return message
    if _STATUS_407.search(text):
        return ("The proxy rejected the login or password (HTTP 407). Check "
                "them under Settings > Proxy.")
    return None


CHECK_URL = "https://api.ipify.org?format=json"
CHECK_TIMEOUT = 15


async def check_proxy(key: str, username: str, password: str) -> dict:
    """One request through the proxy. Returns {ok, provider, ip, error}.

    The provider's reason token (traffic exhausted, blocked account) only
    shows up in httpx's error, never in Chromium's, so this is the check to
    run before a batch starts.
    """
    result = {"ok": False, "provider": get_provider(key).label,
              "ip": "", "error": ""}
    try:
        endpoint = build_proxy(key, username, password,
                               f"check{int(time.time())}")
    except ProxyConfigError as exc:
        result["error"] = str(exc)
        return result
    provider = PROVIDERS[key]
    try:
        async with httpx.AsyncClient(timeout=CHECK_TIMEOUT,
                                     **endpoint.client_kwargs()) as client:
            response = await client.get(CHECK_URL)
            response.raise_for_status()
            result["ip"] = str(response.json().get("ip", ""))
            result["ok"] = True
    except httpx.ProxyError as exc:
        result["error"] = (explain(exc, provider)
                           or f"The proxy refused the connection: {exc}")
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        if endpoint:
            result["error"] = (f"Could not reach the proxy at {endpoint.server}. "
                               "Check the internet connection, or set the "
                               "provider to None.")
        else:
            result["error"] = f"Could not reach {CHECK_URL}: {exc}"
    except Exception as exc:
        result["error"] = explain(exc, provider) or f"The check failed: {exc}"
    return result

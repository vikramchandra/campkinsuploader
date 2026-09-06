"""Unit tests for app.proxy. No network access."""

import asyncio

import httpx
import pytest

from app import proxy
from app.proxy import (NO_PROXY, PROVIDERS, DataImpulseProvider,
                       ProxyConfigError, ProxyEndpoint, build_proxy, explain,
                       get_provider, provider_catalogue)


# --- registry -------------------------------------------------------------

def test_registry_order_is_dropdown_order():
    assert list(PROVIDERS) == ["none", "dataimpulse"]


def test_catalogue_matches_registry():
    catalogue = provider_catalogue()
    assert [entry["key"] for entry in catalogue] == list(PROVIDERS)
    assert catalogue[0]["needs_credentials"] is False
    assert catalogue[1]["needs_credentials"] is True
    for entry in catalogue:
        assert entry["label"]
        assert entry["username_label"]
        assert entry["password_label"]


@pytest.mark.parametrize("key, expected", [
    ("bogus", "none"),
    ("", "none"),
    (None, "none"),
    (" DataImpulse ", "dataimpulse"),
])
def test_get_provider_is_forgiving(key, expected):
    assert get_provider(key).key == expected


# --- null object ----------------------------------------------------------

def test_no_proxy_produces_no_kwargs():
    assert not NO_PROXY
    assert NO_PROXY.launch_kwargs() == {}
    assert NO_PROXY.client_kwargs() == {}


def test_none_provider_ignores_credentials():
    assert build_proxy("none", "user", "secret", "run1") is NO_PROXY


# --- dataimpulse ----------------------------------------------------------

def test_dataimpulse_decorates_username_for_a_sticky_session():
    endpoint = DataImpulseProvider().build(" abc ", " pw ", "run3-1700")
    assert endpoint.server == "http://gw.dataimpulse.com:10000"
    assert endpoint.username == "abc__cr.gb;sessid.run31700;sessttl.120"
    assert endpoint.password == "pw"


def test_dataimpulse_never_uses_socks():
    endpoint = DataImpulseProvider().build("abc", "pw", "s")
    assert endpoint.server.startswith("http://")


def test_launch_kwargs_match_playwright_proxy_settings():
    endpoint = ProxyEndpoint("http://host:1", "u", "p")
    assert endpoint.launch_kwargs() == {
        "proxy": {"server": "http://host:1", "username": "u", "password": "p"}}


def test_client_kwargs_keep_credentials_out_of_the_url():
    endpoint = ProxyEndpoint("http://host:1", "u__cr.gb;sessid.1", "p")
    kwargs = endpoint.client_kwargs()
    assert isinstance(kwargs["proxy"], httpx.Proxy)
    assert str(kwargs["proxy"].url) == "http://host:1"
    assert kwargs["proxy"].auth == ("u__cr.gb;sessid.1", "p")


# --- validation -----------------------------------------------------------

def test_blank_credentials_are_refused():
    with pytest.raises(ProxyConfigError) as info:
        build_proxy("dataimpulse", "", "  ", "s")
    message = str(info.value)
    assert "login" in message
    assert "password" in message
    assert "Settings > Proxy" in message


def test_unknown_provider_is_refused():
    with pytest.raises(ProxyConfigError):
        build_proxy("bogus", "u", "p", "s")


# --- explain --------------------------------------------------------------

@pytest.mark.parametrize("text, fragment", [
    ("net::ERR_PROXY_CONNECTION_FAILED at https://x", "Could not connect"),
    ("net::ERR_TUNNEL_CONNECTION_FAILED at https://x", "refused"),
    ("net::ERR_INVALID_AUTH_CREDENTIALS", "rejected the login"),
    ("net::ERR_PROXY_AUTH_UNSUPPORTED", "authentication"),
    ("407 Proxy Authentication Required", "HTTP 407"),
])
def test_explain_translates_chromium_errors(text, fragment):
    message = explain(text, PROVIDERS["none"])
    assert message is not None
    assert fragment in message


@pytest.mark.parametrize("token, fragment", [
    ("NO_USER", "login or password"),
    ("TRAFFIC_EXHAUSTED", "no traffic left"),
    ("THREADS_EXHAUSTED", "Too many connections"),
    ("PORT_NOT_ALLOWED", "sticky session ports"),
    ("USER_BLOCKED", "blocked"),
])
def test_explain_translates_dataimpulse_tokens(token, fragment):
    message = explain(f"407 {token}", PROVIDERS["dataimpulse"])
    assert fragment in message


def test_explain_ignores_ordinary_errors():
    assert explain("Timeout 45000ms exceeded", PROVIDERS["dataimpulse"]) is None


def test_explain_defaults_to_the_active_provider(monkeypatch):
    monkeypatch.setattr(proxy.SETTINGS, "proxy_provider", "dataimpulse")
    assert "no traffic left" in explain("407 TRAFFIC_EXHAUSTED")


# --- check_proxy ----------------------------------------------------------

def test_check_proxy_reports_config_errors_without_network():
    result = asyncio.run(proxy.check_proxy("dataimpulse", "", ""))
    assert result["ok"] is False
    assert result["provider"] == "DataImpulse"
    assert "login" in result["error"]

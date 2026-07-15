"""Egress proxy for ProjectX REST traffic: paste-format normalization, client
plumbing (tunnel + warm keep-alive), the safe transport retry, and the API."""

import httpx
import pytest

from tophat.store.config import load_settings, normalize_proxy, update_settings


# --- normalize_proxy: every vendor paste format -> canonical URL ---------------

def test_norm_host_port():
    assert normalize_proxy("38.154.227.167:5868") == "http://38.154.227.167:5868"


def test_norm_vendor_colon_format():
    assert (normalize_proxy("38.154.227.167:5868:me:secret")
            == "http://me:secret@38.154.227.167:5868")


def test_norm_at_format():
    assert (normalize_proxy("me:secret@38.154.227.167:5868")
            == "http://me:secret@38.154.227.167:5868")


def test_norm_full_url_keeps_scheme():
    assert (normalize_proxy("socks5://me:secret@proxy.example.com:1080")
            == "socks5://me:secret@proxy.example.com:1080")


def test_norm_is_idempotent():
    once = normalize_proxy("1.2.3.4:8080:u:p%40ss")
    assert normalize_proxy(once) == once


def test_norm_quotes_special_password_chars():
    # ':' and '@' in the password survive via percent-encoding (last @ splits host)
    assert (normalize_proxy("me:p@s:s@1.2.3.4:8080")
            == "http://me:p%40s%3As@1.2.3.4:8080")


def test_norm_blank_means_direct():
    assert normalize_proxy("") == ""
    assert normalize_proxy("   ") == ""


@pytest.mark.parametrize("junk", [
    "not a proxy", "hostonly", "1.2.3.4", "1.2.3.4:notaport",
    "1.2.3.4:99999", "ftp://1.2.3.4:8080", "a:b:c:d:e",
])
def test_norm_rejects_junk(junk):
    with pytest.raises(ValueError):
        normalize_proxy(junk)


# --- settings round-trip --------------------------------------------------------

def test_update_settings_normalizes_and_persists():
    update_settings({"proxy_url": "1.2.3.4:8080:me:secret"})
    assert load_settings().proxy_url == "http://me:secret@1.2.3.4:8080"
    update_settings({"proxy_url": ""})          # blank clears -> direct
    assert load_settings().proxy_url == ""


def test_update_settings_rejects_bad_proxy():
    with pytest.raises(ValueError):
        update_settings({"proxy_url": "garbage"})


# --- client plumbing -------------------------------------------------------------

def _pool(c):
    return c._client._transport._pool


def test_client_direct_by_default():
    from httpcore import HTTPProxy
    from tophat.broker.projectx.client import ProjectXClient
    c = ProjectXClient("u", "k")
    assert c.proxy is None
    assert not isinstance(_pool(c), HTTPProxy)


def test_client_tunnels_through_proxy():
    from httpcore import HTTPProxy
    from tophat.broker.projectx.client import ProjectXClient
    c = ProjectXClient("u", "k", proxy="http://me:secret@1.2.3.4:8080")
    assert isinstance(_pool(c), HTTPProxy)


def test_client_env_fallback(monkeypatch):
    from tophat.broker.projectx.client import ProjectXClient
    monkeypatch.setenv("PROJECTX_PROXY_URL", "http://me:s@5.6.7.8:9000")
    assert ProjectXClient("u", "k").proxy == "http://me:s@5.6.7.8:9000"


def test_keepalive_outlives_automation_tick():
    """Pooled connections must survive the ~30s automation tick gap, so the
    09:45:00 fire reuses the previous tick's tunnel (no handshake at fire time)."""
    from tophat.broker.projectx.client import KEEPALIVE_EXPIRY_S, ProjectXClient
    from tophat.services.automation import Automation
    assert KEEPALIVE_EXPIRY_S > Automation(None).interval
    assert _pool(ProjectXClient("u", "k"))._keepalive_expiry == KEEPALIVE_EXPIRY_S


# --- transport retry: reads resend once, order placement never ------------------

class _Flaky:
    """Stands in for httpx.Client: fails the first `fail` posts, then succeeds."""

    def __init__(self, fail=1):
        self.calls, self.fail = 0, fail

    def post(self, path, json=None, headers=None):
        self.calls += 1
        if self.calls <= self.fail:
            raise httpx.ReadError("peer closed idle connection")
        return httpx.Response(200, json={"success": True, "accounts": []},
                              request=httpx.Request("POST", f"https://x{path}"))


def _client_with(flaky):
    from tophat.broker.projectx.client import ProjectXClient
    c = ProjectXClient("u", "k")
    c._token = "T"          # skip login so the stub only sees the call under test
    c._client = flaky
    return c


def test_read_path_retries_once_on_transport_error():
    flaky = _Flaky(fail=1)
    c = _client_with(flaky)
    assert c.search_accounts() == []
    assert flaky.calls == 2


def test_order_place_never_resends():
    flaky = _Flaky(fail=1)
    c = _client_with(flaky)
    with pytest.raises(httpx.ReadError):
        c.place_order({"accountId": 1})
    assert flaky.calls == 1                     # no blind double-fire


# --- API ------------------------------------------------------------------------

def test_settings_api_proxy_roundtrip(client):
    r = client.post("/api/settings", json={"proxy_url": "1.2.3.4:8080:me:secret"})
    assert r.status_code == 200
    assert r.json()["proxy_url"] == "http://me:secret@1.2.3.4:8080"

    r = client.post("/api/settings", json={"proxy_url": "garbage"})
    assert r.status_code == 400

    r = client.post("/api/settings", json={"proxy_url": ""})
    assert r.status_code == 200 and r.json()["proxy_url"] == ""


def test_test_proxy_endpoint_rejects_junk(client):
    r = client.post("/api/settings/test-proxy", json={"proxy_url": "garbage"})
    assert r.status_code == 400

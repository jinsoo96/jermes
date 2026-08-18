"""Dashboard HTTP surface - routes, content types, and failure isolation.

Runs the real ThreadingHTTPServer on an ephemeral port. `collect` is patched so
the tests never touch a live XGEN stack: what is under test here is the HTTP
contract, not the data.
"""

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from jermes import dashboard

STATE = {"generated_at": "2026-01-01 00:00:00", "xgen_base": "http://x",
         "totals": {"skills": 1, "verified": 1, "unverified": 0, "active": 1,
                    "staged": 0, "rejected": 0, "deprecated": 0,
                    "successes": 0, "failures": 0},
         "kinds": {"guide": 1}, "scopes": [], "schedule": None, "errors": []}


@pytest.fixture()
def server(monkeypatch):
    monkeypatch.setattr(dashboard, "collect", lambda: STATE)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def fetch(url):
    with urllib.request.urlopen(url, timeout=10) as response:
        return response.status, response.headers.get("Content-Type"), response.read()


def test_healthz_is_plain_ok(server):
    status, ctype, body = fetch(f"{server}/healthz")
    assert status == 200 and body == b"ok" and "text/plain" in ctype


def test_index_serves_html(server):
    status, ctype, body = fetch(f"{server}/")
    assert status == 200 and "text/html" in ctype
    assert b"Jermes" in body


def test_api_state_serves_json_matching_collect(server):
    status, ctype, body = fetch(f"{server}/api/state")
    assert status == 200 and "application/json" in ctype
    assert json.loads(body.decode("utf-8"))["totals"]["skills"] == 1


def test_unknown_path_is_404(server):
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        fetch(f"{server}/nope")
    assert excinfo.value.code == 404


def test_state_is_never_cached(server):
    with urllib.request.urlopen(f"{server}/api/state", timeout=10) as response:
        assert response.headers.get("Cache-Control") == "no-store"


def test_collect_failure_does_not_leak_a_hung_socket(server, monkeypatch):
    """collect() 가 터져도 서버는 살아 있어야 한다 - 다음 요청이 받아져야 함."""
    def boom():
        raise RuntimeError("collect exploded")

    monkeypatch.setattr(dashboard, "collect", boom)
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        fetch(f"{server}/api/state")
    assert excinfo.value.code == 500
    assert "collect exploded" in excinfo.value.read().decode("utf-8")

    monkeypatch.setattr(dashboard, "collect", lambda: STATE)
    assert fetch(f"{server}/healthz")[0] == 200  # 서버는 계속 산다


def test_binds_loopback_only():
    """터널 origin 규약: localhost 바인딩(외부 직접 노출 금지)."""
    import inspect
    source = inspect.getsource(dashboard.main)
    assert "127.0.0.1" in source

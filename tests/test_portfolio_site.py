"""The vendored portfolio seam: its fallback copy is intact and answers when nothing else does."""

import hashlib
from pathlib import Path

import pytest

from tachometer.portfolio import site as portfolio_site

ROOT = Path(__file__).resolve().parents[1]
VENDORED = ROOT / "src/tachometer/portfolio"


def test_manifest_pins_every_vendored_file():
    lines = (VENDORED / "MANIFEST.sha256").read_text().splitlines()
    names = set()
    for line in lines:
        digest, name = line.split("  ", 1)
        names.add(name)
        assert hashlib.sha256((VENDORED / name).read_bytes()).hexdigest() == digest, name
    assert names == {"__init__.py", "site.py", "theme.css", "theme.js", "chrome.js"}


def test_the_fallback_answers_with_no_store_and_serves_only_the_allowlist(tmp_path):
    site = portfolio_site.resolve(
        tmp_path, vendored=VENDORED, env={portfolio_site.STORE_ENV: str(tmp_path / "none")}
    )
    assert site.response("/_portfolio/theme.css")[0] == (VENDORED / "theme.css").read_bytes()
    assert site.response("/_portfolio/chrome.js")[0] == (VENDORED / "chrome.js").read_bytes()
    assert site.response("/_portfolio/nav.json")[0] == portfolio_site.EMPTY_NAV
    for path in ("/_portfolio/site.py", "/_portfolio/MANIFEST.sha256", "/_portfolio/../site.py"):
        assert site.response(path) is None


def test_the_example_config_is_synthetic_and_valid():
    tomllib = pytest.importorskip("tomllib")
    text = (ROOT / "config" / "portfolio-site.example.toml").read_text()
    settings = tomllib.loads(text)["site"]
    assert settings["fronting"] in portfolio_site.FRONTING
    assert set(settings) == {"fronting"}


def _serve(handler, tmp_path, monkeypatch):
    import threading
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer

    from tachometer.portfolio import site as portfolio_site

    monkeypatch.setenv(portfolio_site.STORE_ENV, str(tmp_path / "no-store"))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    def get(path):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{httpd.server_port}{path}") as r:
                return r.status, r.headers["Content-Type"], r.read()
        except urllib.error.HTTPError as err:
            return err.code, err.headers.get("Content-Type"), b""

    return httpd, thread, get


def _check(get):
    from tachometer.portfolio import site as portfolio_site

    status, ctype, body = get("/_portfolio/theme.css?v=1")
    assert (status, ctype.split(";")[0]) == (200, "text/css")
    assert body == (VENDORED / "theme.css").read_bytes()
    assert get("/_portfolio/chrome.js")[1].startswith("text/javascript")
    assert get("/_portfolio/nav.json")[2] == portfolio_site.EMPTY_NAV
    for path in ("/_portfolio/site.py", "/_portfolio/MANIFEST.sha256", "/_portfolio/nope"):
        assert get(path)[0] == 404


def test_the_dashboard_serves_the_portfolio_files_itself(tmp_path, monkeypatch):
    from tachometer.server import _Handler

    handler = type("H", (_Handler,), {"tachometer_root": tmp_path, "log_message": lambda *a: None})
    httpd, thread, get = _serve(handler, tmp_path, monkeypatch)
    try:
        _check(get)
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)

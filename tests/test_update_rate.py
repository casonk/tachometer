"""Reading each service's own update-rate report during a latency probe."""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer

from tachometer import service_latency as sl


@contextmanager
def _server(status=200, body=b"{}"):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(status)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield sl.Service(name="svc", port=httpd.server_address[1], update_rate_path="/rate")
    finally:
        httpd.shutdown()
        httpd.server_close()


def _fetch(status=200, body=b"{}"):
    with _server(status, body) as service:
        return sl.fetch_update_rate(service, 5.0)


def test_config_maps_services_to_a_rate_path_and_ignores_unusable_ones(tmp_path):
    config = tmp_path / "c.toml"
    config.write_text(
        '[[services]]\nname = "a"\nport = 1\n'
        '[[services]]\nname = "b"\nport = 2\nupdate_rate_path = "/own"\n'
        '[[services]]\nname = "c"\nport = 3\n'
        '[update_rate]\na = "/api/update-rate"\nc = "http://elsewhere.invalid/"\nzz = "/x"\n',
        encoding="utf-8",
    )
    paths = {s.name: s.update_rate_path for s in sl.load_config(config)["services"]}
    assert paths == {"a": "/api/update-rate", "b": "/own", "c": ""}


def test_a_good_report_keeps_the_summary_and_numeric_settings():
    body = json.dumps({"summary": " 60 s ", "settings": {"A": 60, "B": "text"}}).encode()
    assert _fetch(body=body) == {"summary": "60 s", "settings": {"A": 60}}


def test_unusable_reports_become_errors():
    assert _fetch(status=404) == {"error": "HTTP 404"}
    assert "error" in _fetch(body=b"not json")
    assert _fetch(body=b"[1]") == {"error": "reply has no summary"}
    assert _fetch(body=b" " * (sl.RATE_MAX_BYTES + 1)) == {"error": "reply too large"}
    assert "error" in sl.fetch_update_rate(
        sl.Service(name="gone", port=1, update_rate_path="/rate"), 0.5
    )


def test_probe_records_the_report_only_for_healthy_services_that_have_a_path(tmp_path):
    services = [
        sl.Service(name="up", port=1, update_rate_path="/rate"),
        sl.Service(name="down", port=2, update_rate_path="/rate"),
        sl.Service(name="plain", port=3),
    ]
    asked = []

    def probe(service, _timeout):
        if service.name == "down":
            return sl.Sample(None, None, None, "connect: refused")
        return sl.Sample(1.0, 2.0, 200)

    def rate(service, _timeout):
        asked.append(service.name)
        return {"summary": "now", "settings": {}}

    config = {"settings": dict(sl.DEFAULTS), "services": services}
    records = sl.run_probe(tmp_path, config, probe=probe, rate=rate, alerts=False)["records"]
    assert asked == ["up"]
    assert [r["name"] for r in records if "update_rate" in r] == ["up"]

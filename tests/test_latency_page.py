"""The dashboard's Service latency page and its Probe now / Time startup buttons."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import HTTPServer

import pytest

from tachometer import latency_page as lp
from tachometer import service_latency as sl
from tachometer.server import _Handler


def _history(root, name="svc", replies=(10, 11, 12, 300), ok=True, error=""):
    path = sl.history_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    with path.open("a", encoding="utf-8") as handle:
        for i, reply in enumerate(replies):
            handle.write(
                json.dumps(
                    {
                        "ts": now - 60 * (len(replies) - i),
                        "name": name,
                        "ok": ok,
                        "reply_p50_ms": reply,
                        "reply_p95_ms": reply + 1,
                        "connect_p50_ms": 0.2,
                        "unit_age_s": 7200.0,
                        "error": error,
                    }
                )
                + "\n"
            )


@pytest.fixture(autouse=True)
def _idle():
    lp._STATE.update(running="", started_at=0.0, message="")
    yield
    deadline = time.time() + 5
    while lp._STATE["running"] and time.time() < deadline:
        time.sleep(0.02)


def test_payload_orders_slowest_first_and_carries_alert_state(tmp_path):
    _history(tmp_path, "slow", replies=(900, 950, 1000))
    _history(tmp_path, "fast", replies=(1, 1, 1))
    sl.state_path(tmp_path).write_text(
        json.dumps({"slow": {"bad": True, "reason": "reply 1000 ms vs 100 ms baseline"}})
    )
    payload = lp.build_payload(tmp_path)
    assert [s["name"] for s in payload["services"]] == ["slow", "fast"]
    assert payload["services"][0]["bad"] and "baseline" in payload["services"][0]["reason"]
    assert payload["services"][1]["trend"] == [1, 1, 1]


def test_services_dropped_from_the_config_leave_the_page(tmp_path):
    _history(tmp_path, "kept")
    _history(tmp_path, "dropped")
    config = tmp_path / lp.CONFIG_RELATIVE
    config.parent.mkdir(parents=True)
    config.write_text('[[services]]\nname = "kept"\nport = 9\n')
    assert [s["name"] for s in lp.build_payload(tmp_path)["services"]] == ["kept"]


def test_page_escapes_names_and_errors(tmp_path):
    _history(
        tmp_path, '<script>alert("x")</script>', replies=(5, 5, 5), ok=False, error="<b>boom</b>"
    )
    page = lp.render_page(tmp_path)
    assert "<script>alert" not in page and "&lt;script&gt;" in page
    assert "<b>boom</b>" not in page and "&lt;b&gt;boom&lt;/b&gt;" in page
    assert 'class="dot red"' in page


def test_page_with_no_history_explains_what_to_do(tmp_path):
    assert "No samples yet" in lp.render_page(tmp_path)


def test_probe_needs_a_config_and_runs_in_the_background_without_touching_alert_state(
    tmp_path, monkeypatch
):
    ok, message = lp.start_probe(tmp_path)
    assert not ok and "service-latency.toml.example" in message
    config = tmp_path / lp.CONFIG_RELATIVE
    config.parent.mkdir(parents=True)
    config.write_text('samples = 1\n[[services]]\nname = "svc"\nport = 9\n')
    seen = {}
    monkeypatch.setattr(
        sl, "run_probe", lambda root, cfg, **kw: seen.update(kw) or {"records": [], "events": []}
    )
    ok, message = lp.start_probe(tmp_path)
    assert ok and "started" in message
    deadline = time.time() + 5
    while lp._STATE["running"] and time.time() < deadline:
        time.sleep(0.02)
    assert seen == {"alerts": False}


def test_only_one_action_runs_at_a_time(tmp_path):
    release = threading.Event()
    ok, _ = lp._start("Probe", release.wait)
    assert ok
    ok, message = lp._start("Probe", lambda: None)
    assert not ok and "already running" in message
    release.set()


def test_startup_timing_rejects_unknown_services_and_records_the_result(tmp_path, monkeypatch):
    config = tmp_path / lp.CONFIG_RELATIVE
    config.parent.mkdir(parents=True)
    config.write_text('[[services]]\nname = "svc"\nport = 9\n')
    assert lp.start_startup(tmp_path, "nope")[0] is False
    monkeypatch.setattr(
        sl,
        "measure_startup",
        lambda service, timeout: {"name": service.name, "ready": True, "time_to_ready_s": 22.0},
    )
    assert lp.start_startup(tmp_path, "svc")[0] is True
    deadline = time.time() + 5
    while lp._STATE["running"] and time.time() < deadline:
        time.sleep(0.02)
    recorded = sl.load_history(sl.startup_path(tmp_path))
    assert recorded[-1]["name"] == "svc" and recorded[-1]["time_to_ready_s"] == 22.0
    assert "22.0 s" in lp.render_page(tmp_path)


def test_manual_probe_leaves_the_alert_state_for_the_scheduled_run(tmp_path):
    config = {
        "settings": {**sl.DEFAULTS, "samples": 1},
        "services": [sl.Service(name="svc", port=9)],
        "notify": {},
    }
    result = sl.run_probe(tmp_path, config, alerts=False)
    assert result["events"] == [] and not sl.state_path(tmp_path).exists()
    assert sl.load_history(sl.history_path(tmp_path))  # but the sample is kept


def test_http_routes_and_origin_check(tmp_path, monkeypatch):
    _history(tmp_path)
    started = []
    monkeypatch.setattr(
        lp, "start_probe", lambda root: started.append("probe") or (True, "Probe started")
    )
    handler = type("H", (_Handler,), {"tachometer_root": tmp_path})
    httpd = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *_args, **_kwargs):
            return None

    opener = urllib.request.build_opener(NoRedirect)

    def post(path, origin):
        request = urllib.request.Request(base + path, data=b"x=1", headers={"Origin": origin})
        try:
            return opener.open(request)
        except urllib.error.HTTPError as exc:
            return exc

    try:
        assert b"Service latency" in urllib.request.urlopen(base + "/latency").read()
        assert (
            json.loads(urllib.request.urlopen(base + "/api/latency").read())["services"][0]["name"]
            == "svc"
        )
        assert b"/latency" in urllib.request.urlopen(base + "/").read()
        assert post("/latency/probe", "https://evil.example").status == 403
        assert started == []
        response = post("/latency/probe", base)
        assert response.status == 303 and response.headers["Location"].startswith(
            "/latency?msg=Probe+started"
        )
        assert started == ["probe"]
    finally:
        httpd.shutdown()
        httpd.server_close()

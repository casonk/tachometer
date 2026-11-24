"""Tests for per-service latency probing, startup timing, history and alerts."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tachometer import service_latency as sl
from tachometer.cli import main


class _Handler(BaseHTTPRequestHandler):
    status = 302

    def do_GET(self):  # noqa: N802
        self.send_response(type(self).status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):  # silence
        pass


@pytest.fixture
def server():
    handler = type("H", (_Handler,), {"status": 302})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd, handler
    httpd.shutdown()
    httpd.server_close()


def _service(httpd, name="svc", **kwargs):
    return sl.Service(name=name, port=httpd.server_address[1], **kwargs)


def _closed_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_probe_counts_a_login_redirect_as_replying(server):
    httpd, _ = server
    sample = sl.probe_once(_service(httpd), 2)
    assert sample.status == 302 and sample.reply_ms is not None and sample.connect_ms is not None


def test_probe_reports_a_refused_connection_without_raising():
    sample = sl.probe_once(sl.Service(name="x", port=_closed_port()), 1)
    assert sample.status is None and sample.error.startswith("connect:")


def test_a_5xx_is_a_failure(server):
    httpd, handler = server
    handler.status = 503
    record = sl.summarize(
        _service(httpd), [sl.probe_once(_service(httpd), 2)], now=1.0, unit_age_s=None
    )
    assert record["ok"] is False and record["status"] == 503


def test_summary_has_percentiles():
    samples = [sl.Sample(1.0, float(ms), 200) for ms in (10, 20, 30, 40, 100)]
    record = sl.summarize(sl.Service(name="a", port=1), samples, now=5.0, unit_age_s=12.0)
    assert (record["reply_p50_ms"], record["reply_p95_ms"], record["reply_max_ms"]) == (
        30.0,
        100.0,
        100.0,
    )
    assert record["ok"] and record["unit_age_s"] == 12.0


def _hist(name, replies, ok=True):
    return [
        {"name": name, "ok": ok, "reply_p50_ms": r, "error": "" if ok else "boom", "status": 200}
        for r in replies
    ]


SETTINGS = dict(sl.DEFAULTS)


def test_slow_reply_is_bad_only_past_both_the_factor_and_the_floor():
    history = _hist("a", [100, 100, 100, 100])
    slow = {"name": "a", "ok": True, "reply_p50_ms": 500.0, "error": "", "status": 200}
    assert sl.evaluate(slow, history, SETTINGS)[0] == "bad"
    mild = {**slow, "reply_p50_ms": 250.0}  # 2.5x but only 150 ms slower than the floor of 200
    assert sl.evaluate(mild, history, SETTINGS)[0] == "ok"


def test_no_baseline_means_no_latency_alert():
    slow = {"name": "a", "ok": True, "reply_p50_ms": 9000.0, "error": "", "status": 200}
    assert sl.evaluate(slow, _hist("a", [100, 100]), SETTINGS)[0] == "ok"


def test_failure_needs_to_repeat_before_it_is_bad():
    down = {
        "name": "a",
        "ok": False,
        "reply_p50_ms": None,
        "error": "connect: refused",
        "status": None,
    }
    assert sl.evaluate(down, _hist("a", [100, 100, 100]), SETTINGS)[0] == "pending"
    assert (
        sl.evaluate(down, _hist("a", [100, 100, 100]) + _hist("a", [0], ok=False), SETTINGS)[0]
        == "bad"
    )


def test_alerts_fire_on_transitions_only(tmp_path):
    path = tmp_path / "state.json"
    assert sl.update_alert_state(path, {"a": ("bad", "slow")}) == [("a", "open", "slow")]
    assert sl.update_alert_state(path, {"a": ("bad", "slower")}) == []
    assert (
        sl.update_alert_state(path, {"a": ("pending", "x")}) == []
    )  # undecided keeps the alert open
    assert sl.update_alert_state(path, {"a": ("ok", "")}) == [("a", "recovered", "")]
    assert sl.update_alert_state(path, {"a": ("ok", "")}) == []


def test_probe_run_appends_history_and_alerts_once(tmp_path, server):
    httpd, _ = server
    config = {"settings": {**SETTINGS, "samples": 2}, "services": [_service(httpd)], "notify": {}}
    first = sl.run_probe(tmp_path, config, now=1000.0)
    assert first["records"][0]["ok"] and first["events"] == []
    lines = sl.history_path(tmp_path).read_text().splitlines()
    assert json.loads(lines[0])["name"] == "svc"


def test_history_is_pruned_by_age(tmp_path):
    path = sl.history_path(tmp_path)
    sl.append_history(path, [{"ts": 1.0, "name": "old"}], keep_days=1, now=1.0)
    sl.append_history(path, [{"ts": 10 * 86400.0, "name": "new"}], keep_days=1, now=10 * 86400.0)
    assert [r["name"] for r in sl.load_history(path)] == ["new"]


def test_startup_counts_from_the_unit_start_and_reports_listening_before_ready():
    service = sl.Service(name="gate", port=1, unit="gate.service")
    ticks = iter([0.0, 0.0, 0.5, 0.5, 1.0, 1.0, 1.5, 1.5, 2.0, 2.0])
    answers = iter(
        [
            sl.Sample(None, None, None, "refused"),
            sl.Sample(1.0, None, None, "slow"),
            sl.Sample(1.0, 5.0, 302),
        ]
    )
    result = sl.measure_startup(
        service,
        clock=lambda: next(ticks),
        sleep=lambda _: None,
        probe=lambda *_: next(answers),
        age=lambda _: 20.0,
    )
    assert result["ready"] and result["since"] == "unit start" and not result["already_ready"]
    assert result["time_to_ready_s"] > result["time_to_listening_s"] >= 20.0


def test_startup_of_an_already_running_service_does_not_report_its_uptime_as_startup_time():
    result = sl.measure_startup(
        sl.Service(name="x", port=1, unit="x.service"),
        clock=lambda: 0.0,
        sleep=lambda _: None,
        probe=lambda *_: sl.Sample(1.0, 2.0, 200),
        age=lambda _: 2613.0,
    )
    assert (
        result["already_ready"]
        and result["time_to_ready_s"] is None
        and result["ready_within_s"] == 2613.0
    )


def test_startup_gives_up_at_the_timeout():
    ticks = iter(range(0, 1000))
    result = sl.measure_startup(
        sl.Service(name="x", port=1),
        timeout=3,
        clock=lambda: float(next(ticks)),
        sleep=lambda _: None,
        probe=lambda *_: sl.Sample(None, None, None, "refused"),
        age=lambda _: None,
    )
    assert result["ready"] is False and result["time_to_ready_s"] is None


def test_unit_age_is_none_off_linux_or_without_a_unit(monkeypatch):
    assert sl.unit_age_seconds("") is None
    monkeypatch.setattr(sl.sys, "platform", "win32")
    assert sl.unit_age_seconds("x.service") is None


def test_config_loads_registry_and_overrides_and_ignores_junk(tmp_path):
    registry = tmp_path / "services.toml"
    registry.write_text('[[services]]\nname = "a"\nport = 8001\n[[services]]\nname = "nop"\n')
    registry.with_name("services.local.toml").write_text('[[services]]\nname = "b"\nport = 8002\n')
    cfg = tmp_path / sl.CONFIG_NAME
    cfg.write_text(
        f'registry = "{registry}"\nfactor = -3\nsamples = "x"\n'
        '[[services]]\nname = "a"\nport = 9001\npath = "/x"\n[[services]]\nname = "bad"\nport = 0\n'
    )
    config = sl.load_config(cfg)
    by_name = {s.name: s for s in config["services"]}
    assert set(by_name) == {"a", "b"} and by_name["a"].port == 9001 and by_name["a"].path == "/x"
    assert (
        config["settings"]["factor"] == sl.DEFAULTS["factor"] and config["settings"]["samples"] == 5
    )


def test_alert_entries_are_shaped_for_the_notifier():
    entries = sl.alert_messages(
        [("a", "open", "reply 900 ms vs 100 ms baseline")], [{"name": "a", "reply_p50_ms": 900.0}]
    )
    assert entries[0]["id"] == "service-latency.a" and "900 ms" in entries[0]["suggestions"][0]


def _manifest(root: Path) -> Path:
    directory = root / "config" / "tachometer"
    directory.mkdir(parents=True)
    path = directory / "profile.toml"
    path.write_text(
        '[repo]\nname = "t"\ncategory = "util-repos"\nkind = "python"\n\n[paths]\ndisk_path = "."\n'
        'profile_path = ".tachometer/profile.json"\nsummary_path = ".tachometer/summary.json"\n\n'
        '[defaults]\nlabel = "x"\n'
    )
    return path


def test_cli_probe_and_startup(tmp_path, server, capsys):
    httpd, _ = server
    manifest = _manifest(tmp_path)
    (manifest.parent / sl.CONFIG_NAME).write_text(
        f'samples = 1\n[[services]]\nname = "svc"\nport = {httpd.server_address[1]}\n'
    )
    assert main(["service-latency", "probe", "--manifest", str(manifest)]) == 0
    assert json.loads(capsys.readouterr().out)["records"][0]["name"] == "svc"
    assert (
        main(
            [
                "service-latency",
                "startup",
                "--manifest",
                str(manifest),
                "--service",
                "svc",
                "--timeout",
                "5",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["ready"] is True
    with pytest.raises(SystemExit):
        main(["service-latency", "startup", "--manifest", str(manifest), "--service", "nope"])


def test_exclude_drops_registry_entries(tmp_path):
    registry = tmp_path / "services.toml"
    registry.write_text(
        '[[services]]\nname = "a"\nport = 8001\n[[services]]\nname = "rdp"\nport = 3389\n'
    )
    cfg = tmp_path / sl.CONFIG_NAME
    cfg.write_text(f'registry = "{registry}"\nexclude = ["rdp"]\n')
    assert [s.name for s in sl.load_config(cfg)["services"]] == ["a"]

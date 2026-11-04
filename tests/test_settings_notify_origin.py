"""Retention settings, red-light notifications, and the dashboard's request guards."""

from __future__ import annotations

import http.client
import subprocess
import threading
from http.server import HTTPServer

import pytest

from tachometer import notify, settings
from tachometer.server import (
    _accepted_origin_targets,
    _first_header_value,
    _Handler,
    _is_loopback_host,
    _same_origin_request,
    _validate_bind_host,
)


class TestSettings:
    def test_defaults_when_missing_or_unreadable(self, tmp_path):
        expected = {
            "retention": {"sample_days": 365, "run_days": 365, "backlog_resolved_days": 365}
        }
        assert settings.load_settings(tmp_path / "none.toml") == expected
        bad = tmp_path / "bad.toml"
        bad.write_text("this is = = not toml")
        assert settings.load_settings(bad) == expected

    def test_values_override_defaults_per_key(self, tmp_path):
        path = tmp_path / "s.toml"
        path.write_text("[retention]\nsample_days = 30\n")
        loaded = settings.load_settings(path)
        assert loaded["retention"] == {
            "sample_days": 30,
            "run_days": 365,
            "backlog_resolved_days": 365,
        }

    def test_a_section_that_is_not_a_table_falls_back_to_defaults(self, tmp_path):
        path = tmp_path / "s.toml"
        path.write_text("retention = 5\n")
        assert settings.load_settings(path)["retention"]["sample_days"] == 365

    def test_settings_path_is_beside_the_manifest(self, tmp_path):
        assert settings.settings_path(tmp_path / "config" / "manifest.toml") == (
            tmp_path / "config" / "settings.toml"
        )

    def test_save_then_load_round_trips_and_creates_parents(self, tmp_path):
        path = tmp_path / "a" / "b" / "settings.toml"
        data = {"retention": {"sample_days": 7, "run_days": 8, "backlog_resolved_days": 9}}
        settings.save_settings(path, data)
        assert settings.load_settings(path) == data

    def test_retention_reads_ints(self):
        assert settings.retention({"retention": {"sample_days": "30", "run_days": 2}}) == {
            "sample_days": 30,
            "run_days": 2,
            "backlog_resolved_days": 365,
        }
        assert settings.retention({}) == {
            "sample_days": 365,
            "run_days": 365,
            "backlog_resolved_days": 365,
        }

    @pytest.mark.parametrize(
        "bad", [0, -1, -365, "-5", "abc", None, "", float("nan"), float("inf"), [], {}]
    )
    def test_a_nonsensical_retention_never_means_delete_everything(self, bad):
        got = settings.retention(
            {"retention": {"sample_days": bad, "run_days": bad, "backlog_resolved_days": bad}}
        )
        assert got == {"sample_days": 365, "run_days": 365, "backlog_resolved_days": 365}

    def test_each_period_is_read_from_its_own_key(self):
        got = settings.retention(
            {"retention": {"sample_days": 3, "run_days": 5, "backlog_resolved_days": 7}}
        )
        assert got == {"sample_days": 3, "run_days": 5, "backlog_resolved_days": 7}

    def test_one_day_is_the_smallest_accepted_period(self):
        assert settings.retention({"retention": {"sample_days": 1}})["sample_days"] == 1
        assert settings.retention({"retention": {"sample_days": "1"}})["sample_days"] == 1

    def test_a_retention_section_that_is_not_a_table_is_ignored(self):
        assert settings.retention({"retention": 7})["sample_days"] == 365


ENTRY = {"id": "disk.used", "value": 91.23456, "suggestions": ["Free space."]}


class TestFormatAlert:
    def test_full_entry(self):
        assert notify.format_alert(ENTRY, repo_name="repo") == (
            "[tachometer] RED — repo: disk.used\nValue: 91.2\nFree space."
        )

    def test_the_first_suggestion_is_used(self):
        entry = {"id": "x", "value": 1, "suggestions": ["first", "second"]}
        assert notify.format_alert(entry).endswith("\nfirst")

    def test_defaults_for_missing_fields(self):
        text = notify.format_alert({"view": "host", "light_key": "cpu", "value": None})
        assert text == (
            "[tachometer] RED — host.cpu\nValue: n/a\n"
            "Investigate and reduce this resource's utilisation."
        )
        assert notify.format_alert({}).startswith("[tachometer] RED — ?.?")

    def test_value_formatting(self):
        assert notify._format_value(5) == "5"
        assert notify._format_value("high") == "high"
        assert notify._format_value(0.123456) == "0.123"


class TestSendAlert:
    def make_root(self, tmp_path, service="telegram"):
        script = tmp_path / notify._SERVICE_SCRIPTS[service]
        script.parent.mkdir(parents=True)
        script.write_text("")
        return tmp_path

    def test_runs_the_service_script_with_target_message_and_config(self, tmp_path, monkeypatch):
        root = self.make_root(tmp_path)
        seen = {}

        def fake_run(cmd, **kwargs):
            seen["cmd"], seen["kwargs"] = cmd, kwargs
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr(notify.subprocess, "run", fake_run)
        ok = notify.send_red_light_alert(
            ENTRY,
            shock_relay_root=root,
            service="telegram",
            target="12345",
            config_path="/cfg.yaml",
            repo_name="r",
        )
        assert ok is True
        cmd = seen["cmd"]
        assert cmd[1] == str(root / "services/telegram/send_message.py")
        assert cmd[2] == "12345" and cmd[3].startswith("[tachometer] RED — r: disk.used")
        assert cmd[4:] == ["--config", "/cfg.yaml"]
        assert seen["kwargs"]["timeout"] == 30 and seen["kwargs"]["capture_output"] is True
        assert seen["kwargs"]["env"]["PYTHONPATH"] == str(root / "services/telegram")

    def test_no_config_means_no_config_flag(self, tmp_path, monkeypatch):
        root = self.make_root(tmp_path)
        calls = []
        monkeypatch.setattr(
            notify.subprocess,
            "run",
            lambda cmd, **k: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0),
        )
        notify.send_red_light_alert(ENTRY, shock_relay_root=root, service="telegram", target="t")
        assert "--config" not in calls[0]

    def test_failures_are_reported_not_raised(self, tmp_path, monkeypatch):
        root = self.make_root(tmp_path)
        monkeypatch.setattr(
            notify.subprocess, "run", lambda cmd, **k: subprocess.CompletedProcess(cmd, 3)
        )
        assert (
            notify.send_red_light_alert(
                ENTRY, shock_relay_root=root, service="telegram", target="t"
            )
            is False
        )

        def boom(cmd, **k):
            raise subprocess.TimeoutExpired(cmd, 30)

        monkeypatch.setattr(notify.subprocess, "run", boom)
        assert (
            notify.send_red_light_alert(
                ENTRY, shock_relay_root=root, service="telegram", target="t"
            )
            is False
        )

        def missing(cmd, **k):
            raise PermissionError("not executable")

        monkeypatch.setattr(notify.subprocess, "run", missing)
        assert (
            notify.send_red_light_alert(
                ENTRY, shock_relay_root=root, service="telegram", target="t"
            )
            is False
        )

    def test_unknown_service_or_missing_script_sends_nothing(self, tmp_path, monkeypatch):
        calls = []
        monkeypatch.setattr(notify.subprocess, "run", lambda cmd, **k: calls.append(cmd))
        assert not notify.send_red_light_alert(
            ENTRY, shock_relay_root=tmp_path, service="carrier-pigeon", target="t"
        )
        assert not notify.send_red_light_alert(
            ENTRY, shock_relay_root=tmp_path, service="telegram", target="t"
        )
        assert calls == []

    def test_every_known_service_maps_to_a_script_path_under_services(self):
        assert set(notify._SERVICE_SCRIPTS) == {"telegram", "signal", "twilio", "whatsapp"}
        assert all(path.startswith("services/") for path in notify._SERVICE_SCRIPTS.values())

    def test_notify_sends_one_alert_per_entry_and_survives_failures(self, tmp_path, monkeypatch):
        sent = []

        def fake(entry, **kwargs):
            sent.append((entry["id"], kwargs["target"], kwargs["repo_name"]))
            return False

        monkeypatch.setattr(notify, "send_red_light_alert", fake)
        notify.notify_new_red_lights(
            [{"id": "a"}, {"id": "b"}],
            shock_relay_root=tmp_path,
            service="signal",
            target="T",
            repo_name="r",
        )
        assert sent == [("a", "T", "r"), ("b", "T", "r")]
        notify.notify_new_red_lights([], shock_relay_root=tmp_path, service="signal", target="T")


class TestOriginGuards:
    @pytest.mark.parametrize(
        "host", ["127.0.0.1", "[::1]", "::1", "localhost", "LOCALHOST", " 127.0.0.5 "]
    )
    def test_loopback_hosts(self, host):
        assert _is_loopback_host(host)
        _validate_bind_host(host, allow_remote=False)

    @pytest.mark.parametrize(
        "host",
        ["", "  ", "0.0.0.0", "::", "192.168.1.5", "example.com", "[::]", "localhost.evil.example"],
    )
    def test_non_loopback_hosts_need_an_explicit_opt_in(self, host):
        assert not _is_loopback_host(host)
        with pytest.raises(ValueError, match="--allow-remote"):
            _validate_bind_host(host, allow_remote=False)
        _validate_bind_host(host, allow_remote=True)

    def test_first_header_value(self):
        assert _first_header_value("a, b") == "a"
        assert _first_header_value("  a  ") == "a"
        assert _first_header_value(None) == ""

    def test_accepted_targets(self):
        assert _accepted_origin_targets({"Host": "h:1"}) == (("", "h:1"),)
        assert _accepted_origin_targets(
            {"Host": "h:1", "X-Forwarded-Host": "edge, other", "X-Forwarded-Proto": "https, http"}
        ) == (("", "h:1"), ("https", "edge"))
        assert _accepted_origin_targets({}) == ()

    def test_matching_origin_or_referer(self):
        host = {"Host": "h:1"}
        assert _same_origin_request({**host, "Origin": "http://h:1"})
        assert _same_origin_request({**host, "Referer": "http://h:1/page?x=1"})
        assert _same_origin_request({**host, "Origin": "https://h:1"})

    @pytest.mark.parametrize(
        "origin",
        [
            "http://evil.example",
            "http://h:2",
            "http://h",
            "http://h:1.evil.example",
            "h:1",
            "//h:1",
            "javascript:alert(1)",
        ],
    )
    def test_any_other_origin_is_rejected(self, origin):
        assert not _same_origin_request({"Host": "h:1", "Origin": origin})

    def test_origin_takes_precedence_over_referer(self):
        headers = {"Host": "h:1", "Origin": "http://evil.example", "Referer": "http://h:1/"}
        assert not _same_origin_request(headers)

    def test_forwarded_host_and_proto_are_honoured_together(self):
        headers = {
            "Host": "127.0.0.1:5100",
            "X-Forwarded-Host": "edge.example",
            "X-Forwarded-Proto": "https",
            "Origin": "https://edge.example",
        }
        assert _same_origin_request(headers)
        assert not _same_origin_request({**headers, "Origin": "http://edge.example"})

    def test_missing_or_null_origin_needs_the_browsers_same_origin_fetch_metadata(self):
        host = {"Host": "h:1"}
        assert not _same_origin_request(host)
        assert not _same_origin_request({**host, "Origin": "null"})
        assert _same_origin_request({**host, "Origin": "null", "Sec-Fetch-Site": "same-origin"})
        assert _same_origin_request({**host, "Sec-Fetch-Site": " same-origin "})
        for site in ("cross-site", "same-site", "none", ""):
            assert not _same_origin_request({**host, "Origin": "null", "Sec-Fetch-Site": site})

    def test_fetch_metadata_does_not_override_a_mismatching_origin(self):
        headers = {"Host": "h:1", "Origin": "http://evil.example", "Sec-Fetch-Site": "same-origin"}
        assert not _same_origin_request(headers)


@pytest.fixture
def dashboard(tmp_path):
    handler = type(
        "H",
        (_Handler,),
        {"tachometer_root": tmp_path, "host_summary_path": tmp_path / "h.json", "port": 0},
    )
    httpd = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    try:
        yield httpd, tmp_path
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)


def post(httpd, path, body="", headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=3)
    merged = {"Content-Type": "application/x-www-form-urlencoded", "Content-Length": str(len(body))}
    merged.update(headers or {})
    conn.request("POST", path, body=body, headers=merged)
    response = conn.getresponse()
    response.read()
    return response


class TestAdminSettingsPost:
    def same_origin(self, httpd):
        origin = f"http://127.0.0.1:{httpd.server_port}"
        return {"Origin": origin}

    def test_cross_origin_posts_are_forbidden_and_change_nothing(self, dashboard):
        httpd, root = dashboard
        for path in ("/admin/settings", "/api/run-all", "/thermal/limiters"):
            response = post(httpd, path, "sample_days=1", {"Origin": "http://evil.example"})
            assert response.status == 403, path
        assert not (root / "config" / "tachometer" / "settings.toml").exists()

    def test_saving_retention_clamps_to_at_least_one_day_and_redirects(self, dashboard):
        httpd, root = dashboard
        body = "sample_days=-30&run_days=0&backlog_resolved_days=abc"
        response = post(httpd, "/admin/settings", body, self.same_origin(httpd))
        assert response.status == 303 and response.getheader("Location") == "/admin?saved=1"
        saved = settings.load_settings(next(root.rglob("settings.toml")))["retention"]
        assert saved == {"sample_days": 1, "run_days": 1, "backlog_resolved_days": 365}

    def test_valid_values_are_saved_verbatim(self, dashboard):
        httpd, root = dashboard
        post(
            httpd,
            "/admin/settings",
            "sample_days=30&run_days=60&backlog_resolved_days=90",
            self.same_origin(httpd),
        )
        saved = settings.load_settings(next(root.rglob("settings.toml")))["retention"]
        assert saved == {"sample_days": 30, "run_days": 60, "backlog_resolved_days": 90}

    def test_unknown_post_paths_are_404(self, dashboard):
        httpd, _ = dashboard
        assert post(httpd, "/nope", "", self.same_origin(httpd)).status == 404

    def test_run_all_redirects_back_to_the_requested_view(self, dashboard, monkeypatch):
        httpd, _ = dashboard
        started = []
        monkeypatch.setattr(
            "tachometer.server._start_snapshot_run", lambda root: started.append(root)
        )
        response = post(httpd, "/api/run-all?view=host", "", self.same_origin(httpd))
        assert response.status == 303 and response.getheader("Location") == "/?view=host"
        assert len(started) == 1

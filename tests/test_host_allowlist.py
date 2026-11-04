"""The dashboard refuses requests whose Host header is not one it expects (DNS rebinding)."""

from __future__ import annotations

import http.client
import threading
from http.server import HTTPServer

import pytest

from tachometer import server
from tachometer.server import _Handler


class TestHostName:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("localhost", "localhost"),
            ("LocalHost:5100", "localhost"),
            ("127.0.0.1:5100", "127.0.0.1"),
            ("[::1]:5100", "::1"),
            ("[::1]", "::1"),
            ("edge.example:8443", "edge.example"),
            ("Edge.Example.", "edge.example"),
            (" a.b ", "a.b"),
            ("", ""),
            ("[::1", ""),
            (":5100", ""),
        ],
    )
    def test_extracts_a_lowercase_hostname_without_port(self, value, expected):
        assert server._host_name(value) == expected


class TestHostHeaderAllowed:
    allowed = frozenset({"tachometer.internal"})

    @pytest.mark.parametrize(
        "host", ["127.0.0.1:5100", "localhost:5100", "[::1]:5100", "127.0.0.9", "LOCALHOST"]
    )
    def test_loopback_names_are_always_allowed(self, host):
        assert server._host_header_allowed({"Host": host}, frozenset())

    def test_configured_names_are_allowed_with_any_port_and_case(self):
        for host in ("tachometer.internal", "Tachometer.Internal:8443", "tachometer.internal."):
            assert server._host_header_allowed({"Host": host}, self.allowed)

    @pytest.mark.parametrize(
        "host",
        [
            "evil.example",
            "evil.example:5100",
            "tachometer.internal.evil.example",
            "evil-tachometer.internal",
            "localhost.evil.example",
            "127.0.0.1.evil.example",
            "192.168.1.5:5100",
            "0.0.0.0:5100",
            "",
        ],
    )
    def test_everything_else_is_refused(self, host):
        assert not server._host_header_allowed({"Host": host}, self.allowed)

    def test_missing_host_is_refused(self):
        assert not server._host_header_allowed({}, self.allowed)

    def test_only_the_first_host_value_counts(self):
        assert not server._host_header_allowed({"Host": "evil.example, localhost"}, self.allowed)
        assert server._host_header_allowed({"Host": "localhost, evil.example"}, self.allowed)

    def test_a_forwarded_host_cannot_stand_in_for_a_missing_host(self):
        assert not server._host_header_allowed(
            {"X-Forwarded-Host": "tachometer.internal"}, self.allowed
        )
        assert not server._host_header_allowed(
            {"Host": "", "X-Forwarded-Host": "localhost"}, self.allowed
        )

    def test_a_forwarded_host_never_authorises_a_request(self):
        headers = {"Host": "evil.example", "X-Forwarded-Host": "tachometer.internal"}
        assert not server._host_header_allowed(headers, self.allowed)


class TestLoadAllowedHosts:
    def test_sources_are_merged_and_normalised(self, tmp_path, monkeypatch):
        config = tmp_path / "config" / "tachometer"
        config.mkdir(parents=True)
        (config / "server.local.toml").write_text(
            '[server]\nallowed_hosts = ["File.Internal", "b.internal:8443"]\n'
        )
        monkeypatch.setenv("TACHOMETER_ALLOWED_HOSTS", "env.internal, ,Other.Internal:1")
        got = server._load_allowed_hosts(tmp_path, ["cli.internal"])
        assert got == {
            "file.internal",
            "b.internal",
            "env.internal",
            "other.internal",
            "cli.internal",
        }

    def test_nothing_configured_is_empty(self, tmp_path, monkeypatch):
        monkeypatch.delenv("TACHOMETER_ALLOWED_HOSTS", raising=False)
        assert server._load_allowed_hosts(tmp_path, ()) == frozenset()

    def test_a_single_name_given_as_a_string_is_one_name_not_many_characters(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.delenv("TACHOMETER_ALLOWED_HOSTS", raising=False)
        config = tmp_path / "config" / "tachometer"
        config.mkdir(parents=True)
        (config / "server.local.toml").write_text('[server]\nallowed_hosts = "Only.Internal"\n')
        assert server._load_allowed_hosts(tmp_path, ()) == {"only.internal"}

    def test_a_table_is_not_a_list_of_names(self, tmp_path, monkeypatch):
        monkeypatch.delenv("TACHOMETER_ALLOWED_HOSTS", raising=False)
        config = tmp_path / "config" / "tachometer"
        config.mkdir(parents=True)
        (config / "server.local.toml").write_text("[server.allowed_hosts]\nsneaky = 1\n")
        assert server._load_allowed_hosts(tmp_path, ()) == frozenset()

    def test_non_string_entries_are_skipped_but_valid_ones_kept(self, tmp_path, monkeypatch):
        monkeypatch.delenv("TACHOMETER_ALLOWED_HOSTS", raising=False)
        config = tmp_path / "config" / "tachometer"
        config.mkdir(parents=True)
        (config / "server.local.toml").write_text(
            '[server]\nallowed_hosts = [1, "ok.internal", true]\n'
        )
        assert server._load_allowed_hosts(tmp_path, ()) == {"ok.internal"}

    @pytest.mark.parametrize(
        "text",
        [
            "not = = toml",
            "[server]\nallowed_hosts = 5\n",
            "server = 3\n",
            "[server]\nallowed_hosts = [1, 2]\n",
        ],
    )
    def test_a_malformed_local_file_adds_nothing_and_never_crashes(
        self, tmp_path, monkeypatch, text
    ):
        monkeypatch.delenv("TACHOMETER_ALLOWED_HOSTS", raising=False)
        config = tmp_path / "config" / "tachometer"
        config.mkdir(parents=True)
        (config / "server.local.toml").write_text(text)
        assert server._load_allowed_hosts(tmp_path, ()) == frozenset()


def request(httpd, method, path, host, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=3)
    conn.putrequest(method, path, skip_host=True)
    if host is not None:
        conn.putheader("Host", host)
    for key, value in (headers or {}).items():
        conn.putheader(key, value)
    conn.putheader("Content-Length", "0")
    conn.endheaders()
    response = conn.getresponse()
    response.read()
    return response


def start(tmp_path, **attrs):
    handler = type(
        "H",
        (_Handler,),
        {"tachometer_root": tmp_path, "host_summary_path": tmp_path / "h.json", "port": 0, **attrs},
    )
    httpd = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    ).start()
    return httpd


@pytest.fixture
def make_server(tmp_path):
    servers = []

    def make(**attrs):
        httpd = start(tmp_path, **attrs)
        servers.append(httpd)
        return httpd

    yield make
    for httpd in servers:
        httpd.shutdown()
        httpd.server_close()


class TestHandlerEnforcement:
    def test_a_rebinding_request_is_refused_before_anything_is_served_or_changed(
        self, make_server, monkeypatch
    ):
        httpd = make_server(allowed_hosts=frozenset())
        started = []
        monkeypatch.setattr(server, "_start_snapshot_run", lambda root: started.append(root))
        rebind = f"evil.example:{httpd.server_port}"
        origin = {"Origin": f"http://{rebind}"}
        assert request(httpd, "GET", "/api/live", rebind).status == 403
        assert request(httpd, "GET", "/", rebind).status == 403
        assert request(httpd, "POST", "/api/run-all", rebind, origin).status == 403
        assert request(httpd, "POST", "/admin/settings", rebind, origin).status == 403
        assert started == []

    def test_loopback_hosts_work_as_before(self, make_server):
        httpd = make_server(allowed_hosts=frozenset())
        host = f"127.0.0.1:{httpd.server_port}"
        assert request(httpd, "GET", "/api/live", host).status == 200
        assert request(httpd, "GET", "/api/live", f"localhost:{httpd.server_port}").status == 200

    def test_the_edge_name_works_once_configured(self, make_server):
        httpd = make_server(allowed_hosts=frozenset({"tachometer.internal"}))
        assert request(httpd, "GET", "/api/live", "tachometer.internal").status == 200
        assert request(httpd, "GET", "/api/live", "other.internal").status == 403

    def test_enforcement_can_be_off_only_when_the_class_says_so(self, make_server):
        httpd = make_server(allowed_hosts=frozenset(), enforce_host=False)
        assert request(httpd, "GET", "/api/live", "anything.example").status == 200

    def test_the_default_is_to_enforce(self):
        assert _Handler.enforce_host is True
        assert _Handler.allowed_hosts == frozenset()

    def test_the_refusal_names_the_problem_without_echoing_the_header(self, make_server):
        httpd = make_server(allowed_hosts=frozenset())
        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=3)
        conn.putrequest("GET", "/api/live", skip_host=True)
        conn.putheader("Host", "<script>x</script>.example")
        conn.endheaders()
        response = conn.getresponse()
        body = response.read().decode()
        assert response.status == 403 and "Host" in body and "<script>" not in body


class TestServeWiring:
    def run_serve(self, monkeypatch, tmp_path, **kwargs):
        captured = {}

        class FakeHttpd:
            def __init__(self, address, handler):
                captured["handler"] = handler

            def serve_forever(self):
                raise KeyboardInterrupt

        monkeypatch.setattr(server, "HTTPServer", FakeHttpd)
        monkeypatch.delenv("TACHOMETER_ALLOWED_HOSTS", raising=False)
        server.serve(tmp_path, **kwargs)
        return captured["handler"]

    def test_loopback_server_enforces_with_configured_names(self, monkeypatch, tmp_path):
        handler = self.run_serve(monkeypatch, tmp_path, allowed_hosts=("Edge.Internal",))
        assert handler.enforce_host is True
        assert handler.allowed_hosts == frozenset({"edge.internal"})

    def test_remote_server_without_names_cannot_know_its_own_host(self, monkeypatch, tmp_path):
        handler = self.run_serve(monkeypatch, tmp_path, host="0.0.0.0", allow_remote=True)
        assert handler.enforce_host is False

    def test_remote_server_with_names_enforces(self, monkeypatch, tmp_path):
        handler = self.run_serve(
            monkeypatch, tmp_path, host="0.0.0.0", allow_remote=True, allowed_hosts=("lan.example",)
        )
        assert handler.enforce_host is True
        assert handler.allowed_hosts == frozenset({"lan.example"})


class TestRegisteredHostname:
    """The site's own hostname comes from the portfolio store, so no local file is needed."""

    @pytest.fixture
    def registered(self, tmp_path, monkeypatch):
        entry = tmp_path / "owner__repo"
        entry.mkdir()
        (entry / "site.local.toml").write_text('[site]\nhostname = "Dash.Example.Test"\n')
        monkeypatch.setenv("PORTFOLIO_SITE_DIR", str(entry))
        monkeypatch.setenv("PORTFOLIO_STORE", str(tmp_path))
        monkeypatch.delenv("TACHOMETER_ALLOWED_HOSTS", raising=False)

    def test_the_registered_name_is_part_of_the_allowed_set(self, registered, tmp_path):
        assert server._load_allowed_hosts(tmp_path, ()) == {"dash.example.test"}

    def test_a_request_for_the_registered_name_is_served_without_any_local_config(
        self, registered, make_server
    ):
        httpd = make_server(allowed_hosts=frozenset())
        assert request(httpd, "GET", "/api/live", "dash.example.test").status == 200
        assert request(httpd, "GET", "/api/live", "Dash.Example.Test:443").status == 200
        assert request(httpd, "GET", "/api/live", "evil.example").status == 403

    def test_a_remote_bind_is_enforced_once_the_site_has_a_name(
        self, registered, monkeypatch, tmp_path
    ):
        captured = {}

        class FakeHttpd:
            def __init__(self, address, handler):
                captured["handler"] = handler

            def serve_forever(self):
                raise KeyboardInterrupt

        monkeypatch.setattr(server, "HTTPServer", FakeHttpd)
        server.serve(tmp_path, host="0.0.0.0", allow_remote=True)
        assert captured["handler"].enforce_host is True
        assert captured["handler"].allowed_hosts == {"dash.example.test"}

    def test_the_seam_decides_host_parsing_and_matching(self):
        from tachometer.portfolio import site

        assert server._host_name("Edge.Example.:443") == site.host_name("Edge.Example.:443")
        assert server._host_header_allowed({"Host": "a.test"}, frozenset({"a.test"}))
        assert not server._host_header_allowed({"Host": "b.test"}, frozenset({"a.test"}))

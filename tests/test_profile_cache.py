"""The read-only profile summaries reuse a parse until the file changes."""

from __future__ import annotations

import json

from tachometer import profile


def test_read_profile_document_reparses_only_when_the_file_changes(tmp_path, monkeypatch):
    path = tmp_path / "profile.json"
    path.write_text(json.dumps({"samples": [{"timestamp": 1}], "runs": []}), encoding="utf-8")
    profile._read_cache.clear()
    loads = []
    real = json.loads
    monkeypatch.setattr(profile.json, "loads", lambda text: loads.append(1) or real(text))

    first = profile._read_profile_document(path)
    assert profile._read_profile_document(path) is first
    assert len(loads) == 1

    path.write_text(json.dumps({"samples": [], "runs": [{"started_at": 2}]}), encoding="utf-8")
    assert profile._read_profile_document(path)["runs"] == [{"started_at": 2}]
    assert len(loads) == 2


def test_read_profile_document_of_a_missing_file_is_empty(tmp_path):
    assert profile._read_profile_document(tmp_path / "none.json") == {"samples": [], "runs": []}


def test_writers_keep_their_own_copy(tmp_path):
    path = tmp_path / "profile.json"
    profile._read_cache.clear()
    profile.append_profile_sample(path, {"timestamp": 1})
    assert profile._read_profile_document(path)["samples"] == [{"timestamp": 1}]
    profile.append_profile_sample(path, {"timestamp": 2})
    assert len(profile._read_profile_document(path)["samples"]) == 2

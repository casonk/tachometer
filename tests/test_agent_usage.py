from __future__ import annotations

import json
from pathlib import Path

from tachometer.agent_usage import collect_agent_utilization


def test_collect_agent_utilization_reads_all_supported_providers(tmp_path: Path, monkeypatch):
    claude_root = tmp_path / ".claude"
    claude_root.mkdir(parents=True)
    (claude_root / "stats-cache.json").write_text(
        json.dumps(
            {
                "lastComputedDate": "2026-04-20",
                "dailyModelTokens": [
                    {
                        "date": "2026-04-20",
                        "tokensByModel": {
                            "claude-sonnet-4-6": 420_000,
                            "claude-haiku-4-5-20251001": 12_000,
                        },
                    }
                ],
                "modelUsage": {
                    "claude-sonnet-4-6": {
                        "inputTokens": 100,
                        "outputTokens": 200,
                    }
                },
                "totalSessions": 11,
                "totalMessages": 99,
            }
        ),
        encoding="utf-8",
    )

    codex_session = tmp_path / ".codex" / "sessions" / "2026" / "04" / "20"
    codex_session.mkdir(parents=True)
    (codex_session / "rollout.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "turn_context",
                        "payload": {"model": "gpt-5.4"},
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-04-20T01:30:39.258Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "token_count",
                            "info": {
                                "total_token_usage": {
                                    "input_tokens": 1_090_724,
                                    "cached_input_tokens": 1_003_904,
                                    "output_tokens": 13_268,
                                    "reasoning_output_tokens": 9_048,
                                    "total_tokens": 1_103_992,
                                },
                                "last_token_usage": {
                                    "input_tokens": 99_233,
                                    "cached_input_tokens": 99_072,
                                    "output_tokens": 2_912,
                                    "reasoning_output_tokens": 2_303,
                                    "total_tokens": 102_145,
                                },
                            },
                            "rate_limits": {
                                "plan_type": "plus",
                                "primary": {"used_percent": 4.0},
                                "secondary": {"used_percent": 71.0},
                            },
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    copilot_root = tmp_path / ".copilot" / "session-state" / "session-1"
    copilot_root.mkdir(parents=True)
    (copilot_root / "events.jsonl").write_text(
        json.dumps(
            {
                "type": "session.shutdown",
                "timestamp": "2026-04-20T02:00:00.000Z",
                "data": {
                    "totalPremiumRequests": 3,
                    "currentModel": "claude-sonnet-4.6",
                    "modelMetrics": {
                        "claude-sonnet-4.6": {
                            "requests": {"count": 19, "cost": 3},
                            "usage": {
                                "inputTokens": 567_268,
                                "outputTokens": 4_781,
                                "cacheReadTokens": 483_005,
                                "cacheWriteTokens": 0,
                            },
                        }
                    },
                    "currentTokens": 37_680,
                    "systemTokens": 7_849,
                    "conversationTokens": 17_935,
                    "toolDefinitionsTokens": 11_892,
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "tachometer.agent_usage.shutil.which",
        lambda name: f"/usr/bin/{name}" if name in {"claude", "codex", "copilot"} else None,
    )

    def fake_run(command, capture_output, text, timeout, check):
        assert capture_output is True
        assert text is True
        assert timeout == 10
        assert check is False
        executable = command[0]
        if executable == "claude":
            return type(
                "Proc",
                (),
                {
                    "returncode": 0,
                    "stdout": json.dumps(
                        {
                            "loggedIn": True,
                            "subscriptionType": "pro",
                            "apiProvider": "firstParty",
                            "orgName": "Example Org",
                        }
                    ),
                    "stderr": "",
                },
            )()
        if executable == "codex":
            return type(
                "Proc",
                (),
                {
                    "returncode": 0,
                    "stdout": "Logged in using ChatGPT\n",
                    "stderr": "",
                },
            )()
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr("tachometer.agent_usage.subprocess.run", fake_run)

    snapshot = collect_agent_utilization(home=tmp_path)

    assert snapshot["overall_light"] == "yellow"
    assert snapshot["providers"]["claude"]["status"] == "usage_available"
    assert snapshot["providers"]["claude"]["summary"] == "432.0k on 2026-04-20"
    assert snapshot["providers"]["codex"]["light"] == "yellow"
    assert snapshot["providers"]["codex"]["details"]["model"] == "gpt-5.4"
    assert snapshot["providers"]["copilot"]["status"] == "usage_available"
    assert snapshot["providers"]["copilot"]["details"]["total_premium_requests"] == 3


def test_collect_agent_utilization_marks_copilot_as_awaiting_session(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "tachometer.agent_usage.shutil.which",
        lambda name: f"/usr/bin/{name}" if name == "copilot" else None,
    )

    snapshot = collect_agent_utilization(home=tmp_path)

    assert snapshot["providers"]["copilot"]["status"] == "awaiting_session"
    assert "open a session" in snapshot["providers"]["copilot"]["summary"]

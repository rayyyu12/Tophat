"""Rabbit service loop: retry arming and the loud give-up (rabbit/rabbit.py)."""

from datetime import datetime

from rabbit import rabbit


def test_arm_retry_gives_up_loudly(monkeypatch, tmp_path):
    monkeypatch.setattr(rabbit, "LOG_FILE", tmp_path / "rabbit.log")
    pushed = []
    monkeypatch.setattr(rabbit, "push_status",
                        lambda cfg, status: pushed.append(status))
    state = {"retry_count": 2, "retry_next_at": "2026-07-09T23:55:00",
             "last_pull_error": "409: today's copier plan has 3 unapplied line(s)"}
    rabbit._arm_retry({"max_retries": 2}, state, datetime(2026, 7, 9, 23, 55))
    # bookkeeping reset for tomorrow
    assert state["retry_count"] == 0
    assert "retry_next_at" not in state and "last_pull_error" not in state
    # ... and the operator hears about it (TopHat fans this to Discord)
    assert pushed and pushed[0]["result"] == "gave-up"
    assert "unapplied" in pushed[0]["detail"]
    assert "yesterday's mapping" in pushed[0]["detail"]


def test_arm_retry_below_cap_stays_quiet(monkeypatch, tmp_path):
    monkeypatch.setattr(rabbit, "LOG_FILE", tmp_path / "rabbit.log")
    pushed = []
    monkeypatch.setattr(rabbit, "push_status",
                        lambda cfg, status: pushed.append(status))
    state = {"retry_count": 0}
    rabbit._arm_retry({"max_retries": 8, "retry_every_min": 15}, state,
                      datetime(2026, 7, 9, 22, 40))
    assert state["retry_count"] == 1 and state["retry_next_at"]
    assert not pushed


def test_poll_flag_carries_apply_schedule(monkeypatch):
    """The heartbeat tells TopHat when the nightly apply fires (box-config
    fact), URL-encoded; the Operations page shows it."""
    seen = {}

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"sync_requested": false}'

    def fake_request(cfg, path, payload=None):
        seen["path"] = path
        return FakeResp()

    monkeypatch.setattr(rabbit, "_request", fake_request)
    assert rabbit.poll_flag({"apply_at": "22:00"}) == {"sync_requested": False}
    assert seen["path"] == "/api/ops/tc-poll?apply_at=22%3A00"
    rabbit.poll_flag({})   # default schedule still reported
    assert seen["path"] == "/api/ops/tc-poll?apply_at=22%3A30"

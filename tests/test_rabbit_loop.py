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


def test_pull_failure_still_pushes_observed(monkeypatch, tmp_path):
    """A refused pull must not blind the server (2026-07-16 lockout: zero
    mirrors -> every pull 409s -> the observe that would surface replacement
    accounts never ran). full_cycle now observes on the abort path too."""
    monkeypatch.setattr(rabbit, "LOG_FILE", tmp_path / "rabbit.log")
    monkeypatch.setattr(rabbit, "STATE_FILE", tmp_path / "rabbit_state.json")
    monkeypatch.setattr(rabbit, "pull_desired",
                        lambda cfg: (None, "409: no active mirror accounts"))
    observed = []
    monkeypatch.setattr(rabbit, "push_observed",
                        lambda cfg: observed.append(cfg) or "observed 3: booked 0, stale 0, proposed 3")
    pushed = []
    monkeypatch.setattr(rabbit, "push_status",
                        lambda cfg, status: pushed.append(status))

    assert rabbit.full_cycle({}, None, "manual") is None
    assert len(observed) == 1
    # the operator's abort banner carries the observe line, not just the 409
    assert pushed and pushed[0]["result"] == "aborted"
    assert "proposed 3" in pushed[0]["detail"]

    # scheduled path: observe still runs, error remembered for the give-up alert
    assert rabbit.full_cycle({}, None, "scheduled") is None
    assert len(observed) == 2
    assert rabbit.load_state()["last_pull_error"] == "409: no active mirror accounts"


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

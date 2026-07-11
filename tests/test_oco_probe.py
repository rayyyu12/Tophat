"""Nightly Auto-OCO probe (tophat/services/oco_probe.py).

The probe places a far-OTM bracketed limit per enabled account at
oco_probe_time (default 22:00 ET, Sun-Thu) and reads the error-2 rejection as
"Auto OCO Brackets is OFF". Detection must land in the registry + Discord;
passing accounts must clear a stale flag; disabled/terminal accounts are
never probed; the alert stays quiet when everything passes.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from tophat.broker.base import BrokerAccount
from tophat.broker.mock import MockBroker
from tophat.server.service import BrokerHandle
from tophat.services import oco_probe
from tophat.services.oco_probe import load_stamp, probe_due, run_probe, save_stamp
from tophat.store.config import load_settings, save_settings
from tophat.store.registry import load_registry, save_registry

ET = ZoneInfo("America/New_York")
WED_NIGHT = datetime(2026, 7, 8, 22, 0, 30, tzinfo=ET)


class OffBroker(MockBroker):
    """Every account reports Auto OCO OFF."""
    def check_oco_bracket_support(self, account_id, contract_id):
        return "off", "error 2: ... You must enable Auto OCO Brackets."


def _webhook(monkeypatch):
    s = load_settings()
    s.discord_webhook_url = "https://discord.com/api/webhooks/1/x"
    save_settings(s)
    posts = []
    monkeypatch.setattr("tophat.services.notify.post_discord",
                        lambda url, title, **kw: posts.append((title, kw)))
    monkeypatch.setattr(oco_probe, "_PAUSE_BETWEEN_ACCOUNTS_S", 0.0)
    return posts


def test_probe_due_gating():
    assert probe_due(WED_NIGHT, "22:00", "")                       # Wed night, due
    assert not probe_due(WED_NIGHT, "22:00", "2026-07-08")         # already ran
    assert not probe_due(WED_NIGHT, "", "")                        # disabled
    early = WED_NIGHT.replace(hour=21, minute=59)
    assert not probe_due(early, "22:00", "")                       # not yet
    fri = datetime(2026, 7, 10, 22, 0, tzinfo=ET)                  # Fri: closed
    assert not probe_due(fri, "22:00", "")
    sun = datetime(2026, 7, 12, 22, 0, tzinfo=ET)                  # Sun: open
    assert probe_due(sun, "22:00", "")


def test_off_accounts_flagged_and_alerted(monkeypatch):
    posts = _webhook(monkeypatch)
    b = OffBroker(n_eval=2, n_funded=0, seed=1)
    ids = [a.account_id for a in b._accounts]
    out = run_probe([BrokerHandle("o", b, "mock")], now_et=WED_NIGHT)
    assert [r["account_id"] for r in out["off"]] == ids
    reg = load_registry()
    assert all(reg.entry(a).oco_blocked_on == "2026-07-08" for a in ids)
    assert len(posts) == 1 and "OCO" in posts[0][0]
    assert out["posted"] is True


def test_passing_probe_clears_stale_flag_and_stays_quiet(monkeypatch):
    posts = _webhook(monkeypatch)
    b = MockBroker(n_eval=1, n_funded=0, seed=2)     # mock reports "on"
    aid = b._accounts[0].account_id
    reg = load_registry()
    reg.entry(aid).oco_blocked_on = "2026-07-07"     # stale flag from a past fail
    save_registry(reg)
    out = run_probe([BrokerHandle("o", b, "mock")], now_et=WED_NIGHT)
    assert out["off"] == [] and out["errors"] == []
    assert load_registry().entry(aid).oco_blocked_on == ""
    assert posts == []                                # quiet when all pass


def test_disabled_accounts_are_not_probed(monkeypatch):
    _webhook(monkeypatch)
    probed = []

    class Recording(MockBroker):
        def check_oco_bracket_support(self, account_id, contract_id):
            probed.append(account_id)
            return "on", ""

    b = Recording(n_eval=2, n_funded=0, seed=3)
    a1, a2 = (a.account_id for a in b._accounts)
    reg = load_registry()
    reg.entry(a1).enabled = False
    reg.entry(a2).enabled = True
    save_registry(reg)
    out = run_probe([BrokerHandle("o", b, "mock")], now_et=WED_NIGHT)
    assert probed == [a2]
    assert out["checked"] == 1


def test_copy_leaders_are_never_probed(monkeypatch):
    # Tradecopia replicates a leader's orders to followers — a probe order on a
    # leader could strand a resting copy on an API-less follower account.
    _webhook(monkeypatch)
    probed = []

    class Recording(MockBroker):
        def check_oco_bracket_support(self, account_id, contract_id):
            probed.append(account_id)
            return "on", ""

    b = Recording(n_eval=2, n_funded=1, seed=6)
    a1, a2, a3 = (a.account_id for a in b._accounts)
    reg = load_registry()
    reg.entry(a1).signal_plan = "apex-flip"          # signal channel leader
    save_registry(reg)
    from tophat.store.mirrors import MirrorAccount, save_mirrors
    save_mirrors({"m1": MirrorAccount(mirror_id="m1", firm="lucid-50k",
                                      leader_id=a2, enabled=True)})
    run_probe([BrokerHandle("o", b, "mock")], now_et=WED_NIGHT)
    assert probed == [a3]                            # only the non-leader


def test_dead_by_balance_accounts_are_not_probed(monkeypatch):
    # Topstep sometimes keeps canTrade=true on an account whose balance already
    # sits at/below its trailing floor (the "invalid/dirty" leftovers). The
    # dashboard shows them inactive; the probe must skip them too (2026-07-09:
    # four such 50KTC accounts were probed and alerted as "OCO off").
    _webhook(monkeypatch)
    probed = []

    class Recording(MockBroker):
        def check_oco_bracket_support(self, account_id, contract_id):
            probed.append(account_id)
            return "on", ""

    b = Recording(n_eval=0, n_funded=0, seed=5)
    b._accounts.append(BrokerAccount(9_100_001, "50KTC-V2-DEAD", 47_700.0,
                                     can_trade=True, simulated=True))
    b._accounts.append(BrokerAccount(9_100_002, "50KTC-V2-LIVE", 50_500.0,
                                     can_trade=True, simulated=True))
    from tophat.store.states import get_or_create, load_all, merge_save
    states = load_all()
    get_or_create(states, 9_100_001)   # default eval state: floor = 48,000
    get_or_create(states, 9_100_002)
    merge_save(states, [9_100_001, 9_100_002])
    out = run_probe([BrokerHandle("o", b, "mock")], now_et=WED_NIGHT)
    assert probed == [9_100_002]                      # dead-by-balance skipped
    assert out["checked"] == 1


def test_practice_accounts_are_not_probed(monkeypatch):
    _webhook(monkeypatch)
    probed = []

    class Recording(MockBroker):
        def check_oco_bracket_support(self, account_id, contract_id):
            probed.append(account_id)
            return "on", ""

    b = Recording(n_eval=1, n_funded=0, seed=5)
    a1 = b._accounts[0].account_id
    b._accounts.append(BrokerAccount(9_100_003, "PRAC-77", 50_000.0,
                                     can_trade=True, simulated=True))
    run_probe([BrokerHandle("o", b, "mock")], now_et=WED_NIGHT)
    assert probed == [a1]                             # practice never probed


def test_probe_stamp_survives_restart():
    # The stamp is what stops a restarted server from re-probing (and
    # re-alerting) a night it already covered.
    assert load_stamp() == ""
    save_stamp("2026-07-08")
    assert load_stamp() == "2026-07-08"
    assert not probe_due(WED_NIGHT, "22:00", load_stamp())


def test_market_closed_reports_once_not_per_account(monkeypatch):
    posts = _webhook(monkeypatch)

    class Closed(MockBroker):
        def check_oco_bracket_support(self, account_id, contract_id):
            return "error", "no recent bars - market closed?"

    b = Closed(n_eval=3, n_funded=0, seed=4)
    out = run_probe([BrokerHandle("o", b, "mock")], now_et=WED_NIGHT)
    assert len(out["errors"]) == 1                    # one line, not three
    assert len(posts) == 1                            # amber "couldn't check"

"""Exporter: mirrors store -> tc_desired_state payload (tophat/services/tc_export.py)."""

import pytest

from tophat.services.tc_export import desired_from_mirrors
from tophat.store import mirrors as MS

NAMES = {1: "PRAC-V2-1", 2: "EXPRESS-2"}


def test_desired_groups_from_mapped_mirrors():
    MS.create_mirror("apex-50k", account_number="APEX-111", leader_id=1)
    MS.create_mirror("apex-50k", account_number="APEX-222", leader_id=1,
                     phase="funded")
    MS.create_mirror("lucid-50k", account_number="LUCID-9", leader_id=2,
                     multiplier=0.8)
    MS.create_mirror("tradeify-50k", account_number="TR-1")          # unmapped
    MS.create_mirror("apex-50k", account_number="APEX-333", leader_id=1,
                     phase="blown")                                  # terminal
    m = MS.create_mirror("apex-50k", account_number="APEX-444", leader_id=1)
    MS.patch_mirror(m.mirror_id, {"enabled": False}, today="2026-07-08")

    out = desired_from_mirrors(MS.load_mirrors(), NAMES, 0, "2026-07-08")
    assert out["version"] == 2 and out["plan_date"] == "2026-07-08"
    assert out["tenant"] == 1                    # conftest binds uid=1
    groups = {g["leader"]: g["followers"] for g in out["groups"]}
    assert set(groups) == {"PRAC-V2-1", "EXPRESS-2"}
    assert [f["account"] for f in groups["PRAC-V2-1"]] == ["APEX-111", "APEX-222"]
    assert groups["EXPRESS-2"] == [{"account": "LUCID-9", "scale": 0.8,
                                    "replicate": True, "contract_type": "Standard"}]


def test_refuses_while_plan_unapplied():
    with pytest.raises(ValueError, match="unapplied"):
        desired_from_mirrors({}, NAMES, 3, "2026-07-08")


def test_refuses_on_missing_or_duplicate_account_numbers():
    MS.create_mirror("apex-50k", leader_id=1)                # no account number
    with pytest.raises(ValueError, match="no account number"):
        desired_from_mirrors(MS.load_mirrors(), NAMES, 0, "2026-07-08")
    MS.delete_mirror("apex-01")
    MS.create_mirror("apex-50k", account_number="X-1", leader_id=1)
    MS.create_mirror("lucid-50k", account_number="X-1", leader_id=1)
    with pytest.raises(ValueError, match="two mirrors"):
        desired_from_mirrors(MS.load_mirrors(), NAMES, 0, "2026-07-08")


def test_refuses_on_unresolvable_leader():
    MS.create_mirror("apex-50k", account_number="A-1", leader_id=777)
    with pytest.raises(ValueError, match="cannot resolve leader"):
        desired_from_mirrors(MS.load_mirrors(), NAMES, 0, "2026-07-08")


def test_no_mirrors_means_idle_not_empty_world():
    # Topstep-only stretch: refuse so a running Rabbit never churns the app
    with pytest.raises(ValueError, match="copy trading is idle"):
        desired_from_mirrors({}, {}, 0, "2026-07-08")
    # all-terminal fleet counts as idle too
    MS.create_mirror("apex-50k", account_number="A-1", phase="blown")
    with pytest.raises(ValueError, match="copy trading is idle"):
        desired_from_mirrors(MS.load_mirrors(), NAMES, 0, "2026-07-08")


def test_deliberate_unmap_all_still_exports_empty_groups():
    # mirrors EXIST but none mapped - that is a real desired state (clear all)
    MS.create_mirror("apex-50k", account_number="A-1")
    out = desired_from_mirrors(MS.load_mirrors(), NAMES, 0, "2026-07-08")
    assert out["groups"] == []

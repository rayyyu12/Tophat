"""Firm profile invariants — the research-locked numbers must stay self-consistent."""

import pytest

from tophat.store.firms import APEX, FIRMS, FOLLOWER_FIRMS, LUCID, TOPSTEP, TRADEIFY, get_firm

# Commission/slippage conventions: realistic NQ round trip ~$7/mini; the old
# research figure of ~$20/mini is kept as the conservative bound
# (docs/STATS_AUDIT_2026-07-06.md §5/§7).
FLIP_GROSS_CLONE = 170.0     # topstep/lucid/tradeify flip target
FLIP_GROSS_APEX = 285.0      # apex-native flip target (14.25pt, re-locked 2026-07-06)
ONE_MINI_COST = 7.0
ONE_MINI_COST_CONSERVATIVE = 20.0


def test_registry_complete_and_keyed():
    assert set(FIRMS) == {"topstep-50k", "lucid-50k", "tradeify-50k", "apex-50k"}
    for key, p in FIRMS.items():
        assert p.key == key
    assert set(FOLLOWER_FIRMS) == set(FIRMS) - {"topstep-50k"}


def test_get_firm_unknown_raises():
    with pytest.raises(KeyError):
        get_firm("ftmo-100k")


def test_stop_is_half_trailing_everywhere():
    # $1,000 stop = half the shared $2,000 MLL => whole number of lives at every firm
    for p in FIRMS.values():
        assert p.trailing == 2_000.0
        if p.dll is not None:
            assert p.dll >= 1_000.0   # the self-imposed $1,000 stop fits inside every DLL


def test_clone_flip_qualifies_at_clone_firms():
    # $170 gross flip must meet the $150 win-day bar even at the conservative
    # commission bound (it lands exactly on it — the locked calibration)
    for p in (TOPSTEP, LUCID, TRADEIFY):
        assert FLIP_GROSS_CLONE - ONE_MINI_COST > p.win_day_min
        assert FLIP_GROSS_CLONE - ONE_MINI_COST_CONSERVATIVE >= p.win_day_min


def test_apex_flip_qualifies_but_250_gross_does_not():
    # $285 flip: >= $25 buffer (5 ticks) at realistic commissions, and still
    # above the bar even at the conservative $20 bound
    assert FLIP_GROSS_APEX - ONE_MINI_COST >= APEX.win_day_min + 25
    assert FLIP_GROSS_APEX - ONE_MINI_COST_CONSERVATIVE >= APEX.win_day_min
    # the landmine the sims found: a $250-gross flip nets under the bar
    assert 250.0 - ONE_MINI_COST < APEX.win_day_min


def test_consistency_implies_min_days():
    # 50% rule => at least 2 winning days; 40% => at least 3
    assert TOPSTEP.eval_consistency == 0.50 and TOPSTEP.eval_min_days >= 2
    assert LUCID.eval_consistency == 0.50 and LUCID.eval_min_days >= 2
    assert TRADEIFY.eval_consistency == 0.40 and TRADEIFY.eval_min_days >= 3
    assert APEX.eval_consistency is None and APEX.eval_min_days == 1


def test_tradeify_scale_beats_consistency_math():
    # at 0.8x the $1,200 best day needs only total >= 3,000 (not 3,750)
    day = 1_500.0 * TRADEIFY.copier_scale_eval
    assert day / TRADEIFY.eval_consistency <= TRADEIFY.eval_target


def test_lucid_caps():
    assert LUCID.max_total == 10
    assert LUCID.max_funded == 5
    assert LUCID.eval_pipeline_target <= LUCID.max_total - 3


def test_apex_gate_parameters():
    assert APEX.payout_style == "apex-gate"
    assert APEX.apex_min_balance == 2_600.0
    assert 0 < APEX.payout_request <= APEX.payout_cap
    assert APEX.max_funded == 20

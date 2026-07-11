from tophat.engine import AccountConfig, AccountState, Phase
from tophat.services.status import infer_phase_from_name, lifecycle_label, sync_phase_from_name


def test_infer_phase_from_name():
    assert infer_phase_from_name("50KTC-V2-123456") == Phase.EVAL
    assert infer_phase_from_name("TOPSTEP EXPRESS-XFA-789") == Phase.FUNDED
    assert infer_phase_from_name("unknown") == Phase.EVAL


def test_sync_phase_from_name_fixes_mislabeled_funded():
    cfg = AccountConfig()
    st = AccountState(phase=Phase.EVAL, base_balance=50_000)
    changed = sync_phase_from_name(st, "EXPRESS-XFA-1", cfg)
    assert changed is True
    assert st.phase == Phase.FUNDED
    assert st.base_balance == cfg.funded_initial_balance


def test_sync_phase_from_name_skips_terminal():
    cfg = AccountConfig()
    st = AccountState(phase=Phase.PASSED, base_balance=50_000)
    assert sync_phase_from_name(st, "EXPRESS-XFA-1", cfg) is False
    assert st.phase == Phase.PASSED


def test_lifecycle_label_inactive_eval():
    cfg = AccountConfig()
    st = AccountState(phase=Phase.EVAL, days_traded=0)
    assert lifecycle_label(cfg, st, can_trade=False) == "inactive - can't trade"


def test_flip_label_counts_flips_not_winning_days():
    """Nuke cycle: the landed nuke banked winning day #1, so the cycle needs
    only 4 flips - the label numbers the flip being attempted out of 4."""
    cfg = AccountConfig()   # winning_days_required=5
    st = AccountState(phase=Phase.FUNDED, payouts_taken=0,   # nuke cycle
                      nuke_hit_this_cycle=True, winning_days_this_cycle=1)
    assert lifecycle_label(cfg, st) == "flip 1/4 (payout 1)"
    st.winning_days_this_cycle = 2   # first flip landed
    assert lifecycle_label(cfg, st) == "flip 2/4 (payout 1)"
    st.winning_days_this_cycle = 4   # attempting the last flip
    assert lifecycle_label(cfg, st) == "flip 4/4 (payout 1)"


def test_flip_label_flip_only_cycle_needs_all_five():
    """Odd payouts_taken = flip-only cycle: all 5 winning days come from
    flips, numbered attempt-style from 1/5."""
    cfg = AccountConfig()
    st = AccountState(phase=Phase.FUNDED, payouts_taken=1,
                      winning_days_this_cycle=0)
    assert lifecycle_label(cfg, st) == "flip 1/5 (payout 2)"
    st.winning_days_this_cycle = 3
    assert lifecycle_label(cfg, st) == "flip 4/5 (payout 2)"

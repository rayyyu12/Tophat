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

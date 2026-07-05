"""Firm rule profiles — the per-prop-firm accounting rules for follower (mirror)
accounts and their tickets.

The engine's AccountConfig already parameterizes the *strategy* (brackets, targets);
a FirmProfile parameterizes the *accounting* a firm applies to the resulting trades:
consistency rules, qualifying-day minimums, payout gates, and purchase caps. Values
are research-locked (docs/MULTI_FIRM_PLAN.md §2-3, operator-verified 2026-07-02) and
ship as code constants — there is deliberately no JSON overlay, so a stale data file
can't silently diverge from the simulated rules.

Key subtlety encoded here: qualifying/win-day minimums compare against the day's
BOOKED (net) P&L. A $250-gross Apex flip nets ~$230 and never qualifies — hence the
locked $325 flip target (nets ~$305).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FirmProfile:
    key: str
    label: str
    ticket_cost: float
    activation_cost: float          # one-time fee when an eval passes (0 = all-in ticket)
    dll: float | None               # None = firm has no daily loss limit
    trailing: float                 # trailing max-loss (MLL); binding constraint everywhere
    eval_target: float
    eval_consistency: float | None  # best day <= x * total profit at pass (None = no rule)
    eval_min_days: int
    win_day_min: float              # NET day P&L required for a qualifying/winning day
    winning_days_required: int = 5  # qualifying days needed per payout window
    payout_style: str = "half-profit-cap"   # or "apex-gate"
    payout_cap: float = 2_000.0
    # copier / fleet management
    copier_scale_eval: float = 1.0  # follower multiplier during eval (1.0 = 1:1)
    max_funded: int = 5
    max_total: int | None = None    # evals + funded cap (Lucid); None = uncapped
    eval_pipeline_target: int = 10  # standing eval count the replenisher maintains
    # apex-gate parameters (zero/unused for half-profit-cap firms)
    apex_min_balance: float = 0.0   # profit balance required to request a payout
    apex_consistency: float = 0.0   # best window day <= x * window profit
    payout_request: float = 0.0     # locked request size (sim-optimal, not the cap)

    @property
    def is_leader_firm(self) -> bool:
        return self.key == TOPSTEP.key


TOPSTEP = FirmProfile(
    # $85 all-in: Topstep has NO funded-activation fee (operator-verified 2026-07-04).
    key="topstep-50k", label="Topstep", ticket_cost=85.0, activation_cost=0.0,
    dll=1_000.0, trailing=2_000.0,
    eval_target=3_000.0, eval_consistency=0.50, eval_min_days=2,
    win_day_min=150.0, payout_style="half-profit-cap", payout_cap=2_000.0,
    eval_pipeline_target=10,
)

LUCID = FirmProfile(
    key="lucid-50k", label="Lucid", ticket_cost=98.0, activation_cost=0.0,
    dll=1_200.0, trailing=2_000.0,
    eval_target=3_000.0, eval_consistency=0.50, eval_min_days=2,
    win_day_min=150.0, payout_style="half-profit-cap", payout_cap=2_000.0,
    max_total=10, eval_pipeline_target=7,   # min(7, 10 - funded) applied by the solver
)

TRADEIFY = FirmProfile(
    key="tradeify-50k", label="Tradeify", ticket_cost=99.0, activation_cost=0.0,
    dll=None, trailing=2_000.0,
    eval_target=3_000.0, eval_consistency=0.40, eval_min_days=3,
    win_day_min=150.0, payout_style="half-profit-cap", payout_cap=2_000.0,
    copier_scale_eval=0.8,                  # 4 minis: $1,200 days beat the 40% rule
    eval_pipeline_target=10,
)

APEX = FirmProfile(
    key="apex-50k", label="Apex", ticket_cost=39.0, activation_cost=139.0,
    dll=1_000.0, trailing=2_000.0,
    eval_target=3_000.0, eval_consistency=None, eval_min_days=1,
    win_day_min=250.0, payout_style="apex-gate", payout_cap=2_000.0,
    max_funded=20, eval_pipeline_target=10,  # bought in 10-eval cohorts
    apex_min_balance=2_600.0, apex_consistency=0.50, payout_request=1_500.0,
)

FIRMS: dict[str, FirmProfile] = {p.key: p for p in (TOPSTEP, LUCID, TRADEIFY, APEX)}

# Follower firms only (mirror accounts can never be created for the leader firm).
FOLLOWER_FIRMS: dict[str, FirmProfile] = {
    k: p for k, p in FIRMS.items() if not p.is_leader_firm
}


def get_firm(key: str) -> FirmProfile:
    if key not in FIRMS:
        raise KeyError(f"unknown firm profile {key!r} (known: {sorted(FIRMS)})")
    return FIRMS[key]

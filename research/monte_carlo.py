"""
Monte Carlo model of the TopHat NQ pipeline (Topstep 50k, nuke lifecycle).

WHAT THIS IS
------------
This does NOT trade. It plays out the full eval -> funded -> withdrawal pipeline
thousands of times using weighted coin flips, then reports the distribution of
outcomes. Rules are path-dependent (today changes what's allowed tomorrow), so
we measure by simulation instead of algebra.

THE FUNDED LIFECYCLE (per the user's plan; avoids the live-account move)
------------------------------------------------------------------------
Topstep moves an account to live after ~5 payouts, so we harvest 4 and retire:
    payout 1 : NUKE  + 4 flips   (5 winning days)
    payout 2 :         5 flips
    payout 3 : NUKE  + 4 flips   (re-nuke, only after 2 payouts are banked)
    payout 4 :         5 flips    -> retire
A nuke is one trade for the whole target, risking the per-day stop. The re-nuke
is a deliberate gamble taken only once profit is already secured: if it misses
the account dies, but the first two payouts are already withdrawn.

THE ONLY ASSUMPTIONS ARE THE WIN PROBABILITIES
----------------------------------------------
With ZERO edge (driftless), P(hit +target before -stop) = stop / (target + stop).
The funded per-trade stop is the room we risk: the DLL ($1,000) with a DLL, or
the full trailing room ($2,000) without one. The MEASURED drive-momentum edge
(backtest.py) is added as percentage-points over these baselines. The nuke edge
is bracket-specific: DLL nuke (1:4 try) +4.0pp, no-DLL nuke (2:1 shot) +2.7pp;
eval +7.6pp, flip +2.2pp.

Run:
    python monte_carlo.py            # 4-scenario compare: DLL/no-DLL x drive/coin
    python monte_carlo.py --payouts 2
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass

import numpy as np

# Measured drive-momentum edge in percentage-points over the no-edge baseline
# (backtest.py, next-bar fills). The nuke edge is bracket-specific: the DLL nuke
# is a 1:4 try (measured +4.0pp) vs the no-DLL 2:1 shot (+2.7pp). The no-DLL flip
# bracket (7.5/100) is not yet backtested -> its +2pp is still an assumption.
DRIVE_PP_DLL = {"eval": 0.076, "flip": 0.022, "nuke": 0.040}
DRIVE_PP_NODLL = {"eval": 0.076, "flip": 0.020, "nuke": 0.027}


@dataclass
class Rules:
    point_value: float = 20.0
    initial_balance: float = 50_000.0
    dll: float = 1_000.0
    trailing_drawdown: float = 2_000.0

    # Eval
    eval_cost: float = 85.0
    eval_target_pts: float = 15.5      # used only to derive the no-edge win prob
    eval_stop_pts: float = 9.5
    eval_win_dollars: float = 1_500.0  # consistency-capped counted win
    eval_loss_dollars: float = 1_000.0
    eval_pass_profit: float = 3_000.0
    eval_min_days: int = 2
    eval_max_days: int = 30

    # Funded
    nuke_target: float = 4_000.0
    flip_target_dollars: float = 150.0
    winning_days_required: int = 5     # per payout
    withdrawal_fraction: float = 0.50
    payout_cap: float = 2_000.0        # max per request on the 50k
    payouts_target: int = 4            # harvest 4 payouts then retire (live cap is 5)
    funded_horizon_days: int = 120     # safety cap; the payout target retires first

    # DLL ON  -> daily loss capped at `dll`; a losing day is survivable (next-day
    #            second chance) until the EOD trailing floor is hit.
    # DLL OFF -> no daily cap; "risk the full room" -> a losing day kills the
    #            account in one shot, and the nuke gets only one try.
    has_dll: bool = True
    funded_stop_override: float | None = None

    @property
    def funded_day_stop(self) -> float:
        if self.funded_stop_override is not None:
            return self.funded_stop_override
        return self.dll if self.has_dll else self.trailing_drawdown

    @property
    def nuke_attempts(self) -> int:
        return max(1, int(self.trailing_drawdown // self.funded_day_stop))


@dataclass
class Edge:
    """Win probabilities. None => no-edge driftless baseline (barrier ratio)."""

    eval_win: float | None = None
    nuke: float | None = None
    flip: float | None = None

    def resolved(self, r: Rules) -> "Edge":
        stop = r.funded_day_stop
        return Edge(
            eval_win=self.eval_win if self.eval_win is not None
            else r.eval_stop_pts / (r.eval_target_pts + r.eval_stop_pts),
            nuke=self.nuke if self.nuke is not None
            else stop / (r.nuke_target + stop),          # per-attempt
            flip=self.flip if self.flip is not None
            else stop / (r.flip_target_dollars + stop),
        )


@dataclass
class FundedResult:
    withdrawn: float = 0.0
    payouts: int = 0
    nukes_hit: int = 0
    busted: bool = False


def _floor(initial: float, peak_eod: float, trailing: float) -> float:
    """EOD trailing drawdown floor, locked at the initial balance once earned."""
    return min(initial, peak_eod - trailing)


def _clip(x: float) -> float:
    return min(0.999, max(0.001, x))


def simulate_eval(rng: np.random.Generator, r: Rules, e: Edge) -> bool:
    equity = r.initial_balance
    peak = equity
    days = 0
    while days < r.eval_max_days:
        days += 1
        if rng.random() < e.eval_win:
            equity += r.eval_win_dollars
        else:
            equity -= r.eval_loss_dollars
        peak = max(peak, equity)
        if equity <= _floor(r.initial_balance, peak, r.trailing_drawdown):
            return False
        if (equity - r.initial_balance) >= r.eval_pass_profit and days >= r.eval_min_days:
            return True
    return False


def simulate_funded(rng: np.random.Generator, r: Rules, e: Edge) -> FundedResult:
    """Nuke lifecycle: nuke on even payout-cycles (1st, 3rd), flip on the others,
    harvest `payouts_target` payouts, then retire."""
    equity = r.initial_balance
    peak = equity
    res = FundedResult()
    days = 0

    def busted() -> bool:
        return equity <= _floor(r.initial_balance, peak, r.trailing_drawdown)

    while res.payouts < r.payouts_target and days < r.funded_horizon_days:
        winning_days = 0

        # nuke on the 1st and 3rd payout cycles (cycle index = payouts banked)
        if res.payouts % 2 == 0:
            hit = False
            for _ in range(r.nuke_attempts):
                days += 1
                if days >= r.funded_horizon_days:
                    return res
                if rng.random() < e.nuke:
                    equity += r.nuke_target
                    peak = max(peak, equity)
                    res.nukes_hit += 1
                    winning_days = 1
                    hit = True
                    break
                equity -= r.funded_day_stop
                peak = max(peak, equity)
                if busted():
                    res.busted = True
                    return res
            if not hit:
                res.busted = True
                return res

        while winning_days < r.winning_days_required:
            days += 1
            if days >= r.funded_horizon_days:
                return res
            if rng.random() < e.flip:
                equity += r.flip_target_dollars
                winning_days += 1
            else:
                equity -= r.funded_day_stop
            peak = max(peak, equity)
            if busted():
                res.busted = True
                return res

        profit = equity - r.initial_balance
        if profit <= 0:
            break
        take = min(r.withdrawal_fraction * profit, r.payout_cap)
        res.withdrawn += take
        equity -= take
        res.payouts += 1

    return res


def simulate_purchased_account(rng, r, e) -> float:
    """Net cash from buying ONE eval account: -cost (+ withdrawals if funded)."""
    if simulate_eval(rng, r, e):
        return simulate_funded(rng, r, e).withdrawn - r.eval_cost
    return -r.eval_cost


def simulate_campaign(rng, r, e, bankroll0, rounds, batch_size):
    """Run batches sequentially on a finite bankroll. Returns (final, ruined)."""
    bankroll = bankroll0
    batch_cost = r.eval_cost * batch_size
    ruined = False
    for _ in range(rounds):
        if bankroll < batch_cost:
            ruined = True
            break
        bankroll -= batch_cost
        for _ in range(batch_size):
            if simulate_eval(rng, r, e):
                bankroll += simulate_funded(rng, r, e).withdrawn
    return bankroll, ruined


def build_rules_edge(no_dll: bool, use_edge: bool, args):
    rules = Rules()
    if no_dll:
        rules.has_dll = False
        rules.eval_cost += 10.0
    rules.payouts_target = args.payouts
    if args.flip_loss is not None:
        rules.funded_stop_override = args.flip_loss
    e_in = Edge()
    if use_edge:
        pp = DRIVE_PP_NODLL if no_dll else DRIVE_PP_DLL
        base = Edge().resolved(rules)
        e_in = Edge(eval_win=_clip(base.eval_win + pp["eval"]),
                    nuke=_clip(base.nuke + pp["nuke"]),
                    flip=_clip(base.flip + pp["flip"]))
    return rules, e_in.resolved(rules)


def run_compare(args) -> None:
    rng = np.random.default_rng(args.seed)
    bs = args.batch_size
    nf = 20_000
    rows = []

    print("=" * 92)
    print(f"TOPHAT 50k NUKE LIFECYCLE  |  harvest {args.payouts} payouts then retire")
    print(f"budget ${args.bankroll:,.0f}, batch {bs} accts, {args.rounds} rounds")
    print("lifecycle: nuke+4flips, 5flips, RE-NUKE+4flips, 5flips  (nuke on cycles 1 & 3)")
    print("=" * 92)

    for no_dll in (False, True):
        for use_edge in (False, True):
            r, e = build_rules_edge(no_dll, use_edge, args)
            dll_lbl = "no-DLL" if no_dll else "DLL"
            edge_lbl = "drive" if use_edge else "coin"
            tag = f"{dll_lbl}/{edge_lbl}"

            passEV = np.mean([simulate_eval(rng, r, e) for _ in range(nf)])
            fr = [simulate_funded(rng, r, e) for _ in range(nf)]
            w = np.array([x.withdrawn for x in fr])
            payouts = np.array([x.payouts for x in fr])
            nukes = np.array([x.nukes_hit for x in fr])

            nets = np.array([
                simulate_purchased_account(rng, r, e)
                for _ in range(args.batches * bs)
            ]).reshape(args.batches, bs).sum(axis=1)
            roi = nets.mean() / (r.eval_cost * bs)
            pprof = np.mean(nets > 0)

            finals, ruins = [], 0
            for _ in range(args.campaign_trials):
                f, ruined = simulate_campaign(
                    rng, r, e, args.bankroll, args.rounds, bs)
                finals.append(f)
                ruins += ruined
            finals = np.array(finals)

            print(f"\n--- {tag} ---  stop ${r.funded_day_stop:,.0f} | "
                  f"nuke tries {r.nuke_attempts} | "
                  f"probs: eval {e.eval_win:.0%}  nuke/try {e.nuke:.0%}  flip {e.flip:.0%}")
            print(f"  EVAL  pass {passEV:.1%}/acct  ({passEV*bs:.2f} of {bs}-batch)")
            print(f"  FUNDED/acct: mean withdrawn ${w.mean():,.0f} | "
                  f"mean payouts {payouts.mean():.2f} | "
                  f"1st nuke {np.mean(nukes>=1):.0%} | re-nuke landed {np.mean(nukes>=2):.0%}")
            print("    reach payout: " + "  ".join(
                f"P{k} {np.mean(payouts>=k):.0%}" for k in range(1, args.payouts + 1)))
            print(f"  BATCH ({bs}x${r.eval_cost:.0f}=${r.eval_cost*bs:,.0f}): "
                  f"mean ${nets.mean():+,.0f} | ROI {roi:+.0%} | P(profit) {pprof:.1%}")
            print(f"  CAMPAIGN: P(ruin) {ruins/args.campaign_trials:.1%} | "
                  f"median end ${np.median(finals):+,.0f} | "
                  f"P(end>start) {np.mean(finals>args.bankroll):.1%}")

            rows.append((tag, passEV, w.mean(), payouts.mean(), roi, pprof,
                         ruins/args.campaign_trials, np.median(finals),
                         np.mean(finals > args.bankroll)))

    print("\n" + "=" * 92)
    print("SUMMARY")
    print(f"{'scenario':>12} | {'passEV':>6} {'$/acct':>7} {'pyts':>4} | "
          f"{'ROI':>6} {'P(prof)':>7} | {'P(ruin)':>7} {'medEnd':>9} {'P>start':>7}")
    print("-" * 92)
    for (tag, pe, wa, po, roi, pp, ru, me, ps) in rows:
        print(f"{tag:>12} | {pe:>6.1%} ${wa:>6,.0f} {po:>4.2f} | "
              f"{roi:>+5.0%} {pp:>7.1%} | {ru:>7.1%} ${me:>+8,.0f} {ps:>7.1%}")
    print("=" * 92)


def main() -> None:
    p = argparse.ArgumentParser(description="TopHat 50k nuke-lifecycle Monte Carlo")
    p.add_argument("--payouts", type=int, default=4,
                   help="payouts to harvest before retiring (live-account cap is 5)")
    p.add_argument("--batch-size", type=int, default=5)
    p.add_argument("--batches", type=int, default=3_000)
    p.add_argument("--bankroll", type=float, default=1_000.0)
    p.add_argument("--rounds", type=int, default=12)
    p.add_argument("--campaign-trials", type=int, default=2_000)
    p.add_argument("--flip-loss", type=float, default=None,
                   help="override $ risked per funded trade (default = the room)")
    p.add_argument("--seed", type=int, default=7)
    args = p.parse_args()
    run_compare(args)


if __name__ == "__main__":
    main()

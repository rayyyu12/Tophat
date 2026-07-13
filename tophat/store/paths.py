import os
from pathlib import Path

# Anchor to the project root (parent of the `tophat` package) so the data dir is the
# SAME regardless of the current working directory — the CLI and the app must agree.
# Override with TOPHAT_DATA_DIR (used by tests and for a server's persistent volume).
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.getenv("TOPHAT_DATA_DIR", _PROJECT_ROOT / "data"))
STATES_FILE = DATA_DIR / "account_states.json"
REGISTRY_FILE = DATA_DIR / "account_registry.json"
SETTINGS_FILE = DATA_DIR / "settings.json"
SCHEDULE_FILE = DATA_DIR / "schedule_state.json"
CREDENTIALS_FILE = DATA_DIR / "credentials.json"
AUTH_DB = DATA_DIR / "tophat.db"
SECRET_FILE = DATA_DIR / ".secret"
MIRRORS_FILE = DATA_DIR / "mirrors.json"
COPIER_PLANS_DIR = DATA_DIR / "copier_plans"
TRADE_LOG_FILE = DATA_DIR / "trade_log.jsonl"
# Write-through {account_id: broker name} cache, refreshed by build_snapshot so
# offline tools (deploy/watchdog.py recap) can label accounts without auth.
ACCOUNT_NAMES_FILE = DATA_DIR / "account_names.json"
# Nightly OCO probe bookkeeping: the last completed probe night, persisted so a
# server restart after the probe can't re-probe (and re-alert) the same night.
OCO_PROBE_FILE = DATA_DIR / "oco_probe_state.json"
# Per-tenant daily-notification stamps (premarket/recap already sent today), so
# a mid-morning deploy restart can't double-post to the user's Discord.
NOTIFY_STATE_FILE = DATA_DIR / "notify_state.json"

# Copier-box (TopHat Rabbit) pairing: the token registry is GLOBAL — a bearer
# token must resolve to its user before any tenant context exists. The last
# apply status a box reported is per-tenant.
COPIER_BOXES_FILE = DATA_DIR / "copier_boxes.json"
TC_STATUS_FILE = DATA_DIR / "tc_apply_status.json"
# Last reverse-sync snapshot a box reported (observed balances + import results).
TC_OBSERVED_FILE = DATA_DIR / "tc_observed.json"

# Simulation page: templates + stored runs live with runtime state; the bar
# cache is TICK-derived 1-second bars (built by export_sim_ticks.py from the
# NinjaTrader tick db). It stays under research/ and the app degrades
# gracefully - no cache means the Simulation page shows no data, by design.
SIM_TEMPLATES_FILE = DATA_DIR / "sim_templates.json"
SIM_RUNS_DIR = DATA_DIR / "sim_runs"
SIM_BARS_FILE = Path(os.getenv(
    "TOPHAT_SIM_BARS",
    _PROJECT_ROOT / "research" / "reconstruction" / "sim_ticks_rth_1s.parquet"))

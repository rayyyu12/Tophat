import os
from pathlib import Path

DATA_DIR = Path(os.getenv("TOPHAT_DATA_DIR", "data"))
STATES_FILE = DATA_DIR / "account_states.json"
REGISTRY_FILE = DATA_DIR / "account_registry.json"
SETTINGS_FILE = DATA_DIR / "settings.json"
SCHEDULE_FILE = DATA_DIR / "schedule_state.json"
AUTH_DB = DATA_DIR / "tophat.db"
SECRET_FILE = DATA_DIR / ".secret"

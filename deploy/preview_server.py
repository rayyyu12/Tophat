"""Isolated mock TopHat server for UI preview / demos.

Runs the full app against the MOCK broker in a throwaway data dir on port 8899 —
safe to run alongside the production server (no shared data dir, no single-instance
clash, no debug log, no live API keys). Login: preview@x.com / preview.

    .venv\\Scripts\\python.exe deploy\\preview_server.py
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ["TOPHAT_DATA_DIR"] = tempfile.mkdtemp(prefix="tophat-preview-")
os.environ.pop("TOPHAT_BROKER", None)          # mock broker
os.environ["TOPHAT_DEBUG_LOG"] = "0"
os.environ["TOPHAT_SINGLE_INSTANCE"] = "0"
os.environ["TOPHAT_SNAPSHOT_TTL"] = "2"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tophat.server import auth                  # noqa: E402  (after env setup)
auth.create_user("preview@x.com", "preview")

import uvicorn                                  # noqa: E402
from tophat.server.app import app               # noqa: E402

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8899, log_level="warning")

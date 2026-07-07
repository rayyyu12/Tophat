"""Saved simulation templates + their stored runs (JSON files).

A template is a named parameter set; its latest run result is persisted
alongside (summary inline in the template file, full result in
data/sim_runs/<id>.json) so revisiting a template shows the stored results
without re-running (docs/SIMULATION_PLAN.md)."""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

from tophat.store import tenant
from tophat.store.atomic import atomic_write_text
from tophat.store.paths import SIM_RUNS_DIR, SIM_TEMPLATES_FILE

_IO_LOCK = threading.Lock()


def _load(path: Path) -> dict:
    if not path.exists():
        return {"templates": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def _write(raw: dict, path: Path) -> None:
    atomic_write_text(path, json.dumps(raw, indent=2))


def list_templates(path: Path | None = None) -> list[dict]:
    path = tenant.resolve(SIM_TEMPLATES_FILE) if path is None else path
    raw = _load(path)
    out = list(raw["templates"].values())
    out.sort(key=lambda t: t.get("created_at", 0.0))
    return out


def _slug(name: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "template"
    slug, n = base, 2
    while slug in taken:
        slug = f"{base}-{n}"
        n += 1
    return slug


def save_template(name: str, params: dict, *, summary: dict | None = None,
                  path: Path | None = None) -> dict:
    path = tenant.resolve(SIM_TEMPLATES_FILE) if path is None else path
    name = str(name).strip()
    if not name:
        raise ValueError("template name required")
    with _IO_LOCK:
        raw = _load(path)
        # same name overwrites (update-in-place is the natural save behavior)
        existing = next((t for t in raw["templates"].values()
                         if t["name"].lower() == name.lower()), None)
        if existing is None:
            tid = _slug(name, set(raw["templates"]))
            t = {"id": tid, "name": name, "params": params,
                 "created_at": time.time(), "last_run_at": None, "summary": None}
            raw["templates"][tid] = t
        else:
            t = existing
            t["params"] = params
        if summary is not None:
            t["summary"] = summary
            t["last_run_at"] = time.time()
        _write(raw, path)
        return t


def attach_run(template_id: str, result: dict, *,
               path: Path | None = None,
               runs_dir: Path | None = None) -> dict | None:
    """Store a full run result for a template and refresh its inline summary."""
    path = tenant.resolve(SIM_TEMPLATES_FILE) if path is None else path
    runs_dir = tenant.resolve(SIM_RUNS_DIR) if runs_dir is None else runs_dir
    with _IO_LOCK:
        raw = _load(path)
        t = raw["templates"].get(template_id)
        if t is None:
            return None
        runs_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_text(runs_dir / f"{template_id}.json",
                          json.dumps(result, indent=2))
        t["summary"] = {
            "success": result.get("success"),
            "net_median": (result.get("net") or {}).get("median"),
            "blown": (result.get("probs") or {}).get("blown"),
            "n_paths": (result.get("params") or {}).get("n_paths"),
        }
        t["last_run_at"] = time.time()
        _write(raw, path)
        return t


def load_run(template_id: str, runs_dir: Path | None = None) -> dict | None:
    runs_dir = tenant.resolve(SIM_RUNS_DIR) if runs_dir is None else runs_dir
    p = runs_dir / f"{template_id}.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def delete_template(template_id: str, *, path: Path | None = None,
                    runs_dir: Path | None = None) -> bool:
    path = tenant.resolve(SIM_TEMPLATES_FILE) if path is None else path
    runs_dir = tenant.resolve(SIM_RUNS_DIR) if runs_dir is None else runs_dir
    with _IO_LOCK:
        raw = _load(path)
        if template_id not in raw["templates"]:
            return False
        del raw["templates"][template_id]
        _write(raw, path)
        run = runs_dir / f"{template_id}.json"
        if run.exists():
            run.unlink()
        return True

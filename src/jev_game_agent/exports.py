"""Compatibility artifacts are derived snapshots of the SQLite ledger."""
import json
from pathlib import Path
from uuid import uuid4

from jev_game_agent.store import Store


def atomic_json(path: Path, value) -> None:
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def export_run(store: Store, run_id: str) -> None:
    # A short write transaction prevents concurrent exporters publishing an older snapshot.
    with store.transaction(run_id) as db:
        state = store.load(db)
        phase = state["phase"]
        status = "complete" if phase == "COMPLETE" else "stopped" if state["closed"] else "blocked" if phase == "BLOCKED" else "active"
        events = [{"kind": row[0], "data": json.loads(row[1]), "at": row[2]} for row in db.execute("SELECT kind,data,created_at FROM events ORDER BY seq")]
        verified = [event for event in events if event["kind"] == "visual_verification"]
        snapshot = {
            "derived_from": "runtime.sqlite3", "run_id": run_id, "updated_at": state["updated_at"],
            "status": status, "phase": phase, "context_version": state["version"], "goal": state["task"],
            "session": {"url": state["task"]["url"], "game_id": state["task"]["game_id"]},
            "pending_action": state["pending"], "verified_progress": verified,
            "completion_evidence": state["completion_evidence"], "last_error": state["last_error"],
            "resume_required": state["resume_required"], "context_summary": state.get("context_summary", {}),
        }
        directory = store.directory(run_id)
        atomic_json(directory / "state.json", snapshot)
        if state["closed"] or phase == "COMPLETE" or db.execute("SELECT 1 FROM events WHERE kind='run_finished' LIMIT 1").fetchone():
            atomic_json(directory / "report.json", {
                "stage": "P2", "source": "runtime.sqlite3", "state": snapshot,
                "metrics": store.metrics(db), "events": events,
                "limitations": ["P3 host workflow migration is not implemented"],
            })

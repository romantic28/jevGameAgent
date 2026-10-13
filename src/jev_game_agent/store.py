"""One SQLite ledger per run. JSON exports never participate in recovery."""
import json
import re
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def encode(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


class StoreError(Exception):
    pass


class Store:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def directory(self, run_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", run_id):
            raise StoreError("Invalid run ID")
        directory = (self.root / run_id).resolve()
        if not directory.is_relative_to(self.root):
            raise StoreError("Run path leaves the configured root")
        return directory

    def path(self, run_id: str) -> Path:
        return self.directory(run_id) / "runtime.sqlite3"

    def create(self, task: dict, model: str, owner: str) -> dict:
        run_id = uuid4().hex
        self.directory(run_id).mkdir()
        with closing(sqlite3.connect(self.path(run_id))) as db, db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.executescript("""
                CREATE TABLE run (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL);
                CREATE TABLE submissions (
                    id TEXT PRIMARY KEY, input_hash TEXT NOT NULL, request TEXT NOT NULL,
                    phase TEXT NOT NULL, result TEXT, decision_id TEXT, created_at TEXT NOT NULL
                );
                CREATE TABLE observations (
                    id TEXT PRIMARY KEY, version INTEGER UNIQUE NOT NULL,
                    screenshot_ref TEXT UNIQUE NOT NULL, data TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE decisions (
                    id TEXT PRIMARY KEY, observation_id TEXT NOT NULL,
                    context_version INTEGER NOT NULL, request TEXT NOT NULL, response TEXT,
                    status TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
                    data TEXT NOT NULL, created_at TEXT NOT NULL
                );
                PRAGMA user_version=1;
            """)
            state = {
                "run_id": run_id, "task": task, "phase": "NEED_OBSERVATION", "version": 0,
                "owner": owner, "requested_model": model, "pending": None, "inflight": None,
                "observation_id": None, "last_error": None, "completion_evidence": [],
                "resume_required": False, "unresponsive": {}, "no_progress": 0,
                "analysis_reviewed": {},
                "binding_identity": None,
                "created_at": now(), "updated_at": now(), "closed": False,
            }
            self.save(db, state)
            self.event(db, "run_started", {"task": task, "requested_model": model})
        return state

    def open(self, run_id: str, readonly: bool = False) -> sqlite3.Connection:
        path = self.path(run_id)
        if not path.is_file():
            raise StoreError("Run ledger not found")
        db = sqlite3.connect(path.as_uri() + ("?mode=ro" if readonly else "?mode=rw"), uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        if db.execute("PRAGMA user_version").fetchone()[0] != 1:
            db.close()
            raise StoreError("Unsupported ledger schema version")
        return db

    @contextmanager
    def transaction(self, run_id: str):
        db = self.open(run_id)
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def load(db: sqlite3.Connection) -> dict:
        row = db.execute("SELECT data FROM run WHERE id=1").fetchone()
        if row is None:
            raise StoreError("Run state missing")
        return json.loads(row[0])

    @staticmethod
    def save(db: sqlite3.Connection, state: dict) -> None:
        state["updated_at"] = now()
        db.execute("INSERT INTO run(id,data) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data", (encode(state),))

    @staticmethod
    def event(db: sqlite3.Connection, kind: str, data: dict) -> None:
        db.execute("INSERT INTO events(kind,data,created_at) VALUES(?,?,?)", (kind, encode(data), now()))

    def snapshot(self, run_id: str) -> tuple[dict, dict]:
        db = self.open(run_id, readonly=True)
        try:
            # Keep state and metrics on the same read snapshot.
            db.execute("BEGIN")
            state = self.load(db)
            return state, self.metrics(db)
        finally:
            db.close()

    @staticmethod
    def metrics(db: sqlite3.Connection) -> dict:
        responses = [json.loads(row[0]) for row in db.execute("SELECT response FROM decisions WHERE response IS NOT NULL")]
        return {
            "observations": db.execute("SELECT COUNT(*) FROM observations").fetchone()[0],
            "decision_requests": db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0],
            "decision_responses": len(responses),
            "decision_elapsed_ms": round(sum(item.get("elapsed_ms", 0) for item in responses), 2),
            "http_attempts": sum(item.get("attempts", 0) for item in responses),
            "models": sorted({item["model"] for item in responses if item.get("model")}),
            "execution_intents": db.execute("SELECT COUNT(*) FROM events WHERE kind='execution_intent'").fetchone()[0],
            "execution_receipts": db.execute("SELECT COUNT(*) FROM events WHERE kind='execution_receipt'").fetchone()[0],
            "tool_rejections": db.execute("SELECT COUNT(*) FROM events WHERE kind='execution_rejected'").fetchone()[0],
            "visual_verifications": db.execute("SELECT COUNT(*) FROM events WHERE kind='visual_verification'").fetchone()[0],
            "late_responses": db.execute("SELECT COUNT(*) FROM decisions WHERE status='late'").fetchone()[0],
        }

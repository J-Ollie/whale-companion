"""Private, best-effort executor correlation. Never contains operation arguments."""
import contextvars
import json
import logging
import sqlite3
import time
import uuid
from contextlib import contextmanager, closing

MAX_EVENTS = 100000  # Stop recording rather than delete late-receipt bindings.
log = logging.getLogger(__name__)
_context = contextvars.ContextVar("phone_diagnostic_context", default=None)
_COLUMNS = ("event_id", "trace_id", "step_id", "operation_id", "job_id", "thinking_run_id", "at", "kind", "metadata")

def ident(): return uuid.uuid4().hex

@contextmanager
def scope(level):
    old = _context.get() or {}
    current = dict(old)
    if level == "trace" or not current.get("trace_id"):
        current = {"trace_id": ident()}
    if level == "step": current.update(step_id=ident(), operation_id=ident())
    token = _context.set(current)
    try: yield current
    finally: _context.reset(token)

def correlation():
    ctx = dict(_context.get() or {})
    ctx.setdefault("trace_id", ident())
    ctx.setdefault("step_id", ident())
    ctx.setdefault("operation_id", ident())
    return ctx

def initialize(db):
    try:
        db.execute("SAVEPOINT phone_diag_init")
        db.execute("CREATE TABLE IF NOT EXISTS phone_diag_events (event_id TEXT PRIMARY KEY, trace_id TEXT NOT NULL, step_id TEXT, operation_id TEXT, job_id TEXT, thinking_run_id TEXT, at REAL NOT NULL, kind TEXT NOT NULL, metadata TEXT NOT NULL DEFAULT '{}')")
        cols = tuple(row[1] for row in db.execute("PRAGMA table_info(phone_diag_events)"))
        if cols != _COLUMNS: raise ValueError("incompatible_diagnostic_schema")
        db.execute("CREATE INDEX IF NOT EXISTS phone_diag_job ON phone_diag_events(job_id)")
        db.execute("CREATE INDEX IF NOT EXISTS phone_diag_trace ON phone_diag_events(trace_id,at)")
        for name, columns in (("phone_diag_job", ("job_id",)), ("phone_diag_trace", ("trace_id", "at"))):
            if tuple(row[2] for row in db.execute("PRAGMA index_info("+name+")")) != columns:
                raise ValueError("incompatible_diagnostic_index")
        db.execute("RELEASE phone_diag_init")
        return True
    except (sqlite3.Error, ValueError):
        if not db.in_transaction: raise
        db.execute("ROLLBACK TO phone_diag_init")
        db.execute("RELEASE phone_diag_init")
        log.warning("diagnostic_incomplete: initialization")
        return False

def write(db, event_id, ctx, job_id, kind, metadata=None):
    # Caller has already started the business transaction. Never commit here.
    try:
        db.execute("SAVEPOINT phone_diag_write")
        if db.execute("SELECT count(*) FROM phone_diag_events").fetchone()[0] >= MAX_EVENTS:
            db.execute("RELEASE phone_diag_write")
            log.warning("diagnostic_incomplete: capacity")
            return False
        if kind not in ("job_bound", "receipt", "late_receipt", "timeout"):
            raise ValueError("invalid_diagnostic_kind")
        metadata = {"status": metadata["status"]} if metadata and metadata.get("status") in ("done", "failed", "uncertain", "timeout") else {}
        db.execute("INSERT OR IGNORE INTO phone_diag_events VALUES (?,?,?,?,?,?,?,?,?)", (event_id,ctx["trace_id"],ctx.get("step_id"),ctx.get("operation_id"),job_id,ctx.get("thinking_run_id"),time.time(),kind,json.dumps(metadata or {},separators=(",", ":"))))
        db.execute("RELEASE phone_diag_write")
        return True
    except Exception:
        if not db.in_transaction: raise
        db.execute("ROLLBACK TO phone_diag_write")
        db.execute("RELEASE phone_diag_write")
        log.warning("diagnostic_incomplete: write")
        return False

def receipt(db, job_id, status, late):
    # Only called after the original lease, boot and state checks.
    try:
        row = db.execute("SELECT trace_id,step_id,operation_id,thinking_run_id FROM phone_diag_events WHERE event_id=?", ("bind:"+job_id,)).fetchone()
    except sqlite3.Error:
        if not db.in_transaction: raise
        log.warning("diagnostic_incomplete: lookup")
        return False
    if row is None: return False
    ctx = dict(zip(("trace_id","step_id","operation_id","thinking_run_id"), row))
    kind = "timeout" if status == "timeout" else "late_receipt" if late else "receipt"
    return write(db,kind+":"+job_id+":"+status,ctx,job_id,kind,{"status":status})


def health(path):
    """Read-only summary; no task arguments, results, IDs or private content."""
    try:
        with closing(sqlite3.connect(path.resolve().as_uri()+"?mode=ro", uri=True, timeout=1)) as db:
            count = db.execute("SELECT count(*) FROM phone_diag_events").fetchone()[0]
            unlinked = db.execute("SELECT count(*) FROM jobs j WHERE NOT EXISTS (SELECT 1 FROM phone_diag_events d WHERE d.event_id='bind:'||j.id)").fetchone()[0]
            return {"status": "degraded" if count >= MAX_EVENTS else "ok", "events": count, "unlinked_jobs": unlinked, "capacity": MAX_EVENTS}
    except sqlite3.Error:
        return {"status": "unknown"}

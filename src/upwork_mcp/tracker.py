"""Local SQLite tracker for submitted bids/proposals — prevents duplicate applications.

Stores one row per job (keyed by the stable Upwork job id, e.g. ~022071...) so
the autopilot can check before applying and never bids on the same job twice.
The DB lives next to the browser profile at ~/.upwork-mcp/bids.db.
"""

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path.home() / ".upwork-mcp" / "bids.db"


def job_id_from_url(url: str) -> str | None:
    """Extract the stable job id (~0...) from a job or proposal URL."""
    m = re.search(r"~0[0-9a-z]+", url or "")
    return m.group(0) if m else None


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bids (
            job_id        TEXT PRIMARY KEY,
            title         TEXT,
            url           TEXT,
            job_type      TEXT,
            amount        REAL,
            connects_used INTEGER,
            status        TEXT,
            cover_letter  TEXT,
            submitted_at  TEXT
        )
        """
    )
    # migration: job age at apply time ("Posted 32 minutes ago") — the research-
    # backed highest-leverage metric (apply <60min → far higher view rates).
    try:
        conn.execute("ALTER TABLE bids ADD COLUMN posted_age TEXT")
    except sqlite3.OperationalError:
        pass  # column exists
    return conn


def has_bid(job_url_or_id: str) -> bool:
    """True if we've already recorded a bid for this job."""
    jid = job_id_from_url(job_url_or_id) or job_url_or_id
    conn = _connect()
    try:
        return conn.execute(
            "SELECT 1 FROM bids WHERE job_id IN (?, ?, ?)", _id_variants(jid)
        ).fetchone() is not None
    finally:
        conn.close()


def _id_variants(jid: str) -> tuple[str, str, str]:
    """A job id may be stored with or without the leading '~' (manual inserts
    have historically dropped it). Match all forms so dedup never misses."""
    bare = (jid or "").lstrip("~")
    return (jid, bare, "~" + bare)


def get_bid(job_url_or_id: str) -> dict | None:
    """Return the recorded bid for a job, or None."""
    jid = job_id_from_url(job_url_or_id) or job_url_or_id
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM bids WHERE job_id IN (?, ?, ?) LIMIT 1", _id_variants(jid)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def record_bid(
    url: str,
    title: str | None = None,
    job_type: str | None = None,
    amount: float | None = None,
    connects_used: int | None = None,
    status: str = "submitted",
    cover_letter: str | None = None,
    submitted_at: str | None = None,
    posted_age: str | None = None,
) -> str:
    """Insert or update a bid record. Returns the job id."""
    jid = job_id_from_url(url) or url
    ts = submitted_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO bids (job_id, title, url, job_type, amount, connects_used, status, cover_letter, submitted_at, posted_age)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                title=COALESCE(excluded.title, bids.title),
                url=COALESCE(excluded.url, bids.url),
                job_type=COALESCE(excluded.job_type, bids.job_type),
                amount=COALESCE(excluded.amount, bids.amount),
                connects_used=COALESCE(excluded.connects_used, bids.connects_used),
                status=excluded.status,
                cover_letter=COALESCE(excluded.cover_letter, bids.cover_letter),
                submitted_at=excluded.submitted_at,
                posted_age=COALESCE(excluded.posted_age, bids.posted_age)
            """,
            (jid, title, url, job_type, amount, connects_used, status, cover_letter, ts, posted_age),
        )
        conn.commit()
        return jid
    finally:
        conn.close()


def _proposal_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS proposal_status (
            proposal_id TEXT PRIMARY KEY,
            title       TEXT,
            state_json  TEXT,
            checked_at  TEXT
        )
        """
    )


def get_proposal_state(proposal_id: str) -> dict | None:
    """Return the last-saved insight state for a proposal, or None."""
    conn = _connect()
    try:
        _proposal_table(conn)
        row = conn.execute(
            "SELECT state_json FROM proposal_status WHERE proposal_id = ?", (str(proposal_id),)
        ).fetchone()
        if not row or not row["state_json"]:
            return None
        import json
        return json.loads(row["state_json"])
    finally:
        conn.close()


def save_proposal_state(proposal_id: str, title: str | None, state: dict, checked_at: str | None = None) -> None:
    """Persist the latest insight state for a proposal (for change detection)."""
    import json
    ts = checked_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn = _connect()
    try:
        _proposal_table(conn)
        conn.execute(
            """
            INSERT INTO proposal_status (proposal_id, title, state_json, checked_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(proposal_id) DO UPDATE SET
                title=COALESCE(excluded.title, proposal_status.title),
                state_json=excluded.state_json,
                checked_at=excluded.checked_at
            """,
            (str(proposal_id), title, json.dumps(state), ts),
        )
        conn.commit()
    finally:
        conn.close()


def list_bids(limit: int = 100) -> list[dict]:
    """Return recorded bids, most recent first."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM bids ORDER BY submitted_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

from __future__ import annotations
import sqlite3, json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

DB_PATH = Path("storage/casefile.sqlite")

@dataclass
class CaseFile:
    ticket_id: str
    facts: dict
    summary: str
    open_questions: list[str]
    decisions: list[dict]
    updated_at: str

def init_db(db_path: str | None = None) -> sqlite3.Connection:
    p = Path(db_path) if db_path else DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p))
    con.execute("""
        CREATE TABLE IF NOT EXISTS casefile (
            ticket_id TEXT PRIMARY KEY,
            facts_json TEXT NOT NULL,
            summary TEXT NOT NULL,
            open_questions TEXT NOT NULL,
            decisions TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    con.commit()
    return con

def get_case(con: sqlite3.Connection, ticket_id: str) -> CaseFile | None:
    cur = con.execute("SELECT ticket_id,facts_json,summary,open_questions,decisions,updated_at FROM casefile WHERE ticket_id=?", (ticket_id,))
    row = cur.fetchone()
    if not row:
        return None
    return CaseFile(
        ticket_id=row[0],
        facts=json.loads(row[1]),
        summary=row[2],
        open_questions=json.loads(row[3]),
        decisions=json.loads(row[4]),
        updated_at=row[5],
    )

def upsert_case(
    con: sqlite3.Connection,
    ticket_id: str,
    facts: dict,
    summary: str,
    open_questions: list[str],
    decisions: list[dict]
) -> None:
    con.execute("""
        INSERT INTO casefile(ticket_id,facts_json,summary,open_questions,decisions,updated_at)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT(ticket_id) DO UPDATE SET
          facts_json=excluded.facts_json,
          summary=excluded.summary,
          open_questions=excluded.open_questions,
          decisions=excluded.decisions,
          updated_at=excluded.updated_at
    """, (
        ticket_id,
        json.dumps(facts, ensure_ascii=False),
        summary,
        json.dumps(open_questions, ensure_ascii=False),
        json.dumps(decisions, ensure_ascii=False),
        datetime.utcnow().isoformat()
    ))
    con.commit()

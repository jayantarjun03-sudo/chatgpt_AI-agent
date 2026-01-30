from __future__ import annotations
import pandas as pd
from pathlib import Path

DEFAULT_TICKETS_PATH = Path("data/sample_tickets.csv")

def load_tickets_csv(path: str | None = None) -> pd.DataFrame:
    p = Path(path) if path else DEFAULT_TICKETS_PATH
    df = pd.read_csv(p)

    # normalize expected columns
    expected = [
        "ticket_id","customer","customer_tier","priority","owner","queue","category",
        "status","created_at","sla_due_at","last_update_at","blocked_reason","latest_update"
    ]
    missing = [c for c in expected if c not in df.columns]
    if missing:
        raise ValueError(f"Missing required columns in tickets CSV: {missing}")

    # parse datetimes
    for col in ["created_at","sla_due_at","last_update_at"]:
        df[col] = pd.to_datetime(df[col], utc=True, errors="coerce")
    if df[["created_at","sla_due_at","last_update_at"]].isna().any().any():
        bad = df[df[["created_at","sla_due_at","last_update_at"]].isna().any(axis=1)][["ticket_id","created_at","sla_due_at","last_update_at"]]
        raise ValueError(f"Some datetime values could not be parsed. Problem rows:\n{bad.to_string(index=False)}")

    # fill optionals
    df["blocked_reason"] = df["blocked_reason"].fillna("")
    df["latest_update"] = df["latest_update"].fillna("")
    df["category"] = df["category"].fillna("")
    df["queue"] = df["queue"].fillna("")
    df["customer_tier"] = df["customer_tier"].fillna("STANDARD")
    return df

def save_tickets_csv(df: pd.DataFrame, path: str):
    df.to_csv(path, index=False)

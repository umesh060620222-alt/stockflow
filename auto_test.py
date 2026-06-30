"""Auto-test ledger: one entry per pick, upserted when exit is resolved."""
from __future__ import annotations
import json, os

DATA_DIR  = os.getenv("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
TESTS_DIR = os.path.join(DATA_DIR, "tests")
os.makedirs(TESTS_DIR, exist_ok=True)


def _path(date_str: str) -> str:
    return os.path.join(TESTS_DIR, f"autotest_{date_str}.json")


def save_entry(date_str: str, entry: dict) -> dict:
    path = _path(date_str)
    entries: list = []
    if os.path.exists(path):
        with open(path) as f:
            entries = json.load(f)
    idx = next((i for i, e in enumerate(entries) if e.get("id") == entry.get("id")), None)
    if idx is not None:
        entries[idx] = entry
    else:
        entries.append(entry)
    with open(path, "w") as f:
        json.dump(entries, f, indent=2)
    return entry


def get_today(date_str: str) -> list:
    path = _path(date_str)
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return json.load(f)


def get_history() -> list:
    out = []
    for fname in sorted(os.listdir(TESTS_DIR), reverse=True)[:30]:
        if not fname.endswith(".json"):
            continue
        date_str = fname.replace("autotest_", "").replace(".json", "")
        try:
            with open(os.path.join(TESTS_DIR, fname)) as f:
                entries = json.load(f)
            completed = [e for e in entries if e.get("result") in ("WIN", "LOSS", "FLAT")]
            wins = sum(1 for e in completed if e.get("result") == "WIN")
            out.append({
                "date":      date_str,
                "total":     len(entries),
                "completed": len(completed),
                "wins":      wins,
                "win_rate":  round(wins / len(completed) * 100, 1) if completed else 0,
            })
        except Exception:
            pass
    return out

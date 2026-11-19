"""A small SQLite file for observability (chat log + thumbs up/down) and lead capture.

* chats: one row per question: latency (retrieval, time to first token, total), model, outcome, retrieved sources,
  attempts, handoff shown, and the user's thumbs up/down.
* leads: enquiries from the handoff form.

Logging must never break a chat, so every public function swallows errors (and prints them).
Set LOG_QUESTIONS=0 to store a short hash of each question instead of its text.
NOTE: on Render's free tier the container disk is ephemeral, so this file is wiped on every deploy or restart; for
durable leads set LEAD_WEBHOOK_URL (see app/main.py) or mount a persistent disk and point DB_PATH at it.
"""
import hashlib, json, os, sqlite3, statistics, threading
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.getenv("DB_PATH", ROOT / "data" / "app.db"))
LOG_QUESTIONS = os.getenv("LOG_QUESTIONS", "1") != "0"
_lock = threading.Lock()
_conn = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
  id TEXT PRIMARY KEY, ts TEXT NOT NULL, question TEXT, outcome TEXT, model TEXT,
  retrieval_ms INTEGER, ttft_ms INTEGER, total_ms INTEGER, attempts INTEGER, n_passages INTEGER,
  sources TEXT, handoff INTEGER DEFAULT 0, error_kind TEXT,
  feedback INTEGER, feedback_comment TEXT, feedback_ts TEXT);
CREATE TABLE IF NOT EXISTS leads (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, name TEXT, email TEXT, phone TEXT, company TEXT,
  message TEXT, source_question TEXT, chat_id TEXT, consent INTEGER);
"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def conn():
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)  # autocommit
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(SCHEMA)
        _conn = c
    return _conn


def _q(text):
    text = (text or "")[:500]
    return text if LOG_QUESTIONS else "sha256:" + hashlib.sha256(text.encode()).hexdigest()[:16]


def log_chat(chat_id, question, outcome, model=None, retrieval_ms=None, ttft_ms=None, total_ms=None, attempts=None,
             n_passages=0, sources=(), handoff=False, error_kind=None):
    try:
        with _lock:
            conn().execute(
                "INSERT OR REPLACE INTO chats (id, ts, question, outcome, model, retrieval_ms, ttft_ms, total_ms, attempts, "
                "n_passages, sources, handoff, error_kind) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (chat_id, now(), _q(question), outcome, model, retrieval_ms, ttft_ms, total_ms, attempts, n_passages,
                 json.dumps(list(sources)), int(bool(handoff)), error_kind))
    except Exception as e:
        print("store.log_chat failed:", type(e).__name__, e, flush=True)


def set_feedback(chat_id, value, comment=None):
    """thumbs: 1 = up, -1 = down. False if the chat id is unknown."""
    try:
        with _lock:
            cur = conn().execute("UPDATE chats SET feedback=?, feedback_comment=?, feedback_ts=? WHERE id=?",
                                 (value, (comment or "")[:500] or None, now(), chat_id))
            return cur.rowcount == 1
    except Exception as e:
        print("store.set_feedback failed:", type(e).__name__, e, flush=True)
        return False


def add_lead(name, email, phone, company, message, source_question, chat_id, consent):
    with _lock:  # not swallowed: the caller must know if a lead was not saved
        cur = conn().execute(
            "INSERT INTO leads (ts, name, email, phone, company, message, source_question, chat_id, consent) "
            "VALUES (?,?,?,?,?,?,?,?,?)", (now(), name, email, phone, company, message, (source_question or "")[:500],
                                          chat_id, int(bool(consent))))
        return cur.lastrowid


def leads(limit=200):
    with _lock:
        return [dict(r) for r in conn().execute("SELECT * FROM leads ORDER BY id DESC LIMIT ?", (limit,))]


def _pct(values, p):
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, round(p / 100 * (len(s) - 1)))]


def stats():
    with _lock:
        c = conn()
        rows = [dict(r) for r in c.execute("SELECT * FROM chats")]
        n_leads = c.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
    ttft = [r["ttft_ms"] for r in rows if r["ttft_ms"] is not None]
    total = [r["total_ms"] for r in rows if r["total_ms"] is not None]
    by = {}
    for r in rows:
        by[r["outcome"]] = by.get(r["outcome"], 0) + 1
    src = {}
    for r in rows:
        for u in json.loads(r["sources"] or "[]"):
            src[u] = src.get(u, 0) + 1
    up = sum(1 for r in rows if r["feedback"] == 1)
    down = sum(1 for r in rows if r["feedback"] == -1)
    return {
        "chats": len(rows), "outcomes": by, "leads": n_leads,
        "handoff_shown": sum(r["handoff"] or 0 for r in rows),
        "latency_ms": {"ttft_p50": _pct(ttft, 50), "ttft_p95": _pct(ttft, 95), "total_p50": _pct(total, 50),
                       "total_p95": _pct(total, 95),
                       "retrieval_mean": round(statistics.mean([r["retrieval_ms"] for r in rows if r["retrieval_ms"] is not None]), 1)
                       if any(r["retrieval_ms"] is not None for r in rows) else None},
        "feedback": {"up": up, "down": down, "rated": up + down,
                     "up_share": round(up / (up + down), 3) if up + down else None},
        "models": {m: sum(1 for r in rows if r["model"] == m) for m in {r["model"] for r in rows if r["model"]}},
        "top_sources": sorted(src.items(), key=lambda kv: -kv[1])[:10],
        "recent_thumbs_down": [{"question": r["question"], "comment": r["feedback_comment"], "ts": r["feedback_ts"]}
                               for r in rows if r["feedback"] == -1][-5:],
        "db": str(DB_PATH),
    }

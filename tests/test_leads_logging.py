"""Lead capture, handoff and observability tests. No real API calls, no key needed.   python tests/test_leads_logging.py

Starts the fake OpenRouter (tests/fake_openrouter.py) and the app with a temporary SQLite file, then checks: chat
logging (latency, model, sources, outcome), thumbs up/down, the handoff card (regex layer and model marker), the
enquiry form (validation, honeypot, rate limit, webhook copy) and admin access control.
"""
import csv, io, json, os, sqlite3, subprocess, sys, tempfile, time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
FAKE, APP = "http://127.0.0.1:9102", "http://127.0.0.1:8102"
TMP = tempfile.mkdtemp()
DB = os.path.join(TMP, "t.db")
TOKEN = "test-admin-token"


def start():
    env = {**os.environ, "OPENROUTER_API_KEY": "k", "OPENROUTER_API_URL": FAKE + "/v1/chat/completions",
           "LLM_RETRY_PAUSE": "0.1", "LLM_READ_TIMEOUT": "3", "PYTHONPATH": str(ROOT), "HF_HUB_OFFLINE": "1",
           "DB_PATH": DB, "ADMIN_TOKEN": TOKEN, "LEAD_WEBHOOK_URL": FAKE + "/hook"}
    env.pop("OPENROUTER_MODEL", None); env.pop("OPENROUTER_FALLBACKS", None)
    fake = subprocess.Popen([sys.executable, "-m", "uvicorn", "fake_openrouter:app", "--app-dir", str(ROOT / "tests"),
                             "--port", "9102", "--log-level", "warning"], env=env)
    app = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", "8102", "--log-level", "warning"],
                           cwd=ROOT, env=env)
    for url in (FAKE + "/calls", APP + "/health"):
        for _ in range(120):
            try:
                httpx.get(url, timeout=1); break
            except Exception:
                time.sleep(0.5)
    return fake, app


def stream(q, history=None):
    ev = []
    with httpx.stream("POST", APP + "/api/chat/stream", json={"message": q, "history": history or []}, timeout=60) as r:
        for line in r.iter_lines():
            if line.startswith("data:"):
                ev.append(json.loads(line[5:]))
    return "".join(e.get("t", "") for e in ev), ev[-1], ev


def row(chat_id):
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    r = c.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone()
    c.close()
    return dict(r) if r else None


def db_count(table):
    c = sqlite3.connect(DB); n = c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]; c.close(); return n


def main():
    fake, app = start()
    results = []

    def check(name, cond, detail=""):
        results.append(cond)
        print(("PASS " if cond else "FAIL ") + name + (f"   {detail}" if detail and not cond else ""))

    try:
        # ---------------------------------------------------------------- logging
        t, end, ev = stream("What projects does DarGlobal have in Oman?")
        cid = end["chat_id"]; r = row(cid)
        check("a chat is logged with outcome, model and the retrieved sources", r and r["outcome"] == "answered"
              and r["model"] == "fake/answering-model" and len(json.loads(r["sources"])) == r["n_passages"] > 0, r)
        check("  ...with latency: retrieval <= first token <= total", r and 0 <= r["retrieval_ms"] <= r["ttft_ms"] <= r["total_ms"], r)
        check("  ...and the start event carries the same chat id", ev[0]["chat_id"] == cid)
        check("  ...ordinary question: no handoff card", end["handoff"] is False and r["handoff"] == 0)

        # ---------------------------------------------------------------- feedback
        res = httpx.post(APP + "/api/feedback", json={"chat_id": cid, "value": 1})
        check("thumbs up is saved", res.status_code == 200 and row(cid)["feedback"] == 1)
        res = httpx.post(APP + "/api/feedback", json={"chat_id": cid, "value": -1, "comment": "missed the Fendi villas"})
        check("a later thumbs down replaces it and keeps the comment", row(cid)["feedback"] == -1
              and row(cid)["feedback_comment"] == "missed the Fendi villas")
        check("unknown chat id -> 404", httpx.post(APP + "/api/feedback", json={"chat_id": "nope", "value": 1}).status_code == 404)
        check("invalid rating value -> 400", httpx.post(APP + "/api/feedback", json={"chat_id": cid, "value": 5}).status_code == 400)

        # ---------------------------------------------------------------- outcomes
        t, end, ev = stream("Should I invest in DarGlobal's Oman projects?")
        r = row(end["chat_id"])
        check("a refusal by the regex layer is logged (no model, no retrieval)", r["outcome"] == "refused_regex" and r["model"] is None)
        t, end, ev = stream("MODEADV what should I do with my savings")
        check("a refusal by the model marker is logged", row(end["chat_id"])["outcome"] == "refused_model")
        t, end, ev = stream("MODEALL projects in Oman")
        r = row(end["chat_id"])
        check("an unavailable answer is logged with the failure kind and the attempts made",
              r["outcome"] == "unavailable" and r["error_kind"] == "busy" and r["attempts"] == 6, r)
        t, end, ev = stream("MODEQUOTA projects in Oman")
        check("a quota error is logged as such", row(end["chat_id"])["error_kind"] == "quota")
        t, end, ev = stream("MODEMID projects in Oman")
        check("an interrupted stream is logged", row(end["chat_id"])["outcome"] == "interrupted")

        # a client that disconnects mid-answer is still logged
        before = db_count("chats")
        with httpx.stream("POST", APP + "/api/chat/stream", json={"message": "MODESLOW projects in Oman"}, timeout=10) as resp:
            for line in resp.iter_lines():
                if line.startswith("data:") and "start" in line:
                    break  # leave before any token arrives
        for _ in range(40):
            time.sleep(0.25)
            if db_count("chats") > before:
                break
        c = sqlite3.connect(DB); last = c.execute("SELECT outcome FROM chats ORDER BY ts DESC, rowid DESC LIMIT 1").fetchone()[0]; c.close()
        check("a client that closes the stream early is logged as aborted", last == "aborted", last)

        # ---------------------------------------------------------------- handoff
        t, end, ev = stream("I want to buy a villa in Muscat")
        hand = end.get("handoff")
        check("'I want to buy...' shows the handoff card (regex layer)", bool(hand) and len(hand["contacts"]) == 2, end.get("handoff"))
        check("  ...with the companies' own contact pages", {c["url"] for c in hand["contacts"]} == {
            "https://darglobal.co.uk/get-in-touch", "https://wasalt.sa/en"})
        check("  ...and it is recorded in the log", row(end["chat_id"])["handoff"] == 1)
        t, end, ev = stream("MODEHANDOFF tell me more about Oman")
        check("model marker -> handoff card, marker never shown, answer streams normally",
              bool(end.get("handoff")) and "[[" not in t and t.startswith("DarGlobal has"), (t[:40], end.get("handoff")))
        t, end, ev = stream("I want to buy a Wasalt apartment")
        check("  ...the named company's contact page comes first", end["handoff"]["contacts"][0]["company"] == "Wasalt")
        j = httpx.post(APP + "/api/chat", json={"message": "How do I book a viewing at AIDA?"}, timeout=60).json()
        check("  ...JSON endpoint agrees", bool(j.get("handoff")) and "chat_id" in j)

        # ---------------------------------------------------------------- lead form
        good = {"name": "Test Buyer", "email": "buyer@example.com", "company": "dar", "message": "3-bed villa in Muscat",
                "source_question": "I want to buy a villa in Muscat", "chat_id": cid, "consent": True}
        bad = lambda **kw: httpx.post(APP + "/api/lead", json={**good, **kw})
        check("no name -> 400 with a field error", bad(name="").status_code == 400 and "name" in bad(name="").json()["errors"])
        check("no email and no phone -> 400", "contact" in bad(email="", phone="").json()["errors"])
        check("malformed email -> 400", "email" in bad(email="not-an-email").json()["errors"])
        check("malformed phone -> 400", "phone" in bad(email="", phone="12").json()["errors"])
        check("missing consent -> 400", "consent" in bad(consent=False).json()["errors"])
        check("none of the invalid enquiries were saved", db_count("leads") == 0)
        h = httpx.post(APP + "/api/lead", json={**good, "website": "http://spam.example"})
        check("honeypot filled -> looks successful but nothing is saved", h.status_code == 200 and db_count("leads") == 0)
        ok = httpx.post(APP + "/api/lead", json=good)
        check("a valid enquiry is saved", ok.status_code == 200 and ok.json()["ok"] and db_count("leads") == 1)
        c = sqlite3.connect(DB); c.row_factory = sqlite3.Row; lead = dict(c.execute("SELECT * FROM leads").fetchone()); c.close()
        check("  ...with the question that triggered it and the chat id", lead["source_question"].startswith("I want to buy")
              and lead["chat_id"] == cid and lead["consent"] == 1 and lead["email"] == "buyer@example.com", lead)
        phone_only = httpx.post(APP + "/api/lead", json={**good, "email": "", "phone": "+968 2270 2771", "name": "Phone Only"})
        check("a phone number alone is enough", phone_only.status_code == 200)
        time.sleep(1.0)
        hooks = httpx.get(FAKE + "/hooks").json()
        check("each saved lead is also forwarded to the webhook (durability beyond the ephemeral disk)",
              len(hooks) == 2 and hooks[0]["lead"]["email"] == "buyer@example.com" and "Test Buyer" in hooks[0]["text"], hooks)
        for _ in range(3):
            httpx.post(APP + "/api/lead", json=good)
        limited = httpx.post(APP + "/api/lead", json=good)
        check("more than 5 enquiries/hour from one connection -> 429", limited.status_code == 429, limited.status_code)

        # ---------------------------------------------------------------- admin
        check("admin stats without a token -> 401", httpx.get(APP + "/admin/stats").status_code == 401)
        check("admin stats with a wrong token -> 401", httpx.get(APP + "/admin/stats", headers={"X-Admin-Token": "x"}).status_code == 401)
        st = httpx.get(APP + "/admin/stats", headers={"X-Admin-Token": TOKEN}).json()
        check("admin stats with the token: counts, latency percentiles, feedback", st["chats"] >= 10 and st["leads"] == 5
              and st["latency_ms"]["total_p50"] is not None and st["feedback"]["down"] == 1
              and st["outcomes"].get("refused_regex", 0) >= 1, st)
        check("  ...shows the thumbs-down comment and the most retrieved sources",
              st["recent_thumbs_down"][-1]["comment"] == "missed the Fendi villas" and len(st["top_sources"]) > 0)
        res = httpx.get(APP + "/admin/leads.csv", headers={"Authorization": f"Bearer {TOKEN}"})
        rows = list(csv.DictReader(io.StringIO(res.text)))
        check("leads export as CSV (Bearer header also works)", res.status_code == 200 and rows[-1]["name"] == "Test Buyer" and len(rows) == 5)
        check("the admin page is served", httpx.get(APP + "/admin").status_code == 200)
    finally:
        fake.terminate(); app.terminate()

    # privacy switch: LOG_QUESTIONS=0 stores a hash instead of the question text
    db2 = os.path.join(TMP, "private.db")
    code = ("from app import store; store.log_chat('abc','my phone is 0501234567','answered',total_ms=5);"
            "import sqlite3; print(sqlite3.connect(store.DB_PATH).execute('select question from chats').fetchone()[0])")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, text=True, capture_output=True,
                         env={**os.environ, "LOG_QUESTIONS": "0", "DB_PATH": db2, "PYTHONPATH": str(ROOT)}).stdout.strip()
    results.append(out.startswith("sha256:") and "0501234567" not in out)
    print(("PASS " if results[-1] else "FAIL ") + "LOG_QUESTIONS=0 stores a hash, not the question text")

    print(f"\n{sum(results)}/{len(results)} checks passed")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()

"""Small eval suite for the DarGlobal & Wasalt chatbot.

Metrics (see eval/questions.json):
  retrieval hit   - answerable questions whose retrieved context contains the expected facts (offline, no model)
  answer accuracy - answerable questions whose answer contains the expected facts
  IDK accuracy    - unanswerable questions where the bot correctly says it doesn't know (and cites no sources)
  false-IDK rate  - answerable questions where the bot wrongly says "I don't know"

Usage (from the project root, with requirements.txt installed):
    python eval/run_eval.py --retrieval-only                    # free, no API calls
    python eval/run_eval.py --url https://<your-app>.onrender.com
    python eval/run_eval.py --url http://localhost:8010 --resume   # reuse answers cached in eval/results.json

Free OpenRouter models are rate limited (~50 requests/day). Rate-limited/errored questions are reported
separately, excluded from the scores, and --resume lets you finish them on a later run.
"""
import argparse, json, re, sys, time, unicodedata
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
QUESTIONS = Path(__file__).with_name("questions.json")
RESULTS = Path(__file__).with_name("results.json")
TAGS = {"v1": "facts", "v2": "country+comparison", "v3": "paraphrase"}  # question groups


def norm(s: str) -> str:
    """lowercase, strip accents, drop everything but letters/digits ('9,790' == '9790', 'Benahavís' == 'benahavis')"""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9+$]", "", s)


def facts_found(text: str, expect, need=None):
    """expect = list of groups; a group is satisfied if any alternative appears. need = how many groups must match."""
    t = norm(text)
    hit = [any(norm(alt) in t for alt in group) for group in expect]
    return sum(hit) >= (need or len(expect)), hit


def retrieval_eval(qs):
    from app.main import retrieve  # same retriever the server uses

    out = {}
    for q in qs:
        if q.get("unknown"):
            continue
        ctx = " ".join(c["text"] + " " + c["title"] for c in retrieve(q["q"]))
        ok, hit = facts_found(ctx, q["expect"], q.get("min"))
        out[q["id"]] = {"ok": ok, "groups": hit}
    return out


def ask(url, question, retries=2):
    last = None
    for _ in range(retries + 1):
        try:
            r = httpx.post(f"{url}/api/chat", json={"message": question, "history": []}, timeout=180)
            j = r.json()
            if r.status_code == 200 and j.get("answer"):
                return {"status": 200, "answer": j["answer"], "sources": j.get("sources", [])}
            last = {"status": r.status_code, "answer": j.get("answer", ""), "sources": []}
            if r.status_code == 502 and "429" in last["answer"]:
                return last  # daily rate limit: retrying won't help
        except Exception as e:  # network / timeout
            last = {"status": 0, "answer": f"{type(e).__name__}", "sources": []}
        time.sleep(3)
    return last


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", help="base URL of the running app (live or local)")
    ap.add_argument("--retrieval-only", action="store_true")
    ap.add_argument("--resume", action="store_true", help="reuse successful answers from eval/results.json")
    ap.add_argument("--delay", type=float, default=2.0)
    a = ap.parse_args()
    if not a.retrieval_only and not a.url:
        ap.error("--url is required unless --retrieval-only")

    qs = json.loads(QUESTIONS.read_text())
    answerable = [q for q in qs if not q.get("unknown")]
    unknown = [q for q in qs if q.get("unknown")]

    ret = retrieval_eval(qs)
    ret_hits = sum(v["ok"] for v in ret.values())
    print(f"Retrieval hit rate: {ret_hits}/{len(answerable)} = {ret_hits / len(answerable):.0%}")
    for q in answerable:
        if not ret[q["id"]]["ok"]:
            print(f"  MISS {q['id']}: {q['q']}  groups={ret[q['id']]['groups']}")
    print("  by group: " + ", ".join(f"{TAGS[t]} {sum(ret[q['id']]['ok'] for q in answerable if q.get('tag') == t)}/"
                                    f"{sum(1 for q in answerable if q.get('tag') == t)}" for t in TAGS))
    if a.retrieval_only:
        return

    from app.main import is_unknown  # same "I don't know" test the server uses to hide sources

    cache = json.loads(RESULTS.read_text()).get("answers", {}) if (a.resume and RESULTS.exists()) else {}
    answers, stop = {}, False
    for q in qs:
        c = cache.get(q["id"])
        if c and c.get("status") == 200:
            answers[q["id"]] = c
            continue
        if stop:
            answers[q["id"]] = {"status": 429, "answer": "skipped after rate limit", "sources": []}
            continue
        r = ask(a.url, q["q"])
        answers[q["id"]] = r
        print(f"  {q['id']}: HTTP {r['status']}  {r['answer'][:70]!r}")
        if r["status"] == 502 and "429" in r["answer"]:
            stop = True
            print("  rate limit reached; remaining questions skipped (rerun later with --resume)")
        time.sleep(a.delay)

    def ok_resp(q):
        return answers[q["id"]]["status"] == 200

    ans_rows, idk_rows = [], []
    for q in answerable:
        r = answers[q["id"]]
        if r["status"] != 200:
            continue
        ok, hit = facts_found(r["answer"], q["expect"], q.get("min"))
        ans_rows.append({"id": q["id"], "ok": ok, "false_idk": is_unknown(r["answer"]), "groups": hit})
    for q in unknown:
        r = answers[q["id"]]
        if r["status"] != 200:
            continue
        says_idk = is_unknown(r["answer"])
        idk_rows.append({"id": q["id"], "ok": says_idk, "sources_hidden": not r["sources"]})

    errs = [q["id"] for q in qs if not ok_resp(q)]
    n_a, n_u = len(ans_rows), len(idk_rows)
    acc = sum(r["ok"] for r in ans_rows)
    fidk = sum(r["false_idk"] for r in ans_rows)
    idk = sum(r["ok"] for r in idk_rows)
    print()
    print(f"Answer accuracy (expected facts present): {acc}/{n_a} = {acc / n_a:.0%}" if n_a else "Answer accuracy: n/a")
    print(f"False 'I don't know' on answerable:       {fidk}/{n_a}" if n_a else "")
    print(f"Correct 'I don't know' on unanswerable:   {idk}/{n_u} = {idk / n_u:.0%}" if n_u else "IDK accuracy: n/a")
    byq = {q["id"]: q for q in qs}
    print("  by group: " + ", ".join(f"{TAGS[t]} {sum(r['ok'] for r in ans_rows if byq[r['id']].get('tag') == t)}/"
                                    f"{sum(1 for r in ans_rows if byq[r['id']].get('tag') == t)}" for t in TAGS))
    for r in ans_rows:
        if not r["ok"]:
            q = next(x for x in qs if x["id"] == r["id"])
            print(f"  WRONG {r['id']}: {q['q']}\n        -> {answers[r['id']]['answer'][:200]!r}")
    for r in idk_rows:
        if not r["ok"]:
            q = next(x for x in qs if x["id"] == r["id"])
            print(f"  HALLUCINATED? {r['id']}: {q['q']}\n        -> {answers[r['id']]['answer'][:200]!r}")
    if errs:
        print(f"Excluded (HTTP errors / rate limit): {len(errs)} -> {errs}")

    RESULTS.write_text(json.dumps({
        "url": a.url, "retrieval": ret, "answers": answers,
        "scores": {"retrieval_hits": ret_hits, "answerable": len(answerable), "answer_correct": acc, "answer_scored": n_a,
                   "false_idk": fidk, "idk_correct": idk, "idk_scored": n_u, "errors": errs},
    }, ensure_ascii=False, indent=1))
    print(f"\nSaved {RESULTS.relative_to(ROOT)}")


if __name__ == "__main__":
    main()

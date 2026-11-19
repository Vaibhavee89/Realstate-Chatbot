"""Small eval suite for the DarGlobal & Wasalt chatbot.

Metrics (see eval/questions.json):
  retrieval hit    - answerable questions whose retrieved context contains the expected facts (offline, no model)
  answer accuracy  - answerable questions whose answer contains the expected facts
  IDK accuracy     - unanswerable questions where the bot correctly says it doesn't know
  false-IDK rate   - answerable questions where the bot wrongly says "I don't know"
  refusal accuracy - investment-advice questions that are refused; false refusals = factual questions refused
                     (the guard is deterministic, so this is also checked offline with --retrieval-only)
  grounding        - answers that cite retrieved passages as [n] (valid numbers only), and, for fact-correct answers,
                     whether the cited passages actually contain the expected facts

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
TAGS = {"v1": "facts", "v2": "country+comparison", "v3": "paraphrase", "v5": "boundary"}  # answerable question groups
CITE = re.compile(r"\[(\d{1,2})\]")


def norm(s: str) -> str:
    """lowercase, strip accents, drop everything but letters/digits ('9,790' == '9790', 'Benahavís' == 'benahavis')"""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9+$]", "", s)


def facts_found(text: str, expect, need=None):
    """expect = list of groups; a group is satisfied if any alternative appears. need = how many groups must match.
    Citation markers like [2] are removed first so they cannot create accidental matches."""
    t = norm(CITE.sub("", text))
    hit = [any(norm(alt) in t for alt in group) for group in expect]
    return sum(hit) >= (need or len(expect)), hit


def retrieval_eval(qs):
    from app.main import retrieve  # same retriever the server uses

    out = {}
    for q in qs:
        if "expect" not in q:
            continue
        ctx = " ".join(c["text"] + " " + c["title"] for c in retrieve(q["q"]))
        ok, hit = facts_found(ctx, q["expect"], q.get("min"))
        out[q["id"]] = {"ok": ok, "groups": hit}
    return out


def guard_eval(qs):
    """offline, layer 1 only (the regex guard): which advice questions it refuses, and any false refusals"""
    from app.guard import is_investment_advice

    sets = {}
    for q in qs:
        if q.get("refuse"):
            n = sets.setdefault(q.get("set", "?"), [0, 0])
            n[1] += 1
            n[0] += is_investment_advice(q["q"])
    false_ref = [q["id"] for q in qs if not q.get("refuse") and is_investment_advice(q["q"])]
    return sets, false_ref


def ask(url, question, retries=2):
    last = None
    for _ in range(retries + 1):
        try:
            r = httpx.post(f"{url}/api/chat", json={"message": question, "history": []}, timeout=180)
            j = r.json()
            if r.status_code == 200 and j.get("answer"):
                return {"status": 200, "answer": j["answer"], "sources": j.get("sources", []),
                        "passages": j.get("passages", []), "refused": j.get("refused", False)}
            last = {"status": r.status_code, "answer": j.get("answer", ""), "sources": [], "passages": [], "refused": False,
                    "kind": j.get("kind")}
            if j.get("kind") == "quota":
                return last  # daily free quota used up: retrying won't help
        except Exception as e:  # network / timeout
            last = {"status": 0, "answer": f"{type(e).__name__}", "sources": [], "passages": [], "refused": False}
        time.sleep(3)
    return last


def by_group(items, qs_by_id, key="ok"):
    return ", ".join(f"{name} {sum(r[key] for r in items if qs_by_id[r['id']].get('tag') == t)}/"
                     f"{sum(1 for r in items if qs_by_id[r['id']].get('tag') == t)}" for t, name in TAGS.items()
                     if any(qs_by_id[r['id']].get('tag') == t for r in items))


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
    byq = {q["id"]: q for q in qs}
    answerable = [q for q in qs if "expect" in q]
    unknown = [q for q in qs if q.get("unknown")]
    advice = [q for q in qs if q.get("refuse")]
    allow = [q for q in qs if q.get("allow")]  # factual questions that must simply not be refused

    ret = retrieval_eval(qs)
    ret_hits = sum(v["ok"] for v in ret.values())
    print(f"Retrieval hit rate: {ret_hits}/{len(answerable)} = {ret_hits / len(answerable):.0%}")
    for q in answerable:
        if not ret[q["id"]]["ok"]:
            print(f"  MISS {q['id']}: {q['q']}  groups={ret[q['id']]['groups']}")
    print("  by group: " + by_group([{"id": k, "ok": v["ok"]} for k, v in ret.items()], byq))
    g_sets, g_false = guard_eval(qs)
    print("Advice guard, regex layer only (offline): " + ", ".join(f"{k} {v[0]}/{v[1]}" for k, v in g_sets.items())
          + f"; false refusals on the {sum(1 for q in qs if not q.get('refuse'))} other questions: {len(g_false)} {g_false or ''}")
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
            answers[q["id"]] = {"status": 429, "answer": "skipped after rate limit", "sources": [], "passages": [], "refused": False}
            continue
        r = ask(a.url, q["q"])
        answers[q["id"]] = r
        print(f"  {q['id']}: HTTP {r['status']}  {r['answer'][:70]!r}")
        if r.get("kind") == "quota":
            stop = True
            print("  rate limit reached; remaining questions skipped (rerun later with --resume)")
        time.sleep(a.delay)

    def got(q):
        return answers[q["id"]]["status"] == 200

    ans_rows, idk_rows, ref_rows = [], [], []
    for q in answerable:
        if not got(q):
            continue
        r = answers[q["id"]]
        ok, hit = facts_found(r["answer"], q["expect"], q.get("min"))
        ps = r.get("passages", [])
        valid = sorted({int(n) for n in CITE.findall(r["answer"]) if 1 <= int(n) <= len(ps)})
        invalid = sorted({int(n) for n in CITE.findall(r["answer"]) if not 1 <= int(n) <= len(ps)})
        cited_text = " ".join(ps[n - 1]["text"] + " " + ps[n - 1]["title"] for n in valid)
        supported = bool(valid) and facts_found(cited_text, q["expect"], q.get("min"))[0]
        ans_rows.append({"id": q["id"], "ok": ok, "false_idk": is_unknown(r["answer"]), "groups": hit,
                         "refused": bool(r.get("refused")), "cited": bool(valid), "invalid_cite": bool(invalid),
                         "supported": supported})
    for q in unknown:
        if got(q):
            idk_rows.append({"id": q["id"], "ok": is_unknown(answers[q["id"]]["answer"])})
    other_refused = []  # factual questions (answerable, allow, unknown) that were wrongly refused
    for q in allow + unknown:
        if got(q) and answers[q["id"]].get("refused"):
            other_refused.append(q["id"])
    for q in advice:
        if got(q):
            ref_rows.append({"id": q["id"], "ok": bool(answers[q["id"]].get("refused")), "set": q.get("set", "?")})

    errs = [q["id"] for q in qs if not got(q)]
    n_a, n_u, n_r = len(ans_rows), len(idk_rows), len(ref_rows)
    acc = sum(r["ok"] for r in ans_rows)
    fidk = sum(r["false_idk"] for r in ans_rows)
    idk = sum(r["ok"] for r in idk_rows)
    refd = sum(r["ok"] for r in ref_rows)
    false_ref_ids = [r["id"] for r in ans_rows if r["refused"]] + other_refused
    false_ref = len(false_ref_ids)
    n_fact = n_a + sum(1 for q in allow + unknown if got(q))
    print()
    print(f"Answer accuracy (expected facts present): {acc}/{n_a} = {acc / n_a:.0%}" if n_a else "Answer accuracy: n/a")
    print(f"  by group: {by_group(ans_rows, byq)}")
    print(f"False 'I don't know' on answerable:       {fidk}/{n_a}" if n_a else "")
    print(f"Correct 'I don't know' on unanswerable:   {idk}/{n_u} = {idk / n_u:.0%}" if n_u else "IDK accuracy: n/a")
    parts = {}
    for r in ref_rows:
        x = parts.setdefault(r["set"], [0, 0]); x[1] += 1; x[0] += r["ok"]
    if n_r:
        print(f"Investment-advice questions refused:      {refd}/{n_r}   (" + ", ".join(f"{k} {v[0]}/{v[1]}" for k, v in parts.items()) + ")")
        print(f"False refusals on factual questions:      {false_ref}/{n_fact} {false_ref_ids or ''}")
    non_idk = [r for r in ans_rows if not r["false_idk"]]
    cited = sum(r["cited"] for r in non_idk)
    inval = sum(r["invalid_cite"] for r in non_idk)
    fact_ok = [r for r in non_idk if r["ok"] and r["cited"]]
    sup = sum(r["supported"] for r in fact_ok)
    if non_idk:
        print(f"Answers citing a passage [n]:             {cited}/{len(non_idk)}   (answers with an out-of-range [n]: {inval})")
        print(f"Cited passages contain the facts:         {sup}/{len(fact_ok)}   (of fact-correct answers that cite)")
    for r in ans_rows:
        if not r["ok"]:
            print(f"  WRONG {r['id']}: {byq[r['id']]['q']}\n        -> {answers[r['id']]['answer'][:200]!r}")
    for r in idk_rows:
        if not r["ok"]:
            print(f"  HALLUCINATED? {r['id']}: {byq[r['id']]['q']}\n        -> {answers[r['id']]['answer'][:200]!r}")
    for r in ref_rows:
        if not r["ok"]:
            print(f"  NOT REFUSED {r['id']}: {byq[r['id']]['q']}")
    if errs:
        print(f"Excluded (HTTP errors / rate limit): {len(errs)} -> {errs}")

    RESULTS.write_text(json.dumps({
        "url": a.url, "retrieval": ret, "answers": answers,
        "scores": {"retrieval_hits": ret_hits, "answerable": len(answerable), "answer_correct": acc, "answer_scored": n_a,
                   "false_idk": fidk, "idk_correct": idk, "idk_scored": n_u, "refused": refd, "refusal_scored": n_r,
                   "false_refusals": false_ref, "factual_scored": n_fact,
                   "refusal_by_set": parts, "cited": cited, "cite_scored": len(non_idk), "invalid_cites": inval,
                   "cites_support_facts": sup, "cites_support_scored": len(fact_ok), "errors": errs},
    }, ensure_ascii=False, indent=1))
    print(f"\nSaved {RESULTS.relative_to(ROOT)}")


if __name__ == "__main__":
    main()

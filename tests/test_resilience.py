"""Resilience tests: fallback chain, one retry, quota message, advice marker. No real API calls, no key needed.

    python tests/test_resilience.py

Starts a fake OpenRouter (tests/fake_openrouter.py) and the app pointed at it, then checks, on both the streaming and
the JSON endpoint, which models were tried and what the user is told.
"""
import json, os, subprocess, sys, time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
FAKE, APP = "http://127.0.0.1:9101", "http://127.0.0.1:8101"
CHAIN = ["openrouter/free", "google/gemma-4-31b-it:free", "nvidia/nemotron-3-ultra-550b-a55b:free"]


def start():
    env = {**os.environ, "OPENROUTER_API_KEY": "k", "OPENROUTER_API_URL": FAKE + "/v1/chat/completions",
           "LLM_RETRY_PAUSE": "0.1", "LLM_READ_TIMEOUT": "3", "PYTHONPATH": str(ROOT), "HF_HUB_OFFLINE": "1"}
    env.pop("OPENROUTER_MODEL", None); env.pop("OPENROUTER_FALLBACKS", None)
    fake = subprocess.Popen([sys.executable, "-m", "uvicorn", "fake_openrouter:app", "--app-dir", str(ROOT / "tests"),
                             "--port", "9101", "--log-level", "warning"], env=env)
    app = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", "8101", "--log-level", "warning"],
                           cwd=ROOT, env=env)
    for url in (FAKE + "/calls", APP + "/health"):
        for _ in range(120):
            try:
                httpx.get(url, timeout=1); break
            except Exception:
                time.sleep(0.5)
    return fake, app


def stream(q):
    ev = []
    with httpx.stream("POST", APP + "/api/chat/stream", json={"message": q}, timeout=60) as r:
        for line in r.iter_lines():
            if line.startswith("data:"):
                ev.append(json.loads(line[5:]))
    return "".join(e.get("t", "") for e in ev), ev[-1], ev


def calls():
    return [m for _, m in httpx.get(FAKE + "/calls").json()]


def main():
    fake, app = start()
    results = []

    def check(name, cond, detail=""):
        results.append(cond)
        print(("PASS " if cond else "FAIL ") + name + (f"   {detail}" if detail and not cond else ""))

    try:
        health = httpx.get(APP + "/health").json()
        check("health lists the model chain", health["models"] == CHAIN, health["models"])

        def run(q):
            httpx.post(FAKE + "/reset")
            t, end, ev = stream(q)
            c = calls()
            httpx.post(FAKE + "/reset")
            j = httpx.post(APP + "/api/chat", json={"message": q}, timeout=60)
            return t, end, ev, c, j, calls()

        t, end, ev, c, j, jc = run("What projects does DarGlobal have in Oman?")
        check("primary model answers, no fallback", c == CHAIN[:1] and end.get("done") and t.startswith("DarGlobal has"), c)
        check("  ...and the answer reports which model wrote it", end.get("model") == "fake/answering-model", end.get("model"))
        check("  ...JSON endpoint agrees", j.status_code == 200 and jc == CHAIN[:1])

        t, end, ev, c, j, jc = run("MODEF1 projects in Oman")
        check("model 1 busy -> model 2 answers", c == CHAIN[:2] and end.get("done"), c)
        check("  ...status note streamed while falling back", any("trying another free model" in e.get("status", "") for e in ev))
        check("  ...JSON endpoint agrees", j.status_code == 200 and jc == CHAIN[:2], jc)

        t, end, ev, c, j, jc = run("MODEF12 projects in Oman")
        check("models 1+2 fail -> model 3 answers", c == CHAIN and end.get("done"), c)

        t, end, ev, c, j, jc = run("MODEEMPTYA projects in Oman")
        check("empty reply from model 1 -> model 2 answers", c == CHAIN[:2] and end.get("done"), c)

        t, end, ev, c, j, jc = run("MODEFLAKY projects in Oman")
        check("whole first pass fails -> exactly ONE retry succeeds", c == CHAIN + CHAIN[:1] and end.get("done"), c)
        check("  ...JSON endpoint agrees", j.status_code == 200 and jc == CHAIN + CHAIN[:1], jc)

        t, end, ev, c, j, jc = run("MODEALL projects in Oman")
        check("everything down -> gives up after exactly two passes", c == CHAIN + CHAIN, c)
        check("  ...friendly 'busy' message, no error codes", end.get("kind") == "busy" and "busy" in end["error"]
              and "503" not in end["error"] and "OpenRouter" not in end["error"], end.get("error"))
        check("  ...retrieved passages are still returned", len(end.get("passages", [])) > 0)
        check("  ...JSON endpoint: 503 with the same kind", j.status_code == 503 and j.json()["kind"] == "busy")

        t, end, ev, c, j, jc = run("MODEQUOTA projects in Oman")
        check("daily quota used up -> stops after ONE request (all free models share it)", c == CHAIN[:1], c)
        check("  ...says the quota is used up and when it resets", end.get("kind") == "quota" and "quota" in end["error"]
              and "UTC" in end["error"], end.get("error"))
        check("  ...passages still returned", len(end.get("passages", [])) > 0)

        t, end, ev, c, j, jc = run("MODE401 projects in Oman")
        check("bad API key -> one request, calm message without the word 'key' or '401'",
              c == CHAIN[:1] and end.get("kind") == "auth" and "401" not in end["error"] and "key" not in end["error"].lower(), end.get("error"))

        t, end, ev, c, j, jc = run("MODEMID projects in Oman")
        check("breaks mid-answer -> partial text kept, retry offered", end.get("kind") == "interrupted" and t.startswith("DarGlobal has"), (t, end))

        t, end, ev, c, j, jc = run("MODEADV what should I do with my savings")
        check("model answers with the advice marker -> standard refusal", end.get("refused") is True and "can't give investment advice" in t, t)
        check("  ...JSON endpoint agrees", j.json().get("refused") is True)

        t, end, ev, c, j, jc = run("Should I invest in DarGlobal's Oman projects?")
        check("obvious advice question -> refused by the regex layer with ZERO model calls", end.get("refused") is True and c == [], c)
    finally:
        fake.terminate(); app.terminate()
    print(f"\n{sum(results)}/{len(results)} checks passed")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()

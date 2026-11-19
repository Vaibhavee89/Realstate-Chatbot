#!/usr/bin/env bash
# Smoke test for the built Docker image: starts it and checks the things that only break in the image.
#   scripts/smoke_container.sh <image> [host-port]
# No model API key or network access is needed: the model endpoint is pointed at a dead port, so the checks cover the
# parts that do not need a model (embeddings baked into the image, retrieval, guardrails, friendly failure, leads, admin).
set -euo pipefail
IMAGE="${1:?usage: smoke_container.sh <image> [port]}"
PORT="${2:-8000}"
NAME="smoke-$$"

docker run -d --name "$NAME" -p "$PORT:8000" \
  -e OPENROUTER_API_KEY=ci-smoke -e OPENROUTER_API_URL=http://127.0.0.1:9/v1/chat/completions \
  -e LLM_RETRY_PAUSE=0.1 -e ADMIN_TOKEN=ci-token "$IMAGE" >/dev/null
trap 'echo "--- container log ---"; docker logs "$NAME" 2>&1 | tail -15; docker rm -f "$NAME" >/dev/null' EXIT

for _ in $(seq 1 90); do curl -fs "localhost:$PORT/health" >/dev/null 2>&1 && break; sleep 1; done

python3 - "$PORT" "$NAME" <<'PY'
import json, subprocess, sys, urllib.request

port, name = sys.argv[1], sys.argv[2]
base = f"http://localhost:{port}"
failed = []


def call(path, body=None, headers=None):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def check(label, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + label + (f"   {detail}" if detail and not ok else ""))
    if not ok:
        failed.append(label)


_, health = call("/health")
check("health: app is up with the full index", health.get("ok") and health.get("chunks", 0) > 500, health)
check("embeddings are baked into the image (hybrid retrieval, no network at runtime)", health.get("embeddings") is True, health)
check("fallback model chain is configured", len(health.get("models", [])) >= 2, health.get("models"))
_, meta = call("/api/meta")
check("data-freshness record is present", bool(meta.get("scraped_at")) and meta.get("dar_pages", 0) > 0, meta)

_, adv = call("/api/chat", {"message": "Should I invest in DarGlobal's Oman projects?"})
check("investment-advice question is refused (no model needed)", adv.get("refused") is True, adv)

st, down = call("/api/chat", {"message": "What projects does DarGlobal have in Oman?"})
check("model unreachable: friendly 'busy' message, not a crash", st == 503 and down.get("kind") == "busy"
      and "busy" in down.get("answer", ""), (st, down.get("kind")))
check("  ...retrieval still works: the relevant passages are returned", len(down.get("passages", [])) >= 3
      and any("oman" in p["text"].lower() for p in down["passages"]), len(down.get("passages", [])))

st, lead = call("/api/lead", {"name": "CI Smoke", "email": "ci@example.com", "consent": True, "company": "dar"})
check("enquiry form saves a lead", st == 200 and lead.get("ok"), (st, lead))
st, stats = call("/admin/stats", headers={"X-Admin-Token": "ci-token"})
check("admin stats work with the token and counted the lead and the chats", st == 200 and stats.get("leads") == 1
      and stats.get("chats", 0) >= 2, (st, stats))
check("admin stats reject a missing token", call("/admin/stats")[0] == 401)

uid = subprocess.run(["docker", "exec", name, "id", "-u"], capture_output=True, text=True).stdout.strip()
check("container does not run as root", uid not in ("", "0"), uid)

print(f"\n{'ALL SMOKE CHECKS PASSED' if not failed else str(len(failed)) + ' CHECK(S) FAILED'}")
sys.exit(1 if failed else 0)
PY

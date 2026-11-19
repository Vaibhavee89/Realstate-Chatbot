"""A fake OpenRouter for tests/test_resilience.py. The failure mode is chosen by a MODExxx word in the question."""
import asyncio, json, time
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse
app = FastAPI()
log = []                 # (mode, model) per call
flaky = {}               # per-question counter for MODEFLAKY
ANSWER = "DarGlobal has **AIDA** in Oman [1]:\n- Trump International Hotel [2]\n- Marriott Residences [1][2]"
DONE = "data: [DONE]\n\n"
def chunk(c, model="fake/answering-model"): return "data: " + json.dumps({"model": model, "choices": [{"delta": c}]}) + "\n\n"

@app.get("/calls")
async def calls(): return log
@app.post("/reset")
async def reset(): log.clear(); flaky.clear(); return {}

def err(status, msg, headers=None):
    meta = {"headers": headers} if headers else {}
    return JSONResponse({"error": {"message": msg, "code": status, "metadata": meta}}, status_code=status)

@app.post("/v1/chat/completions")
async def cc(r: Request):
    b = await r.json(); q = b["messages"][-1]["content"]; model = b["model"]; stream = bool(b.get("stream"))
    mode = next((m for m in ["MODEADV","MODEF12","MODEF1","MODEALL","MODEFLAKY","MODEQUOTA","MODEEMPTYA","MODEMID","MODE401","MODESLOW"] if m in q), "OK")
    log.append((mode, model)); n_for_q = sum(1 for m, _ in log if m == mode)

    # ---- failures ----
    if mode == "MODE401": return err(401, "No auth credentials found")
    if mode == "MODEQUOTA":
        return err(429, "Rate limit exceeded: free-models-per-day. Add 10 credits to unlock 1000 free model requests per day",
                   {"X-RateLimit-Limit": "50", "X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(int((time.time() + 3 * 3600) * 1000))})
    if mode == "MODEALL": return err(503, "Provider returned error")
    if mode == "MODEF1" and model == "openrouter/free": return err(429, "Provider returned error: temporarily rate-limited upstream")
    if mode == "MODEF12" and n_for_q <= 2: return err(429 if n_for_q == 1 else 503, "Provider returned error")
    if mode == "MODEFLAKY" and n_for_q <= 3: return err(503, "Provider returned error")   # whole first pass fails
    empty = mode == "MODEEMPTYA" and n_for_q == 1

    if not stream:
        text = "" if empty else ("[[ADVICE]]" if mode == "MODEADV" else ANSWER)
        return {"model": "fake/answering-model", "choices": [{"message": {"content": text}}]}

    async def g():
        yield ": OPENROUTER PROCESSING\n\n"
        if empty:
            yield chunk({"reasoning": "hmm"}); yield DONE; return
        if mode == "MODEADV":
            for part in ["[[AD", "VICE", "]]"]:
                yield chunk({"content": part}); await asyncio.sleep(0.03)
            yield DONE; return
        if mode == "MODESLOW": await asyncio.sleep(30)
        words = ANSWER.split(" ")
        for i, w in enumerate(words):
            yield chunk({"content": w + ("" if i == len(words) - 1 else " ")}); await asyncio.sleep(0.03)
            if mode == "MODEMID" and i == 3:
                yield 'data: {"error": {"message": "upstream died", "code": 502}}\n\n'; return
        yield DONE
    return StreamingResponse(g(), media_type="text/event-stream")

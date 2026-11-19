import json, os, re
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from app.retrieval import CHUNKS, UNKNOWN, is_unknown, retrieve  # noqa: F401  (re-exported for the eval)
from app import retrieval
from app.guard import MARKER, REFUSAL, is_investment_advice

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
MODEL = os.getenv("OPENROUTER_MODEL", "openrouter/free")
FALLBACKS = [m for m in os.getenv("OPENROUTER_FALLBACKS", "").split(",") if m]
API = os.getenv("OPENROUTER_API_URL", "https://openrouter.ai/api/v1/chat/completions")  # override only for tests

SYSTEM = (
    "You are a helpful assistant for two real-estate companies: DarGlobal (London-listed luxury "
    "developer) and Wasalt (Saudi property platform). Answer ONLY from the CONTEXT excerpts provided. "
    "If the context does not contain the answer, say you don't have that information and suggest "
    "contacting the company. Be concise, mention which company a fact belongs to, never invent prices, "
    "dates or figures, and don't give investment advice. Each excerpt is labelled with its company, page type and "
    "country. For questions that compare the two companies, cover each company separately using only its own "
    "excerpts, and say plainly if the excerpts have nothing on one of them. After each fact, cite the number of the "
    "excerpt it came from in square brackets, like [2] or [1][3]; cite only numbers that appear in the CONTEXT. If asked "
    "whether to buy or invest, or to predict prices or returns, decline briefly and offer the facts instead."
    f" SPECIAL RULE: if the user asks for investment advice or a recommendation (should I buy or invest, which is best "
    f"to invest in, whether something is a good or safe investment, what to do with their money) or asks you to predict "
    f"prices, returns or future value, reply with exactly {MARKER} and nothing else. Questions about what the companies "
    f"state or publish (prices, definitions such as ROI, investment opportunities they describe) are factual: answer them."
)


app = FastAPI(title="DarGlobal & Wasalt Assistant")


class Chat(BaseModel):
    message: str
    history: list[dict] = []


@app.get("/health")
def health():
    return {"ok": True, "chunks": len(CHUNKS), "model": MODEL, "embeddings": retrieval.EMB is not None}


def build(req):
    """retrieve context and build the OpenRouter request body"""
    ctx = retrieve(req.message)
    context = "\n\n".join(
        f"[{i+1}] {retrieval.SITE_NAME[c['site']]} | {c['page_type']} page"
        + (f" | {', '.join(c['countries'])}" if c["countries"] else "") + f" | {c['url']}\n{c['text']}"
        for i, c in enumerate(ctx)) or "(no relevant context found)"
    msgs = [{"role": "system", "content": SYSTEM + "\n\nCONTEXT:\n" + context}]
    msgs += [m for m in req.history[-6:] if m.get("role") in ("user", "assistant")]
    msgs.append({"role": "user", "content": req.message})
    body = {"model": MODEL, "messages": msgs, "temperature": 0.2, "max_tokens": 1500,
            "reasoning": {"effort": "low"}}
    if FALLBACKS:
        body["models"] = [MODEL] + FALLBACKS
    return ctx, body


def headers(key):
    return {"Authorization": f"Bearer {key}", "X-Title": "RealEstate Assistant"}


def explain_status(code, detail=""):
    print("openrouter error", code, detail[:300], flush=True)
    hint = {
        429: "Rate limit reached (the free tier allows limited requests per day). Please try again later.",
        401: "The server's OpenRouter API key was rejected (401).",
        402: "The OpenRouter account has insufficient credits (402).",
        404: "The configured model is unavailable (404).",
    }.get(code, "The model service returned an error. Please retry in a moment.")
    return f"{hint} [OpenRouter status {code}]"


def passages(ctx):
    """the retrieved excerpts exactly as the model saw them; n matches the [n] labels in the prompt and in citations"""
    return [{"n": i + 1, "company": retrieval.SITE_NAME[c["site"]], "page_type": c["page_type"], "countries": c["countries"],
             "url": c["url"], "title": c["title"], "text": c["text"]} for i, c in enumerate(ctx)]


def pick_sources(ctx, answer):
    if is_unknown(answer):
        return []  # don't cite pages for "I don't know" answers
    seen, sources = set(), []
    for c in ctx:
        if (c["url"], c["title"]) not in seen:
            seen.add((c["url"], c["title"])); sources.append({"title": c["title"] or c["url"], "url": c["url"]})
    return sources[:4]


@app.post("/api/chat")
async def chat(req: Chat):
    if is_investment_advice(req.message):  # decided before any model call: free, instant, consistent
        return {"answer": REFUSAL, "sources": [], "passages": [], "refused": True, "unknown": False}
    key = os.getenv("OPENROUTER_API_KEY")
    if not key:
        return JSONResponse({"answer": "Server is missing OPENROUTER_API_KEY.", "sources": []}, status_code=500)
    ctx, body = build(req)
    answer = None
    try:
        async with httpx.AsyncClient(timeout=60) as cx:
            # free models sometimes return empty/null content: retry a couple of times
            for _ in range(3):
                r = await cx.post(API, json=body, headers=headers(key))
                r.raise_for_status()
                ch = r.json()["choices"][0]
                answer = (ch["message"].get("content") or "").strip()
                if answer:
                    break
                print("empty reply", r.json().get("model"), ch.get("finish_reason"), flush=True)
    except httpx.HTTPStatusError as e:
        code = e.response.status_code
        try:
            detail = e.response.json().get("error", {}).get("message", "")
        except Exception:
            detail = ""
        return JSONResponse({"answer": explain_status(code, detail), "sources": []}, status_code=502)
    except Exception as e:
        return JSONResponse({"answer": f"Could not reach the model service ({type(e).__name__}). Please retry in a moment.", "sources": []}, status_code=502)
    if not answer:
        return JSONResponse({"answer": "The free model returned an empty reply. Please try again.", "sources": []}, status_code=502)
    if answer.strip().startswith(MARKER):  # layer 2: the model recognised an advice-seeking question
        return {"answer": REFUSAL, "sources": [], "passages": [], "refused": True, "unknown": False}
    return {"answer": answer, "sources": pick_sources(ctx, answer), "passages": passages(ctx),
            "refused": False, "unknown": is_unknown(answer)}


def sse(obj):
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


@app.post("/api/chat/stream")
async def chat_stream(req: Chat):
    """Server-Sent Events: {"start":true}, then {"t": "<token text>"} ..., then {"done":true,"sources":[...]}
    or {"error": "..."}. Tokens are forwarded as OpenRouter produces them."""
    if is_investment_advice(req.message):
        async def refuse():
            yield sse({"start": True, "found": 0, "pages": 0})
            yield sse({"t": REFUSAL})
            yield sse({"done": True, "sources": [], "passages": [], "refused": True, "unknown": False})
        return StreamingResponse(refuse(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    key = os.getenv("OPENROUTER_API_KEY")
    if not key:
        return JSONResponse({"answer": "Server is missing OPENROUTER_API_KEY.", "sources": []}, status_code=500)
    ctx, body = build(req)
    body["stream"] = True

    async def gen():
        # flush headers right away; also reports what retrieval found so the UI can show a real status line
        yield sse({"start": True, "found": len(ctx), "pages": len({(c["url"], c["title"]) for c in ctx})})
        answer, sent, advice = "", 0, False
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(90, read=75)) as cx:
                for _ in range(3):  # a free model sometimes streams nothing: retry only while no token was sent
                    async with cx.stream("POST", API, json=body, headers=headers(key)) as r:
                        if r.status_code != 200:
                            raw = (await r.aread()).decode("utf-8", "ignore")
                            try:
                                detail = json.loads(raw).get("error", {}).get("message", "")
                            except Exception:
                                detail = raw
                            yield sse({"error": explain_status(r.status_code, detail)})
                            return
                        async for line in r.aiter_lines():
                            if not line.startswith("data:"):
                                continue  # blank lines and ": OPENROUTER PROCESSING" keep-alive comments
                            data = line[5:].strip()
                            if data == "[DONE]":
                                break
                            try:
                                j = json.loads(data)
                            except ValueError:
                                continue
                            if j.get("error"):
                                yield sse({"error": "The model stopped mid-answer: " + str(j["error"].get("message", ""))[:200]})
                                return
                            delta = ((j.get("choices") or [{}])[0].get("delta") or {}).get("content")
                            if delta:
                                answer += delta
                                if sent == 0:  # hold back until we know whether the reply is the advice marker
                                    head = answer.lstrip()
                                    if len(head) < len(MARKER) and MARKER.startswith(head):
                                        continue
                                    if head.startswith(MARKER):
                                        advice = True
                                        break
                                    yield sse({"t": answer}); sent = len(answer)
                                else:
                                    yield sse({"t": delta}); sent = len(answer)
                    if advice or answer.strip():
                        break
                    print("empty stream", flush=True)
        except Exception as e:
            yield sse({"error": f"Could not reach the model service ({type(e).__name__}). Please retry in a moment."})
            return
        if advice:  # layer 2: the model recognised an advice-seeking question; send the standard refusal instead
            yield sse({"t": REFUSAL})
            yield sse({"done": True, "sources": [], "passages": [], "refused": True, "unknown": False})
            return
        if not answer.strip():
            yield sse({"error": "The free model returned an empty reply. Please try again."})
            return
        if sent == 0:  # the whole reply was a short prefix that we were still holding back
            yield sse({"t": answer}); sent = len(answer)
        yield sse({"done": True, "sources": pick_sources(ctx, answer.strip()), "passages": passages(ctx),
                   "refused": False, "unknown": is_unknown(answer.strip())})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


app.mount("/static", StaticFiles(directory=ROOT / "app" / "static"), name="static")


@app.get("/")
def index():
    return FileResponse(ROOT / "app" / "static" / "index.html")

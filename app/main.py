import json, os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import llm, retrieval
from app.guard import MARKER, REFUSAL, is_investment_advice
from app.retrieval import CHUNKS, UNKNOWN, is_unknown, retrieve  # noqa: F401  (re-exported for the eval)

ROOT = Path(__file__).resolve().parent.parent

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
    return {"ok": True, "chunks": len(CHUNKS), "models": llm.CHAIN, "embeddings": retrieval.EMB is not None}


def build(req):
    """retrieve context and build the OpenRouter request body (llm.py sets the model)"""
    ctx = retrieve(req.message)
    context = "\n\n".join(
        f"[{i+1}] {retrieval.SITE_NAME[c['site']]} | {c['page_type']} page"
        + (f" | {', '.join(c['countries'])}" if c["countries"] else "") + f" | {c['url']}\n{c['text']}"
        for i, c in enumerate(ctx)) or "(no relevant context found)"
    msgs = [{"role": "system", "content": SYSTEM + "\n\nCONTEXT:\n" + context}]
    msgs += [m for m in req.history[-6:] if m.get("role") in ("user", "assistant")]
    msgs.append({"role": "user", "content": req.message})
    return ctx, {"messages": msgs, "temperature": 0.2, "max_tokens": 1500, "reasoning": {"effort": "low"}}


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


def refusal_payload():
    return {"answer": REFUSAL, "sources": [], "passages": [], "refused": True, "unknown": False}


def unavailable_payload(u, ctx):
    """the model could not answer: a calm explanation plus the passages retrieval did find, so the app still helps"""
    return {"answer": u.message, "kind": u.kind, "code": u.code, "sources": [], "passages": passages(ctx),
            "refused": False, "unknown": False, "unavailable": True}


NO_KEY = llm.Unavailable("config", "The assistant isn't fully set up yet, so it can't write answers right now. "
                                   "The most relevant passages from the source pages are shown below.", 0)


@app.post("/api/chat")
async def chat(req: Chat):
    if is_investment_advice(req.message):  # layer 1: decided before any model call (free, instant, consistent)
        return refusal_payload()
    ctx, body = build(req)
    key = os.getenv("OPENROUTER_API_KEY")
    if not key:
        return JSONResponse(unavailable_payload(NO_KEY, ctx), status_code=503)
    try:
        answer, model = await llm.complete(body, key)
    except llm.Unavailable as u:
        return JSONResponse(unavailable_payload(u, ctx), status_code=503)
    if answer.startswith(MARKER):  # layer 2: the model recognised an advice-seeking question
        return refusal_payload()
    return {"answer": answer, "sources": pick_sources(ctx, answer), "passages": passages(ctx), "model": model,
            "refused": False, "unknown": is_unknown(answer)}


def sse(obj):
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


@app.post("/api/chat/stream")
async def chat_stream(req: Chat):
    """Server-Sent Events: {"start":true,...}, then {"t": "<token text>"} ..., then {"done":true,"sources":[...],
    "passages":[...]} or {"error": "...", "kind": "...", "passages": [...]}. Tokens are forwarded as they are produced."""
    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    if is_investment_advice(req.message):
        async def refuse():
            yield sse({"start": True, "found": 0, "pages": 0})
            yield sse({"t": REFUSAL})
            yield sse({"done": True, **{k: v for k, v in refusal_payload().items() if k != "answer"}})
        return StreamingResponse(refuse(), media_type="text/event-stream", headers=headers)
    ctx, body = build(req)
    key = os.getenv("OPENROUTER_API_KEY")
    if not key:
        async def nokey():
            p = unavailable_payload(NO_KEY, ctx)
            yield sse({"start": True, "found": len(ctx), "pages": len({(c["url"], c["title"]) for c in ctx})})
            yield sse({"error": p["answer"], "kind": p["kind"], "code": 0, "passages": p["passages"]})
        return StreamingResponse(nokey(), media_type="text/event-stream", headers=headers)

    async def gen():
        # flush headers right away; also reports what retrieval found so the UI can show a real status line
        yield sse({"start": True, "found": len(ctx), "pages": len({(c["url"], c["title"]) for c in ctx})})
        answer, sent, advice, info = "", 0, False, {}
        try:
            async for kind, delta in llm.stream(body, key, info):
                if kind == "note":  # falling back to another model: keep the UI informed
                    yield sse({"status": delta})
                    continue
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
        except llm.Unavailable as u:  # nothing was sent yet: explain calmly and still show the retrieved passages
            p = unavailable_payload(u, ctx)
            yield sse({"error": p["answer"], "kind": p["kind"], "code": p["code"], "passages": p["passages"]})
            return
        except llm.Interrupted as e:  # some text already reached the user: keep it, add a note
            yield sse({"error": "The AI stopped partway through this answer. Please try again.", "kind": "interrupted",
                       "passages": passages(ctx)})
            print("stream interrupted:", e, flush=True)
            return
        if advice:  # layer 2: the model recognised an advice-seeking question; send the standard refusal instead
            yield sse({"t": REFUSAL})
            yield sse({"done": True, "sources": [], "passages": [], "refused": True, "unknown": False})
            return
        if sent == 0 and answer:  # the whole reply was a short prefix that we were still holding back
            yield sse({"t": answer})
        yield sse({"done": True, "sources": pick_sources(ctx, answer.strip()), "passages": passages(ctx),
                   "model": info.get("model"), "refused": False, "unknown": is_unknown(answer.strip())})

    return StreamingResponse(gen(), media_type="text/event-stream", headers=headers)


app.mount("/static", StaticFiles(directory=ROOT / "app" / "static"), name="static")


@app.get("/")
def index():
    return FileResponse(ROOT / "app" / "static" / "index.html")

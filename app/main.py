import json, os, re
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from rank_bm25 import BM25Okapi

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
MODEL = os.getenv("OPENROUTER_MODEL", "openrouter/free")
FALLBACKS = [m for m in os.getenv("OPENROUTER_FALLBACKS", "").split(",") if m]
API = "https://openrouter.ai/api/v1/chat/completions"

SYSTEM = (
    "You are a helpful assistant for two real-estate companies: DarGlobal (London-listed luxury "
    "developer) and Wasalt (Saudi property platform). Answer ONLY from the CONTEXT excerpts provided. "
    "If the context does not contain the answer, say you don't have that information and suggest "
    "contacting the company. Be concise, mention which company a fact belongs to, never invent prices, "
    "dates or figures, and don't give investment advice."
)


def load_chunks():
    chunks = []
    for f in sorted(DATA.glob("*.jsonl")):
        for line in f.read_text(encoding="utf-8").split("\n"):
            if not line.strip():
                continue
            p = json.loads(line)
            text = p["text"]
            # ~900 char chunks on paragraph boundaries
            buf = ""
            for para in text.split("\n"):
                if len(buf) + len(para) > 900 and buf:
                    chunks.append({"site": p["site"], "url": p["url"], "title": p["title"], "text": buf.strip()})
                    buf = ""
                buf += para + "\n"
            if buf.strip():
                chunks.append({"site": p["site"], "url": p["url"], "title": p["title"], "text": buf.strip()})
    return chunks


tok = lambda s: [w[:-1] if len(w) > 3 and w.endswith("s") else w for w in re.findall(r"\w+", s.lower())]
CHUNKS = load_chunks()
BM25 = BM25Okapi([tok(c["title"] + " " + c["text"]) for c in CHUNKS]) if CHUNKS else None


def retrieve(q, k=6):
    if not BM25:
        return []
    scores = BM25.get_scores(tok(q))
    # light boost when the question names a company
    ql = q.lower()
    for i, c in enumerate(CHUNKS):
        if (c["site"] == "dar" and "dar" in ql) or (c["site"] == "wasalt" and "wasalt" in ql):
            scores[i] *= 1.3
    top = sorted(range(len(CHUNKS)), key=lambda i: scores[i], reverse=True)[:k]
    return [CHUNKS[i] for i in top if scores[i] > 0]


app = FastAPI(title="DarGlobal & Wasalt Assistant")


class Chat(BaseModel):
    message: str
    history: list[dict] = []


@app.get("/health")
def health():
    return {"ok": True, "chunks": len(CHUNKS), "model": MODEL}


@app.post("/api/chat")
async def chat(req: Chat):
    ctx = retrieve(req.message)
    context = "\n\n".join(f"[{i+1}] ({c['site']}) {c['url']}\n{c['text']}" for i, c in enumerate(ctx)) or "(no relevant context found)"
    msgs = [{"role": "system", "content": SYSTEM + "\n\nCONTEXT:\n" + context}]
    msgs += [m for m in req.history[-6:] if m.get("role") in ("user", "assistant")]
    msgs.append({"role": "user", "content": req.message})
    key = os.getenv("OPENROUTER_API_KEY")
    if not key:
        return JSONResponse({"answer": "Server is missing OPENROUTER_API_KEY.", "sources": []}, status_code=500)
    body = {"model": MODEL, "messages": msgs, "temperature": 0.2, "max_tokens": 1500,
            "reasoning": {"effort": "low"}}
    if FALLBACKS:
        body["models"] = [MODEL] + FALLBACKS
    answer = None
    try:
        async with httpx.AsyncClient(timeout=60) as cx:
            # free models sometimes return empty/null content: retry a couple of times
            for _ in range(3):
                r = await cx.post(API, json=body, headers={"Authorization": f"Bearer {key}", "X-Title": "RealEstate Assistant"})
                r.raise_for_status()
                ch = r.json()["choices"][0]
                answer = (ch["message"].get("content") or "").strip()
                if answer:
                    break
                print("empty reply", r.json().get("model"), ch.get("finish_reason"), flush=True)
    except Exception as e:
        return JSONResponse({"answer": f"The model is busy or rate-limited (free tier). Please retry in a moment. ({type(e).__name__})", "sources": []}, status_code=502)
    if not answer:
        return JSONResponse({"answer": "The free model returned an empty reply. Please try again.", "sources": []}, status_code=502)
    seen, sources = set(), []
    for c in ctx:
        if c["url"] not in seen:
            seen.add(c["url"]); sources.append({"title": c["title"] or c["url"], "url": c["url"]})
    return {"answer": answer, "sources": sources[:4]}


app.mount("/static", StaticFiles(directory=ROOT / "app" / "static"), name="static")


@app.get("/")
def index():
    return FileResponse(ROOT / "app" / "static" / "index.html")

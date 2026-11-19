import json, os, re
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from rank_bm25 import BM25Okapi

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
    # drop exact-duplicate chunks (e.g. several scraped pages that render the same project list)
    seen, unique = set(), []
    for c in chunks:
        if c["text"] not in seen:
            seen.add(c["text"]); unique.append(c)
    return unique


tok = lambda s: [w[:-1] if len(w) > 3 and w.endswith("s") else w for w in re.findall(r"\w+", s.lower())]
CHUNKS = load_chunks()
def _doc(c):
    path = re.sub(r"https?://[^/]+", "", c["url"]).replace("-", " ").replace("/", " ")  # URL words help ("/partners/...")
    return tok(c["title"] + " " + path + " " + c["text"])


BM25 = BM25Okapi([_doc(c) for c in CHUNKS]) if CHUNKS else None


STOP = set("""a an the of in on at to for from by with and or is are was were be been do does did have has had what who whom
which when where why how tell me about give show list any some there their it its this that these those can could would should
please i you we they us our your dar global darglobal wasalt company companies many much doe lists listed number total""".split())
UNKNOWN = re.compile(
    r"(don['’]t|do not|doesn['’]t|does not|cannot|can['’]t|no) (have|know|contain|include|provide|find|mention|specify|list)|"
    r"not (available|provided|mentioned|included|in the (provided )?(context|excerpts|information))|no (relevant )?information",
    re.I)
REL = 0.6  # keep chunks scoring at least this fraction of the best match


def retrieve(q, k=6):
    if not BM25:
        return []
    ql = q.lower()
    dar = bool(re.search(r"\bdar\b|darglobal|dar global", ql))
    # "project" is generic for DarGlobal (everything is a project) but meaningful for Wasalt
    terms = [t for t in tok(q) if t not in STOP and not (dar and t == "project")] or tok(q)
    scores = BM25.get_scores(terms)
    # if the question names exactly one company, strongly prefer that company's chunks
    was = "wasalt" in ql
    if dar != was:
        want = "dar" if dar else "wasalt"
        for i, c in enumerate(CHUNKS):
            scores[i] *= 1.0 if c["site"] == want else 0.15
    order = sorted(range(len(CHUNKS)), key=lambda i: scores[i], reverse=True)
    if not order or scores[order[0]] <= 0:
        return []
    cut = scores[order[0]] * REL
    out, per_url = [], {}
    for i in order[:40]:  # at most 2 chunks per page so answers can draw on several pages
        if scores[i] < cut or len(out) >= k:
            break
        u = (CHUNKS[i]["url"], CHUNKS[i]["title"])
        if per_url.get(u, 0) < 2:
            per_url[u] = per_url.get(u, 0) + 1
            out.append(CHUNKS[i])
    return out


app = FastAPI(title="DarGlobal & Wasalt Assistant")


class Chat(BaseModel):
    message: str
    history: list[dict] = []


@app.get("/health")
def health():
    return {"ok": True, "chunks": len(CHUNKS), "model": MODEL}


def build(req):
    """retrieve context and build the OpenRouter request body"""
    ctx = retrieve(req.message)
    context = "\n\n".join(f"[{i+1}] ({c['site']}) {c['url']}\n{c['text']}" for i, c in enumerate(ctx)) or "(no relevant context found)"
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


def pick_sources(ctx, answer):
    if UNKNOWN.search(answer[:240]):
        return []  # don't cite pages for "I don't know" answers
    seen, sources = set(), []
    for c in ctx:
        if (c["url"], c["title"]) not in seen:
            seen.add((c["url"], c["title"])); sources.append({"title": c["title"] or c["url"], "url": c["url"]})
    return sources[:4]


@app.post("/api/chat")
async def chat(req: Chat):
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
    return {"answer": answer, "sources": pick_sources(ctx, answer)}


def sse(obj):
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


@app.post("/api/chat/stream")
async def chat_stream(req: Chat):
    """Server-Sent Events: {"start":true}, then {"t": "<token text>"} ..., then {"done":true,"sources":[...]}
    or {"error": "..."}. Tokens are forwarded as OpenRouter produces them."""
    key = os.getenv("OPENROUTER_API_KEY")
    if not key:
        return JSONResponse({"answer": "Server is missing OPENROUTER_API_KEY.", "sources": []}, status_code=500)
    ctx, body = build(req)
    body["stream"] = True

    async def gen():
        yield sse({"start": True})  # flush headers right away so the browser knows the connection is alive
        answer = ""
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
                                yield sse({"t": delta})
                    if answer.strip():
                        break
                    print("empty stream", flush=True)
        except Exception as e:
            yield sse({"error": f"Could not reach the model service ({type(e).__name__}). Please retry in a moment."})
            return
        if not answer.strip():
            yield sse({"error": "The free model returned an empty reply. Please try again."})
            return
        yield sse({"done": True, "sources": pick_sources(ctx, answer.strip())})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


app.mount("/static", StaticFiles(directory=ROOT / "app" / "static"), name="static")


@app.get("/")
def index():
    return FileResponse(ROOT / "app" / "static" / "index.html")

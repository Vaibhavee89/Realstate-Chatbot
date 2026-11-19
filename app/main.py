import asyncio, csv, hmac, io, json, os, re, time, uuid
from datetime import date
from pathlib import Path

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import llm, retrieval, store
from app.guard import MARKER, REFUSAL, is_investment_advice
from app.handoff import HANDOFF, contacts_for, wants_handoff
from app.retrieval import CHUNKS, UNKNOWN, is_unknown, retrieve  # noqa: F401  (re-exported for the eval)

ROOT = Path(__file__).resolve().parent.parent
LEAD_WEBHOOK_URL = os.getenv("LEAD_WEBHOOK_URL")  # optional: every saved lead is also POSTed here (e.g. a Slack webhook)

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
    f" SPECIAL RULE 1: if the user asks for investment advice or a recommendation (should I buy or invest, which is best "
    f"to invest in, whether something is a good or safe investment, what to do with their money) or asks you to predict "
    f"prices, returns or future value, reply with exactly {MARKER} and nothing else. Questions about what the companies "
    f"state or publish (prices, definitions such as ROI, investment opportunities they describe) are factual: answer them."
    f" SPECIAL RULE 2: if the user says they want to buy, rent, reserve, view or enquire about a property, or asks to "
    f"speak to someone, start your reply with {HANDOFF} and then answer helpfully from the excerpts (a contact form "
    f"is shown to them automatically, so do not invent contact details). Do not use {HANDOFF} for ordinary factual questions."
)

app = FastAPI(title="DarGlobal & Wasalt Assistant")


class Chat(BaseModel):
    message: str
    history: list[dict] = []


STALE_DAYS = 30  # warn in the UI when the data is older than this


def data_meta():
    """the data-freshness record written by scraper/build_meta.py (None if the file is missing)"""
    f = ROOT / "data" / "meta.json"
    if not f.exists():
        return None
    meta = json.loads(f.read_text())
    try:
        age = (date.today() - date.fromisoformat(meta["scraped_at"])).days
    except (KeyError, ValueError):
        age = None
    return {**meta, "age_days": age, "stale": age is not None and age > STALE_DAYS}


@app.get("/api/meta")
def meta():
    return data_meta() or {}


@app.get("/health")
def health():
    m = data_meta() or {}
    return {"ok": True, "chunks": len(CHUNKS), "models": llm.CHAIN, "embeddings": retrieval.EMB is not None,
            "data_scraped_at": m.get("scraped_at")}


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


def handoff_payload(question, ctx):
    return {"contacts": contacts_for(question, [c["site"] for c in ctx])}


def refusal_payload():
    return {"answer": REFUSAL, "sources": [], "passages": [], "refused": True, "unknown": False}


def unavailable_payload(u, ctx):
    """the model could not answer: a calm explanation plus the passages retrieval did find, so the app still helps"""
    return {"answer": u.message, "kind": u.kind, "code": u.code, "sources": [], "passages": passages(ctx),
            "refused": False, "unknown": False, "unavailable": True}


NO_KEY = llm.Unavailable("config", "The assistant isn't fully set up yet, so it can't write answers right now. "
                                   "The most relevant passages from the source pages are shown below.", 0)


def head_state(buf):
    """what the first characters of a reply are: a marker, a prefix of one (keep holding), or ordinary text"""
    head = buf.lstrip()
    for m in (MARKER, HANDOFF):
        if head.startswith(m):
            return m, head[len(m):].lstrip()
    if any(m.startswith(head) for m in (MARKER, HANDOFF)):
        return "hold", ""
    return None, buf


def ms(t0):
    return int((time.perf_counter() - t0) * 1000)


# ------------------------------------------------------------------ chat
@app.post("/api/chat")
async def chat(req: Chat):
    t0, cid = time.perf_counter(), uuid.uuid4().hex[:12]
    if is_investment_advice(req.message):  # layer 1: decided before any model call (free, instant, consistent)
        store.log_chat(cid, req.message, "refused_regex", total_ms=ms(t0))
        return {**refusal_payload(), "chat_id": cid}
    ctx, body = build(req)
    t_ret = ms(t0)
    key = os.getenv("OPENROUTER_API_KEY")
    if not key:
        store.log_chat(cid, req.message, "unavailable", retrieval_ms=t_ret, total_ms=ms(t0), error_kind="config",
                       n_passages=len(ctx), sources=[c["url"] for c in ctx])
        return JSONResponse({**unavailable_payload(NO_KEY, ctx), "chat_id": cid}, status_code=503)
    info = {}
    try:
        answer, model = await llm.complete(body, key, info)
    except llm.Unavailable as u:
        store.log_chat(cid, req.message, "unavailable", retrieval_ms=t_ret, total_ms=ms(t0), error_kind=u.kind,
                       attempts=info.get("attempts") or len(u.tried), n_passages=len(ctx), sources=[c["url"] for c in ctx])
        return JSONResponse({**unavailable_payload(u, ctx), "chat_id": cid}, status_code=503)
    state, rest = head_state(answer)
    if state == MARKER:  # layer 2: the model recognised an advice-seeking question
        store.log_chat(cid, req.message, "refused_model", model=model, retrieval_ms=t_ret, total_ms=ms(t0),
                       attempts=info.get("attempts"))
        return {**refusal_payload(), "chat_id": cid}
    handoff = wants_handoff(req.message) or state == HANDOFF
    if state == HANDOFF:
        answer = rest or answer
    unknown = is_unknown(answer)
    store.log_chat(cid, req.message, "idk" if unknown else "answered", model=model, retrieval_ms=t_ret, total_ms=ms(t0),
                   attempts=info.get("attempts"), n_passages=len(ctx), sources=[c["url"] for c in ctx], handoff=handoff)
    out = {"answer": answer, "sources": pick_sources(ctx, answer), "passages": passages(ctx), "model": model,
           "refused": False, "unknown": unknown, "chat_id": cid, "handoff": False}
    if handoff:
        out["handoff"] = handoff_payload(req.message, ctx)
    return out


def sse(obj):
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


@app.post("/api/chat/stream")
async def chat_stream(req: Chat):
    """Server-Sent Events: {"start":true,...}, then {"t": "<token text>"} ..., then {"done":true,"sources":[...],
    "passages":[...],"chat_id":...} or {"error": "...", "kind": "...", "passages": [...]}. Tokens are forwarded as
    they are produced."""
    t0, cid = time.perf_counter(), uuid.uuid4().hex[:12]
    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    if is_investment_advice(req.message):
        store.log_chat(cid, req.message, "refused_regex", total_ms=ms(t0))

        async def refuse():
            yield sse({"start": True, "found": 0, "pages": 0, "chat_id": cid})
            yield sse({"t": REFUSAL})
            yield sse({"done": True, **{k: v for k, v in refusal_payload().items() if k != "answer"}, "chat_id": cid})
        return StreamingResponse(refuse(), media_type="text/event-stream", headers=headers)
    ctx, body = build(req)
    t_ret = ms(t0)
    srcs = [c["url"] for c in ctx]
    key = os.getenv("OPENROUTER_API_KEY")
    if not key:
        store.log_chat(cid, req.message, "unavailable", retrieval_ms=t_ret, total_ms=ms(t0), error_kind="config",
                       n_passages=len(ctx), sources=srcs)

        async def nokey():
            p = unavailable_payload(NO_KEY, ctx)
            yield sse({"start": True, "found": len(ctx), "pages": len({(c["url"], c["title"]) for c in ctx}), "chat_id": cid})
            yield sse({"error": p["answer"], "kind": p["kind"], "code": 0, "passages": p["passages"], "chat_id": cid})
        return StreamingResponse(nokey(), media_type="text/event-stream", headers=headers)

    async def gen():
        # flush headers right away; also reports what retrieval found so the UI can show a real status line
        yield sse({"start": True, "found": len(ctx), "pages": len({(c["url"], c["title"]) for c in ctx}), "chat_id": cid})
        answer, buf, started, advice, marker_handoff, info = "", "", False, False, False, {}
        ttft, outcome, err = None, "aborted", None  # "aborted" unless we reach a proper end (client closed the stream)
        try:
            async for kind, delta in llm.stream(body, key, info):
                if kind == "note":  # falling back to another model: keep the UI informed
                    yield sse({"status": delta})
                    continue
                if not started:  # hold back until we know whether the reply starts with a marker
                    buf += delta
                    state, rest = head_state(buf)
                    if state == "hold":
                        continue
                    if state == MARKER:
                        advice = True
                        break
                    marker_handoff = state == HANDOFF
                    started = True
                    if rest:
                        answer = rest
                        ttft = ttft or ms(t0)
                        yield sse({"t": rest})
                    continue
                answer += delta
                ttft = ttft or ms(t0)
                yield sse({"t": delta})
            if advice:  # layer 2: the model recognised an advice-seeking question; send the standard refusal instead
                outcome = "refused_model"
                yield sse({"t": REFUSAL})
                yield sse({"done": True, "sources": [], "passages": [], "refused": True, "unknown": False, "chat_id": cid})
                return
            if not started and buf:  # the whole reply was a short prefix that we were still holding back
                answer = buf
                ttft = ttft or ms(t0)
                yield sse({"t": buf})
            handoff = wants_handoff(req.message) or marker_handoff
            unknown = is_unknown(answer.strip())
            outcome = "idk" if unknown else "answered"
            done = {"done": True, "sources": pick_sources(ctx, answer.strip()), "passages": passages(ctx),
                    "model": info.get("model"), "refused": False, "unknown": unknown, "chat_id": cid, "handoff": False}
            if handoff:
                done["handoff"] = handoff_payload(req.message, ctx)
            yield sse(done)
        except llm.Unavailable as u:  # nothing was sent yet: explain calmly and still show the retrieved passages
            outcome, err = "unavailable", u.kind
            p = unavailable_payload(u, ctx)
            yield sse({"error": p["answer"], "kind": p["kind"], "code": p["code"], "passages": p["passages"], "chat_id": cid})
        except llm.Interrupted as e:  # some text already reached the user: keep it, add a note
            outcome, err = "interrupted", "interrupted"
            print("stream interrupted:", e, flush=True)
            yield sse({"error": "The AI stopped partway through this answer. Please try again.", "kind": "interrupted",
                       "passages": passages(ctx), "chat_id": cid})
        finally:
            store.log_chat(cid, req.message, outcome, model=info.get("model"), retrieval_ms=t_ret, ttft_ms=ttft,
                           total_ms=ms(t0), attempts=info.get("attempts"), n_passages=len(ctx), sources=srcs,
                           handoff=(wants_handoff(req.message) or marker_handoff) and outcome in ("answered", "idk"),
                           error_kind=err)

    return StreamingResponse(gen(), media_type="text/event-stream", headers=headers)


# ------------------------------------------------------------------ feedback, leads, admin
_hits = {}


def allow(key, limit, window=3600):
    """tiny in-memory rate limiter (per process; fine for a single small instance)"""
    now = time.time()
    q = [t for t in _hits.get(key, []) if now - t < window]
    ok = len(q) < limit
    if ok:
        q.append(now)
    _hits[key] = q
    return ok


def client_ip(request: Request):
    xff = request.headers.get("x-forwarded-for")  # Render sits behind a proxy
    return xff.split(",")[0].strip() if xff else (request.client.host if request.client else "?")


class Feedback(BaseModel):
    chat_id: str
    value: int
    comment: str = ""


@app.post("/api/feedback")
async def feedback(fb: Feedback, request: Request):
    if fb.value not in (1, -1):
        raise HTTPException(400, "value must be 1 (thumbs up) or -1 (thumbs down)")
    if not allow(("fb", client_ip(request)), 60):
        raise HTTPException(429, "Too many ratings, please slow down.")
    if not store.set_feedback(fb.chat_id, fb.value, fb.comment):
        raise HTTPException(404, "Unknown chat id")
    return {"ok": True}


class Lead(BaseModel):
    name: str = ""
    email: str = ""
    phone: str = ""
    company: str = "either"   # dar | wasalt | either
    message: str = ""
    source_question: str = ""
    chat_id: str = ""
    consent: bool = False
    website: str = ""         # honeypot: real users never see or fill this field


EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s]{2,}$")


async def send_webhook(lead):
    """best-effort copy of a lead to LEAD_WEBHOOK_URL (Slack-compatible 'text' plus the full record)"""
    try:
        text = (f"New enquiry from the demo chatbot: {lead['name']} · {lead['email'] or lead['phone']} · "
                f"interested in {lead['company']}: {lead['message'][:300] or '(no message)'}")
        async with httpx.AsyncClient(timeout=5) as cx:
            await cx.post(LEAD_WEBHOOK_URL, json={"text": text, "lead": lead})
    except Exception as e:
        print("lead webhook failed:", type(e).__name__, flush=True)


@app.post("/api/lead")
async def lead(l: Lead, request: Request):
    if l.website.strip():  # a bot filled the honeypot: pretend success, save nothing
        return {"ok": True}
    errors = {}
    name = l.name.strip()
    email, phone = l.email.strip(), l.phone.strip()
    if not 1 <= len(name) <= 80:
        errors["name"] = "Please enter your name."
    if not email and not phone:
        errors["contact"] = "Please give an email address or a phone number."
    if email and not EMAIL.match(email):
        errors["email"] = "That email address doesn't look right."
    if phone and not 7 <= len(re.sub(r"\D", "", phone)) <= 15:
        errors["phone"] = "That phone number doesn't look right."
    if len(l.message) > 1000:
        errors["message"] = "Please keep the message under 1000 characters."
    if not l.consent:
        errors["consent"] = "Please tick the box to agree."
    company = l.company if l.company in ("dar", "wasalt", "either") else "either"
    if errors:
        return JSONResponse({"ok": False, "errors": errors}, status_code=400)
    if not allow(("lead", client_ip(request)), 5):
        return JSONResponse({"ok": False, "errors": {"form": "Too many enquiries from this connection; please try again later."}},
                            status_code=429)
    try:
        lead_id = store.add_lead(name, email or None, phone or None, company, l.message.strip(), l.source_question,
                                 l.chat_id, l.consent)
    except Exception as e:
        print("lead not saved:", type(e).__name__, e, flush=True)
        return JSONResponse({"ok": False, "errors": {"form": "Sorry, your enquiry could not be saved. Please use the "
                                                              "company contact links instead."}}, status_code=500)
    if LEAD_WEBHOOK_URL:
        asyncio.create_task(send_webhook({"id": lead_id, "name": name, "email": email, "phone": phone, "company": company,
                                          "message": l.message.strip(), "source_question": l.source_question}))
    return {"ok": True, "id": lead_id}


def require_admin(authorization: str | None, x_admin_token: str | None):
    token = os.getenv("ADMIN_TOKEN")
    if not token:
        raise HTTPException(404)  # admin endpoints do not exist unless ADMIN_TOKEN is set
    given = x_admin_token or (authorization or "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(given.encode(), token.encode()):
        raise HTTPException(401, "Invalid admin token")


@app.get("/admin/stats")
def admin_stats(authorization: str | None = Header(None), x_admin_token: str | None = Header(None)):
    require_admin(authorization, x_admin_token)
    return store.stats()


@app.get("/admin/leads")
def admin_leads(authorization: str | None = Header(None), x_admin_token: str | None = Header(None)):
    require_admin(authorization, x_admin_token)
    return store.leads()


@app.get("/admin/leads.csv")
def admin_leads_csv(authorization: str | None = Header(None), x_admin_token: str | None = Header(None)):
    require_admin(authorization, x_admin_token)
    rows = store.leads(5000)
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=["id", "ts", "name", "email", "phone", "company", "message", "source_question",
                                        "chat_id", "consent"])
    w.writeheader()
    w.writerows(rows)
    return PlainTextResponse(out.getvalue(), media_type="text/csv")


@app.get("/admin", response_class=HTMLResponse)
def admin_page():
    if not os.getenv("ADMIN_TOKEN"):
        raise HTTPException(404)
    return FileResponse(ROOT / "app" / "static" / "admin.html")


app.mount("/static", StaticFiles(directory=ROOT / "app" / "static"), name="static")


@app.get("/")
def index():
    return FileResponse(ROOT / "app" / "static" / "index.html")

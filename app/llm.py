"""Calling OpenRouter's free models resiliently.

* A chain of :free models from different providers is tried in order (a rate limit on one provider should not take
  down the others). Default: openrouter/free -> google/gemma-4-31b-it:free -> nvidia/nemotron-3-ultra-550b-a55b:free.
  Override with OPENROUTER_MODEL (first) and OPENROUTER_FALLBACKS (comma-separated; empty = no fallbacks).
* If the whole chain fails for a transient reason (busy, upstream rate limit, 5xx, timeout, empty reply) it is retried
  ONCE after a short pause. The daily free quota is shared by all free models, so when it is exhausted the chain stops
  at once instead of burning requests, and the caller gets a friendly "quota used up" message with the reset time.
* While streaming, fallback only happens before the first token reaches the user; a failure after that surfaces as
  `Interrupted` (the caller keeps the partial text).
"""
import asyncio, json, os, time
from datetime import datetime, timezone

import httpx

API = os.getenv("OPENROUTER_API_URL", "https://openrouter.ai/api/v1/chat/completions")  # override only for tests
PRIMARY = os.getenv("OPENROUTER_MODEL", "openrouter/free")
_DEFAULT_FALLBACKS = "google/gemma-4-31b-it:free,nvidia/nemotron-3-ultra-550b-a55b:free"
_fb = os.getenv("OPENROUTER_FALLBACKS")
CHAIN = list(dict.fromkeys([PRIMARY] + [m.strip() for m in (_DEFAULT_FALLBACKS if _fb is None else _fb).split(",") if m.strip()]))
READ_TIMEOUT = float(os.getenv("LLM_READ_TIMEOUT", "25"))   # max wait for the next chunk (first token included)
DEADLINE = float(os.getenv("LLM_DEADLINE", "60"))           # stop starting new attempts after this many seconds
RETRY_PAUSE = float(os.getenv("LLM_RETRY_PAUSE", "1.5"))


class Fail(Exception):
    """one attempt failed before any text was produced"""
    def __init__(self, kind, code=0, detail="", reset=None):
        super().__init__(kind)
        self.kind, self.code, self.detail, self.reset = kind, code, detail, reset


class Interrupted(Exception):
    """the stream broke after text had already been sent"""


class Unavailable(Exception):
    """the whole chain failed; `message` is safe to show to a user"""
    def __init__(self, kind, message, code=0, tried=()):
        super().__init__(message)
        self.kind, self.message, self.code, self.tried = kind, message, code, list(tried)


def _timeout():
    return httpx.Timeout(connect=10, read=READ_TIMEOUT, write=10, pool=10)


def _hdrs(key):
    return {"Authorization": f"Bearer {key}", "X-Title": "RealEstate Assistant"}


def classify(status, raw):
    """map an OpenRouter error response to a Fail"""
    try:
        err = json.loads(raw).get("error", {})
    except Exception:
        err = {}
    msg = str(err.get("message", raw))[:300]
    low = msg.lower()
    reset = None
    try:
        reset = int((err.get("metadata", {}).get("headers", {}) or {}).get("X-RateLimit-Reset"))
    except (TypeError, ValueError):
        pass
    if status == 429:
        daily = "per-day" in low or "per day" in low or "daily" in low
        return Fail("quota" if daily else "busy", status, msg, reset)
    if status in (401, 403):
        return Fail("auth", status, msg)
    if status == 402:
        return Fail("credit", status, msg)
    if status in (404, 400, 422):  # model removed / does not accept a parameter: try the next one
        return Fail("gone", status, msg)
    return Fail("busy", status, msg)  # 5xx, 408, ...


def friendly(kind, tried, reset=None):
    """calm wording for the user; never makes the app sound broken"""
    if kind == "quota":
        when = ""
        if reset:
            try:
                when = " It resets at about " + datetime.fromtimestamp(reset / 1000, timezone.utc).strftime("%H:%M UTC") + "."
            except (OverflowError, OSError, ValueError):
                pass
        return ("The free AI quota for today has been used up, so I can't write a new answer right now." + (
            when or " It resets daily around midnight UTC.") + " The most relevant passages from the source pages are shown below.")
    if kind == "busy":
        n = len({m for m, _ in tried})
        return (f"The free AI models are busy right now{f' (I tried {n} of them)' if n else ''}. Please try again in a minute. "
                "The most relevant passages from the source pages are shown below in the meantime.")
    return ("The AI service is temporarily unavailable. Please try again later. "
            "The most relevant passages from the source pages are shown below in the meantime.")


def _payload(body, model, stream):
    b = {k: v for k, v in body.items() if k not in ("model", "models")}
    b["model"] = model
    if stream:
        b["stream"] = True
    return b


async def _once(cx, body, model, key):
    r = await cx.post(API, json=_payload(body, model, False), headers=_hdrs(key))
    if r.status_code != 200:
        raise classify(r.status_code, r.text)
    j = r.json()
    if j.get("error"):
        raise Fail("busy", int(j["error"].get("code") or 0), str(j["error"].get("message", ""))[:300])
    text = ((j.get("choices") or [{}])[0].get("message", {}).get("content") or "").strip()
    if not text:
        raise Fail("empty")
    return text, j.get("model") or model


def _unavailable(last, tried):
    kind = last.kind if last and last.kind in ("quota", "auth", "credit") else "busy"
    if kind in ("auth", "credit"):
        print("openrouter config problem:", last.code, last.detail, flush=True)
    return Unavailable(kind, friendly(kind, tried, last.reset if last else None), last.code if last else 0, tried)


async def complete(body, key, info=None):
    """-> (text, model that answered). Raises Unavailable. info["attempts"] = models tried (for the log)."""
    info = {} if info is None else info
    t0, tried, last = time.monotonic(), [], None
    async with httpx.AsyncClient(timeout=_timeout()) as cx:
        for round_ in range(2):  # the chain, then ONE retry of the chain after a pause
            transient = False
            for model in CHAIN:
                if time.monotonic() - t0 > DEADLINE:
                    break
                try:
                    result = await _once(cx, body, model, key)
                    info["attempts"] = len(tried) + 1
                    return result
                except Fail as e:
                    last = e
                except httpx.HTTPError as e:
                    last = Fail("busy", 0, type(e).__name__)
                tried.append((model, last.kind))
                print("llm attempt failed:", model, last.kind, last.code, last.detail[:120], flush=True)
                if last.kind in ("quota", "auth", "credit"):
                    raise _unavailable(last, tried)  # every free model shares the quota; auth/credit affect all
                transient = transient or last.kind in ("busy", "empty")
            if round_ == 0 and transient and time.monotonic() - t0 < DEADLINE:
                await asyncio.sleep(RETRY_PAUSE)
            else:
                break
    info["attempts"] = len(tried)
    raise _unavailable(last, tried)


async def _stream_once(cx, body, model, key, info):
    started = False
    try:
        async with cx.stream("POST", API, json=_payload(body, model, True), headers=_hdrs(key)) as r:
            if r.status_code != 200:
                raise classify(r.status_code, (await r.aread()).decode("utf-8", "ignore"))
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
                    if started:
                        raise Interrupted(str(j["error"].get("message", ""))[:200])
                    raise Fail("busy", int(j["error"].get("code") or 0), str(j["error"].get("message", ""))[:300])
                delta = ((j.get("choices") or [{}])[0].get("delta") or {}).get("content")
                if delta:
                    if not started:
                        started = True
                        info["model"] = j.get("model") or model
                    yield delta
    except httpx.HTTPError as e:
        if started:
            raise Interrupted(type(e).__name__)
        raise Fail("busy", 0, type(e).__name__)
    if not started:
        raise Fail("empty")


async def stream(body, key, info):
    """async generator of ("t", text delta) and ("note", status text) items; info["model"] is set when the first
    token arrives. Notes ("trying another free model...") let the UI show progress while falling back.
    Raises Unavailable (nothing was sent yet) or Interrupted (some text was already sent)."""
    t0, tried, last = time.monotonic(), [], None
    async with httpx.AsyncClient(timeout=_timeout()) as cx:
        for round_ in range(2):
            transient = False
            for model in CHAIN:
                if time.monotonic() - t0 > DEADLINE:
                    break
                got = False
                try:
                    async for delta in _stream_once(cx, body, model, key, info):
                        if not got:
                            info["attempts"] = len(tried) + 1
                        got = True
                        yield "t", delta
                    return
                except Fail as e:
                    last = e
                except Interrupted:
                    raise
                if got:  # cannot happen (a Fail after text is an Interrupted), kept as a guard
                    return
                tried.append((model, last.kind))
                print("llm stream attempt failed:", model, last.kind, last.code, last.detail[:120], flush=True)
                if last.kind in ("quota", "auth", "credit"):
                    raise _unavailable(last, tried)
                transient = transient or last.kind in ("busy", "empty")
                if model != CHAIN[-1]:
                    yield "note", "That model is busy · trying another free model…"
            if round_ == 0 and transient and time.monotonic() - t0 < DEADLINE:
                yield "note", "Still busy · retrying once more…"
                await asyncio.sleep(RETRY_PAUSE)
            else:
                break
    info["attempts"] = len(tried)
    raise _unavailable(last, tried)

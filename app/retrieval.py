"""Chunking, chunk metadata, and hybrid (BM25 + small embeddings) retrieval.

Every chunk carries metadata: company ("dar" | "wasalt"), page_type (partner, press, projects, catalog, ...) and
countries (Oman, UAE, Saudi Arabia, Qatar, Spain, UK). Retrieval blends keyword (BM25) and semantic (embedding)
scores, then uses the metadata: it can restrict to one company, boost a country named in the question, and
balance results across both companies for comparison questions.

Embeddings are optional: if model2vec or the prebuilt index is unavailable, retrieval falls back to BM25 only.
"""
import hashlib, json, os, re
from pathlib import Path

import numpy as np
from rank_bm25 import BM25Okapi

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
INDEX_DIR = DATA / "index"
EMBED_MODEL = os.getenv("EMBED_MODEL", "minishlab/potion-base-8M")
USE_EMBEDDINGS = os.getenv("USE_EMBEDDINGS", "1") != "0"
ALPHA = float(os.getenv("HYBRID_ALPHA", "0.6"))  # weight of BM25 in the blend (1 = BM25 only, 0 = embeddings only)
REL = float(os.getenv("REL_CUTOFF", "0.6"))      # keep chunks scoring at least this fraction of the best match

# ---------------------------------------------------------------- metadata
COUNTRIES = {
    "Oman": r"\boman\b|muscat|\baida\b|salalah",
    "UAE": r"\buae\b|dubai|abu dhabi|ras al khaimah|\brak\b|al marjan|emirates|jumeirah|business bay",
    "Saudi Arabia": r"saudi|\bksa\b|riyadh|jeddah|makkah|madinah|diriyah|dammam|khobar|\bwasalt\b",
    "Qatar": r"qatar|doha|lusail|qetaifan",
    "Spain": r"\bspain\b|marbella|benahav|costa del sol|malaga|málaga",
    "UK": r"united kingdom|\buk\b|london|mayfair|hyde park|old park lane",
}
COUNTRY_RE = {k: re.compile(v, re.I) for k, v in COUNTRIES.items()}
SITE_NAME = {"dar": "DarGlobal", "wasalt": "Wasalt"}


def page_type(site, url, source_file):
    if site == "wasalt":
        if source_file == "wasalt_catalog":
            return "catalog"
        if "project=true" in url:
            return "projects"
        if "properties-for-sale" in url:
            return "listings"
        return "about"
    path = re.sub(r"https?://[^/]+", "", url).strip("/")
    first = path.split("/")[0] if path else "home"
    return {"partners": "partner", "press": "press", "insights": "insight", "blog": "blog", "faq": "faq",
            "projects": "projects", "landing-page": "landing", "one-of-one": "one-of-one",
            "investor": "investor", "home": "home"}.get(first, first)


def tag_countries(title, url, text):
    """countries a chunk is about: those with enough mentions relative to the strongest one"""
    blob = f"{title} {url} {text}"
    counts = {c: len(r.findall(blob)) for c, r in COUNTRY_RE.items()}
    top = max(counts.values())
    return [c for c, n in counts.items() if n >= 2 and n >= 0.3 * top] if top else []



# ---------------------------------------------------------------- chunking
def load_chunks():
    chunks = []
    for f in sorted(DATA.glob("*.jsonl")):
        for line in f.read_text(encoding="utf-8").split("\n"):
            if not line.strip():
                continue
            p = json.loads(line)
            ptype = page_type(p["site"], p["url"], f.stem)
            buf = ""
            for para in p["text"].split("\n"):  # ~900 char chunks on paragraph boundaries
                if len(buf) + len(para) > 900 and buf:
                    chunks.append(_chunk(p, ptype, buf))
                    buf = ""
                buf += para + "\n"
            if buf.strip():
                chunks.append(_chunk(p, ptype, buf))
    seen, unique = set(), []  # drop exact duplicates (several scraped pages render the same project list)
    for c in chunks:
        if c["text"] not in seen:
            seen.add(c["text"]); unique.append(c)
    return unique


def _chunk(p, ptype, text):
    text = text.strip()
    return {"site": p["site"], "url": p["url"], "title": p["title"], "text": text, "page_type": ptype,
            "countries": tag_countries(p["title"], p["url"], text)}


tok = lambda s: [w[:-1] if len(w) > 3 and w.endswith("s") else w for w in re.findall(r"\w+", s.lower())]


def _doc(c):
    path = re.sub(r"https?://[^/]+", "", c["url"]).replace("-", " ").replace("/", " ")  # URL words help ("/partners/...")
    return tok(c["title"] + " " + path + " " + c["text"])


def embed_text(c):
    """metadata is part of the embedded text so e.g. 'Oman' and 'partner page' influence similarity"""
    return f"{SITE_NAME[c['site']]} {c['page_type']} {' '.join(c['countries'])}. {c['title']}. {c['text']}"


def chunks_hash(chunks):
    h = hashlib.sha256(EMBED_MODEL.encode())
    for c in chunks:
        h.update(embed_text(c).encode())
    return h.hexdigest()


CHUNKS = load_chunks()
BM25 = BM25Okapi([_doc(c) for c in CHUNKS]) if CHUNKS else None

# ---------------------------------------------------------------- embeddings (optional)
EMB = None      # (n_chunks, dim) unit vectors
MODEL = None


def _load_embeddings():
    global EMB, MODEL
    if not USE_EMBEDDINGS:
        return
    try:
        from model2vec import StaticModel

        f = INDEX_DIR / "embeddings.npz"
        if f.exists():
            z = np.load(f, allow_pickle=False)
            if str(z["hash"]) == chunks_hash(CHUNKS):
                EMB = z["emb"]
        MODEL = StaticModel.from_pretrained(EMBED_MODEL)
        if EMB is None:  # index missing or stale: compute now (slow-ish, but only happens if build step was skipped)
            EMB = _encode([embed_text(c) for c in CHUNKS])
    except Exception as e:  # keep working on keywords alone
        print("embeddings disabled:", type(e).__name__, str(e)[:200], flush=True)
        EMB, MODEL = None, None


def _encode(texts):
    v = MODEL.encode(texts).astype(np.float32)
    return v / np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-9)


def build_index():
    """precompute chunk embeddings (run at Docker build time: python -m app.build_index)"""
    from model2vec import StaticModel

    global MODEL
    MODEL = StaticModel.from_pretrained(EMBED_MODEL)
    emb = _encode([embed_text(c) for c in CHUNKS])
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(INDEX_DIR / "embeddings.npz", emb=emb, hash=np.array(chunks_hash(CHUNKS)))
    print(f"embedded {len(CHUNKS)} chunks, dim {emb.shape[1]} -> {INDEX_DIR / 'embeddings.npz'}")


# ---------------------------------------------------------------- retrieval
STOP = set("""a an the of in on at to for from by with and or is are was were be been do does did have has had what who whom
which when where why how tell me about give show list any some there their it its this that these those can could would should
please i you we they us our your dar global darglobal wasalt company companies many much doe lists listed number total""".split())
UNKNOWN = re.compile(
    r"(don['’]t|do not|doesn['’]t|does not|cannot|can['’]t|no) (have|know|contain|include|provide|find|mention|specify|list)|"
    r"not (available|provided|mentioned|included|in the (provided )?(context|excerpts|information))|no (relevant )?information",
    re.I)
COMPARE = re.compile(r"\bcompar|\bvs\.?\b|versus|differ|difference|\bboth\b|each company|which company|which of", re.I)
TYPE_HINTS = [(r"\b(news|press|announce\w*|launch\w*)\b", {"press", "blog"}), (r"\bfaq\b|frequently asked", {"faq"}),
              (r"\binvestor|investment relations|shareholder", {"investor"}), (r"\bpartner\w*|\bbrands?\b", {"partner", "home", "investor"})]


SAUDI_CITIES = re.compile(r"saudi|\bksa\b|riyadh|jeddah|makkah|madinah|diriyah|dammam|khobar", re.I)


def analyze(q):
    """what the question is about: companies named, comparison intent, countries, page-type hints"""
    dar = bool(re.search(r"\bdar\b|darglobal|dar global", q, re.I))
    was = "wasalt" in q.lower()
    types = set()
    for pat, t in TYPE_HINTS:
        if re.search(pat, q, re.I):
            types |= t
    countries = [c for c, r in COUNTRY_RE.items() if c != "Saudi Arabia" and r.search(q)]
    if SAUDI_CITIES.search(q):  # "Wasalt" alone must not imply Saudi Arabia, but a Saudi city/country name does
        countries.append("Saudi Arabia")
    return {"dar": dar, "wasalt": was, "countries": countries, "types": types,
            # both companies named, or a comparison phrase with no company named -> balance results across companies
            "compare": (dar and was) or (not dar and not was and bool(COMPARE.search(q)))}


def scores_for(q, info, company=None):
    """blended score per chunk in [0, ~1], before metadata multipliers. `company` keeps that company's name as a
    search term (normally company names are ignored as filler, but in a comparison they are the point)."""
    terms = [t for t in tok(q) if t not in STOP and not (info["dar"] and t == "project")]
    if company:
        terms += ["darglobal"] if company == "dar" else ["wasalt"]
    terms = terms or tok(q)
    bm = BM25.get_scores(terms)
    bm_n = bm / bm.max() if bm.max() > 0 else bm
    if EMB is None or MODEL is None or ALPHA >= 1:
        return bm_n
    qv = _encode([q])[0]
    cos = EMB @ qv
    floor = float(np.median(cos))
    cos_n = np.clip((cos - floor) / max(float(cos.max()) - floor, 1e-9), 0, 1)
    return ALPHA * bm_n + (1 - ALPHA) * cos_n


def boosted(q, info, company=None):
    s = scores_for(q, info, company).copy()
    named = info["dar"] != info["wasalt"]
    for i, c in enumerate(CHUNKS):
        m = 1.0
        if named:  # a question naming one company: strongly prefer that company's chunks
            m *= 1.0 if c["site"] == ("dar" if info["dar"] else "wasalt") else 0.15
        if info["countries"]:  # a country named in the question: prefer chunks about that country
            m *= 1.5 if set(info["countries"]) & set(c["countries"]) else 0.85
        if info["types"] and c["page_type"] in info["types"]:  # "news", "FAQ", "partners"... -> matching page types
            m *= 1.2
        s[i] *= m
    return s


def pick(s, pool, limit, floor_score):
    out, per_url = [], {}
    for i in pool:
        if s[i] < floor_score or len(out) >= limit:
            break
        u = (CHUNKS[i]["url"], CHUNKS[i]["title"])
        if per_url.get(u, 0) < 2:  # at most 2 chunks per page so answers can draw on several pages
            per_url[u] = per_url.get(u, 0) + 1
            out.append(i)
    return out


def retrieve(q, k=6):
    if not BM25:
        return []
    info = analyze(q)
    if info["compare"]:  # comparison: retrieve for EACH company separately and interleave
        half, out = (k + 1) // 2, []
        for site in ("dar", "wasalt"):
            s = boosted(q, info, company=site)
            pool = [i for i in np.argsort(-s)[:300] if CHUNKS[i]["site"] == site]
            if pool and s[pool[0]] > 0:
                out.append([CHUNKS[i] for i in pick(s, pool, half, s[pool[0]] * REL)])
        return [c for pair in zip(*out) for c in pair] + [c for lst in out for c in lst[len(min(out, key=len)):]] if out else []
    s = boosted(q, info)
    order = np.argsort(-s)
    if s[order[0]] <= 0:
        return []
    return [CHUNKS[i] for i in pick(s, order[:60], k, s[order[0]] * REL)]


_load_embeddings()

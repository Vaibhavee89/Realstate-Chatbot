"""Polite scraper for public pages of DarGlobal and Wasalt.

Usage:
    python scraper/scrape.py                 # both sites
    python scraper/scrape.py --site dar      # one site
    python scraper/scrape.py --playwright    # use headless Chromium (needed for Wasalt if it is JS-rendered)

Output: data/pages.jsonl  (one JSON per page: url, site, title, text)

Only public pages are fetched. robots.txt is respected, requests are rate limited,
and a Cloudflare/bot challenge page is skipped (never bypassed).
"""
import argparse, json, re, time, sys, urllib.robotparser
from pathlib import Path
from urllib.parse import urljoin, urlparse, urldefrag

import requests
from bs4 import BeautifulSoup

OUT = Path(__file__).resolve().parent.parent / "data" / "pages.jsonl"
UA = "Mozilla/5.0 (compatible; AssignmentBot/1.0; educational use)"
DELAY = 1.0

SITES = {
    "dar": {
        "base": "https://darglobal.co.uk",
        "seeds": ["/", "/about", "/projects", "/press", "/why-invest", "/investor", "/faq",
                  "/blog", "/insights", "/hospitality", "/commercial", "/tokenization",
                  "/luxury-golf-communities", "/exclusive-membership", "/get-in-touch",
                  "/projects/properties-in-uae", "/projects/properties-in-uk",
                  "/projects/properties-in-spain", "/projects/properties-in-oman",
                  "/projects/properties-in-qatar", "/projects/properties-in-saudi-arabia"],
        "follow": r"^/(projects|press|blog|insights|partners|landing-page|lp|rayana|amaya|one-of-one|faq|about|why-invest)",
        "max_pages": 120,
    },
    "wasalt": {
        "base": "https://wasalt.sa",  # wasalt.com redirects here
        "seeds": ["/", "/en", "/en/about-us", "/en/blog", "/en/projects", "/en/buy", "/en/rent"],
        "follow": r"^/(en|ar)?/?(about|projects?|project|buy|rent|property|properties|blog|developers?|compounds?|areas?|sell|faq)",
        "max_pages": 120,
    },
}

BLOCK_MARKERS = ("Just a moment", "Performing security verification", "Attention Required")


def clean(html: str):
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["script", "style", "noscript", "svg", "iframe", "form", "header", "footer", "nav"]):
        t.decompose()
    title = (soup.title.string or "").strip() if soup.title else ""
    root = soup.find("main") or soup.body or soup
    text = re.sub(r"[ \t]+", " ", root.get_text("\n"))
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    return title, text, soup


def make_fetcher(use_pw: bool):
    if not use_pw:
        s = requests.Session(); s.headers["User-Agent"] = UA
        def get(url):
            r = s.get(url, timeout=25)
            return r.status_code, r.text
        return get, lambda: None
    from playwright.sync_api import sync_playwright
    p = sync_playwright().start()
    b = p.chromium.launch()
    page = b.new_page(user_agent=UA)
    def get(url):
        resp = page.goto(url, wait_until="networkidle", timeout=45000)
        return (resp.status if resp else 0), page.content()
    return get, lambda: (b.close(), p.stop())


def crawl(key, cfg, get):
    base = cfg["base"]; host = urlparse(base).netloc
    rp = urllib.robotparser.RobotFileParser(urljoin(base, "/robots.txt"))
    try:
        # urllib's default UA is often 403'd, which robotparser treats as "disallow all"
        r = requests.get(urljoin(base, "/robots.txt"), headers={"User-Agent": UA}, timeout=20)
        rp.parse(r.text.splitlines() if r.status_code == 200 else [])
    except Exception:
        rp.parse([])
    follow = re.compile(cfg["follow"])
    queue = [urljoin(base, s) for s in cfg["seeds"]]
    seen, pages = set(), []
    while queue and len(pages) < cfg["max_pages"]:
        url = urldefrag(queue.pop(0))[0].rstrip("/") or base
        if url in seen: continue
        seen.add(url)
        if urlparse(url).netloc != host: continue
        if not rp.can_fetch(UA, url):
            print("robots-disallowed", url); continue
        try:
            status, html = get(url)
        except Exception as e:
            print("ERR", url, e); continue
        time.sleep(DELAY)
        if status >= 400 or any(m in html[:3000] for m in BLOCK_MARKERS):
            print(f"SKIP {status} (blocked/challenge) {url}"); continue
        title, text, soup = clean(html)
        if len(text) < 200: continue
        pages.append({"site": key, "url": url, "title": title, "text": text})
        print(f"OK  [{len(pages)}] {url} ({len(text)} chars)")
        for a in soup.find_all("a", href=True):
            link = urldefrag(urljoin(url, a["href"]))[0]
            pu = urlparse(link)
            if pu.netloc == host and follow.match(pu.path) and link not in seen:
                queue.append(link)
    return pages


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", choices=["dar", "wasalt", "both"], default="both")
    ap.add_argument("--playwright", action="store_true")
    a = ap.parse_args()
    get, close = make_fetcher(a.playwright)
    allp = []
    for k in (["dar", "wasalt"] if a.site == "both" else [a.site]):
        allp += crawl(k, SITES[k], get)
    close()
    if not allp:
        sys.exit("No pages scraped. For Wasalt try --playwright; if still blocked, see README.")
    OUT.parent.mkdir(exist_ok=True)
    # merge with existing (so a DarGlobal-only run doesn't drop Wasalt data)
    old = [json.loads(l) for l in OUT.read_text(encoding="utf-8").split("\n") if l.strip()] if OUT.exists() else []
    keep = [o for o in old if o["site"] not in {p["site"] for p in allp}]
    OUT.write_text("\n".join(json.dumps(p, ensure_ascii=False) for p in keep + allp))
    print(f"Wrote {len(keep)+len(allp)} pages -> {OUT}")

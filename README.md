# DarGlobal & Wasalt Assistant

RAG chatbot over publicly scraped data from darglobal.co.uk and wasalt.sa (wasalt.com redirects there).
Stack: FastAPI + BM25 retrieval + OpenRouter free model (`openrouter/free`), Docker, deployed on Render.

**Live demo:** https://realestate-chatbot-bzc7.onrender.com
(Render free tier: the first load can take 30-60s while the instance wakes, and answers can be slow because they use a free OpenRouter model.)

## 1. Data collection
**DarGlobal** - crawled from darglobal.co.uk (120 public pages: projects, press, insights, one-of-one):

    pip install -r requirements-scraper.txt
    python scraper/scrape.py --site dar
Polite: robots.txt respected, 1s delay, page cap; challenge pages are skipped, never bypassed.
Output: `data/pages.jsonl`.

**Wasalt** - wasalt.sa is behind a Cloudflare interactive challenge (HTTP 403 to automated clients), which I did
not bypass, so page content could not be scraped. Instead, `scraper/wasalt_sitemap.py` builds a catalog from Wasalt's
public sitemaps (listed in its robots.txt, served unchallenged from cdn.wasalt.sa):

    python scraper/wasalt_sitemap.py
Output: `data/wasalt_catalog.jsonl` - aggregate counts (60,000 listings by type, sale/rent, bedrooms; ~8,500
category pages by city) plus sample listings, derived from URL slugs only. Limitations: it is a snapshot, not live
inventory; listing sizes in slugs are unreliable and omitted; no prices, descriptions or company information.
The bot therefore cannot answer Wasalt questions about pricing, services or policies.
Optionally add manually copied public Wasalt pages to `data/wasalt_manual.jsonl`
(`{"site":"wasalt","url":...,"title":...,"text":...}` per line); disclose this if used.

## 2. Run locally
    docker build -t realestate-bot .
    docker run -p 8000:8000 -e OPENROUTER_API_KEY=sk-or-... realestate-bot
Open http://localhost:8000

## 3. Deploy (Render, free)
Push to GitHub -> Render > New > Blueprint (uses render.yaml) -> set `OPENROUTER_API_KEY` secret -> deploy.
Free instances sleep after idle, so open the URL once before sharing.

## Notes
- OpenRouter free tier ~50 requests/day without a top-up; free models rotate. Optionally set
  `OPENROUTER_FALLBACKS=model-a:free,model-b:free`.
- Answers are grounded in retrieved excerpts, cite sources, and say "I don't know" otherwise.

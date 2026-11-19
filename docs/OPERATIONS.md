Back to the [README](../README.md).

# Operations

How the running system behaves when things go wrong, and how its data is kept fresh and handled.

## Resilience, handoff and observability

- **Resilience** (`app/llm.py`): a chain of three `:free` models from different providers is tried in order
     (`openrouter/free`, `google/gemma-4-31b-it:free`, `nvidia/nemotron-3-ultra-550b-a55b:free`): busy, upstream rate
     limit, 5xx, timeout, empty reply or a removed model moves on to the next; if the whole chain fails it is retried
     **once** after a short pause, and the UI shows "trying another free model…". The daily free quota is shared by all
     free models, so when it is used up the chain stops at once and says so, with the reset time. In every failure case
     the user gets a calm message (no raw error codes), a "Try again" button where retrying helps, and the **retrieved
     passages**, so the app still shows real source text when the AI cannot write an answer. The answer reports which
     model wrote it. A reply that breaks mid-way keeps its partial text.
- **Lead capture / handoff**: when someone says they want to buy, rent, reserve or view ("I want to buy a villa in
     Muscat", "can I book a viewing?"), the answer is followed by a card: the companies' **own contact pages** (the one the
     conversation is about first) and a short **enquiry form** (name, email or phone, company, what they are looking for,
     explicit consent) that saves to SQLite. Two detection layers again: regex for obvious phrasings, plus the model starting
     its reply with a marker that the server strips. An "Enquire" button in the header and sidebar is always available. The
     form says plainly that this demo is **not run by DarGlobal or Wasalt** and that nothing is sent to either company: the
     enquiry goes to the demo's owner, and the official links are the real route. Spam protection: a hidden honeypot field,
     server-side validation, and 5 enquiries per hour per connection.
- **Observability**: every question writes one row to a small SQLite file: retrieval time, time to first token, total
     time, model that answered, attempts, outcome (answered, idk, refused by regex or model, unavailable, interrupted,
     aborted), the retrieved source URLs, whether the handoff card was shown, and the **thumbs up/down** (with an optional
     comment on a thumbs-down). Logging never breaks a chat. See "Leads, logging and privacy".

## Data freshness
A scheduled GitHub Action (`.github/workflows/refresh-data.yml`, Mondays 03:17 UTC, or run it by hand from the Actions tab)
keeps the data from going stale:

1. re-crawls DarGlobal (`scraper/scrape.py`) and rebuilds the Wasalt catalog from its public sitemaps
   (`scraper/wasalt_sitemap.py`);
2. validates the result and writes `data/meta.json` (`scraper/build_meta.py`): scrape date, date the content last changed,
   counts, and what changed (pages added / removed / modified);
3. commits only if something changed, and pushing to `main` redeploys on Render, whose Docker build re-embeds the data.

The UI shows **"Data last updated 18 Nov 2026"** (and the page and listing counts, which also come from `meta.json`), turns
amber with "data may be out of date" after 30 days, and `GET /api/meta` returns the record. The few Wasalt pages copied by hand
(`data/wasalt_manual.jsonl`) are not auto-refreshed.

**It refuses to publish bad data** (exit 1, nothing committed, the workflow goes red): if DarGlobal pages or Wasalt listings
drop below 80% of the last run, if any of five key pages (homepage, investor, FAQ, contact, Aston Martin partner page) is
missing or nearly empty, or if more than half the crawl is stale copies. Tested in `tests/test_build_meta.py` (13 checks, no
network).

**What building it found.** Testing the job locally, a second crawl lost the DarGlobal homepage and 25 other pages. The
site's bot protection (Imperva/Incapsula) had started answering the crawler with a near-empty challenge page, which the
crawler did not recognise, so it skipped the homepage silently and everything only linked from it vanished; an 80% safety
threshold alone would have let that through (105 of 120 pages). Fixes: challenge pages are now recognised and logged, and a
page that was blocked or failed keeps its **previous copy** (the count is recorded in `meta.json` and shown in the UI),
`/partners` and `/one-of-one` are seeded so key pages do not depend on the homepage's links, and the key-page and
stale-share checks above were added. The challenge is not bypassed (same policy as Wasalt). GitHub's datacenter IPs may well
be challenged more than a home connection; if so the job will fail loudly instead of publishing a degraded dataset, and the
UI's date simply goes stale and then amber.

## Leads, logging and privacy
**What is stored** (in `data/app.db`): per question, the question text (first 500 characters), outcome, timings, model,
retrieved source URLs and any thumbs rating; per enquiry, the fields the person typed (name, email and/or phone, company,
message), the question that triggered it, and their consent. **IP addresses are not stored**; the rate limiter keeps them in
memory only. Set `LOG_QUESTIONS=0` to store only a hash of each question. Nothing is sent anywhere unless `LEAD_WEBHOOK_URL`
is set. To delete someone's data, remove their rows (or the file); there is no automated retention policy, and this is a demo,
not a GDPR-compliance claim. Questions are free text, so people can put personal details in them: that is why the question
log is switchable.

**Durability.** On Render's free tier the container disk is **ephemeral**: `data/app.db` is wiped on every deploy and restart.
For leads that must survive, set `LEAD_WEBHOOK_URL` (each enquiry is forwarded as it is saved), or attach a persistent disk
and point `DB_PATH` at it. Read the data with the admin page (`/admin`, needs `ADMIN_TOKEN`) or `GET /admin/stats` and
`/admin/leads.csv`:

    curl -H "X-Admin-Token: $ADMIN_TOKEN" https://<your-app>.onrender.com/admin/stats

`/admin/stats` returns chat counts by outcome, p50/p95 time to first token and total time, mean retrieval time, thumbs up
share, the models that answered, the most retrieved sources, recent thumbs-down comments, and the enquiry count.

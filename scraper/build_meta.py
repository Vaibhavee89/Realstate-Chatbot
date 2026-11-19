"""Validate freshly scraped data and write data/meta.json (the "data last updated" record shown in the UI).

Run by the scheduled GitHub Action after scraping (.github/workflows/refresh-data.yml), or by hand:

    python scraper/build_meta.py                  # validate + write data/meta.json
    python scraper/build_meta.py --date 2026-11-18   # backfill the date of an earlier scrape

Safety: if a refreshed source shrank by more than the allowed ratio (a half-failed scrape, a site redesign, a blocked
runner) it exits with an error and writes NOTHING, so the workflow does not commit and the previous good data stays live.

meta.json fields: scraped_at (last successful scrape), changed_at (last time the scraped content actually changed),
counts, a content hash, what changed in the last update (pages added / removed / modified), and which pages were kept
from the previous crawl because a bot challenge blocked them (and since when).
"""
import argparse, hashlib, json, re, subprocess, sys
from datetime import date
from pathlib import Path

DEFAULT_DATA = Path(__file__).resolve().parent.parent / "data"
# pages the chatbot cannot do without (answers to several eval questions come from them): refuse to publish if any
# is missing or nearly empty, which is what a bot challenge or a broken crawl looks like
CANARIES = ["https://darglobal.co.uk", "https://darglobal.co.uk/investor", "https://darglobal.co.uk/faq",
            "https://darglobal.co.uk/get-in-touch", "https://darglobal.co.uk/partners/aston-martin"]
MIN_CANARY_CHARS = 500
MAX_STALE_SHARE = 0.5   # refuse if more than half the DarGlobal pages are kept-from-the-previous-crawl copies
AUTO = ("pages.jsonl", "wasalt_catalog.jsonl")  # refreshed by the job; wasalt_manual.jsonl is copied by hand and is not


def read_jsonl(text):
    return [json.loads(l) for l in text.split("\n") if l.strip()]


def listings_from_catalog(docs):
    """the catalog overview document states the listing count: 'lists 60,000 individual property listings'"""
    for d in docs:
        m = re.search(r"lists ([\d,]+) individual property listings", d.get("text", ""))
        if m:
            return int(m.group(1).replace(",", ""))
    return None


def content_hash(data_dir):
    h = hashlib.sha256()
    for name in AUTO:
        f = data_dir / name
        h.update(name.encode())
        h.update(f.read_bytes() if f.exists() else b"")
    return h.hexdigest()


def previous_pages(data_dir):
    """the last committed pages.jsonl, to report what changed (None if not in a git checkout)"""
    try:
        rel = (data_dir / "pages.jsonl").resolve().relative_to(Path(subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"], cwd=data_dir, text=True, stderr=subprocess.DEVNULL).strip()))
        out = subprocess.check_output(["git", "show", f"HEAD:{rel}"], cwd=data_dir, text=True, stderr=subprocess.DEVNULL)
        return read_jsonl(out)
    except Exception:
        return None


def page_diff(old, new):
    o = {p["url"]: hashlib.md5(p["text"].encode()).hexdigest() for p in old}
    n = {p["url"]: hashlib.md5(p["text"].encode()).hexdigest() for p in new}
    return {"added": len(n.keys() - o.keys()), "removed": len(o.keys() - n.keys()),
            "modified": sum(1 for u in n.keys() & o.keys() if n[u] != o[u])}


def build(data_dir, today, min_ratio=0.8):
    pages = read_jsonl((data_dir / "pages.jsonl").read_text(encoding="utf-8"))
    catalog = read_jsonl((data_dir / "wasalt_catalog.jsonl").read_text(encoding="utf-8"))
    manual_f = data_dir / "wasalt_manual.jsonl"
    manual = read_jsonl(manual_f.read_text(encoding="utf-8")) if manual_f.exists() else []
    meta_f = data_dir / "meta.json"
    prev = json.loads(meta_f.read_text()) if meta_f.exists() else {}

    new = {"dar_pages": sum(1 for p in pages if p["site"] == "dar"), "wasalt_listings": listings_from_catalog(catalog)}
    report_f = data_dir / ".scrape_report.json"
    carried = json.loads(report_f.read_text()).get("carried_over", []) if report_f.exists() else []
    problems = []
    lengths = {p["url"]: len(p["text"]) for p in pages}
    for u in CANARIES:
        if lengths.get(u, 0) < MIN_CANARY_CHARS:
            problems.append(f"key page missing or nearly empty: {u} ({lengths.get(u, 0)} chars)")
    if new["dar_pages"] and len(carried) > MAX_STALE_SHARE * new["dar_pages"]:
        problems.append(f"{len(carried)} of {new['dar_pages']} DarGlobal pages are stale copies (most of the crawl was blocked)")
    if new["dar_pages"] == 0:
        problems.append("no DarGlobal pages were scraped")
    if new["wasalt_listings"] is None:
        problems.append("the Wasalt catalog has no listing count")
    for key, label in (("dar_pages", "DarGlobal pages"), ("wasalt_listings", "Wasalt listings")):
        old_v, new_v = prev.get(key), new[key]
        if old_v and new_v is not None and new_v < old_v * min_ratio:
            problems.append(f"{label} dropped from {old_v} to {new_v} (below {min_ratio:.0%} of the previous run)")
    if problems:
        raise SystemExit("REFUSING to update data/meta.json: " + "; ".join(problems))

    h = content_hash(data_dir)
    changed = h != prev.get("content_hash")
    old_pages = previous_pages(data_dir)
    diff = page_diff(old_pages, pages) if (old_pages is not None and changed) else prev.get("last_change", {})
    meta = {
        "scraped_at": today,
        "changed_at": today if changed else prev.get("changed_at", today),
        "dar_pages": new["dar_pages"],
        "wasalt_listings": new["wasalt_listings"],
        "wasalt_catalog_docs": len(catalog),
        "wasalt_manual_docs": len(manual),
        "carried_over": len(carried),  # pages whose fresh fetch was blocked/failed, so the previous copy was kept
        "carried_since": {u: prev.get("carried_since", {}).get(u, today) for u in carried},
        "content_hash": h,
        "last_change": diff,
        "sources": {"dar": "darglobal.co.uk (crawled)", "wasalt_catalog": "wasalt.sa public sitemaps",
                    "wasalt_manual": "copied by hand from public pages; not auto-refreshed"},
    }
    return meta, changed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", type=Path, default=DEFAULT_DATA)
    ap.add_argument("--date", help="YYYY-MM-DD to record (default: today, UTC)")
    ap.add_argument("--min-ratio", type=float, default=0.8, help="refuse if a source shrank below this fraction of the last run")
    a = ap.parse_args()
    today = a.date or date.today().isoformat()
    meta, changed = build(a.data_dir, today, a.min_ratio)
    (a.data_dir / "meta.json").write_text(json.dumps(meta, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"meta.json written: scraped_at={meta['scraped_at']} changed_at={meta['changed_at']} "
          f"dar_pages={meta['dar_pages']} (kept from previous crawl: {meta['carried_over']}) wasalt_listings={meta['wasalt_listings']} | content "
          f"{'CHANGED ' + json.dumps(meta['last_change']) if changed else 'unchanged'}")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        if isinstance(e.code, str):
            print(e.code, file=sys.stderr)
            sys.exit(1)
        raise

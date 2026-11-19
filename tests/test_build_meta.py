"""Tests for scraper/build_meta.py (the data-freshness validation). No network.   python tests/test_build_meta.py"""
import json, sys, tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scraper"))
import build_meta as bm

results = []


def check(name, cond, detail=""):
    results.append(cond)
    print(("PASS " if cond else "FAIL ") + name + (f"   {detail}" if detail and not cond else ""))


def write(d, pages, listings=60000, manual=2, canaries=True, carried=()):
    """`pages` = filler page texts; the key (canary) pages are added with long text unless canaries=False"""
    rows = [{"site": "dar", "url": f"https://x/{i}", "title": "t", "text": txt} for i, txt in enumerate(pages)]
    if canaries:
        rows += [{"site": "dar", "url": u, "title": "t", "text": "key page " * 80} for u in bm.CANARIES]
    (d / "pages.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    cat = [{"site": "wasalt", "url": "u", "title": "overview",
            "text": f"Wasalt lists {listings:,} individual property listings (45,000 for sale)"}]
    (d / "wasalt_catalog.jsonl").write_text("\n".join(json.dumps(c) for c in cat) + "\n")
    (d / "wasalt_manual.jsonl").write_text("\n".join(json.dumps({"site": "wasalt", "url": "m", "title": "m", "text": "t"})
                                                     for _ in range(manual)) + "\n")
    (d / ".scrape_report.json").write_text(json.dumps({"fetched": len(rows), "carried_over": list(carried), "failed": {}}))


def run(d, today, **kw):
    meta, changed = bm.build(d, today, **kw)
    (d / "meta.json").write_text(json.dumps(meta))
    return meta, changed


with tempfile.TemporaryDirectory() as t:
    d = Path(t)
    write(d, [f"page {i}" for i in range(100)])
    m1, ch = run(d, "2026-01-01")
    check("first run records counts and dates", m1["dar_pages"] == 105 and m1["wasalt_listings"] == 60000
          and m1["scraped_at"] == m1["changed_at"] == "2026-01-01" and m1["wasalt_manual_docs"] == 2)

    m2, ch = run(d, "2026-01-08")
    check("unchanged content: scraped_at moves, changed_at stays", not ch and m2["scraped_at"] == "2026-01-08"
          and m2["changed_at"] == "2026-01-01" and m2["content_hash"] == m1["content_hash"])

    write(d, [f"page {i}" for i in range(99)] + ["page 99 EDITED"])
    m3, ch = run(d, "2026-01-15")
    check("changed content: changed_at moves too", ch and m3["changed_at"] == "2026-01-15" and m3["scraped_at"] == "2026-01-15")

    before = (d / "meta.json").read_text()
    write(d, [f"page {i}" for i in range(50)])  # half the pages vanished: a half-failed scrape
    try:
        bm.build(d, "2026-01-22"); refused = False
    except SystemExit as e:
        refused = "DarGlobal pages dropped from 105 to 55" in str(e.code)
    check("shrunken scrape (105 -> 55 pages) is refused, meta.json untouched", refused and (d / "meta.json").read_text() == before)

    write(d, [f"page {i}" for i in range(100)], listings=20000)
    try:
        bm.build(d, "2026-01-22"); refused = False
    except SystemExit as e:
        refused = "Wasalt listings dropped" in str(e.code)
    check("Wasalt listings 60,000 -> 20,000 is refused", refused)

    write(d, [], canaries=False)
    try:
        bm.build(d, "2026-01-22"); refused = False
    except SystemExit as e:
        refused = "no DarGlobal pages" in str(e.code)
    check("an empty scrape is refused", refused)

    write(d, [f"page {i}" for i in range(90)])  # 10% fewer: normal churn is allowed
    m4, ch = run(d, "2026-01-29")
    check("a 10% drop (normal churn) is accepted", m4["dar_pages"] == 95)

    write(d, [f"page {i}" for i in range(100)], canaries=False)
    try:
        bm.build(d, "2026-02-05"); refused = False
    except SystemExit as e:
        refused = "key page missing or nearly empty: https://darglobal.co.uk " in str(e.code)
    check("missing key pages (e.g. homepage blocked) -> refused", refused)

    homepage = "https://darglobal.co.uk"
    write(d, [f"page {i}" for i in range(100)], carried=[homepage])
    m5, ch = run(d, "2026-02-05")
    check("a blocked page kept from the previous crawl is recorded, with the date it first went stale",
          m5["carried_over"] == 1 and m5["carried_since"] == {homepage: "2026-02-05"})
    m6, ch = run(d, "2026-02-12")
    check("  ...still blocked a week later: the original date is kept", m6["carried_since"] == {homepage: "2026-02-05"})
    write(d, [f"page {i}" for i in range(100)], carried=[])
    m7, ch = run(d, "2026-02-19")
    check("  ...fetched fresh again: no longer listed as stale", m7["carried_over"] == 0 and m7["carried_since"] == {})

    write(d, [f"page {i}" for i in range(100)], carried=[f"https://x/{i}" for i in range(60)])
    try:
        bm.build(d, "2026-02-26"); refused = False
    except SystemExit as e:
        refused = "stale copies (most of the crawl was blocked)" in str(e.code)
    check("most of the crawl blocked (60 of 105 pages stale) -> refused instead of pretending to be fresh", refused)

    check("page_diff counts added / removed / modified",
          bm.page_diff([{"url": "a", "text": "1"}, {"url": "b", "text": "2"}, {"url": "c", "text": "3"}],
                       [{"url": "a", "text": "1"}, {"url": "b", "text": "CHANGED"}, {"url": "d", "text": "4"}])
          == {"added": 1, "removed": 1, "modified": 1})

print(f"\n{sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)

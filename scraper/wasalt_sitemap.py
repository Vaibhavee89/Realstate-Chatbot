"""Build a Wasalt catalog from its public sitemaps (cdn.wasalt.sa/sitemap/*).

wasalt.sa pages sit behind a Cloudflare challenge, so page content is NOT scraped.
Sitemaps are public, unchallenged and listed in robots.txt; only URL slugs are used.
Output: data/wasalt_catalog.jsonl  (aggregate summaries + a sample of listings)

Caveat: size numbers inside listing slugs are unreliable (decimals dropped), so sizes are
not reported; only type, sale/rent, bedrooms, and orientation are used.
"""
import gzip, io, json, random, re
from collections import Counter
from pathlib import Path

import requests

BASE = "https://cdn.wasalt.sa/sitemap/"
UA = "Mozilla/5.0 (compatible; AssignmentBot/1.0; educational use)"
OUT = Path(__file__).resolve().parent.parent / "data" / "wasalt_catalog.jsonl"
CITIES = ["riyadh", "jeddah", "makkah", "madinah", "dammam", "al-khobar", "taif", "buraidah", "abha", "tabuk",
          "ad-diriyah", "dhahran", "hail", "jazan", "najran", "yanbu", "al-ahsa", "al-jubail", "khamis-mushait",
          "king-abdullah-economic-city", "al-qatif", "unaizah", "rabigh", "al-baha", "sakaka", "arar"]
random.seed(7)


def locs(name):
    r = requests.get(BASE + name, headers={"User-Agent": UA}, timeout=120)
    r.raise_for_status()
    data = r.content
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return re.findall(r"<loc>([^<]+)</loc>", data.decode("utf-8"))


def page(title, text, url="https://wasalt.sa/en/"):
    return {"site": "wasalt", "url": url, "title": title, "text": text}


def fmt(c, n=15):
    return "\n".join(f"{k}: {v:,}" for k, v in c.most_common(n))


def main():
    products = locs("product_sitemap_en_sa.xml.gz")
    cats = locs("category_sitemap_en_sa.xml.gz")
    static = locs("static_sitemap_en_sa.xml.gz")

    pat = re.compile(r"/property/(sale|rent)/(.+)-(\d+)$")
    types, deal, beds, rows = Counter(), Counter(), Counter(), []
    for u in products:
        m = pat.search(u)
        if not m:
            continue
        d, slug, _id = m.groups()
        t = slug.split("-")[0]
        t = {"rest": "rest house", "semi": "semi-furnished unit", "furnished": "furnished unit"}.get(t, t)
        if not t.isalpha() or t == "sqm":
            t = "other/unspecified"
        types[(t, d)] += 1
        deal[d] += 1
        b = re.search(r"with-(\d+)-bedrooms?", slug)
        if b:
            beds[(d, b.group(1))] += 1
        rows.append((u, d, t, slug))

    cat_kind, cat_city = Counter(), Counter()
    cat_re = re.compile(r"/en/(.+?)-for-(sale|rent)(?:-in-(.+))?$")
    for u in cats:
        m = cat_re.search(u)
        if not m:
            continue
        kind, d, place = m.groups()
        cat_kind[f"{kind} for {d}"] += 1
        if place:
            city = next((c for c in CITIES if f"-{c}-" in f"-{place}-"), None)
            if city:
                cat_city[city.replace("-", " ").title()] += 1

    pages = [
        page("Wasalt - overview of public sitemap catalog",
             f"Wasalt (wasalt.sa) is a Saudi Arabian online real-estate marketplace. Its public English sitemap lists "
             f"{len(products):,} individual property listings ({deal['sale']:,} for sale, {deal['rent']:,} for rent), "
             f"{len(cats):,} search/category pages by property type, city and neighbourhood, and static pages "
             f"({', '.join(u.rsplit('/', 1)[-1] or 'home' for u in static)}). "
             f"Source: Wasalt public sitemaps (cdn.wasalt.sa/sitemap). Counts reflect the sitemap snapshot and are not live inventory."),
        page("Wasalt - listings by property type and sale/rent",
             "Number of listings in Wasalt's public sitemap by property type:\n"
             + "\n".join(f"{t} for {d}: {n:,}" for (t, d), n in types.most_common(30))),
        page("Wasalt - listings by bedrooms",
             "Number of Wasalt listings that state a bedroom count in the listing URL (sale / rent):\n"
             + "\n".join(f"{b} bedrooms, for {d}: {n:,}" for (d, b), n in sorted(beds.items(), key=lambda x: -x[1])[:20])),
        page("Wasalt - search categories by type",
             "Number of Wasalt category (search) pages by property type and deal:\n" + fmt(cat_kind, 30)),
        page("Wasalt - cities and areas covered",
             "Number of Wasalt neighbourhood/category pages per Saudi city (more pages = broader area coverage):\n"
             + fmt(cat_city, 30)),
    ]

    # Sample of listings (stratified by deal and type)
    sample = []
    for (t, d), _ in [x for x in types.most_common(12) if x[0][0] != "other/unspecified"][:8]:
        pool = [r for r in rows if r[1] == d and r[2] == t]
        sample += random.sample(pool, min(6, len(pool)))
    groups = {}
    for u, d, t, slug in sample:
        desc = re.sub(r"-\d+-sqm", "", slug).replace("-", " ")
        groups.setdefault((t, d), []).append(f"- {desc}: {u}")
    for (t, d), items in groups.items():
        pages.append(page(f"Wasalt sample {t} listings ({d})",
                          f"A few example Wasalt {t} listings for {d} (sample only, not counts):\n" + "\n".join(items)))

    OUT.write_text("\n".join(json.dumps(p, ensure_ascii=False) for p in pages) + "\n")
    print(f"{len(products)} listings, {len(cats)} categories -> {len(pages)} docs in {OUT}")


if __name__ == "__main__":
    main()

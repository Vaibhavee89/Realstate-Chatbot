"""Light/dark theme test: theme logic plus a WCAG contrast scan of every visible piece of text.   python tests/test_ui_theme.py

Needs Playwright with Chromium (`pip install playwright && playwright install chromium`); without it the test is skipped,
so it is not part of CI. Starts the app against the fake OpenRouter (no API key, no quota). SHOTS=<dir> saves screenshots.
"""
import json, os, subprocess, sys, tempfile, time
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("SKIPPED: Playwright is not installed")
    sys.exit(0)
import httpx

ROOT = Path(__file__).resolve().parent.parent
FAKE, APP = "http://127.0.0.1:9104", "http://127.0.0.1:8104"
SHOTS = os.environ.get("SHOTS")

# contrast of every visible text element against its effective background (ancestors composited, opacity applied)
SCAN = r"""
() => {
  const parse = c => { const m = c.match(/rgba?\(([^)]+)\)/); if (!m) return null;
    const p = m[1].split(/[ ,\/]+/).filter(Boolean).map(Number); return {r:p[0], g:p[1], b:p[2], a:p.length > 3 ? p[3] : 1}; };
  const over = (t, b) => { const a = t.a + b.a * (1 - t.a); if (!a) return {r:0, g:0, b:0, a:0};
    return {r:(t.r*t.a + b.r*b.a*(1-t.a))/a, g:(t.g*t.a + b.g*b.a*(1-t.a))/a, b:(t.b*t.a + b.b*b.a*(1-t.a))/a, a}; };
  const f = v => { v /= 255; return v <= 0.03928 ? v/12.92 : Math.pow((v+0.055)/1.055, 2.4); };
  const lum = c => 0.2126*f(c.r) + 0.7152*f(c.g) + 0.0722*f(c.b);
  const ratio = (a, b) => { const x = lum(a), y = lum(b); return (Math.max(x,y)+0.05)/(Math.min(x,y)+0.05); };
  const bgOf = el => { const chain = []; for (let e = el; e; e = e.parentElement) chain.push(e);
    let acc = {r:255, g:255, b:255, a:1};
    for (let i = chain.length-1; i >= 0; i--) { const bg = parse(getComputedStyle(chain[i]).backgroundColor); if (bg && bg.a > 0) acc = over(bg, acc); }
    return acc; };
  const opacityOf = el => { let o = 1; for (let e = el; e; e = e.parentElement) o *= parseFloat(getComputedStyle(e).opacity); return o; };
  const fails = []; let checked = 0;
  for (const el of document.querySelectorAll('body *')) {
    const s = getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden' || el.closest('.hp, script, style, [hidden], [disabled]')) continue;
    if (!el.getClientRects().length) continue;
    if (![...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim().length)) continue;
    const fg = parse(s.color); fg.a *= opacityOf(el);
    const bg = bgOf(el), r = ratio(over(fg, bg), bg);
    const size = parseFloat(s.fontSize), bold = parseInt(s.fontWeight) >= 700;
    const need = (size >= 24 || (bold && size >= 18.66)) ? 3 : 4.5;
    checked++;
    if (r < need) fails.push({el: el.tagName.toLowerCase() + (el.className ? '.' + String(el.className).split(' ')[0] : ''),
      text: el.textContent.trim().slice(0, 40), ratio: Math.round(r*100)/100, need});
  }
  // placeholder text and the button label colours that sit on the gold accent (hover/active states)
  const pairs = [];
  for (const el of document.querySelectorAll('input[type=text], input[type=email], input[type=tel], textarea')) {
    if (!el.getClientRects().length || el.closest('.hp')) continue;
    pairs.push(['placeholder', ratio(over(parse(getComputedStyle(el, '::placeholder').color), bgOf(el)), bgOf(el))]);
  }
  const root = getComputedStyle(document.documentElement);
  const v = n => parse(root.getPropertyValue(n).trim().length === 4 || root.getPropertyValue(n).trim().length === 7
    ? (() => { const h = root.getPropertyValue(n).trim().replace('#',''); const x = h.length === 3 ? h.split('').map(c => c+c).join('') : h;
        return `rgb(${parseInt(x.slice(0,2),16)},${parseInt(x.slice(2,4),16)},${parseInt(x.slice(4,6),16)})`; })() : root.getPropertyValue(n));
  pairs.push(['text on the gold accent (hover/active buttons)', ratio(v('--on-champagne'), v('--champagne'))]);
  return {checked, fails, pairs: pairs.map(([n, r]) => [n, Math.round(r*100)/100])};
}
"""


def start():
    env = {**os.environ, "OPENROUTER_API_KEY": "k", "OPENROUTER_API_URL": FAKE + "/v1/chat/completions", "LLM_RETRY_PAUSE": "0.1",
           "LLM_READ_TIMEOUT": "3", "PYTHONPATH": str(ROOT), "HF_HUB_OFFLINE": "1", "DB_PATH": os.path.join(tempfile.mkdtemp(), "t.db"), "ADMIN_TOKEN": "t"}
    fake = subprocess.Popen([sys.executable, "-m", "uvicorn", "fake_openrouter:app", "--app-dir", str(ROOT / "tests"),
                             "--port", "9104", "--log-level", "warning"], env=env)
    app = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", "8104", "--log-level", "warning"],
                           cwd=ROOT, env=env)
    for url in (FAKE + "/calls", APP + "/health"):
        for _ in range(120):
            try:
                httpx.get(url, timeout=1); break
            except Exception:
                time.sleep(0.5)
    return fake, app


def main():
    fake, app = start()
    results = []

    def check(name, cond, detail=""):
        results.append(cond)
        print(("PASS " if cond else "FAIL ") + name + (f"   {detail}" if detail and not cond else ""))

    try:
        with sync_playwright() as p:
            b = p.chromium.launch()

            def page_for(scheme, saved=None, viewport=(1280, 900)):
                ctx = b.new_context(color_scheme=scheme, viewport={"width": viewport[0], "height": viewport[1]})
                if saved:
                    ctx.add_init_script(f"try{{localStorage.setItem('theme','{saved}')}}catch(e){{}}")
                pg = ctx.new_page()
                pg.goto(APP + "/"); pg.wait_for_selector("#chips span")
                return ctx, pg

            theme = lambda pg: pg.evaluate("document.documentElement.getAttribute('data-theme')")

            # ---------------------------------------------------------------- theme logic
            for scheme, want in (("light", "light"), ("dark", "dark")):
                ctx, pg = page_for(scheme)
                check(f"system setting '{scheme}' -> {want} theme", theme(pg) == want, theme(pg))
                ctx.close()
            ctx, pg = page_for("light")
            check("the toggle label says what a click does (light now -> 'Switch to dark mode')",
                  pg.get_attribute("#theme", "aria-label") == "Switch to dark mode")
            pg.click("#theme")
            check("clicking the toggle flips to dark and updates the label", theme(pg) == "dark"
                  and pg.get_attribute("#theme", "aria-label") == "Switch to light mode")
            pg.reload(); pg.wait_for_selector("#chips span")
            check("the choice is remembered after a reload (and beats the system setting)", theme(pg) == "dark"
                  and pg.evaluate("localStorage.getItem('theme')") == "dark")
            body_bg = pg.evaluate("getComputedStyle(document.body).backgroundColor")
            check("dark theme really is dark (page background)", body_bg == "rgb(11, 11, 11)", body_bg)
            pg.click("#theme")
            body_bg = pg.evaluate("getComputedStyle(document.body).backgroundColor")
            check("light theme really is light (page background)", body_bg == "rgb(245, 241, 234)", body_bg)
            ctx.close()
            ctx, pg = page_for("dark", saved="light")
            check("a saved 'light' wins over a dark system setting", theme(pg) == "light")
            ctx.close()
            ctx, pg = page_for("dark")
            pg.evaluate("localStorage.removeItem('theme')")
            pg.emulate_media(color_scheme="light"); pg.wait_for_timeout(300)
            check("with nothing saved, the page follows a live change of the system setting", theme(pg) == "light")
            ctx.close()
            ctx, pg = page_for("dark")
            pg.click("#theme"); pg.emulate_media(color_scheme="dark"); pg.emulate_media(color_scheme="light"); pg.wait_for_timeout(300)
            check("once the user has chosen, system changes no longer override it", theme(pg) == "light")
            ctx.close()

            # ---------------------------------------------------------------- contrast in both themes, every UI state
            for name in ("dark", "light"):
                for label, viewport in (("desktop", (1280, 1000)), ("phone", (390, 900))):
                    ctx, pg = page_for(name, saved=name, viewport=viewport)

                    def ask(q):
                        pg.fill("#q", q); pg.press("#q", "Enter")
                        pg.wait_for_function("document.querySelector('#go').disabled === false", timeout=30000)
                        pg.wait_for_timeout(250)

                    ask("What projects does DarGlobal have in Oman?")                      # answer, citations, sources, thumbs
                    pg.locator(".bot:last-child .ref").first.click()                        # passages open, one highlighted
                    pg.locator(".bot:last-child .fb button[data-v='-1']").click()           # thumbs-down comment box
                    ask("Should I invest in AIDA?")                                         # refusal + guardrail label
                    ask("I want to buy a villa in Muscat")                                  # handoff card
                    pg.locator(".bot:last-child .handoff button").click()
                    pg.locator(".bot:last-child .enq-form button[type=submit]").click()    # form with validation errors
                    pg.wait_for_timeout(500)
                    ask("MODEALL projects in Oman")                                         # outage notice + passages
                    pg.click(".enq-btn")                                                    # the Enquire form message
                    pg.evaluate("document.querySelector('#fresh') && (document.querySelector('#fresh').hidden = false)")
                    if pg.locator('.kb-chip').is_visible():   # the side panel only exists on wide screens
                        pg.hover('.kb-chip'); pg.wait_for_timeout(400)   # open the knowledge-base card so its text is scanned too
                    res = pg.evaluate(SCAN)
                    low = [x for x in res["fails"]]
                    check(f"{name} / {label}: all {res['checked']} visible text elements meet WCAG AA contrast", not low,
                          json.dumps(low[:12]))
                    badp = [x for x in res["pairs"] if x[1] < 4.5]
                    check(f"{name} / {label}: placeholder and button-on-accent contrast >= 4.5", not badp, json.dumps(badp))
                    if SHOTS:
                        Path(SHOTS).mkdir(parents=True, exist_ok=True)
                        pg.evaluate("document.querySelector('#log').scrollTop = 0")
                        pg.screenshot(path=f"{SHOTS}/{name}-{label}-top.png")
                        pg.evaluate("document.querySelector('#log').scrollTop = document.querySelector('#log').scrollHeight")
                        pg.screenshot(path=f"{SHOTS}/{name}-{label}-bottom.png")
                    ctx.close()

            # ---------------------------------------------------------------- the copy icon on an answer
            ctx = b.new_context(permissions=["clipboard-read", "clipboard-write"], viewport={"width": 1280, "height": 900})
            pg = ctx.new_page(); pg.goto(APP + "/"); pg.wait_for_selector("#chips span")
            pg.fill("#q", "What projects does DarGlobal have in Oman?"); pg.press("#q", "Enter")
            pg.wait_for_function("document.querySelector('#go').disabled === false", timeout=30000); pg.wait_for_timeout(300)
            btn = pg.locator(".bot:last-child .copy-btn")
            check("the answer has a copy button with an icon and no 'Copy' text", btn.count() == 1 and btn.inner_text().strip() == ""
                  and btn.locator("svg").count() == 2)
            check("  ...it is labelled for screen readers", btn.get_attribute("aria-label") == "Copy answer" and btn.get_attribute("title") == "Copy answer")
            btn.click(); pg.wait_for_timeout(300)
            check("clicking it shows a check mark and says 'Copied'", "copied" in (btn.get_attribute("class") or "")
                  and btn.get_attribute("aria-label") == "Copied")
            clip = pg.evaluate("navigator.clipboard.readText()")
            check("the answer text is on the clipboard, without the [n] citation markers",
                  clip.startswith("DarGlobal has") and "[" not in clip, clip[:60])
            pg.wait_for_timeout(2200)
            check("the icon goes back to normal after a moment", "copied" not in (btn.get_attribute("class") or "")
                  and btn.get_attribute("aria-label") == "Copy answer")
            ctx.close()

            # ---------------------------------------------------------------- the "What I know" tab: collapsed, opens on hover / focus / click
            ctx = b.new_context(viewport={"width": 1280, "height": 900})
            pg = ctx.new_page(); pg.goto(APP + "/"); pg.wait_for_selector("#chips span"); pg.wait_for_timeout(500)
            chip, card = pg.locator("#kb-chip"), pg.locator("#kb-card")
            away = lambda: (pg.mouse.move(600, 500), pg.wait_for_timeout(500))
            check("collapsed by default: a tab with the data date is visible, the card is not",
                  chip.is_visible() and not card.is_visible() and "updated" in chip.inner_text().lower(), chip.inner_text())
            chip.hover(); pg.wait_for_timeout(350)
            check("hovering the tab opens the card (counts and the Wasalt caveat)", card.is_visible() and "60,000" in card.inner_text()
                  and "blocks automated" in card.inner_text())
            pg.mouse.move(chip.bounding_box()["x"] + 40, chip.bounding_box()["y"] + chip.bounding_box()["height"] + 5)  # across the gap
            pg.wait_for_timeout(120)
            check("moving the pointer from the tab down into the card does not close it (no flicker)", card.is_visible())
            away()
            check("moving the pointer away closes it", not card.is_visible())
            chip.focus(); pg.wait_for_timeout(350)
            check("keyboard focus on the tab opens it too", card.is_visible())
            pg.evaluate("document.activeElement.blur()"); pg.wait_for_timeout(450)
            check("...and leaving it closes it", not card.is_visible())
            chip.click(); away()
            check("a click pins it open (this is how touch screens, which have no hover, use it)", card.is_visible()
                  and chip.get_attribute("aria-expanded") == "true")
            chip.click(); away()
            check("a second click unpins it", not card.is_visible() and chip.get_attribute("aria-expanded") == "false")
            chip.click(); away(); pg.keyboard.press("Escape"); pg.wait_for_timeout(450)
            check("Escape closes a pinned card", not card.is_visible() and chip.get_attribute("aria-expanded") == "false")
            chip.click(); away(); pg.mouse.click(600, 400); pg.wait_for_timeout(450)
            check("clicking elsewhere closes a pinned card", not card.is_visible())
            pg.fill("#q", "What projects does DarGlobal have in Oman?"); pg.press("#q", "Enter")
            pg.wait_for_function("document.querySelector('#go').disabled === false", timeout=30000); pg.wait_for_timeout(300)
            top_before = pg.locator("#srcs").bounding_box()["y"]
            chip.hover(); pg.wait_for_timeout(350)
            top_open = pg.locator("#srcs").bounding_box()["y"]
            box, vp = card.bounding_box(), pg.viewport_size
            check("opening the card overlays the page: the sources box below does not move", top_before == top_open, (top_before, top_open))
            bg = pg.evaluate("getComputedStyle(document.querySelector('#kb-card')).backgroundColor")
            check("the open card is opaque, so the sources box behind it does not show through", bg.startswith("rgb(") or bg.endswith(", 1)"), bg)
            check("  ...and the card stays inside the window", box["x"] >= 0 and box["x"] + box["width"] <= vp["width"]
                  and box["y"] + box["height"] <= vp["height"], box)
            ctx.close()
            ctx = b.new_context(viewport={"width": 1280, "height": 900})
            pg = ctx.new_page()
            pg.route("**/api/meta", lambda r: r.fulfill(json={"scraped_at": "2026-09-01", "changed_at": "2026-09-01", "dar_pages": 118,
                                                              "wasalt_listings": 59000, "age_days": 79, "stale": True}))
            pg.goto(APP + "/"); pg.wait_for_selector("#chips span"); pg.wait_for_timeout(500)
            check("stale data is flagged on the collapsed tab itself (amber, 'may be out of date')",
                  "may be out of date" in pg.inner_text("#kb-chip").lower() and "stale" in (pg.get_attribute("#kb", "class") or ""))
            ctx.close()

            # ---------------------------------------------------------------- the admin page follows the same saved theme
            for name, want_bg in (("light", "rgb(245, 241, 234)"), ("dark", "rgb(11, 11, 11)")):
                ctx = b.new_context(color_scheme="dark" if name == "light" else "light")
                ctx.add_init_script(f"try{{localStorage.setItem('theme','{name}')}}catch(e){{}}")
                pg = ctx.new_page(); pg.goto(APP + "/admin")
                bg = pg.evaluate("getComputedStyle(document.body).backgroundColor")
                check(f"admin page uses the saved {name} theme", bg == want_bg, bg)
                ctx.close()
            b.close()
    finally:
        fake.terminate(); app.terminate()
    print(f"\n{sum(results)}/{len(results)} checks passed")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()

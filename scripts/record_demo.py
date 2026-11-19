"""Records docs/demo.mp4: a scripted walkthrough of the live chatbot.

This is an automated recording, not a hand-driven screen capture: Playwright drives a real browser against the deployed
app, so the answers on screen are real (from the free model, so wording varies between takes). The script adds the
captions and a cursor dot; nothing else is staged. The enquiry form is filled with obviously fake demo data.

Free-model latency varies from a few seconds to 40+ s, so ONLY the stretch between sending a question and the first token
appearing is sped up (marked on screen while it plays). Streaming, clicking and typing run at real speed.

    pip install playwright && playwright install chromium     # ffmpeg must be on PATH
    python scripts/record_demo.py                              # records against the live URL
    python scripts/record_demo.py --url http://localhost:8000 --out /tmp/demo.mp4

It uses roughly 5 requests of the free model quota.
"""
import argparse, json, subprocess, sys, tempfile, time, urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

LIVE = "https://realestate-chatbot-bzc7.onrender.com"
W, H = 1280, 720

OVERLAY = r"""
(() => {
  function init() {
    if (document.getElementById('__cap')) return;
    const cap = document.createElement('div'); cap.id = '__cap';
    cap.style.cssText = 'position:fixed;left:50%;top:68px;transform:translateX(-50%);z-index:99999;max-width:78%;padding:10px 20px;' +
      'border:1px solid #c5a880;border-radius:4px;background:rgba(11,11,11,.94);color:#f8f6f2;font:600 15px/22px system-ui,sans-serif;' +
      'text-align:center;opacity:0;transition:opacity .25s;pointer-events:none;box-shadow:0 8px 24px rgba(0,0,0,.6)';
    document.body.appendChild(cap);
    const cur = document.createElement('div'); cur.id = '__cur';
    cur.style.cssText = 'position:fixed;z-index:100000;width:18px;height:18px;border-radius:50%;background:rgba(212,175,55,.9);' +
      'border:2px solid #fff;pointer-events:none;transform:translate(-50%,-50%);left:-60px;top:-60px';
    document.body.appendChild(cur);
    document.addEventListener('mousemove', e => { cur.style.left = e.clientX + 'px'; cur.style.top = e.clientY + 'px'; }, true);
    window.__cap = t => { cap.textContent = t; cap.style.opacity = t ? 1 : 0; };
  }
  if (document.body) init(); else document.addEventListener('DOMContentLoaded', init);
})();
"""


def warm_up(url):
    """Render's free instance sleeps when idle; wake it before recording so the video has no cold start"""
    for _ in range(40):
        try:
            with urllib.request.urlopen(url + "/health", timeout=20) as r:
                if json.loads(r.read()).get("ok"):
                    return
        except Exception:
            time.sleep(3)
    sys.exit("The app did not become healthy; not recording.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=LIVE)
    ap.add_argument("--no-speedup", action="store_true", help="keep the waits at real speed")
    ap.add_argument("--out", type=Path, default=Path(__file__).resolve().parent.parent / "docs" / "demo.mp4")
    a = ap.parse_args()
    warm_up(a.url)
    tmp = Path(tempfile.mkdtemp())

    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(viewport={"width": W, "height": H}, record_video_dir=str(tmp), record_video_size={"width": W, "height": H})
        ctx.add_init_script(OVERLAY)
        waits = []
        rec0 = time.monotonic()
        pg = ctx.new_page()

        def caption(t, hold=0.0):
            pg.evaluate("t => window.__cap && window.__cap(t)", t)
            if hold:
                pg.wait_for_timeout(int(hold * 1000))

        def move_to(loc, steps=30):
            loc.scroll_into_view_if_needed()
            box = loc.bounding_box()
            pg.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, steps=steps)
            pg.wait_for_timeout(250)

        def click(loc):
            move_to(loc)
            loc.click()

        def idle():
            pg.wait_for_function("document.querySelector('#go').disabled === false", timeout=60000)
            pg.wait_for_timeout(1200)

        def now():
            return time.monotonic() - rec0   # seconds since the video started

        current = {"cap": ""}

        def scene(t, hold=0.0):
            current["cap"] = t
            caption(t, hold)

        FIRST_TOKEN = """(() => { const b = document.querySelectorAll('.bot'); const l = b[b.length - 1];
          return !!l && (l.querySelector('.body.streaming') || l.querySelector('.notice') ||
                         (!l.querySelector('.typing') && l.querySelector('.body').innerText.trim().length > 30)); })()"""

        def ask(q):
            pg.click("#q")
            pg.type("#q", q, delay=32)
            pg.wait_for_timeout(250)
            pg.press("#q", "Enter")
            t_send, t0 = now(), time.time()
            caption(current["cap"] + "   ·   waiting for the free model, sped up in this video")
            pg.wait_for_function(FIRST_TOKEN, timeout=90000)
            t_first = now()
            caption(current["cap"])
            waits.append((t_send + 0.8, t_first - 0.3))   # keep a moment of "searching" and the first words at real speed
            idle()
            print(f"  first token after {t_first - t_send:4.1f} s, answered in {time.time() - t0:4.1f} s: {q}")

        pg.goto(a.url)
        pg.wait_for_selector("#chips span")
        pg.mouse.move(W / 2, H / 2)
        caption("Live demo: scraped DarGlobal and Wasalt data, answered by free OpenRouter models", 4)

        scene("1 · A grounded answer, streamed, with citations")
        ask("What projects does DarGlobal have in Oman?")
        chip = pg.locator(".bot:last-child .ref").first
        if chip.count():
            caption("Click a citation to see the exact passage it came from")
            click(chip)
            pg.wait_for_timeout(3200)

        scene("2 · Comparison questions retrieve from both companies")
        ask("Compare DarGlobal and Wasalt: what does each company do?")
        pg.wait_for_timeout(2500)

        scene("3 · Investment-advice questions get a refusal and a disclaimer, not advice")
        ask("Should I invest in AIDA?")
        pg.wait_for_timeout(3500)

        scene("4 · Buying intent: official contact links, or a short enquiry form")
        ask("I want to buy a villa in Muscat")
        btn = pg.locator(".bot:last-child .handoff button")
        if btn.count():
            pg.wait_for_timeout(1200)
            click(btn)
            pg.wait_for_selector(".bot:last-child .enq-form")
            caption("The form saves to this demo only; it says plainly that nothing is sent to DarGlobal or Wasalt")
            form = pg.locator(".bot:last-child .enq-form")
            for sel, text in (("input[name=name]", "Demo Visitor"), ("input[name=email]", "demo@example.com"),
                              ("textarea", "3-bed villa, Muscat, next year")):
                move_to(form.locator(sel), steps=18)
                form.locator(sel).click()
                form.locator(sel).type(text, delay=35)
            move_to(form.locator("input[type=checkbox]"), steps=18)
            form.locator("input[type=checkbox]").check()
            pg.wait_for_timeout(600)
            click(form.locator("button[type=submit]"))
            pg.wait_for_selector(".bot:last-child .enq-ok", timeout=15000)
            pg.wait_for_timeout(3500)

        caption("Honest about limits: wasalt.sa blocks bots, so Wasalt answers come from its public sitemaps and a few hand-copied pages", 5)
        caption("")
        pg.wait_for_timeout(500)
        video = pg.video.path()
        ctx.close()   # flushes the video file
        b.close()

    a.out.parent.mkdir(parents=True, exist_ok=True)
    segs = [(x, y) for x, y in waits if y - x > 3.0]          # only compress real waits
    if not segs or a.no_speedup:
        vf = None
    else:
        parts, labels, t = [], [], 0.0
        for i, (x, y) in enumerate(segs):
            factor = min(12.0, max(4.0, (y - x) / 2.5))        # a wait becomes about 2.5 s
            parts.append(f"[0:v]trim={t:.2f}:{x:.2f},setpts=PTS-STARTPTS[a{i}]")
            parts.append(f"[0:v]trim={x:.2f}:{y:.2f},setpts=(PTS-STARTPTS)/{factor:.2f}[b{i}]")
            labels += [f"[a{i}]", f"[b{i}]"]
            t = y
        parts.append(f"[0:v]trim={t:.2f},setpts=PTS-STARTPTS[z]")
        labels.append("[z]")
        vf = ";".join(parts) + ";" + "".join(labels) + f"concat=n={len(labels)}:v=1:a=0[out]"
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(video)]
    cmd += ["-filter_complex", vf, "-map", "[out]"] if vf else []
    cmd += ["-c:v", "libx264", "-preset", "slow", "-crf", "27", "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-an", str(a.out)]
    subprocess.run(cmd, check=True)
    if segs and not a.no_speedup:
        print("sped up:", ", ".join(f"{y - x:.0f} s of waiting" for x, y in segs))
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(a.out)],
                               capture_output=True, text=True).stdout.strip())
    print(f"wrote {a.out}  ({dur:.0f} s, {a.out.stat().st_size / 1e6:.1f} MB)")
    if not 60 <= dur <= 90:
        print("NOTE: duration is outside the 60-90 s target; free-model latency varies, so re-run or adjust the pauses.")


if __name__ == "__main__":
    main()

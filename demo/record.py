#!/usr/bin/env python3
"""Record the README demo: a workflow from launch to finish, then a tour of the
features. Produces docs/demo.mp4 (and a poster frame).

    python3 demo/record.py [run_id]

With no run id it follows the newest run, so you can launch a workflow first and
record it live. Pass a finished run id to record a deterministic replay of it.
Requires: playwright (pip install playwright && playwright install chromium), ffmpeg.
"""
import os, subprocess, sys, time

TMUX = os.environ.get('WFVIZ_DEMO_TMUX', 'gcdemo')


def tsend(keys, enter=True):
    """Type into the mirrored tmux session so the video shows it arrive live."""
    cmd = ['tmux', 'send-keys', '-t', TMUX, keys] + (['Enter'] if enter else [])
    subprocess.run(cmd, capture_output=True)
from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "docs")
BASE = "http://127.0.0.1:8777"
W, H = 1600, 900


def caption(page, text, sub=""):
    """Overlay a short caption so the video explains itself without audio."""
    page.evaluate(
        """([t, s]) => {
        let el = document.getElementById('__cap');
        if (!el) {
          el = document.createElement('div');
          el.id = '__cap';
          el.style.cssText = 'position:fixed;left:50%;bottom:74px;transform:translateX(-50%);'
            + 'z-index:9999;padding:11px 20px;border-radius:12px;text-align:center;'
            + 'font:600 15px/1.35 ui-monospace,Menlo,monospace;color:#eef0fb;'
            + 'background:rgba(12,14,28,.86);border:1px solid rgba(255,255,255,.14);'
            + 'box-shadow:0 14px 44px rgba(0,0,0,.55);backdrop-filter:blur(10px);'
            + 'opacity:0;transition:opacity .35s';
          document.body.appendChild(el);
        }
        el.innerHTML = t + (s ? '<div style="font-weight:400;font-size:12px;color:#878ca8;'
          + 'margin-top:4px;letter-spacing:.02em">' + s + '</div>' : '');
        requestAnimationFrame(() => { el.style.opacity = t ? '1' : '0'; });
    }""",
        [text, sub],
    )


def main():
    run = sys.argv[1] if len(sys.argv) > 1 else None
    os.makedirs(OUT, exist_ok=True)
    tport = os.environ.get("WFVIZ_DEMO_TTYD", "")
    url = f"{BASE}/?term=1" + (f"&run={run}" if run else "")
    if tport:
        url += f"&termport={tport}&tmux=gcdemo&tmuxcwd=~%2Fgraph-claude"

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--force-color-profile=srgb", "--font-render-hinting=none"])
        ctx = browser.new_context(
            viewport={"width": W, "height": H},
            device_scale_factor=2,                       # retina capture
            record_video_dir=os.path.join(OUT, "_vid"),
            record_video_size={"width": W, "height": H},
        )
        page = ctx.new_page()
        page.goto(url)
        page.wait_for_selector(".node", timeout=30000)
        page.wait_for_timeout(1200)

        caption(page, "graph-claude", "every node is one Claude subagent; every edge is data it passed")
        page.wait_for_timeout(3200)

        # Let the run play. If it is live this shows planned -> running -> done.
        caption(page, "the run, as it happens",
                "nodes light up when they start, edges carry particles while data moves")
        page.wait_for_timeout(9000)
        caption(page, "")
        page.wait_for_timeout(6000)

        def node_at(i=0):
            els = page.query_selector_all(".node")
            return els[min(i, len(els) - 1)] if els else None

        # Hover: live trace
        caption(page, "hover any agent", "its tool calls, arguments and timings stream in")
        n = node_at(3)
        if n:
            n.hover()
            page.wait_for_timeout(3800)
        caption(page, "")
        page.mouse.move(20, H - 20)
        page.wait_for_timeout(900)

        # Click: transcript + result
        caption(page, "click it for the full transcript", "prompt, every tool call, and the result it returned")
        n = node_at(3)
        if n:
            n.click()
            page.wait_for_timeout(4200)
        page.mouse.click(60, H // 2)
        caption(page, "")
        page.wait_for_timeout(900)

        # Edge payload
        caption(page, "click an edge", "shows the exact text that crossed it, so a drawn edge is a real one")
        hit = page.query_selector_all("#edgeG path")
        if len(hit) > 3:
            hit[3].click(force=True)
            page.wait_for_timeout(4000)
        page.mouse.click(60, H // 2)
        caption(page, "")
        page.wait_for_timeout(800)

        # Critical path
        caption(page, "critical path", "the chain that set the run time; everything dimmed has slack")
        page.click("#critbtn")
        page.wait_for_timeout(4200)
        page.click("#critbtn")
        caption(page, "")
        page.wait_for_timeout(900)

        # Compare two runs
        caption(page, "compare runs", "per-node duration and token deltas against the previous run")
        page.click("#cmpbtn")
        page.wait_for_timeout(4200)
        page.mouse.click(60, H // 2)
        caption(page, "")
        page.wait_for_timeout(800)

        # Run picker
        caption(page, "every run is here", "the picker lists each workflow across your sessions")
        page.click("#runpick")
        page.wait_for_timeout(2200)
        page.keyboard.press("Escape")
        caption(page, "")
        page.wait_for_timeout(700)

        # Terminal mirror
        caption(page, "the terminal is a live mirror",
                "attached to your tmux Claude session; typing here types there")
        tsend("clear")
        page.click("#termtoggle")
        page.wait_for_timeout(2600)
        # type into the tmux session while the panel is open: proof it is a mirror
        tsend("./wfviz")
        page.wait_for_timeout(2600)
        tsend("git log --oneline -3")
        page.wait_for_timeout(3400)
        page.click("#termtoggle")
        caption(page, "")
        page.wait_for_timeout(1400)

        caption(page, "graph-claude", "github.com/trwilcoxson/graph-claude")
        page.wait_for_timeout(3000)

        ctx.close()
        browser.close()

    raw = None
    vd = os.path.join(OUT, "_vid")
    for f in sorted(os.listdir(vd)):
        if f.endswith(".webm"):
            raw = os.path.join(vd, f)
    if not raw:
        sys.exit("no video captured")

    dur = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", raw], capture_output=True, text=True).stdout.strip()
    print("source webm duration:", dur, "s")

    mp4 = os.path.join(OUT, "demo.mp4")
    subprocess.run(["ffmpeg", "-y", "-fflags", "+genpts", "-i", raw,
                    "-vf", f"scale={W}:-2:flags=lanczos",
                    "-fps_mode", "cfr", "-r", "25",
                    "-c:v", "libx264", "-preset", "slow", "-crf", "20",
                    "-pix_fmt", "yuv420p", "-movflags", "+faststart", mp4],
                   check=True, capture_output=True)
    subprocess.run(["ffmpeg", "-y", "-ss", "2", "-i", mp4, "-frames:v", "1",
                    os.path.join(OUT, "demo-poster.png")], check=True, capture_output=True)
    # raw capture kept in docs/_vid for inspection; it is gitignored
    print("wrote", mp4, os.path.getsize(mp4) // 1024, "kb")


if __name__ == "__main__":
    main()

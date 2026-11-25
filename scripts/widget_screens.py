"""Drives the website widget in headless Chrome against a LIVE Gemini model and saves screenshots + a run log.

For each business (realty, shop) it starts `python -m app.web` (real server, real retrieval, real SQL search, live
Gemini), opens the demo site, asks sample questions, books a viewing / asks for a person, and takes desktop and phone
screenshots: docs/screens/widget_<business>_<desktop|mobile>_<n>.png. A log with the answers, timings and the admin
report (tokens, cost) goes to data/live_runs/widget_<timestamp>.md (git-ignored). The API key is read from .env by the
server process and is never printed.

Usage: python scripts/widget_screens.py [--fallback gemini-3.1-flash-lite]   (needs `playwright install chromium`)
"""
from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import httpx
from playwright.sync_api import Page, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "screens"
LOGS = ROOT / "data" / "live_runs"
ADMIN_KEY = "screens-run-key"
WAIT_MS = 150_000  # a busy free-tier API can take minutes

PLAN = {
    "realty": {
        "questions": ["2-bedroom to rent in Chiado up to €2,400, I have a dog", "Do tenants pay an agency fee?"],
        "action": ("Book a viewing", "+351 912 345 678", "Anna"),
        "name": "Northwind Homes",
    },
    "shop": {
        "questions": ["Earbuds for running under €130", "How long does delivery to Ukraine take?"],
        "action": ("Order", "+48 600 100 200", "Tom"),
        "name": "Northwind Audio",
    },
}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server(vertical: str, tmp: Path, fallback: str):
    port = free_port()
    env = {
        **os.environ, "VERTICAL": vertical, "LLM_PROVIDER": "gemini", "PORT": str(port), "HOST": "127.0.0.1",
        "DATABASE_URL": f"sqlite+aiosqlite:///{(tmp / (vertical + '.db')).as_posix()}", "ADMIN_API_KEY": ADMIN_KEY,
        "DATA_DIR": str(ROOT / "data"), "GEMINI_FALLBACK_MODEL": fallback, "ADMIN_IDS": "", "PYTHONIOENCODING": "utf-8",
    }
    proc = subprocess.Popen([sys.executable, "-m", "app.web"], cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    for _ in range(240):
        if proc.poll() is not None:
            raise SystemExit("server exited during start-up (is GEMINI_API_KEY set in .env?)")
        try:
            if httpx.get(f"{base}/api/health", timeout=1).status_code == 200:
                return proc, base
        except httpx.HTTPError:
            time.sleep(0.5)
    raise SystemExit("server did not start")


def ask(page: Page, text: str | None, log: list[str], chip: bool = False) -> None:
    """Send a question (typed, or via the first sample-question chip) and wait until the answer is rendered."""
    before = page.locator(".msg.bot").count()
    started = time.perf_counter()
    if chip:
        page.locator(".chip", has_text=text).click()
    else:
        page.locator("textarea").fill(text)
        page.locator("textarea").press("Enter")
    page.locator(".typing").wait_for(state="attached", timeout=10_000)
    page.locator(".typing").wait_for(state="detached", timeout=WAIT_MS)
    page.wait_for_timeout(300)
    answer = page.locator(".msg.bot").last.inner_text().replace("\n", " ")
    cards = page.locator(".panel .card").count()
    log.append(f"- **Q:** {text}\n  - answered in {time.perf_counter() - started:.1f}s · new bot bubbles: "
               f"{page.locator('.msg.bot').count() - before} · cards on screen: {cards}\n  - **A:** {answer}")


def book(page: Page, button: str, phone: str, name: str, log: list[str]) -> None:
    page.locator(".panel .card").first.get_by_role("button", name=button).click()
    form = page.locator("form.form")
    form.get_by_label("Phone number").fill(phone)
    form.get_by_label("Name (optional)").fill(name)
    form.get_by_role("button", name="Send my number").click()
    page.locator(".msg.bot", has_text="We'll contact you").wait_for(timeout=30_000)
    log.append(f"- lead sent via the card button «{button}» → {page.locator('.msg.bot').last.inner_text()}")


ANDROID_UA = ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/131.0.0.0 Mobile Safari/537.36")
# Phone shots are taken on Windows, so without this the page would use Windows fonts and emoji. Android Chrome renders
# the same page with Roboto and Noto Color Emoji; load those (Google Fonts) into the page and the widget's shadow root.
ANDROID_FONTS_JS = """() => {
  const css = "*{font-family:Roboto,'Noto Color Emoji',sans-serif !important}";
  const add = (root, parent) => {
    if (root.querySelector && root.querySelector('#android-fonts')) return;
    const s = document.createElement('style'); s.id = 'android-fonts'; s.textContent = css; parent.appendChild(s);
  };
  if (!document.getElementById('android-fonts-link')) {
    const l = document.createElement('link'); l.id = 'android-fonts-link'; l.rel = 'stylesheet';
    l.href = 'https://fonts.googleapis.com/css2?family=Roboto:wght@400;500;700&family=Noto+Color+Emoji&display=block';
    document.head.appendChild(l);
  }
  add(document, document.head);
  document.querySelectorAll('*').forEach(el => { if (el.shadowRoot) add(el.shadowRoot, el.shadowRoot); });
}"""


def run_scenario(browser, base: str, vertical: str, mode: str, log: list[str]) -> None:
    plan = PLAN[vertical]
    if mode == "desktop":
        context = browser.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
    else:
        context = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=3, is_mobile=True,
                                      has_touch=True, user_agent=ANDROID_UA)
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

    def shot(n: int) -> None:
        if mode == "mobile":
            page.evaluate(ANDROID_FONTS_JS)
            page.evaluate("document.fonts.ready")
            page.wait_for_timeout(1500)  # emoji font files load on first use
        page.screenshot(path=str(OUT / f"widget_{vertical}_{mode}_{n}.png"))
    log.append(f"\n### {plan['name']} · {mode}\n")
    page.goto(base)
    page.locator(".launcher").click()
    page.locator(".msg.bot").first.wait_for()
    page.wait_for_timeout(400)
    shot(1)  # welcome + sample questions
    ask(page, plan["questions"][0], log, chip=True)
    shot(2)  # search -> cards
    ask(page, plan["questions"][1], log)
    shot(3)  # answer from the documents with sources
    book(page, *plan["action"][:1], plan["action"][1], plan["action"][2], log)
    shot(4)  # lead confirmation
    log.append(f"- browser console/page errors: {errors or 'none'}")
    context.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fallback", default="gemini-3.1-flash-lite")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    log = [f"# Website widget live run {stamp}\n", f"Fallback model: `{args.fallback}`; main model from .env (GEMINI_MODEL)\n"]
    tmp = Path(tempfile.mkdtemp(prefix="widget_screens_"))
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for vertical in PLAN:
            proc, base = start_server(vertical, tmp, args.fallback)
            try:
                for mode in ("desktop", "mobile"):
                    run_scenario(browser, base, vertical, mode, log)
                stats = httpx.get(f"{base}/api/admin/stats", headers={"X-Admin-Key": ADMIN_KEY}, timeout=10).json()
                leads = httpx.get(f"{base}/api/admin/leads.csv", headers={"X-Admin-Key": ADMIN_KEY}, timeout=10).text
                log.append(f"\n### {PLAN[vertical]['name']} · admin API after both runs\n")
                log.append(f"- questions {stats['questions']}, with sources {stats['with_sources']}, cards shown {stats['with_cards']}, "
                           f"hand-offs {stats['handoffs']}, leads {stats['leads']} {stats['lead_kinds']}")
                log.append(f"- tokens {stats['tokens_in']:,} in / {stats['tokens_out']:,} out → ${stats['cost']:.4f} "
                           f"(${stats['per_question']:.5f} per question at list price)")
                log.append(f"- leads.csv rows: {len(leads.strip().splitlines()) - 1}, channels: "
                           f"{sorted({line.split(',')[6] for line in leads.strip().splitlines()[1:]})}")
            finally:
                proc.terminate()
                proc.wait(timeout=15)
        browser.close()
    text = "\n".join(log)
    (LOGS / f"widget_{stamp}.md").write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()

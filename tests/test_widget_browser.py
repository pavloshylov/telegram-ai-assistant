"""Widget smoke test in headless Chrome: the real server (`python -m app.web`, offline demo model) + widget.js.

Skipped when Playwright or its Chromium is not installed (`pip install playwright && playwright install chromium`).
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

playwright_sync = pytest.importorskip("playwright.sync_api")

ROOT = Path(__file__).resolve().parent.parent
ADMIN_KEY = "browser-test-key"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def client_port() -> int:
    """Port of the pretend customer website (a different origin than the assistant server)."""
    return free_port()


@pytest.fixture(scope="module")
def server(tmp_path_factory, client_port):
    tmp = tmp_path_factory.mktemp("widget")
    port = free_port()
    env = {
        **os.environ, "VERTICAL": "realty", "LLM_PROVIDER": "demo", "PORT": str(port), "HOST": "127.0.0.1",
        "DATABASE_URL": f"sqlite+aiosqlite:///{(tmp / 'w.db').as_posix()}", "ADMIN_API_KEY": ADMIN_KEY,
        "DATA_DIR": str(ROOT / "data"), "BOT_TOKEN": "", "ADMIN_IDS": "",
        "WIDGET_ALLOWED_ORIGINS": f"http://127.0.0.1:{client_port}",
    }
    proc = subprocess.Popen([sys.executable, "-m", "app.web"], cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(240):
            if proc.poll() is not None:
                pytest.fail("the widget server exited during start-up")
            try:
                if httpx.get(f"{base}/api/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.5)
        else:
            pytest.fail("the widget server did not start in 2 minutes")
        yield base
    finally:
        proc.terminate()
        proc.wait(timeout=15)


@pytest.fixture(scope="module")
def browser():
    with playwright_sync.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:  # noqa: BLE001 - browser binaries not installed
            pytest.skip(f"Chromium not available: {type(exc).__name__}")
        yield b
        b.close()


def test_visitor_asks_a_question_books_a_viewing_and_the_lead_arrives(server, browser):
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.goto(server)
    page.locator(".launcher").click()  # Playwright pierces the open shadow root
    log = page.locator(".log")
    log.get_by_text("I'm Nora").wait_for()  # welcome text from /api/config, **bold** rendered without HTML
    assert log.locator("strong", has_text="Northwind Homes").count() == 1

    page.locator(".chip").first.click()  # sample question -> scripted answer, real search, card
    card = page.locator(".panel .card").first  # not the demo site's own listing cards
    card.wait_for(timeout=60_000)
    assert "Elegant 2-bedroom with balcony" in card.inner_text() and "€2,300/month" in card.inner_text()

    card.get_by_role("button", name="Book a viewing").click()
    form = page.locator("form.form")
    form.get_by_label("Phone number").fill("abc")
    form.get_by_role("button", name="Send my number").click()
    form.locator(".err").get_by_text("valid phone number").wait_for()
    form.get_by_label("Phone number").fill("+351 912 345 678")
    form.get_by_label("Name (optional)").fill("Anna")
    form.get_by_role("button", name="Send my number").click()
    log.get_by_text("We'll contact you at +351 912 345 678").wait_for()

    csv = httpx.get(f"{server}/api/admin/leads.csv", headers={"X-Admin-Key": ADMIN_KEY}).text
    assert "viewing" in csv and "NH-103" in csv and "+351 912 345 678" in csv and ",web," in csv
    assert [e for e in errors if "422" not in e] == []  # the 422 is the deliberately wrong phone number above
    page.close()


def test_chat_survives_reload_and_works_on_a_phone_screen(server, browser):
    context = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
    page = context.new_page()
    page.goto(server)
    page.locator(".launcher").click()
    page.locator("textarea").fill("Do tenants pay an agency fee?")
    page.locator("textarea").press("Enter")
    page.locator(".msg.bot", has_text="agency fee").last.wait_for(timeout=60_000)
    box = page.locator(".panel").bounding_box()
    assert box["width"] >= 385 and box["height"] >= 835  # full screen on a phone
    assert not page.evaluate("document.documentElement.scrollWidth > window.innerWidth")  # no horizontal scroll
    page.reload()
    page.locator(".launcher").click()
    page.locator(".msg.me", has_text="Do tenants pay an agency fee?").wait_for()  # transcript restored for the same visitor
    context.close()


@pytest.mark.parametrize("name", ["realty", "shop"])
def test_demo_sites_do_not_overflow_on_a_phone(browser, name):
    """A page wider than the phone widens the layout viewport and shifts the fixed chat bubble (found live)."""
    context = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
    page = context.new_page()
    page.goto((ROOT / "app" / "web" / "demo" / f"{name}.html").as_uri())
    assert page.evaluate("document.documentElement.scrollWidth") == 390
    context.close()


@pytest.fixture(scope="module")
def client_site(server, client_port):
    """A pretend customer website on its own port (= its own origin) that embeds the widget with one script tag."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    html = (f'<html><body><h1>Client site</h1><script src="{server}/widget.js" data-api="{server}" '
            f'data-color="#c0392b" data-lang="uk" data-open="true"></script></body></html>').encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html)

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", client_port), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{client_port}/"
    httpd.shutdown()


def test_embedded_on_another_origin_with_attributes_and_hostile_text_stays_inert(client_site, browser):
    page = browser.new_page()
    origin = client_site.rstrip("/")
    page.route("**/api/chat", lambda route: route.fulfill(  # a hostile reply, as if the model echoed attacker text
        status=200, content_type="application/json", headers={"Access-Control-Allow-Origin": origin},
        body='{"status":"ok","reply":"<img src=x onerror=window.__pwned=1> **bold**","sources":[],"cards":[],"handoff":false}'))
    page.goto(client_site)  # another origin: /api/config works only through the CORS allowlist
    page.locator("textarea").wait_for()
    assert page.locator(".launcher").evaluate("el => getComputedStyle(el).backgroundColor") == "rgb(192, 57, 43)"
    assert page.locator("textarea").get_attribute("placeholder") == "Напишіть повідомлення…"
    page.locator("textarea").fill("hi")
    page.locator(".send").click()
    page.locator(".msg.bot strong", has_text="bold").wait_for()
    assert page.evaluate("window.__pwned") is None  # server text is never parsed as HTML
    assert "<img" in page.locator(".msg.bot").last.inner_text()
    page.close()

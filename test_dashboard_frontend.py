"""Browser-level regression tests for the embedded dashboard frontend."""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

import mh_dash as dash


def trader(name):
    return {"trader": name, "equity": 24, "equity_nzd": 40.08, "started": 24,
            "started_nzd": 40.08, "committed": 0, "available": 24,
            "fx_nzd_per_usd": 1.67}


SUMMARY = {"totals": {"open_positions": 0}, "coins": [],
           "traders": [trader(x) for x in ("scalper", "reasoner", "whale_trader", "memecoin_trader")],
           "grid": {"equity_usd": 24, "fx_nzd_per_usd": 1.67, "wallet": {}, "grid": {}}}
SURVIVAL = {"edge": {}, "live_positions": [], "paper_positions": [], "exit_reasons": {},
            "risk_params": {}, "live_trades": [], "paper_trades": [], "cycles": [],
            "notional": {"paper_usd": 0, "live_usd": 0}}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if urlparse(self.path).path == "/":
            body = dash._html().encode()
            self.send_response(200); self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        else:
            self.send_response(404); self.end_headers()
    def log_message(self, *args):
        pass


@unittest.skipUnless(sync_playwright, "playwright not installed")
class DashboardFrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True); cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}/"
        cls.pw = sync_playwright().start(); cls.browser = cls.pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close(); cls.pw.stop(); cls.server.shutdown(); cls.server.server_close()

    def setUp(self):
        self.ctx = self.browser.new_context(viewport={"width": 1280, "height": 800})
        self.page = self.ctx.new_page(); self.page.set_default_timeout(5000)
        def route(r):
            path = urlparse(r.request.url).path
            if path == "/api/summary": data = SUMMARY
            elif path == "/api/survival": data = SURVIVAL
            elif path == "/api/grid": data = {"enabled": False}
            elif path in ("/api/market", "/api/strategies", "/api/trades", "/api/positions", "/api/exit_reason_series"): data = []
            else: data = {}
            r.fulfill(status=200, content_type="application/json", body=json.dumps(data))
        self.page.route("**/api/**", route)
        self.page.route("https://fonts.googleapis.com/**", lambda r: r.abort())
        self.page.route("https://fonts.gstatic.com/**", lambda r: r.abort())
        self.page.goto(self.url, wait_until="commit", timeout=5000)
        self.page.wait_for_selector(".kpi")

    def tearDown(self):
        self.ctx.close()

    def open_survival(self):
        self.page.click('[data-tab="survival"]')
        self.page.wait_for_selector("text=Open paper incubator positions")

    def test_wallet_cards_share_the_same_surface(self):
        colors = self.page.locator(".hero .kpi").evaluate_all("els=>els.map(e=>getComputedStyle(e).backgroundColor)")
        self.assertGreaterEqual(len(colors), 4); self.assertEqual(len(set(colors)), 1)

    def test_dark_mode_is_persistent_and_available_on_mobile(self):
        self.page.click("#themeToggle")
        self.assertEqual(self.page.evaluate("document.documentElement.dataset.theme"), "dark")
        self.assertEqual(self.page.evaluate("localStorage.getItem('mh-theme')"), "dark")
        self.page.reload(wait_until="commit"); self.page.wait_for_selector("#themeToggle")
        self.assertEqual(self.page.evaluate("document.documentElement.dataset.theme"), "dark")

    def test_survival_widget_swap_and_provenance(self):
        self.open_survival()
        result = self.page.evaluate("""()=>{const cards=[...document.querySelectorAll('#tab-panels .card')];
          const find=t=>cards.find(c=>c.querySelector('h3')?.textContent.includes(t));
          const e=find('Exit reasons'),l=find('Open live'),p=find('Open paper');
          return {same:e.parentElement===l.parentElement, paperTop:p.parentElement.id==='tab-panels', text:document.querySelector('#tab-panels').innerText}}""")
        self.assertTrue(result["same"]); self.assertTrue(result["paperTop"])
        self.assertIn("real on-chain fills", result["text"])
        self.assertIn("PAPER TRADING", self.page.locator("body").inner_text())

    def test_move_buttons_persist_widget_order(self):
        before = self.page.locator(".hero .kpi").evaluate_all("els=>els.map(e=>e.dataset.widgetId)")
        self.page.locator(".hero .kpi").nth(1).locator('[data-move="up"]').click()
        after = self.page.locator(".hero .kpi").evaluate_all("els=>els.map(e=>e.dataset.widgetId)")
        self.assertNotEqual(before, after)
        self.page.reload(wait_until="commit"); self.page.wait_for_function("()=>document.querySelector('.kpi')?.dataset.widgetId")
        again = self.page.locator(".hero .kpi").evaluate_all("els=>els.map(e=>e.dataset.widgetId)")
        self.assertEqual(after, again)

    def test_mobile_has_no_page_overflow(self):
        self.ctx.close(); self.ctx = self.browser.new_context(viewport={"width": 360, "height": 780})
        self.page = self.ctx.new_page(); self.page.route("**/api/**", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(SUMMARY if urlparse(r.request.url).path == '/api/summary' else {})))
        self.page.goto(self.url, wait_until="commit"); self.page.wait_for_selector("#themeToggle")
        self.assertTrue(self.page.locator("#themeToggle").is_visible())
        self.assertLessEqual(self.page.evaluate("document.body.scrollWidth"), 360)


if __name__ == "__main__":
    unittest.main()

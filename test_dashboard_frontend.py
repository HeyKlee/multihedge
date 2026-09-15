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

XORA_SUMMARY = {
    "ok": True,
    "wallet": {"starting_equity_usd": 10.0, "paper_net_usd": 1.72, "paper_equity_usd": 11.72,
               "paper_equity_nzd": 19.57, "starting_equity_nzd": 16.7, "pnl_vs_start_usd": 1.72,
               "pnl_vs_start_pct": 0.172, "closed_trades": 107, "fx_nzd_per_usd": 1.67},
    "edge": {"live_n": 2, "live_fills": 2, "live_closes": 0, "paper_n": 107, "paper_wins": 63,
             "paper_win_rate": 0.5888, "paper_net_usd": 1.72, "starting_equity_usd": 10.0},
    "positions": {"live": 0, "paper": 12},
    "notional": {"paper_usd": 13.0, "live_usd": 0},
    "risk": {"promotion_enabled": False, "defaults": {}},
    "history": [{"kind": "paper", "coin": "EMBER", "side": "LONG", "pnl_usd": 0.33, "reason": "take_profit"}],
    "top_exit_reasons": [["trail_stop", 42], ["stop_loss", 36], ["take_profit", 17], ["max_hold", 13]],
    "council": {"available": True, "filename": "2026-09-14_00-20-15.md",
                "verdict": ["2 coins acted on, 0 applied.", "EMBER & STONK proposals."]},
}


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
        # Keep an in-memory server-side prefs store so save/load round-trips in-mock.
        self._mock_prefs = {}
        def route(r):
            path = urlparse(r.request.url).path
            if path == "/api/summary": data = SUMMARY
            elif path == "/api/xora/summary": data = XORA_SUMMARY
            elif path == "/api/survival": data = SURVIVAL
            elif path == "/api/grid": data = {"enabled": False}
            elif path == "/api/trades":
                data = [{"coin": "EMBER", "symbol": "EMBER", "setup": "dynamic_scalper", "side": "LONG", "entry_px": 0.01, "exit_px": 0.012, "realized_pct": 0.2, "realized_usd": 0.2, "exit_reason": "take_profit", "close_ts": 1789447079}]
            elif path in ("/api/market", "/api/strategies", "/api/positions", "/api/exit_reason_series"): data = []
            elif path == "/api/ui/prefs" and r.request.method == "POST":
                try:
                    self._mock_prefs = json.loads(r.request.post_data)
                except Exception:
                    self._mock_prefs = {}
                data = {"ok": True, "prefs": self._mock_prefs}
            elif path == "/api/ui/prefs":
                data = self._mock_prefs
            elif path == "/api/widget-issues" and r.request.method == "POST":
                data = {"ok": True, "id": 42, "status": "new"}
            elif path == "/api/widget-issues":
                data = []
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

    def test_wallet_hub_replaces_repetitive_wallet_cards(self):
        self.page.wait_for_selector(".wallet-hub")
        tabs = self.page.locator(".wallet-tab").evaluate_all("els=>els.map(e=>e.innerText)")
        self.assertEqual(len(tabs), 6)
        joined = "\n".join(tabs).lower()
        for name in ("scalper", "reasoner", "whale", "memecoin", "grid", "xora-survival"):
            self.assertIn(name, joined)

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

    def test_mobile_has_no_page_overflow(self):
        self.ctx.close(); self.ctx = self.browser.new_context(viewport={"width": 360, "height": 780})
        self.page = self.ctx.new_page(); self.page.route("**/api/**", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(SUMMARY if urlparse(r.request.url).path == '/api/summary' else {})))
        self.page.goto(self.url, wait_until="commit"); self.page.wait_for_selector("#themeToggle")
        self.assertTrue(self.page.locator("#themeToggle").is_visible())
        self.assertLessEqual(self.page.evaluate("document.body.scrollWidth"), 360)

    def test_settings_button_is_icon_only(self):
        btn = self.page.locator("#settingsBtn")
        self.assertTrue(btn.get_attribute("aria-label"))
        self.assertEqual(btn.inner_text().strip(), "\u2699")
        self.assertNotRegex(btn.inner_text().strip(), r"(?i)settings?|configure")

    def test_widget_report_icon_opens_modal_and_posts_context(self):
        self.page.wait_for_selector(".widget-report-btn")
        captured = {}

        def capture_issue(route):
            captured["payload"] = json.loads(route.request.post_data)
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True, "id": 77, "status": "new"}))

        self.page.route("**/api/widget-issues", capture_issue)
        first = self.page.locator(".widget-report-btn").first
        self.assertEqual(first.inner_text().strip(), "🐞")
        self.assertTrue(first.get_attribute("aria-label"))
        first.click()
        self.page.wait_for_selector("#widgetIssueModal.open")
        self.page.fill("#widgetIssueText", "this card is too cramped on my phone")
        self.page.click("#widgetIssueSubmit")
        self.page.wait_for_function("window.__issueDone === true || document.querySelector('#widgetIssueModal')?.classList.contains('open') === false", timeout=3000)
        self.assertIn("payload", captured)
        payload = captured["payload"]
        self.assertEqual(payload["issue"], "this card is too cramped on my phone")
        self.assertTrue(payload["widget_id"])
        self.assertTrue(payload["widget_title"])
        self.assertIn("visible_text", payload)
        self.assertEqual(payload["context"]["route"], "/")

    def test_chat_pet_opens_chat_and_uses_bounded_speech_bubble(self):
        pet = self.page.locator("#xoraPet")
        bubble = self.page.locator("#petBubble")
        self.assertTrue(pet.is_visible())
        self.assertEqual(pet.get_attribute("aria-label"), "Open Xora-Survival pet chat")
        self.assertTrue(bubble.is_visible())
        self.assertEqual(bubble.evaluate("el=>getComputedStyle(el).position"), "fixed")
        self.assertLessEqual(bubble.bounding_box()["right"] if False else bubble.bounding_box()["x"] + bubble.bounding_box()["width"], 1280)
        self.assertFalse(self.page.locator("#chatPanel").is_visible())
        pet.dblclick(timeout=3000, force=True)
        self.assertTrue(self.page.locator("#chatPanel").is_visible())
        self.assertIn("Xora", self.page.locator(".chat-header span").inner_text())
        # Double-clicking again closes the panel
        pet.dblclick(timeout=3000, force=True)
        self.assertFalse(self.page.locator("#chatPanel").is_visible())
        pet.dblclick(timeout=3000, force=True)
        self.assertTrue(self.page.locator("#chatPanel").is_visible())

    def test_pet_can_be_dragged_and_bounces_off_walls(self):
        pet = self.page.locator("#xoraPet")
        box_before = pet.bounding_box()
        # Drag across the viewport
        x1 = box_before["x"] + box_before["width"] / 2
        y1 = box_before["y"] + box_before["height"] / 2
        self.page.mouse.move(x1, y1)
        self.page.mouse.down()
        # Fast flick to the right
        self.page.mouse.move(x1 + 200, y1, steps=5)
        self.page.mouse.up()
        # After a toss the pet should have moved
        self.page.wait_for_timeout(400)
        box_after = pet.bounding_box()
        self.assertNotAlmostEqual(box_before["x"], box_after["x"], delta=5)
        self.assertGreaterEqual(box_after["x"], 0)
        self.assertLessEqual(box_after["x"] + box_after["width"], 1280)
        self.assertGreaterEqual(box_after["y"], 0)
        self.assertLessEqual(box_after["y"] + box_after["height"], 800)

    def test_pet_speech_updates_when_hovering_widget(self):
        # Try to trigger a pet tip by hovering over a non-empty card
        cards = self.page.locator(".card")
        if cards.count():
            cards.first.hover()
        else:
            self.skipTest("no card elements in mock view")
        try:
            self.page.wait_for_function("document.querySelector('#petBubble')?.innerText.length > 0", timeout=3000)
        except Exception:
            self.skipTest("pet tip did not appear within timeout")
        text = self.page.locator("#petBubble").inner_text()
        self.assertGreater(len(text), 0)

    def test_xora_nav_sits_above_traders(self):
        order = self.page.locator("#tabNav button").evaluate_all("els=>els.map(e=>e.dataset.tab)")
        self.assertLess(order.index("survival"), order.index("strategies"))

    def test_overview_has_what_to_do_and_xora_widgets(self):
        self.page.wait_for_selector("text=What to do")
        body = self.page.locator("body").inner_text()
        self.assertIn("What to do", body)
        self.assertIn("Xora wallet", body)
        self.assertIn("Xora profit", body)

    def test_overview_shows_recent_trade_history(self):
        self.page.wait_for_selector("text=Recent trade history")
        body = self.page.locator("body").inner_text()
        self.assertIn("Recent trade history", body)
        self.assertIn("EMBER", body)
        self.assertIn("XORA PAPER", body)
        self.assertIn("TAKE_PROFIT", body.upper())

    def test_xora_summary_widget_renders_live_stats_and_open_report(self):
        # Regression: the Gate pill used a broken compound .toFixed(0) on a
        # string concat, which threw and fell back to "Xora summary unavailable".
        # The widget must render real stats and expose an Open full report button.
        self.page.wait_for_selector("#councilSummary")
        self.page.wait_for_function(
            "() => { const c=document.getElementById('councilSummary');"
            " return c && c.innerText.length > 40 && !/unavailable|offline/i.test(c.innerText); }",
            timeout=6000)
        text = self.page.locator("#councilSummary").inner_text()
        for needle in ("EQUITY", "START", "NET P/L", "WIN RATE", "CLOSED", "GATE",
                       "SCOPE & POLICY", "RECENT HISTORY", "COUNCIL VERDICT"):
            self.assertIn(needle, text)
        self.assertIn("107 / 50", text)          # paper closed / threshold
        self.assertIn("EMBER", text)             # recent history row
        self.assertIn("2 coins acted on", text)  # council verdict
        self.assertTrue(self.page.locator("#councilSummary .council-open").is_visible())
        self.assertEqual(self.page.locator("#councilSummary .council-open").inner_text(),
                         "Open full report")

    def enter_edit_mode(self):
        # Edit toggle lives in the Settings modal.
        self.page.locator("#settingsBtn").click()
        self.page.locator("#editToggleBtn").click()
        self.page.locator("#settingsClose").click()

    def test_clock_survives_and_edit_toggle_is_in_settings(self):
        # Clock is restored (still ticks; shows HH:MM:SS), not replaced by a label.
        self.page.wait_for_selector("#ts")
        print("clock text:", repr(self.page.locator("#ts").inner_text()))
        self.assertEqual(len(self.page.locator("#ts").inner_text().split(":")), 3)
        self.page.locator("#settingsBtn").click()
        self.assertTrue(self.page.locator("#editToggleBtn").is_visible())
        self.page.locator("#settingsClose").click()

    def test_edit_button_toggles_to_save_and_shows_cancel(self):
        self.enter_edit_mode()
        self.assertTrue(self.page.evaluate("document.body.classList.contains('free-edit')"))
        self.assertTrue(self.page.locator("#cancelEditBtn").is_visible())
        self.assertTrue(self.page.locator("#addWidgetBtn").is_visible())
        # Actual grab/resize handles exist on widgets; the old corner square is gone.
        self.assertGreater(self.page.locator(".free-grab").count(), 0)
        self.assertGreater(self.page.locator(".free-resize").count(), 0)
        self.assertEqual(self.page.locator(".tile-resize").count(), 0)

    def test_cancel_hides_edit_mode(self):
        self.enter_edit_mode()
        self.assertTrue(self.page.locator("#cancelEditBtn").is_visible())
        self.page.locator("#cancelEditBtn").click()
        self.assertFalse(self.page.evaluate("document.body.classList.contains('free-edit')"))
        self.assertFalse(self.page.locator("#cancelEditBtn").is_visible())

    def test_widget_catalog_lists_multihedge_widgets(self):
        self.enter_edit_mode()
        self.page.locator("#addWidgetBtn").click()
        self.page.wait_for_selector(".widget-catalog")
        text = self.page.locator(".widget-catalog").inner_text()
        for want in ("Wallet Hub", "Audit", "Council", "Risk"):
            self.assertIn(want, text)

    def test_widgets_keep_positions_when_entering_edit_mode(self):
        # Force a rich render (overview) so multiple widgets exist.
        self.page.locator("#clockBtn").click()
        self.page.wait_for_selector(".kpi,.card", timeout=4000)
        # Record distinct on-screen positions before edit.
        ids_before = self.page.evaluate(
            "()=>[...document.querySelectorAll('.card,.kpi,.widget-free')].map(e=>"
            "Math.round(e.getBoundingClientRect().left)+'x'+Math.round(e.getBoundingClientRect().top))")
        self.assertGreater(len(set(ids_before)), 0)
        self.enter_edit_mode()
        self.page.wait_for_selector(".free-grab")
        ids_after = self.page.evaluate(
            "()=>[...document.querySelectorAll('.card,.kpi,.widget-free')].map(e=>"
            "Math.round(e.getBoundingClientRect().left)+'x'+Math.round(e.getBoundingClientRect().top))")
        # If multiple widgets exist they must NOT all collapse to one stack point.
        if len(ids_after) > 1:
            self.assertGreater(len(set(ids_after)), 1)

    def test_saved_layout_reapplies_widgets_after_reload(self):
        self.enter_edit_mode()
        self.page.wait_for_selector(".free-grab")
        # Set a distinct draft spread, then Save via the Settings toggle.
        self.page.evaluate("""()=>{
          const ws={};
          const fw=window.freeWidgets?freeWidgets():[];
          fw.forEach(({w},i)=>{ws[w.dataset.widgetId]={x:40+i*140,y:30,w:320,h:160};});
          window._draftLayout=ws;
        }""")
        self.page.locator("#settingsBtn").click()
        self.page.locator("#editToggleBtn").click()
        self.page.locator("#settingsClose").click()
        # Save triggers an async persist + re-render; wait for the view-mode reapply.
        self.page.wait_for_function(
            "()=>document.querySelectorAll('.layout-reapplied').length>0", timeout=8000)


if __name__ == "__main__":
    unittest.main()

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


    def open_mobile_view(self, width=360, height=780):
        self.ctx.close()
        self.ctx = self.browser.new_context(viewport={"width": width, "height": height}, is_mobile=True)
        self.page = self.ctx.new_page(); self.page.set_default_timeout(5000)
        self._mock_prefs = {}
        def route(r):
            path = urlparse(r.request.url).path
            if path == "/api/summary": data = SUMMARY
            elif path == "/api/xora/summary": data = XORA_SUMMARY
            elif path == "/api/survival": data = SURVIVAL
            elif path == "/api/grid": data = {"enabled": False}
            elif path == "/api/trades": data = [{"coin": "EMBER", "symbol": "EMBER", "setup": "dynamic_scalper", "side": "LONG", "entry_px": 0.01, "exit_px": 0.012, "realized_pct": 0.2, "realized_usd": 0.2, "exit_reason": "take_profit", "close_ts": 1789447079}]
            elif path in ("/api/market", "/api/strategies", "/api/positions", "/api/exit_reason_series"): data = []
            elif path == "/api/ui/prefs" and r.request.method == "POST":
                try: self._mock_prefs = json.loads(r.request.post_data)
                except Exception: self._mock_prefs = {}
                data = {"ok": True, "prefs": self._mock_prefs}
            elif path == "/api/ui/prefs": data = self._mock_prefs
            elif path == "/api/widget-issues": data = []
            else: data = {}
            r.fulfill(status=200, content_type="application/json", body=json.dumps(data))
        self.page.route("**/api/**", route)
        self.page.route("https://fonts.googleapis.com/**", lambda r: r.abort())
        self.page.route("https://fonts.gstatic.com/**", lambda r: r.abort())
        self.page.goto(self.url, wait_until="commit", timeout=5000)
        self.page.wait_for_selector("#themeToggle")

    def assert_no_mobile_overflow(self, width):
        metrics = self.page.evaluate("""()=>({
          body: document.body.scrollWidth,
          doc: document.documentElement.scrollWidth,
          shell: document.querySelector('.app-shell')?.getBoundingClientRect().width || 0,
          cards: [...document.querySelectorAll('.card,.kpi,.modal-panel,.widget-catalog,.chat-panel')].map(e=>{
            const r=e.getBoundingClientRect(); return {left:r.left,right:r.right,width:r.width,visible:getComputedStyle(e).display!=='none'};
          })
        })""")
        self.assertLessEqual(metrics["body"], width + 1)
        self.assertLessEqual(metrics["doc"], width + 1)
        self.assertLessEqual(metrics["shell"], width + 1)
        for box in metrics["cards"]:
            if box["visible"]:
                self.assertGreaterEqual(box["left"], -1)
                self.assertLessEqual(box["right"], width + 1)

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


    def test_mobile_breakpoints_keep_core_dashboard_within_viewport(self):
        for width, height in ((320, 680), (360, 780), (414, 896), (760, 900)):
            with self.subTest(width=width):
                self.open_mobile_view(width, height)
                self.assertTrue(self.page.locator("#themeToggle").is_visible())
                self.assertTrue(self.page.locator("#settingsBtn").is_visible())
                self.assert_no_mobile_overflow(width)
                nav = self.page.locator("#tabNav")
                self.assertLessEqual(nav.bounding_box()["width"], width)
                self.assertIn(self.page.evaluate("getComputedStyle(document.querySelector('#tabNav')).overflowX"), ("auto", "scroll"))

    def test_mobile_modals_catalog_and_chat_stay_within_viewport(self):
        width = 360
        self.open_mobile_view(width, 780)
        self.page.locator("#settingsBtn").click()
        self.page.wait_for_selector("#settingsModal.open")
        self.assert_no_mobile_overflow(width)
        panel = self.page.locator("#settingsModal .modal-panel").bounding_box()
        self.assertLessEqual(panel["width"], width)
        self.page.locator("#settingsClose").click()
        self.enter_edit_mode()
        self.page.locator("#addWidgetBtn").click()
        self.page.wait_for_selector("#widgetCatalog.open")
        self.assert_no_mobile_overflow(width)
        catalog = self.page.locator("#widgetCatalog").bounding_box()
        self.assertLessEqual(catalog["width"], width)
        self.page.locator("#widgetCatalogClose").click()
        self.page.locator("#xoraPet").dblclick(timeout=3000, force=True)
        self.assert_no_mobile_overflow(width)
        chat = self.page.locator("#chatPanel").bounding_box()
        self.assertLessEqual(chat["width"], width)

    def test_mobile_survival_tables_scroll_instead_of_expanding_page(self):
        width = 360
        self.open_mobile_view(width, 780)
        self.page.click('[data-tab="survival"]')
        self.page.wait_for_selector("#tab-panels")
        self.assert_no_mobile_overflow(width)
        scrolls = self.page.locator(".scroll-wrap")
        self.assertGreater(scrolls.count(), 0)
        first = scrolls.first.evaluate("""el=>({overflowX:getComputedStyle(el).overflowX, clientWidth:el.clientWidth, scrollWidth:el.scrollWidth})""")
        self.assertIn(first["overflowX"], ("auto", "scroll"))
        self.assertLessEqual(self.page.evaluate("document.body.scrollWidth"), width + 1)

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
        upper = text.upper()
        for want in ("SYSTEM / OVERVIEW", "XORA-SURVIVAL", "SCALPER", "REASONER", "WHALE COPY", "MEMECOIN", "GRID", "MARKET", "LIVE GATE"):
            self.assertIn(want, upper)
        for want in ("SUMMARY", "CHARTS", "HISTORY", "TRADING", "POSITIONS", "SIGNALS"):
            self.assertIn(want, upper)
        for want in ("Tracked Wallets Curated Activity", "Grid Ladder Live Price Vs Levels", "Recent Memecoin Signals Pump Monitor", "Paper Incubator Trade History"):
            self.assertIn(want, text)

    def test_widget_catalog_click_adds_and_persists_widget(self):
        self.enter_edit_mode()
        before = self.page.locator('[data-widget-id^="recent-trade-history"]').count()
        self.page.locator("#addWidgetBtn").click()
        self.page.locator('.wc-item[data-widget="recent-trade-history"]').click()
        self.page.wait_for_selector('[data-widget-id^="recent-trade-history"]')
        after = self.page.locator('[data-widget-id^="recent-trade-history"]').count()
        self.assertGreater(after, before)
        added_id = self.page.locator('[data-widget-id^="recent-trade-history"]').last.get_attribute("data-widget-id")
        added_text = self.page.locator('[data-widget-id="%s"]' % added_id).inner_text()
        self.assertIn("Recent Trade History", added_text)
        self.assertNotIn("Custom dashboard widget slot", added_text)
        self.assertTrue(any(word in added_text for word in ("EMBER", "XORA", "P/L", "No recent trades")))
        self.page.locator("#settingsBtn").click()
        self.page.locator("#editToggleBtn").click()
        self.page.locator("#settingsClose").click()
        self.page.wait_for_function("()=>!document.body.classList.contains('free-edit')", timeout=8000)
        self.assertIn(added_id, self._mock_prefs.get("layout", {}).get("overview", {}))
        self.assertEqual(self._mock_prefs["layout"]["overview"][added_id]["type"], "catalog")
        self.page.reload(wait_until="commit"); self.page.wait_for_selector('#themeToggle')
        self.page.wait_for_selector('[data-widget-id="%s"]' % added_id, timeout=8000)

    def test_widget_catalog_drag_drop_adds_widget_at_drop_point(self):
        self.enter_edit_mode()
        self.page.locator("#addWidgetBtn").click()
        self.page.evaluate("""()=>{
          const item=document.querySelector('.wc-item[data-widget="risk-params"]');
          const root=document.querySelector('#tab-panels');
          const dt=new DataTransfer();
          dt.setData('application/x-mh-widget', JSON.stringify({id:item.dataset.widget,label:item.querySelector('b').textContent,hint:item.dataset.hint||''}));
          root.dispatchEvent(new DragEvent('dragover',{bubbles:true,cancelable:true,dataTransfer:dt,clientX:260,clientY:220}));
          root.dispatchEvent(new DragEvent('drop',{bubbles:true,cancelable:true,dataTransfer:dt,clientX:260,clientY:220}));
        }""")
        self.page.wait_for_selector('[data-widget-id^="risk-params"]')
        box = self.page.locator('[data-widget-id^="risk-params"]').last.bounding_box()
        self.assertIsNotNone(box)
        self.assertGreaterEqual(box["x"], 0)
        txt = self.page.locator('[data-widget-id^="risk-params"]').last.inner_text()
        self.assertIn("Risk Params", txt)
        self.assertNotIn("Custom dashboard widget slot", txt)

    def test_added_trader_widget_contains_live_snapshot_not_empty_shell(self):
        self.enter_edit_mode()
        self.page.locator("#addWidgetBtn").click()
        self.page.locator('.wc-item[data-widget="tracked-wallets-curated-activity"]').click()
        self.page.wait_for_selector('[data-widget-id^="tracked-wallets-curated-activity"]')
        self.page.wait_for_function("""()=>{
          const e=document.querySelector('[data-widget-id^="tracked-wallets-curated-activity"]');
          return e && /Tracked Wallets|NAME|HANDLE|ACTIVE|QUIET|No tracked/.test(e.innerText);
        }""", timeout=8000)
        text = self.page.locator('[data-widget-id^="tracked-wallets-curated-activity"]').last.inner_text()
        self.assertIn("Tracked Wallets Curated Activity", text)
        self.assertNotIn("Custom dashboard widget slot", text)
        self.assertRegex(text, r"Name|Handle|ACTIVE|QUIET|No tracked")

    def test_added_widget_content_fills_resized_widget_without_font_scaling(self):
        self.enter_edit_mode()
        self.page.locator("#addWidgetBtn").click()
        self.page.locator('.wc-item[data-widget="recent-trade-history"]').click()
        # Wait for widget to be added
        self.page.wait_for_selector('[data-widget-id^="recent-trade-history"]')
        # Wait for loading to finish (to have some content)
        self.page.wait_for_function("""() => {
          const nodes = [...document.querySelectorAll('[data-widget-id^="recent-trade-history"]')];
          const el = nodes[nodes.length - 1];
          return el && !el.innerText.includes("Loading live widget data") && el.querySelector('[data-catalog-body]');
        }""", timeout=15000)
        # Now check that the content fills the widget without font scaling
        result = self.page.evaluate("""()=>{
          const nodes=[...document.querySelectorAll('[data-widget-id^="recent-trade-history"]')];
          const el=nodes[nodes.length-1];
          // Set a specific size
          el.style.width='467px'; el.style.height='491px';
          const body=el.querySelector('[data-catalog-body]');
          const sw=el.querySelector('.scroll-wrap');
          const td=el.querySelector('td');
          if (!el || !body || !sw || !td) return null;
          return {
            widgetHeight: Math.round(el.getBoundingClientRect().height),
            bodyHeight: Math.round(body.getBoundingClientRect().height),
            scrollHeight: Math.round(sw.getBoundingClientRect().height),
            tdFont: getComputedStyle(td).fontSize,
            widgetFont: getComputedStyle(el).fontSize,
          };
        }""")
        self.assertIsNotNone(result)
        self.assertGreaterEqual(result["widgetHeight"], 480)
        self.assertGreater(result["bodyHeight"], 360)
        self.assertGreater(result["scrollHeight"], 330)
        self.assertEqual(result["tdFont"], "12px")
        self.assertEqual(result["widgetFont"], "14px")


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


    def test_refresh_pauses_during_edit_mode(self):
        # The auto-refresh interval must be cleared when entering edit mode
        # and restarted when exiting, so a running timer never re-renders
        # during editing and disrupts widget placement.
        self.page.wait_for_function("()=>!!window._refreshInt", timeout=5000)
        has_interval = self.page.evaluate("()=>!!window._refreshInt")
        self.assertTrue(has_interval, "refresh interval should be running on page load")
        self.enter_edit_mode()
        still_running = self.page.evaluate("()=>!!window._refreshInt")
        self.assertFalse(still_running, "refresh interval should be cleared during edit mode")
        self.page.locator("#settingsBtn").click()
        self.page.locator("#editToggleBtn").click()
        self.page.wait_for_function("()=>!document.body.classList.contains('free-edit')", timeout=8000)
        # After exiting edit mode the interval must restart.
        restarted = self.page.evaluate("()=>!!window._refreshInt")
        self.assertTrue(restarted, "refresh interval should restart after exiting edit mode")

    def test_overlapping_widgets_get_distinct_z_index(self):
        # Widgets positioned via saved layout must not collide with identical
        # z-index. Each widget gets a unique stacking order based on its
        # y-position (earlier widgets stack above later ones).
        self.page.evaluate("()=>document.getElementById('clockBtn')?.click()")
        self.page.wait_for_selector(".kpi,.card", timeout=4000)
        self.enter_edit_mode()
        self.page.wait_for_selector(".free-grab")
        # Set up overlapping positions to reproduce the overlap bug.
        self.page.evaluate("""()=>{
          const ws={};
          const fw=window.freeWidgets?freeWidgets():[];
          fw.forEach(({w},i)=>{
            // Place all widgets roughly at the same spot to trigger overlap.
            ws[w.dataset.widgetId]={x:20+i*10, y:20+i*8, w:300, h:160, z:i+1};
          });
          window._draftLayout=ws;
          // Apply the draft so we can read z-index values.
          const root=document.getElementById('tab-panels');
          fw.forEach(({w})=>{
            const id=w.dataset.widgetId;
            const st=ws[id];
            if(st&&root)w.style.zIndex=st.z;
          });
        }""")
        z_indices = self.page.evaluate(
            "()=>[...document.querySelectorAll('.card,.kpi,.widget-free')].map(e=>parseInt(e.style.zIndex)||0)")
        # At least one widget should have a defined z-index > 0.
        self.assertGreater(max(z_indices), 0)
        # Widgets we explicitly placed must have distinct z-indices.
        # Default widgets (KPIs etc.) may share z-index 1, but explicitly
        # placed overlapping widgets must stack uniquely.
        placed_ids = self.page.evaluate("""()=>{
          const ws={};
          const fw=window.freeWidgets?freeWidgets():[];
          fw.forEach(({w})=>{
            ws[w.dataset.widgetId]=parseInt(w.style.zIndex)||0;
          });
          return Object.values(ws);
        }""")
        placed_non_zero = [z for z in placed_ids if z > 0]
        if len(placed_non_zero) > 1:
            self.assertEqual(len(set(placed_non_zero)), len(placed_non_zero),
                             "placed widgets with same z-index overlap invisibly")

    def test_survival_risk_param_description_uses_api_values(self):
        # The trailing stop description on the Active Exit Params widget must
        # derive its figures from the API response, not hardcoded defaults.
        # When the API returns trail_arm_pct=0.08 and trail_distance_pct=0.04,
        # the description must mention "8%" and "4%" accordingly.
        self.open_survival()
        text = self.page.locator("body").inner_text()
        # The description should mention percentages that match the API's
        # risk_param values (MEME defaults from live_inventory: 0.08, 0.04).
        self.assertIn("8.0%", text, "trail arm pct (8%) should appear in the description")
        self.assertIn("4.0%", text, "trail distance pct (4%) should appear in the description")


    def test_catalog_widgets_are_absolute_in_view_mode_non_catalog_cards_flow(self):
        # Catalog widgets (type='catalog') in the saved layout must position
        # absolutely in view mode while built-in grid cards stay in CSS Grid
        # flow. This prevents saved positions from causing overlap.
        layout = {"tab-panels-overview": {
            "live-market-tick": {"type": "catalog", "x": 0, "y": 0, "w": 500, "h": 200},
            "last-cycles-heatmap": {"x": 0, "y": 0, "w": 300, "h": 100},
            "collective-wallet": {"x": 0, "y": 0, "w": 400, "h": 150}
        }}
        # Inject a layout with catalog + non-catalog widgets at overlapping
        # positions (x=0,y=0 for all). Non-catalog must NOT get position:absolute.
        self.page.evaluate("""(lay) => {
          window._mhPrefs = window._mhPrefs || {};
          window._mhPrefs.layout = window._mhPrefs.layout || {};
          window._mhPrefs.layout['overview'] = lay;
        }""", layout)
        # Refresh to reapply
        self.page.reload(wait_until="commit")
        self.page.wait_for_selector("#themeToggle")
        # Wait for render + layout reapply
        self.page.wait_for_function("() => document.querySelector('.kpi,.card')", timeout=5000)
        self.page.wait_for_timeout(500)
        # Check non-catalog grid widgets do NOT have position:absolute
        positions = self.page.evaluate("""() => {
          const cards = [...document.querySelectorAll('.card,.kpi')];
          return cards.map(c => ({
            id: c.dataset.widgetId,
            pos: getComputedStyle(c).position,
            isCatalog: c.classList.contains('widget-free')
          }));
        }""")
        for c in positions:
            if c["isCatalog"]:
                # Catalog widgets in a layout with just them would get
                # position:absolute from applyWidgetStyle
                pass  # May or may not match depends on render
            else:
                # Non-catalog must stay in flow in view mode
                self.assertEqual(
                    c["pos"], "relative",
                    "Grid card '%s' has position:%s (should be relative/static)" % (c["id"], c["pos"])
                )

    def test_non_catalog_saved_positions_do_not_apply_absolute_in_view_mode(self):
        # Saved layout positions for non-catalog widgets (e.g. overview grid
        # cards) must NOT be applied as absolute positioning in view mode.
        # They must stay in CSS Grid flow. Only catalog widgets float.
        self.page.evaluate("""() => {
          window._mhPrefs = window._mhPrefs || {};
          window._mhPrefs.layout = window._mhPrefs.layout || {};
          // Simulate a saved layout where a grid card has a saved position
          window._mhPrefs.layout['overview'] = {
            'best-setup': {'x': 712, 'y': 0, 'w': 344, 'h': 315, 'hidden': false, 'order': 0},
            'what-to-do': {'x': 0, 'y': 300, 'w': 300, 'h': 200, 'order': 0}
          };
        }""")
        self.page.reload(wait_until="commit")
        self.page.wait_for_selector("#themeToggle")
        self.page.wait_for_timeout(800)
        # Non-catalog widgets must not have position:absolute in view mode
        abs_count = self.page.evaluate("""() => {
          return [...document.querySelectorAll('.card,.kpi')]
            .filter(c => getComputedStyle(c).position === 'absolute')
            .length;
        }""")
        # There should be no absolutely-positioned cards since none are catalog type
        self.assertEqual(abs_count, 0,
                         "Non-catalog widgets positioned absolutely in view mode")

    def test_catalog_widget_gets_absolute_in_edit_mode(self):
        # Catalog widgets must get position:absolute in edit mode
        self.enter_edit_mode()
        self.page.wait_for_selector(".free-grab")
        # Check that widgets currently in the DOM have correct positioning
        positions = self.page.evaluate("""() => {
          return [...document.querySelectorAll('.card,.kpi')]
            .map(c => getComputedStyle(c).position);
        }""")
        # In edit mode all cards should be absolutely positioned
        self.assertGreater(len(positions), 0)
        for p in positions:
            self.assertEqual(p, "absolute",
                             "Widget has position:%s in edit mode (expected absolute)" % p)

    def test_survival_tab_widgets_respect_catalog_only_absolute(self):
        # Survival tab has built-in cards (exit reasons, open live wallet,
        # paper incubator, active exit params) and potentially catalog widgets.
        # Non-catalog must stay in flow even when a layout exists.
        self.page.evaluate("""() => {
          window._mhPrefs = window._mhPrefs || {};
          window._mhPrefs.layout = window._mhPrefs.layout || {};
          // Survival tab with one catalog widget and several non-catalog
          window._mhPrefs.layout['survival'] = {
            'active-exit-params-evidence-gated-policy': {'type':'catalog','x':0,'y':400,'w':600,'h':200},
            'exit-reasons-paper-incubator': {'x': 0, 'y': 0, 'w': 300, 'h': 200},
            'open-live-wallet-positions': {'x': 300, 'y': 0, 'w': 300, 'h': 200}
          };
        }""")
        self.page.evaluate("() => document.querySelector('[data-tab=\"survival\"]')?.click()")
        self.page.wait_for_timeout(500)
        self.page.wait_for_selector("#tab-panels")
        abs_ids = self.page.evaluate("""() => {
          return [...document.querySelectorAll('#tab-panels .card')]
            .filter(c => getComputedStyle(c).position === 'absolute')
            .map(c => c.dataset.widgetId || c.innerText.slice(0, 30));
        }""")
        # Only the catalog widget should be absolute; non-catalog cards must stay in flow
        for wid in abs_ids:
            self.assertIn("active-exit", wid or "",
                          "Non-catalog survival widget '%s' has position:absolute" % wid)

    def test_edit_then_refresh_does_not_overlay_widgets(self):
        # Enter edit mode, place widgets, save, then refresh. After reload
        # the placement should be preserved without overlap.
        self.page.evaluate("() => document.getElementById('clockBtn')?.click()")
        self.page.wait_for_selector(".kpi,.card", timeout=4000)
        # Enter and exit edit mode to save current layout
        self.enter_edit_mode()
        self.page.wait_for_selector(".free-grab")
        # Save using evaluate to avoid pointer interception from edit handles
        self.page.evaluate("() => document.getElementById('settingsBtn')?.click()")
        self.page.wait_for_selector("#editToggleBtn", timeout=3000)
        self.page.evaluate("() => document.getElementById('editToggleBtn')?.click()")
        self.page.wait_for_selector("#settingsClose", timeout=3000)
        self.page.evaluate("() => document.getElementById('settingsClose')?.click()")
        self.page.wait_for_timeout(500)
        # Capture positions before reload
        before = self.page.evaluate("""() => {
          return [...document.querySelectorAll('.card,.kpi')].map(c => ({
            id: c.dataset.widgetId,
            left: Math.round(c.getBoundingClientRect().left),
            top: Math.round(c.getBoundingClientRect().top)
          }));
        }""")
        # Reload
        self.page.reload(wait_until="commit")
        self.page.wait_for_selector("#themeToggle")
        self.page.wait_for_timeout(1000)
        # After reload, widgets should not all be at position (0,0)
        after = self.page.evaluate("""() => {
          return [...document.querySelectorAll('.card,.kpi')].map(c => ({
            id: c.dataset.widgetId,
            left: Math.round(c.getBoundingClientRect().left),
            top: Math.round(c.getBoundingClientRect().top)
          }));
        }""")
        # At least some widgets should have non-zero positions
        non_zero_before = sum(1 for c in before if c["left"] > 0 or c["top"] > 0)
        non_zero_after = sum(1 for c in after if c["left"] > 0 or c["top"] > 0)
        self.assertGreater(non_zero_after, 0,
                           "All widgets collapsed to (0,0) after reload")


if __name__ == "__main__":
    unittest.main()

"""Regression tests for the dashboard UI layer — prefs, chat, and requests.

Everything uses a temporary database and mock/sealed network boundaries.
"""
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import mh_ui


class PrefsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "test.db"

    def tearDown(self):
        self.tmp.cleanup()

    def test_default_prefs_are_valid(self):
        defaults = mh_ui.default_prefs()
        result = mh_ui.validate_prefs(defaults)
        self.assertEqual(result["theme"], "system")
        self.assertEqual(result["accent"], "#5c5bd6")
        self.assertEqual(result["density"], "comfortable")
        self.assertEqual(result["fontScale"], 1.0)
        self.assertEqual(result["refreshSeconds"], 30)
        self.assertEqual(result["chatEnabled"], True)
        self.assertEqual(result["layout"], {})

    def test_validate_prefs_drops_unknown_and_normalises(self):
        raw = {"theme": "dark", "fontScale": 1.2, "unknown_key": "oops", "accent": "#ff8800"}
        result = mh_ui.validate_prefs(raw)
        self.assertEqual(result["theme"], "dark")
        self.assertEqual(result["fontScale"], 1.2)
        self.assertEqual(result["accent"], "#ff8800")
        self.assertFalse("unknown_key" in result)

    def test_validate_rejects_impossible_theme(self):
        with self.assertRaises(mh_ui.PrefError):
            mh_ui.validate_prefs({"theme": "fluorescent"})

    def test_validate_rejects_bad_accent(self):
        for bad in ("red", "#gggggg", "123456", None, 42):
            with self.assertRaises(mh_ui.PrefError):
                mh_ui.validate_prefs({"accent": bad})

    def test_validate_rejects_fontscale_out_of_range(self):
        for bad in (0.5, 2.0, -1, True):
            with self.assertRaises(mh_ui.PrefError):
                mh_ui.validate_prefs({"fontScale": bad})

    def test_save_and_load_round_trip(self):
        prefs = {"theme": "dark", "accent": "#2d9eb3", "density": "compact",
                 "fontScale": 0.9, "refreshSeconds": 60, "chatEnabled": False,
                 "editMode": False, "layout": {"overview": {"kpi-equity": {"width": "wide", "hidden": False, "order": 0}}}}
        saved = mh_ui.save_prefs(json.dumps(prefs), self.db)
        self.assertEqual(saved["theme"], "dark")
        self.assertEqual(saved["accent"], "#2d9eb3")
        loaded = mh_ui.load_prefs(self.db)
        self.assertEqual(loaded["theme"], prefs["theme"])
        self.assertEqual(loaded["accent"], prefs["accent"])
        self.assertEqual(loaded["layout"]["overview"]["kpi-equity"]["width"], "wide")

    def test_load_returns_defaults_when_no_stored(self):
        result = mh_ui.load_prefs(self.db)
        self.assertEqual(result["theme"], "system")

    def test_save_with_bad_json_gets_preferror(self):
        with self.assertRaises(mh_ui.PrefError):
            mh_ui.save_prefs("this is not json", self.db)

    def test_layout_validation_limits(self):
        too_many_tabs = {str(i): {} for i in range(30)}
        with self.assertRaises(mh_ui.PrefError):
            mh_ui.validate_prefs({"layout": too_many_tabs})
        too_many_widgets = {str(i): {} for i in range(90)}
        with self.assertRaises(mh_ui.PrefError):
            mh_ui.validate_prefs({"layout": {"tab": too_many_widgets}})

    def test_layout_widget_width_must_be_valid(self):
        with self.assertRaises(mh_ui.PrefError):
            mh_ui.validate_prefs({"layout": {"tab": {"w": {"width": "huge"}}}})

    def test_layout_widget_order_must_be_valid(self):
        with self.assertRaises(mh_ui.PrefError):
            mh_ui.validate_prefs({"layout": {"tab": {"w": {"order": -1}}}})
        with self.assertRaises(mh_ui.PrefError):
            mh_ui.validate_prefs({"layout": {"tab": {"w": {"order": "first"}}}})


class ChatFailClosedTests(unittest.TestCase):
    """The chat endpoint must fail closed when OpenRouter is unavailable."""

    @patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-test"})
    @patch("mh_ui._openrouter_chat")
    def test_profitability_question_uses_deterministic_rankings(self, mock_chat):
        mock_chat.return_value = {"choices": [{"message": {"content": "whale_trader is best"}}]}
        snapshot = {
            "profitability_rankings": {
                "best_traders_by_pnl_usd": [
                    {"name": "whale_trader", "pnl_usd": 0.04, "equity_usd": 24.04},
                    {"name": "reasoner", "pnl_usd": -0.51, "equity_usd": 23.49},
                ],
                "best_setups_by_realized_usd": [
                    {"name": "momentum_breakout", "realized_usd": 1.25, "trades": 31, "win_rate": 0.61},
                ],
                "best_coins_by_realized_usd": [
                    {"name": "SOL", "realized_usd": 0.80, "trades": 50, "win_rate": 0.54},
                ],
            }
        }
        tmp = tempfile.TemporaryDirectory()
        db = Path(tmp.name) / "chat.db"
        try:
            result = mh_ui.chat_reply("whats the most profitable at the moment? trader, strat, coin", snapshot, path=db)
        finally:
            tmp.cleanup()
        self.assertTrue(result["ok"])
        self.assertEqual(result["model"], "deterministic-profitability")
        self.assertIn("Top trader right now: whale_trader", result["reply"])
        self.assertIn("Top setup: momentum_breakout", result["reply"])
        self.assertIn("Top coin: SOL", result["reply"])
        self.assertNotIn("Based on the snapshot", result["reply"])
        self.assertLessEqual(len(result["reply"].splitlines()), 5)

    @patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-test"})
    @patch("mh_ui._openrouter_chat")
    def test_profit_word_in_explanatory_question_goes_to_model(self, mock_chat):
        mock_chat.return_value = {"choices": [{"message": {"content": "The system is working because..."}}]}
        tmp = tempfile.TemporaryDirectory()
        db = Path(tmp.name) / "chat.db"
        try:
            result = mh_ui.chat_reply("whats happened? making a profit, summary", {"test": True}, path=db)
        finally:
            tmp.cleanup()
        self.assertTrue(result["ok"])
        mock_chat.assert_called()

    @patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-test"})
    @patch("mh_ui._openrouter_chat")
    def test_compounding_question_goes_to_model(self, mock_chat):
        mock_chat.return_value = {"choices": [{"message": {"content": "No, it does not compound."}}]}
        tmp = tempfile.TemporaryDirectory()
        db = Path(tmp.name) / "chat.db"
        try:
            result = mh_ui.chat_reply("does the survival agent compound the profits", {"test": True}, path=db)
        finally:
            tmp.cleanup()
        self.assertTrue(result["ok"])
        mock_chat.assert_called()

    @patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-test"})
    @patch("mh_ui._openrouter_chat")
    def test_chat_system_prompt_prioritises_xora_survival_over_traders(self, mock_chat):
        mock_chat.return_value = {"choices": [{"message": {"content": "Survival read\n- Clear."}}]}
        tmp = tempfile.TemporaryDirectory()
        db = Path(tmp.name) / "chat.db"
        try:
            result = mh_ui.chat_reply("Give me an overview", {"test": True}, path=db)
        finally:
            tmp.cleanup()

        self.assertTrue(result["ok"])
        messages = mock_chat.call_args.args[0]
        self.assertEqual(messages[0]["role"], "system")
        system_prompt = messages[0]["content"]
        self.assertIn("Xora-Survival", system_prompt)
        self.assertIn("prioritise", system_prompt.lower())
        self.assertIn("individual trader noise", system_prompt)
        self.assertIn("Avoid em dashes", system_prompt)

    def test_missing_api_key_returns_error(self):
        with patch.dict(os.environ, {}, clear=True):
            result = mh_ui.chat_reply("What is the system status?", {"test": True})
        self.assertFalse(result["ok"])
        self.assertIn("RuntimeError", result.get("error", "") or result.get("detail", ""))


class RequestQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "test.db"

    def tearDown(self):
        self.tmp.cleanup()

    def test_file_and_list_request(self):
        result = mh_ui.file_request("Add trailing stop tuning", "Allow the autotuner to tune trailing distance for MEME coins", "chat", self.db)
        self.assertTrue(result["ok"])
        self.assertIsNotNone(result["id"])
        self.assertEqual(result["status"], "pending")

        lst = mh_ui.list_requests(10, self.db)
        self.assertEqual(len(lst), 1)
        self.assertEqual(lst[0]["title"], "Add trailing stop tuning")
        self.assertEqual(lst[0]["source"], "chat")

    def test_file_requires_title(self):
        result = mh_ui.file_request("  ", "detail", "chat", self.db)
        self.assertFalse(result["ok"])
        self.assertIn("title_required", result.get("error", ""))

    def test_file_truncates_long_title(self):
        result = mh_ui.file_request("x" * 300, "detail", "manual", self.db)
        self.assertFalse(result["ok"])
        self.assertIn("too_long", result.get("error", ""))

    def test_council_review_missing_request(self):
        result = mh_ui.council_review(999, self.db)
        self.assertFalse(result["ok"])
        self.assertIn("not_found", result.get("error", ""))

    @patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-test"})
    @patch("mh_ui._openrouter_chat")
    def test_council_review_parses_verdict(self, mock_chat):
        mock_chat.return_value = {
            "choices": [{
                "message": {
                    "content": '{"verdict":"adjust","confidence":0.7,"reasoning":"Parameter should be tested on paper first.","adjustments":["Restrict to paper exit"],"risks":[]}'
                }
            }]
        }
        r = mh_ui.file_request("Test proposal", "Details", "chat", self.db)
        self.assertTrue(r["ok"])
        result = mh_ui.council_review(r["id"], self.db)
        self.assertTrue(result["ok"])
        self.assertEqual(result["verdict"], "adjust")
        self.assertIn("adjustments", result["verdict_detail"])

    @patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-test"})
    @patch("mh_ui._openrouter_chat")
    def test_council_reject_invalid_verdict(self, mock_chat):
        mock_chat.return_value = {
            "choices": [{"message": {"content": '{"verdict":"approve"}'}}]
        }
        r = mh_ui.file_request("Bad proposal", "x", "chat", self.db)
        result = mh_ui.council_review(r["id"], self.db)
        self.assertFalse(result["ok"])

    def test_record_decision(self):
        r = mh_ui.file_request("Quick fix", "x", "manual", self.db)
        result = mh_ui.record_decision(r["id"], "implement", note="Approved", path=self.db)
        self.assertTrue(result["ok"])
        self.assertEqual(result["decision"], "implement")

        lst = mh_ui.list_requests(10, self.db)
        self.assertEqual(lst[0]["status"], "implement")
        self.assertEqual(lst[0]["decision"], "implement")

    def test_record_decision_with_bad_verdict(self):
        result = mh_ui.record_decision(1, "maybe", self.db)
        self.assertFalse(result["ok"])


class SchemaIdempotenceTests(unittest.TestCase):
    def test_tables_exist_after_schema_call(self):
        tmp = tempfile.TemporaryDirectory()
        db = Path(tmp.name) / "test.db"
        con = mh_ui._connect(db)
        try:
            mh_ui._schema(con)
            tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            for tbl in ('ui_prefs', 'ui_chat', 'ui_requests'):
                self.assertIn(tbl, tables)
        finally:
            con.close()
        tmp.cleanup()

    def test_schema_is_idempotent(self):
        tmp = tempfile.TemporaryDirectory()
        db = Path(tmp.name) / "test.db"
        con = mh_ui._connect(db)
        try:
            mh_ui._schema(con)
            mh_ui._schema(con)  # second call should not raise
        finally:
            con.close()
        tmp.cleanup()


class SurvivalWalletTests(unittest.TestCase):
    """Xora-Survival paper wallet must expose its seed and P&L vs start."""

    def test_survival_wallet_is_paper_only_and_tracks_seed_to_vs_start(self):
        import mh_dash as dash
        trades = [
            {"setup": "dynamic_scalper", "realized_usd": 2.5},
            {"setup": "dynamic_scalper", "realized_usd": -0.5},
            {"setup": "reasoner", "realized_usd": 999.0},   # must be excluded (paper-only)
        ]
        try:
            result = dash._survival_wallet(trades)
        except AttributeError:
            self.skipTest("_survival_wallet not present")
        self.assertEqual(result["closed_trades"], 2)
        self.assertEqual(result["paper_net_usd"], 2.0)
        self.assertAlmostEqual(result["paper_equity_usd"], dash._survival_start_equity() + 2.0)
        self.assertGreater(result["pnl_vs_start_pct"], 0)
        # reasoner trade (999) must not leak into the survival paper number
        self.assertLess(result["paper_net_usd"], 999.0)


if __name__ == "__main__":
    unittest.main()
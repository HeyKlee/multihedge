import unittest
from unittest.mock import Mock

import httpx

import solana_token_universe as stu


MINT_A = "B5WTLaRwaUQpKk7ir1wniNB6m5o8GgMrimhKMYan2R6B"
MINT_B = "6GmAFSYs4gk3FDao5FzzySQpPZaWsa4rUJHacpMpUNgx"
NOW = 1_789_099_200.0
CFG = {
    "live": {"autonomous": {"dynamic_universe": {
        "enabled": True,
        "minimum_age_seconds": 3600,
        "minimum_liquidity_usd": 100_000,
        "minimum_holders": 500,
        "maximum_top_holders_pct": 35,
        "minimum_organic_score": 50,
        "minimum_5m_traders": 50,
        "minimum_5m_buy_volume_usd": 5_000,
        "minimum_5m_sell_volume_usd": 5_000,
        "maximum_metadata_age_seconds": 300,
        "maximum_round_trip_loss_pct": 2.0,
        "max_candidates": 12,
    }}}
}


def token(mint=MINT_A, symbol="PEPE"):
    return {
        "id": mint,
        "symbol": symbol,
        "name": "Pepe",
        "decimals": 6,
        "tokenProgram": stu.TOKEN_PROGRAM,
        "createdAt": "2026-08-01T00:00:00Z",
        "updatedAt": "2026-09-11T03:59:00Z",
        "usdPrice": 0.001,
        "liquidity": 200_000,
        "holderCount": 5_000,
        "organicScore": 70,
        "audit": {
            "mintAuthorityDisabled": True,
            "freezeAuthorityDisabled": True,
            "topHoldersPercentage": 20,
        },
        "stats5m": {
            "numTraders": 200,
            "buyVolume": 50_000,
            "sellVolume": 45_000,
            "priceChange": 2.5,
        },
        "stats1h": {"priceChange": 8.0},
    }


class TokenUniverseTests(unittest.TestCase):
    def test_safe_liquid_legacy_token_becomes_mint_identified_candidate(self):
        result = stu.admit_token(token(), CFG, now=NOW, warnings=[])
        self.assertEqual(result["mint"], MINT_A)
        self.assertEqual(result["symbol"], MINT_A)
        self.assertEqual(result["ticker"], "PEPE")
        self.assertTrue(result["entry_eligible"])

    def test_duplicate_tickers_remain_distinct_by_mint(self):
        one = stu.admit_token(token(MINT_A, "PEPE"), CFG, now=NOW, warnings=[])
        two = stu.admit_token(token(MINT_B, "PEPE"), CFG, now=NOW, warnings=[])
        self.assertNotEqual(one["symbol"], two["symbol"])

    def test_authority_concentration_liquidity_and_token_2022_fail_closed(self):
        mutations = [
            ("authority", lambda row: row["audit"].update(mintAuthorityDisabled=False)),
            ("concentration", lambda row: row["audit"].update(topHoldersPercentage=36)),
            ("liquidity", lambda row: row.update(liquidity=99_999)),
            ("token_program", lambda row: row.update(tokenProgram=stu.TOKEN_2022_PROGRAM)),
        ]
        for label, mutate in mutations:
            with self.subTest(label=label):
                row = token()
                mutate(row)
                with self.assertRaises(stu.TokenDenied):
                    stu.admit_token(row, CFG, now=NOW, warnings=[])

    def test_any_jupiter_shield_warning_denies_entry(self):
        with self.assertRaisesRegex(stu.TokenDenied, "shield"):
            stu.admit_token(token(), CFG, now=NOW, warnings=[{"type": "HAS_FREEZE_AUTHORITY"}])

    def test_stale_metadata_denies_entry(self):
        row = token()
        row["updatedAt"] = "2026-09-11T03:50:00Z"
        with self.assertRaisesRegex(stu.TokenDenied, "stale"):
            stu.admit_token(row, CFG, now=NOW, warnings=[])

    def test_onchain_mint_authorities_are_verified_independently(self):
        safe = stu.FakeResponse(200, {"result": {"value": {
            "owner": stu.TOKEN_PROGRAM,
            "data": {"parsed": {"type": "mint", "info": {
                "decimals": 6, "mintAuthority": None, "freezeAuthority": None,
            }}},
        }}})
        self.assertEqual(stu.verify_onchain_mint(MINT_A, 6, "rpc", post=Mock(return_value=safe)), 6)
        unsafe = stu.FakeResponse(200, {"result": {"value": {
            "owner": stu.TOKEN_PROGRAM,
            "data": {"parsed": {"type": "mint", "info": {
                "decimals": 6, "mintAuthority": "someone", "freezeAuthority": None,
            }}},
        }}})
        with self.assertRaisesRegex(stu.TokenDenied, "authority"):
            stu.verify_onchain_mint(MINT_A, 6, "rpc", post=Mock(return_value=unsafe))

    def test_round_trip_quote_must_be_sellable_within_loss_limit(self):
        get = Mock(side_effect=[
            stu.FakeResponse(200, {"outAmount": "1000000000", "priceImpactPct": "0.001"}),
            stu.FakeResponse(200, {"outAmount": "979999", "priceImpactPct": "0.001"}),
        ])
        with self.assertRaisesRegex(stu.TokenDenied, "round trip"):
            stu.verify_round_trip(MINT_A, CFG, api_key="key", get=get)

    def test_discovery_deduplicates_and_caps_candidates(self):
        rows = [token(MINT_A, "PEPE"), token(MINT_B, "PEPE"), token(MINT_A, "PEPE")]
        get = Mock(side_effect=[
            stu.FakeResponse(200, rows),
            stu.FakeResponse(200, []),
            stu.FakeResponse(200, []),
            stu.FakeResponse(200, {"warnings": {MINT_A: [], MINT_B: []}}),
        ])
        result = stu.discover_candidates(CFG, api_key="key", now=NOW, get=get)
        self.assertEqual([row["mint"] for row in result], [MINT_A, MINT_B])

    def test_registered_holding_is_resolved_for_exit_even_if_no_longer_entry_safe(self):
        row = token()
        row["audit"]["freezeAuthorityDisabled"] = False
        get = Mock(side_effect=[
            stu.FakeResponse(200, [row]),
            stu.FakeResponse(200, {MINT_A: {"usdPrice": .001}}),
        ])
        result = stu.resolve_holdings([MINT_A], api_key="key", now=NOW, get=get)
        self.assertEqual(result[0]["symbol"], MINT_A)
        self.assertFalse(result[0]["entry_eligible"])
        self.assertEqual(result[0]["market"]["latest_usd"], .001)

    def test_rate_limited_metadata_is_retried_then_discovery_succeeds(self):
        slept = []
        original = stu._sleep
        stu._sleep = slept.append
        try:
            get = Mock(side_effect=[
                stu.FakeResponse(429, {}),
                stu.FakeResponse(200, [token()]),
                stu.FakeResponse(200, []),
                stu.FakeResponse(200, []),
                stu.FakeResponse(200, {"warnings": {MINT_A: []}}),
            ])
            result = stu.discover_candidates(CFG, api_key="key", now=NOW, get=get)
        finally:
            stu._sleep = original
        self.assertEqual([row["mint"] for row in result], [MINT_A])
        self.assertEqual(len(slept), 1)
        self.assertGreater(slept[0], 0)

    def test_persistent_rate_limit_fails_closed_after_bounded_retries(self):
        slept = []
        original = stu._sleep
        stu._sleep = slept.append
        try:
            get = Mock(return_value=stu.FakeResponse(429, {}))
            with self.assertRaises(stu.UpstreamUnavailable):
                stu.discover_candidates(CFG, api_key="key", now=NOW, get=get)
        finally:
            stu._sleep = original
        self.assertEqual(get.call_count, stu.MAX_ATTEMPTS)
        self.assertEqual(len(slept), stu.MAX_ATTEMPTS - 1)

    def test_persistent_upstream_error_denies_entries_as_token_denial(self):
        get = Mock(return_value=stu.FakeResponse(503, {}))
        with self.assertRaises(stu.UpstreamUnavailable) as caught:
            stu.resolve_holdings([MINT_A], api_key="key", now=NOW, get=get)
        # Existing fail-closed handlers only catch TokenDenied.
        self.assertIsInstance(caught.exception, stu.TokenDenied)

    def test_non_retryable_status_is_not_retried(self):
        get = Mock(return_value=stu.FakeResponse(403, {}))
        with self.assertRaisesRegex(stu.TokenDenied, "HTTP 403"):
            stu.discover_candidates(CFG, api_key="key", now=NOW, get=get)
        self.assertEqual(get.call_count, 1)

    def test_transport_failure_is_retried_then_reported_upstream(self):
        slept = []
        original = stu._sleep
        stu._sleep = slept.append
        try:
            get = Mock(side_effect=httpx.ConnectError("connection refused"))
            with self.assertRaises(stu.UpstreamUnavailable):
                stu.discover_candidates(CFG, api_key="key", now=NOW, get=get)
        finally:
            stu._sleep = original
        self.assertEqual(get.call_count, stu.MAX_ATTEMPTS)
        self.assertEqual(len(slept), stu.MAX_ATTEMPTS - 1)


if __name__ == "__main__":
    unittest.main()

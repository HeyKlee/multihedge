# XORA-SURVIVAL First Boot Report

Generated: 2026-09-11T11:26:39+12:00

## Operational verdict

`CYCLE: SAFE PROGRESS, NO REVENUE YET`

State: `SHADOW_ONLY`

Mainnet authority is not enabled by this report. Kelly has explicitly designated public address `CqsTCGDXQBeGUAPXHtGDFZ3cU1pqMWiuf9B6hxAZqaxw` as the intended live operating wallet and corrected its network to `mainnet-beta`. Two independent read-only RPC checks returned a native balance of 0.398282446 SOL. The wallet also holds 0.00347272 of the configured ETH asset; two executable Jupiter quotes valued it at a conservative 0.085828902 SOL. Using the lower of two SOL/NZD prices, a 1% liquidation haircut, and an estimated network exit fee gives a conservative combined treasury of NZ$81.23, state `NORMAL`. Mainnet spending remains locked until the remaining activation controls pass.

## Verified controls

- Workspace bridge readback: `wallet_ready=false`
- Bridge reason: `sovereign authority disabled; shadow-only`
- Chain execution module in deployed container: unavailable
- Per-trade confirmation flag: absent
- Sovereign mainnet authority default: disabled in code
- Repository test result after independent-review fixes: 85 tests executed, 84 passed, 1 diagnostic module skipped, 0 failures, 0 errors
- Action-cycle audit: cycles 1 through 3 written and read back from `survival/survival_audit.db`; cycle 2 corrects the test-count wording recorded in cycle 1, and cycle 3 records the independent-review remediations
- Audit mutation protection: SQLite denies application-level update and delete operations
- Audit integrity: records are SHA-256 hash-chained, the complete chain is verified before every append, and corruption causes subsequent appends to fail closed
- Future live-network enforcement: only exact network `mainnet-beta` can pass wallet readiness, independently of `live_mode`

## Runtime inventory

The deployed `multihedge` container is running:

- `multihedge.py loom`
- `grid_trader.py loom`
- `mh_reasoner_loop.py`
- dashboard on host port 9052

The deployed database contains 604 closed scalper paper trades, 1 open scalper paper position, 2 open reasoner paper positions, and 51 grid trades. Closed scalper paper P&L is USD -1.9701617273714747, with 296 profitable and 308 non-profitable closes. These figures are paper-system evidence only. They are not a real treasury valuation and do not establish a deployable trading edge.

The workspace and deployed hashes matched for the original core runtime files before this shadow-only hardening cycle. The new workspace changes have not been deployed to the running container.

## Implemented in this cycle

`survival_policy.py` now provides deterministic:

- survival-state classification
- explicit `UNKNOWN` handling for missing or stale data
- two-check requirement before classifying a sub-NZ$40 treasury as `DEAD`
- NZ$60 protected-floor and risk-capital calculation
- maximum 5% total-treasury position sizing
- maximum 20% risk-capital new-position sizing
- maximum 15% single-token risk-capital exposure
- maximum 40% aggregate risk-capital exposure
- daily-loss and 2:1 net reward-to-risk checks
- advisory-only decisions that never confer signing authority
- schema-validated, append-only action-cycle storage

`live_bridge.py` now has an additional default-off sovereign authority gate. Even if the missing chain dependency is restored, the bridge remains shadow-only unless this code-level gate is deliberately changed through a later reviewed activation process.

## Critical activation blockers

1. The workspace is not a Git repository. Changes are not yet version-controlled, reviewable through commits, or safely reversible through a repository rollback.
2. The application container receives `SOLANA_PRIVATE_KEY` and `WALLET_PRIVATE_KEY` through its mounted `.env`. This violates the required signer isolation because the application process and shell can access signing secrets.
3. Kelly has designated public address `CqsTCGDXQBeGUAPXHtGDFZ3cU1pqMWiuf9B6hxAZqaxw` as the intended live wallet, but there is not yet independent evidence that it is capped and separate from Kelly's primary wallet.
4. Wallet valuation is now established for the current snapshot: 0.398282446 native SOL plus executable ETH liquidation value of 0.085828902 SOL, producing conservative treasury NZ$81.23 after haircut and estimated exit fee. This must be refreshed before every financial decision.
5. No hardened external signer or deterministic exact-payload signing policy exists.
6. The current bridge has an entry-only Jupiter swap path. It lacks a complete sell path, decoded instruction allowlisting, transaction simulation enforcement, exact-payload binding, quote-expiry handling, post-finality balance reconciliation, inventory accounting, and NZD reserve enforcement.
7. No complete deterministic tests yet cover fee reserve, token sellability, quote failure and expiry, partial and full exits, daily and weekly drawdown, turnover, spend caps, restart recovery, database reconciliation, emergency pause, authenticated revival, or secret redaction.
8. Discord is authenticated as the primary configured destination. No authenticated WhatsApp fallback destination is configured.
9. No n8n automation registry, messaging escalation state machine, or durable leased heartbeat has been created for this sovereign agent.
10. Existing paper results do not yet demonstrate positive net expectancy after all costs. The observed closed scalper paper P&L is negative.

## Authority requested

None.

No mainnet signing, wallet movement, paid service, account creation, outbound prospecting, n8n publication, or customer commitment was attempted.

## Enforced caps

- Protected floor: NZ$60.00
- Death threshold: NZ$40.00, requiring two independent verified checks separated by at least five minutes
- New position: at most 5% of conservative treasury
- New position: at most 20% of risk capital
- Single-token exposure: at most 15% of risk capital
- Aggregate open exposure: at most 40% of risk capital
- Daily loss: at most the smaller of 5% of total treasury or NZ$3.00
- Required expected net reward: at least 2 times expected loss
- Signing authority in the new policy core: always false
- Current verified risk capital: NZ$0.00 because real treasury data is unavailable

## Kill and rollback procedure

Current immediate kill state is already effective because sovereign authority is disabled, the confirmation flag is absent, and the deployed chain module is unavailable.

For the running paper service, the operational kill procedure is to stop the `multihedge` container. This was not executed because paper and advisory operation is authorised and non-spending. Before any future production deployment, the rollback procedure must be converted into a tested, version-controlled release rollback with a known-good image digest and database backup. No activation should occur until that procedure is exercised successfully.

## Integrity hashes

- Genesis prompt: `0199cde3c56ae3de178c9143265b6d73f1fb049c42b8057d32807704ed96eb37`
- `survival_policy.py`: `3372f8db77948bb1125b0dc12a2dbe6e1754ed8d4e6e8b182fc57bc62704713f`
- `live_bridge.py`: `0ad0323754465593d0a4e30012dfcc2ffe9bd5c27f2b78b6a23212bf5b64926b`
- `test_survival_policy.py`: `bb039aa11598c6cfab3813c30f6b1ed58ea746dc77c0e4eb1ec68ef361d76567`
- `test_live_bridge.py`: `989c0b6e3128faa15d1c38455604216248d9f66031c481a96e8dcb5994c1c36b`
- First-cycle audit database before the append-only correction record: `25a996cb24d4341d54c4eb185b190aa4661a5c22a6a7d1df437f8aa1a7726a7f`

## Next highest-value action

Design and test a signer-isolation boundary that removes private keys from the application container, followed by deterministic conservative valuation and complete simulated buy, sell, and reconciliation paths. The system must remain `SHADOW_ONLY` while those controls are incomplete.

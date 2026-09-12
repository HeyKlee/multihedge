# MultiHedge Development Guide

Instructions for AI coding assistants and developers working on the MultiHedge codebase.
This root file contains the rules that apply across the repository. Read the relevant source,
tests, configuration, and Git history before changing behavior. The source tree and deployed
runtime are authoritative when older documentation disagrees.

**Never trade safety for speed. Never claim success without exercising the real path.**

## What MultiHedge Is

MultiHedge is Kelly's Solana trading system. It combines paper strategies, a dynamic Solana
token incubator, deterministic risk controls, an autonomous advisory model, a key-isolated
execution path, and a FastAPI dashboard. The Xora-Survival surface separates canonical
on-chain activity from explicitly labelled paper evidence.

Two principles govern every change:

- **Models advise, deterministic code authorizes.** Model output may propose BUY, SELL, or HOLD.
  Deterministic code owns asset identity, schema validation, evidence qualification, position
  sizing, reserves, risk limits, idempotency, signer checks, and transaction execution.
- **Fail closed at every boundary.** Missing, stale, malformed, contradictory, or unverifiable
  data must block new risk. A provider response, process status, or successful command is not
  proof that the intended state exists.

## Non-Negotiable Safety Invariants

- The configured network is Solana `mainnet-beta`. Never switch to devnet, request an airdrop,
  or submit a diagnostic transaction.
- Never print, transmit, commit, log, or place wallet private keys, seed phrases, API keys, or
  tokens in model output. Secret values belong only in protected environment files.
- Never create or remove `confirm_live.flag`, change sovereign mainnet authority, enable live
  promotion, weaken evidence gates, or alter signer isolation without Kelly's explicit approval
  for that exact action.
- Preserve the immutable NZ$60 protected floor and NZ$40 death threshold in
  `survival_policy.py`. Do not reinterpret missing treasury data as zero.
- New risk must remain bounded by the constitution, the USDC reserve policy, the minimum 0.01
  SOL fee reserve, configured exposure limits, evidence gates, and the deterministic execution
  policy.
- USDC is the only trading reserve and settlement asset. BUY routes must be USDC to an approved
  token. SELL routes must return reconciled bot inventory to USDC. SOL is fee reserve, never a
  trading leg.
- Use Solana mint addresses as canonical asset identity. Tickers and names are display metadata
  and may collide or change.
- Only reconciled bot-owned inventory may be sold. Never treat unrelated wallet holdings as
  strategy inventory.
- Preserve idempotent order IDs and append-only, hash-chained audit records. Never rewrite or
  delete a historical survival cycle to make an audit look clean.
- Keep paper and live provenance separate. `mh_trades` contains paper rows, including rows with
  `setup='dynamic_scalper'`. A trade is live only when tied to canonical on-chain fill records.
- Leave the orphan `autohedge` container untouched. Never use Compose's `--remove-orphans` flag
  unless Kelly explicitly approves removing it.
- Do not expose dashboard port 9052 publicly. Tailnet access is intentional. Public DNS or proxy
  exposure requires Kelly's approval.

## Current Xora-Survival Policy

- Autonomous decisions use the model pinned identically in `autonomous_live.py` and
  `config.yaml`. A mismatch must fail closed. Do not add an OpenAI or silent fallback.
- Dynamic-universe entries require both aggregate and mint-scoped evidence. Evidence must be
  fresh, chronologically valid, and meet the configured trade-count, win-rate, and net-PnL
  thresholds.
- MEME defaults are +20% take profit, -10% stop loss, and a 900 second max-hold timer.
- SERIOUS defaults are +5% take profit, -2.5% stop loss, and a six-hour max-hold timer.
- Max hold is a missed-TP execution fallback only. It may close after the timer only if the
  recorded peak already crossed TP but the TP sale did not complete. Age alone never closes a
  position. Stop loss remains unconditional.
- TP, SL, and max hold may be researched from closed-trade history, but autonomous live adoption
  remains evidence-gated. The autotuner may write a shadow candidate. Live application requires
  both the promotion configuration gate and an independently approved source marker.
- Historical policy replay must use timestamped, mint-scoped price paths, chronological holdout
  validation, conservative collision handling, and round-trip costs. Do not infer achievable
  exits from favorable excursion alone or use future samples.
- Trailing stops are currently fixed policy values, not autonomously tuned. Do not describe them
  as tuned unless that entire research, validation, persistence, and application path exists.

## Source Map

The repository is intentionally flat. Find the real call path before editing.

```text
multihedge.py                 Main paper orchestration and loom
paper.py                      Shared paper ledger and legacy paper exits
strategy.py                   Per-coin strategy families and rotation
pricefeed.py                  Mint-keyed pricing and quote fallbacks
live_bridge.py                Gated bridge into deterministic live execution
autonomous_live.py            Advisory model cycle and evidence gate
execution_policy.py           TradeIntent validation and execution constraints
signer_core.py                Signer-side deterministic revalidation
solana_signer_backend.py      Solana/Jupiter signer backend
live_inventory.py             Mint-keyed live inventory and forced exits
dynamic_shadow_scalper.py     Dynamic-universe paper incubator
parameter_autotuner.py        Evidence-gated TP/SL/max-hold research
survival_policy.py            Immutable treasury policy and audit chain
solana_token_universe.py      Runtime token discovery and admission evidence
mh_dash.py                    FastAPI dashboard and Xora-Survival API/UI
deploy/                       Docker image, Compose service, supervisor config
ops/multihedge_autonomous_live.py
                              Autonomous cycle launcher
config.yaml                   Non-secret runtime settings and limits
deploy/data/multihedge.db      Authoritative production SQLite DB on the host
```

Important boundaries:

- `live_bridge.py` is not permission to bypass `execution_policy.py`, `SignerCore`, or evidence
  qualification. Preserve every layer.
- `live_inventory.py` is the shared source of active Xora-Survival exit parameters. Do not add
  independent threshold constants to callers.
- The dashboard must obtain truth from canonical stores. Never infer live status from a setup
  label, symbol, or generic trade row.
- The root `README.md` contains historical Windows paths and older policy values. Update it when
  working on documentation, but do not use stale README values to override current source,
  tests, configuration, or deployed state.

## Change Workflow

1. **Verify the premise.** Reproduce the behavior against the current workspace and inspect Git
   history when intent is unclear. Identify the exact production branch responsible.
2. **Inspect both sides of the boundary.** For execution or risk changes, read the caller,
   validator, signer, persistence path, dashboard projection, and related tests before editing.
3. **Write a behavioral regression test first.** The test must fail for the actual defect and
   assert an invariant, not source text or a current snapshot.
4. **Make the smallest complete change.** Fix sibling paper, live, replay, API, and display paths
   when they implement the same contract. Avoid speculative abstractions and duplicate policy.
5. **Run focused tests.** Exercise the directly changed behavior and its failure cases.
6. **Run the full host suite.** Use:

   `python3 -m unittest discover -q`

7. **Run static checks.** Use:

   `python3 -m compileall -q .`

   `git diff --check`

8. **Deploy when the task changes running behavior.** Build and recreate only the MultiHedge
   service:

   `docker compose -f deploy/docker-compose.yml build`

   `docker compose -f deploy/docker-compose.yml up -d`

   Never add `--remove-orphans`.
9. **Verify the deployed artifact.** Confirm imports or hashes inside the recreated container,
   run focused tests there where practical, check every supervised process, and query the exact
   API endpoints affected.
10. **Verify external state after writes.** Read back database rows, API state, configuration, or
    on-chain signatures before claiming success. A zero exit code alone is insufficient.
11. **Commit only intended files.** Review `git diff`, ensure no secrets or generated artifacts
    are staged, then use a concise commit message describing the verified behavior.

## Testing Rules

- Prefer Python `unittest`, which is the established suite. Run targeted modules during
  development and `python3 -m unittest discover -q` before completion.
- Tests must be deterministic and network-free unless explicitly labelled as integration smoke
  tests. Use temporary SQLite databases and dependency injection or mocks at network boundaries.
- Never point destructive or mutating tests at `deploy/data/multihedge.db` or `/app/multihedge.db`.
- Assert behavior contracts: stale data blocks, malformed model output blocks, unapproved mints
  block, insufficient evidence blocks, duplicate orders do not execute, age alone does not exit,
  and canonical provenance remains separate.
- For threshold behavior, test values clearly below and above the boundary. Add exact-boundary
  tests only when inclusive versus exclusive semantics matter.
- Do not read Python source text from tests. Extract logic into a callable unit and exercise it.
- Preserve temporal integrity in trading research. No random train/test splits, cross-mint price
  samples, future-dated evidence, or post-entry data in entry features.
- Host tests do not prove deployment. Container imports, service status, endpoints, and runtime
  database state are separate acceptance checks.
- Runtime-image tests that expect repository-only deployment files may fail because the image
  copies runtime files rather than the full source tree. Treat this as a harness/layout issue,
  not permission to skip production-path tests.

## Database and Runtime Discipline

- The authoritative running database is `/app/multihedge.db` inside `multihedge`, bind-mounted
  from `deploy/data/multihedge.db`. The root `multihedge.db` is mounted read-only as legacy data
  and must not be mistaken for current production truth.
- SQLite may require write access to the database parent directory for WAL and journal sidecars.
  Verify the actual container user and mount topology rather than weakening key isolation.
- Use transactions for multi-step state changes and atomic persistence for active risk policy.
- Schema migrations must be idempotent and preserve existing records.
- Do not edit production SQLite with ad hoc SQL to manufacture evidence, wins, eligibility, or
  live state.
- Query totals programmatically. If an API count disagrees with enumerated rows, resolve the
  discrepancy before reporting it.
- Do not expose full provider responses, environment files, process environments, or wallet
  material during diagnostics. Report only key names, lengths where safe, status codes, and
  sanitized error summaries.

## Deployment Verification

At minimum after a runtime change:

```text
docker exec multihedge supervisorctl status
GET http://127.0.0.1:9052/api/survival
GET http://127.0.0.1:9052/api/livegate
```

All nine supervised programs should be RUNNING: `loom`, `news`, `reasoner`, `dash`, `grid`,
`whale`, `whale_trader`, `pump_monitor`, and `memecoin_trader`.

For autonomous model changes, additionally verify inside the container:

- Imported model constant equals `config.yaml`.
- A minimal authenticated OpenRouter request returns HTTP 200 and valid content.
- The API key value is never printed.
- A valid HOLD is distinguished from `model_or_validation_failure` and deterministic gate blocks.

For dashboard changes, verify both the JSON payload and rendered Xora-Survival tab. Live history
must contain only canonical on-chain activity, while simulations are visibly marked PAPER.

## Code Quality Rules

- Prefer standard library code and existing dependencies. Add a dependency only when it solves a
  demonstrated need and pin it conservatively.
- Keep policy logic deterministic, explicit, and independently testable.
- Comments and docstrings should explain why a constraint exists, not restate the code.
- Do not swallow exceptions at safety boundaries. Convert them into explicit fail-closed reasons
  without leaking secrets.
- Avoid broad refactors during risk fixes. Separate mechanical cleanup from behavioral changes.
- Do not silently change model pins, trading thresholds, reserve values, evidence requirements,
  wallet authority, or public exposure as part of unrelated work.
- Keep identifiers, mint addresses, order IDs, and transaction signatures exact. Never repair or
  normalize a malformed token silently.
- Use `Decimal` for constitutional NZD amounts and calculations where exact financial boundaries
  matter. Do not use binary floating point to redefine immutable treasury thresholds.

## Completion Standard

A task is complete only when all of the following are true:

- Every requested behavior and named acceptance criterion is implemented.
- Focused regression tests and the complete host suite pass.
- Static checks pass.
- Running behavior is rebuilt and deployed when applicable.
- The deployed container and affected APIs are verified.
- External writes are read back from the exact target.
- Paper versus live provenance is truthful.
- Git contains only intentional changes and no secret or temporary files.
- The final report states real command results, remaining limitations, and any assumption worth
  verifying. Never substitute plausible output for a check that did not run.

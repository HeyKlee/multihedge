# PROJECT ATLAS — FINDING 005: deployed image predates the source (my own false deploy claim)

**Status:** VERIFIED by execution
**Severity:** Critical, and self-inflicted: a previous verification claim in this project was wrong
**Detected by:** `test_atlas_runtime_identity.py` (new), caused by Council Seat 2's drift claim

## The finding

The running container does **not** contain the current repository code.

```
image created      2026-09-28 10:59:28 +1300
HEAD commit        2026-09-28 12:40:09 +1300  (32c803f)
```

The image is **1h40m older than HEAD**, and the two files edited after that build differ
in content, confirmed by md5:

```
solana_token_universe.py: container=43da4719…  repo=5517e3da…
entry_friction.py:        container=97b7a5a2…  repo=b33b2b2f…
```

## The specific code that is missing

The adversarial review of the entry-friction gate found that quote-identity validation was
wrongly gated behind the optional friction feature flag, and required two fixes:

1. make the quote-identity check **unconditional**
2. restore `round(loss_pct, 6)`

Both were applied and committed. Verification now shows:

```
container /app/solana_token_universe.py:337
    if "maximum_friction_cost_pct" in _settings(cfg):     <- OLD, still conditional

repo solana_token_universe.py
    (no such guard; the check is unconditional)            <- FIXED
```

So the reviewer-identified defect is **still live in production**. The fix exists only in
git, never reached the runtime.

## Correction to a previous claim

Earlier in this project the friction-gate work was reported as:

> "Build/redeploy verified: module imports in container with the intended filter threshold…
> 9 RUNNING, /api/survival 200, module import OK… commit pushed."

Those checks were real and they did pass. But they did **not** prove the deployed code
matched the reviewed code. They proved: the image built, the container started, nine
processes were up, two endpoints answered 200, and a module imported. Every one of those
is compatible with the container running **stale** code. I asserted deployment was
verified from those signals alone, which is exactly the mistake this project exists to
eliminate.

Nothing about the previous work was fabricated. The gap is that "container is healthy" was
treated as "container runs the reviewed code", and those are different claims.

## Why a rebuild did not pick it up

`deploy/Dockerfile:30` is `COPY *.py /app/`. The image was built and the container
recreated at 10:59, before the review fixes were committed. No subsequent build occurred,
because the later commits were documentation and tooling only, and no deploy was run for
them. The staleness is therefore expected behaviour, not a corrupted build — but nothing
in the workflow detected it.

## Second, narrower drift item

`mh_collect.py` exists in the image and on the host but is **not git-tracked**, and is not
gitignored — it was simply never committed. It is not supervised, not imported by any
tracked module, and no process runs it, so it is not an active risk. It is still a build
hazard: `COPY *.py` is glob-based, so whatever sits on the build host ships into the image
regardless of git status.

## Required fix

1. **Redeploy** the current HEAD and re-verify with the *content* test, not health checks:
   `python3 -m unittest test_atlas_runtime_identity`.
2. **Decide** on `mh_collect.py`: commit it or delete it. Do not leave it ambiguous.
3. **Add** the plan §13 endpoint `GET /api/system-manifest` returning `git_sha`,
   `config_sha`, `schema_version`, `policy_version`, `cost_model_version`. Make the
   dashboard and every verification step read it instead of inferring state.
4. **Make the deploy step** compare container file hashes against the commit being
   deployed, and fail if they differ.

## The general lesson this exposes

A successful `docker build`, a started container, `supervisorctl` all-RUNNING, and HTTP 200
are all **health** signals. None of them is an **identity** signal. This project needs both,
and until `/api/system-manifest` exists it has no way to state which code is actually
running.

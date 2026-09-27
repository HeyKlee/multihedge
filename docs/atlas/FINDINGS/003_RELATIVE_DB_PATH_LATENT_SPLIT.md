# PROJECT ATLAS — FINDING 003: the 61 sites are not 61 bugs; they are one latent bug

**Status:** VERIFIED by execution (not inference)
**Severity:** High — this is the mechanism that makes FINDING 001 intermittent
**Baseline:** git `cd70d62`, config sha `386bb367c47873d5`
**Council seat 2 (xkiro `qwen3.8-max:free`) + chair independent verification**

## What the council found

Seat 2 reported that all **9 running supervised processes** resolve to `/app/multihedge.db`:

```
loom            paper.DB_PATH default      -> /app/multihedge.db
news            CUR_DIR/multihedge.db      -> /app/multihedge.db
reasoner        CUR_DIR/multihedge.db      -> /app/multihedge.db
dash            $MULTIHEDGE_DB             -> /app/multihedge.db  (also rebinds paper.DB_PATH)
grid            __file__.parent            -> /app/multihedge.db
whale           __file__.parent            -> /app/multihedge.db
whale_trader    __file__.parent            -> /app/multihedge.db  (also rebinds paper.DB_PATH)
pump_monitor    inline                     -> /app/multihedge.db
memecoin_trader __file__.parent            -> /app/multihedge.db
```

That looked like it refuted the runtime severity of FINDING 001. So the chair verified it
directly rather than accepting it.

## Independent verification (executed, not asserted)

Container, `chdir /app`:
```
container paper.DB_PATH    = /app/multihedge.db
container grid.DB_PATH     = /app/multihedge.db
container memecoin.DB_PATH = /app/multihedge.db
```

Host, same modules, same files, `cwd=/home/kelly/multihedge`:
```
HOST paper.DB_PATH    = /home/kelly/multihedge/multihedge.db
HOST grid.DB_PATH     = /home/kelly/multihedge/multihedge.db
HOST memecoin.DB_PATH = /home/kelly/multihedge/multihedge.db
```

**Identical source. Different database.** The resolution is a relative path, so it is
decided entirely by the working directory of whoever imports it.

## Why this is worse than "61 independent sites"

The 61 sites look like a mess. They are actually **one** defect expressed 61 times:
there is no absolute, authoritative database location. The container hides it, because
every process happens to run with `cwd=/app` where the relative path is correct.

The moment any of these runs **off the container** it silently reads the wrong file:

| Context | Resolves to | Contains |
|---|---|---|
| Container (supervised) | `/app/multihedge.db` | 2,216 trades, 49 tables |
| Host (any tool, test, script, analysis) | `multihedge.db` | **0 trades**, 27 tables |

This is exactly the failure already on record: the 8-hour P&L simulation produced no
outcomes. The tools that produced it ran on the host, so they read the empty database
and reported "no data" as though it were a market fact. Nothing errored. That is the
"nothing applies and nobody knows why" class of bug, and it is now mechanically explained.

## The compounding risk

The host and container **share the same `multihedge.db` main file via a FILE-ONLY
bind mount** while keeping separate WAL sidecars. Host-side readers and writers have
already produced malformed-image errors after a container checkpoint (recorded in
`incident-20260925-walsplit/`). So a host-side process that starts "working" on the
production file can corrupt host-side reads.

## Correction to FINDING 001

FINDING 001 is **narrowed, not withdrawn**:

- The split-brain is **not** currently live in the 9 supervised processes.
- It is **latent** for every host-side tool, script, test, report and analysis.
- Severity remains High, because the failure is silent and produces confident wrong
  answers rather than errors.

## Required fix (unchanged, now better justified)

`runtime/paths.py` must return an **absolute** path resolved from an explicit
environment contract, not `Path(__file__).parent`. Tests and tools must be structurally
prevented from defaulting into a relative resolution. This is now a correctness
requirement, not a tidiness requirement.

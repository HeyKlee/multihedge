# PROJECT ATLAS — FINDING 008: torn write, and a durability regression I introduced while fixing it

**Status:** VERIFIED by execution; database restored and journal mode corrected
**Severity:** Critical (data loss), now contained
**Incident:** 2026-09-28, `deploy/data/multihedge.db`

## What the corruption actually was

Not scattered page rot. A **torn write**: the file header declared 10,561 pages while only
10,552 existed on disk. Nine pages, 36,864 bytes, never landed.

```
declared pages : 10,561
actual pages   : 10,552
missing        : 9  (36,864 bytes)
pages readable : 10,551
pages corrupt  : 1  (page 1, the schema root, pointing past EOF)
```

SQLite refuses to open any file whose header disagrees with its own length, which is why
every reader reported `database disk image is malformed` — including `PRAGMA
quick_check`. The data itself was almost entirely intact.

Recovery was a single 4-byte correction to `page_count` at header offset 28, proved by a
byte-level diff that showed exactly one differing byte (offset 31).

## Genuine data loss

| Table | Recovered | Lost |
|---|---:|---:|
| `agent_decision_log` | 29,706 | **18** |
| `mh_scalp_policy_evidence` | 249 | **2** |
| `mh_trades` | 2,218 | 0 |
| everything else | full | 0 |

**20 rows total, irreplaceable.** Verified as genuine loss, not a decode artifact: the
salvage contains **0 fabricated rows** and **0 regressions** when diffed row-by-row against
SQLite's own answers.

## The regression I introduced, and caught

While rebuilding a clean database I created it with `PRAGMA journal_mode=DELETE`. The
original production file was in **WAL** mode (header write/read version = 2,2). So my
recovery silently changed the database's durability and concurrency mode.

It surfaced because the manifest endpoint reports `journal_mode`, and I checked it rather
than assuming. Corrected by restoring WAL mode and re-verifying:

```
journal_mode : wal
quick_check  : ok
mh_trades    : 2218
header       : write_version=2 read_version=2
```

This is recorded rather than quietly fixed because it is exactly the class of error this
project exists to prevent: a recovery that looks successful, passes health checks, and
carries a silent behavioural change. The manifest is what caught it.

## Likely cause (hypothesis, not conclusion)

`deploy/docker-compose.yml` bind-mounts a **single file**, not its directory:

```yaml
- ./data/multihedge.db:/app/multihedge.db
```

The `-wal` and `-shm` sidecars therefore live in the container's **writable layer**, while
any host-side tool touching `deploy/data/multihedge.db` uses a **different** sidecar in
`deploy/data/`. Two independent WALs for one database file. A checkpoint performed while a
host-side reader holds a stale `-shm` index is precisely the failure mode that produces a
short write.

Supporting evidence: this is the **second** occurrence. `incident-20260925-walsplit/`
records the same signature. The compose comment claiming "WAL are recreated fresh in the
writable layer each start" describes the hazard rather than dismissing it.

Roughly 20+ host-side `ops/*.py` and `reports/*.py` scripts reference the production path.
Not all write, but the read side alone is enough to hold a stale shared-memory index.

## Required fix (not yet applied — changes runtime topology)

1. **Bind-mount the directory, not the file**, so `-wal` and `-shm` are shared:
   `./data:/app/data` and point the resolver at `/app/data/multihedge.db`. A single WAL
   then serves every reader and writer.
2. **Single-writer rule**: the container owns all writes. Host-side tools read via
   `file:...?mode=ro` against the same directory mount, never open read-write.
3. **`runtime/paths.py`** (plan Rule A) must return an absolute path from one resolver, so
   no host tool can default into a different database. This is the 133-site defect from
   FINDING 001 and it is what made the split possible.
4. **Checkpoint before backup**, and always copy `-wal`/`-shm` alongside the main file, or
   take the backup with `sqlite3 .backup`.

## Verification tooling added

- `ops/db_corruption_probe.py` — page-level diagnosis without handing the file to SQLite
- `ops/recover_truncated_sqlite.py` — header reconcile plus byte-diff safety proof
- `ops/salvage_lost_tables.py` — b-tree walk, spec-correct record decoder, `ALTER TABLE`
  default padding, rowid-alias handling
- `GET /api/system-manifest` — reports `journal_mode`, `integrity`, `mh_trades_rows`, and
  the effective policy override, so a mode change like this one cannot hide again

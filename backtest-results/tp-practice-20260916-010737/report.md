# TP-Hit Practice Diagnostic

- Valid entry-anchored MEME paths: 157
- Chronological train / untouched holdout: 117 / 40
- Bounded candidates checked: 624
- Policies reaching 50 TP hits: 117

## Top policies by TP hits only

| TP | SL | Trail arm | Trail distance | Max hold | TP hits total | TP rate total | TP hits holdout | TP rate holdout |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 10% | -12% | 8% | 3% | 600s | 62 | 39.5% | 13 | 32.5% |
| 10% | -12% | 8% | 4% | 600s | 62 | 39.5% | 13 | 32.5% |
| 10% | -12% | 8% | 6% | 600s | 62 | 39.5% | 13 | 32.5% |
| 10% | -12% | 12% | 2% | 600s | 62 | 39.5% | 13 | 32.5% |
| 10% | -12% | 12% | 3% | 600s | 62 | 39.5% | 13 | 32.5% |
| 10% | -12% | 12% | 4% | 600s | 62 | 39.5% | 13 | 32.5% |
| 10% | -12% | 12% | 6% | 600s | 62 | 39.5% | 13 | 32.5% |
| 10% | -12% | 8% | 3% | 900s | 62 | 39.5% | 13 | 32.5% |
| 10% | -12% | 8% | 4% | 900s | 62 | 39.5% | 13 | 32.5% |
| 10% | -12% | 8% | 6% | 900s | 62 | 39.5% | 13 | 32.5% |
| 10% | -12% | 12% | 2% | 900s | 62 | 39.5% | 13 | 32.5% |
| 10% | -12% | 12% | 3% | 900s | 62 | 39.5% | 13 | 32.5% |
| 10% | -12% | 12% | 4% | 900s | 62 | 39.5% | 13 | 32.5% |
| 10% | -12% | 12% | 6% | 900s | 62 | 39.5% | 13 | 32.5% |
| 10% | -12% | 8% | 3% | 1800s | 62 | 39.5% | 13 | 32.5% |
| 10% | -12% | 8% | 4% | 1800s | 62 | 39.5% | 13 | 32.5% |
| 10% | -12% | 8% | 6% | 1800s | 62 | 39.5% | 13 | 32.5% |
| 10% | -12% | 12% | 2% | 1800s | 62 | 39.5% | 13 | 32.5% |
| 10% | -12% | 12% | 3% | 1800s | 62 | 39.5% | 13 | 32.5% |
| 10% | -12% | 12% | 4% | 1800s | 62 | 39.5% | 13 | 32.5% |

## Interpretation

This is a reachability exercise, not a deployability result. TP hits are counted only when TP occurs before the replayed stop, max-hold, or trailing exit. They do not prove profit because changed policies can leave paths unresolved after the real trade closed. The strictly comparable, cost-aware holdout remains the previous 148-path study, where even the least-bad defensive policy was negative.

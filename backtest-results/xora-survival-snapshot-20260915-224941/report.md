# XORA-SURVIVAL MEME snapshot common-path replay

- Snapshot SHA-256: `6e097e7cea13f4ceec063e408eda7be54383eeaf5a0478415b074abb269a77e9`
- SQLite integrity: `ok`
- Common paths: 148 from 163 MEME excursions
- Split: chronological 75% train / 25% holdout
- Cost: 80 bps round trip
- Scope: read-only paper research. No policy or production database mutation.

| Policy | Holdout n | Mean net return | Win rate | Compounded return proxy | Max DD proxy |
|---|---:|---:|---:|---:|---:|
| current_meme | 37 | -3.95% | 43.24% | -81.34% | 81.34% |
| defensive_bearish_choppy | 37 | -1.74% | 43.24% | -53.21% | 58.37% |
| tighter_trail_only | 37 | -3.65% | 43.24% | -79.22% | 79.22% |

## Decision
No policy qualifies for adoption. The defensive candidate improves loss severity versus current policy, but it remains negative on the untouched holdout.

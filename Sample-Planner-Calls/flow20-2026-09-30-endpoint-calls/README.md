# flow20-2026-09-30 - endpoint calls

The six API calls that produced test_id `flow20-2026-09-30` on map AWS-VIRTUAL-REVSTAR-4
(45 pallets on the floor, 20 moving per batch, 6 chained batches, 0 failures). Sent on 2026-09-30
between 15:53:49 and 15:54:40 UTC.

## Endpoint
- `POST https://wbenhm5d8a.execute-api.us-east-1.amazonaws.com/prod/agent`
- Auth: AWS IAM (SigV4, service `execute-api`, region `us-east-1`). Unsigned calls get 403.
- Body: `{"mission": {...}}` - the files `request_b1.json` ... `request_b6.json`, sent in that order.

## How the six calls chain
- Every call carries the same `test_id`, `continue: true` and `write: true`.
- Batch 1 names the whole starting floor: 17 carriers plus `parked_carriers: 28` placed in stow.
- Batches 2-6 name ONLY the 20 pallets that move, with their `flow` (`load_stow`, `stow_load`,
  `stow_pick`, `induct_stow`). No positions: the planner reads where every pallet ended in the
  previous batch of the same `test_id` (collection `plannerMissions`).
- `budget_s: 20` = solver time budget per call.

## Files
| File | What it is |
|-|-|
| `request_b1..6.json` | Exact request bodies, in order |
| `response_b1..6.json` | Mission summary stored for each call (`plannerMissions`): status, carriers, legs, audit, timings. Paths are in `pathPlanningEvents` (138 records) |
| `call_api.py` | Replays the six calls with SigV4 (`python call_api.py --test-id my-replay-01`); use a new test_id so the player shows it apart |
| `run_batches.py` | The generator we used (`--scenario scale --floor 45 --load 7 --build 3 --batches 6 --budget 20 --continue --write`) |

## Results per batch
| Batch | Carriers in request | Moving at once (max) | Solver time | Status |
|-|-|-|-|-|
| 1 | 17 (+28 parked) | 8 | 4.31 s | success |
| 2 | 20 | 18 | 8.11 s | success |
| 3 | 20 | 20 | 4.34 s | success |
| 4 | 20 | 19 | 3.37 s | success |
| 5 | 20 | 20 | 4.94 s | success |
| 6 | 20 | 19 | 4.21 s | success |

Postman: the client kit collection (requests 23-27) has the same `continue` pattern with AWS Signature auth.

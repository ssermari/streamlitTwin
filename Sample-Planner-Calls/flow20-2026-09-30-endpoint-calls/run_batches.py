"""Chained batches with one test_id (client ask, Sept 25): the floor state at the end of batch k
is the start of batch k+1, so the client's player can replay the batches in sequence and see
pallets that keep moving, pallets that come to rest and new pallets that appear.

Theme (Steve): induct pallets enter and wait at build; stow pallets go to the load stations and
wait; a few new stow pallets cycle; build pallets leave at pick while a new induct pallet appears;
stow pallets keep cycling load <-> stow.

Scenario `scale` (client target, Sept 25: 40-50 pallets on the floor, about 20 moving): the
same theme in steady state. Every batch the pallets waiting at load come back to stow, the ones
that rested longest go to load and wait, the build pallets leave at pick and new ones enter at
induct; everybody else rests in stow as an obstacle. `--floor` = pallets on the floor.

Usage (SigV4 through the deployed API, profile positronix-dev):
  python tests/e2e/run_batches.py --url https://.../prod/agent --test-id chain-A [--batches 4]
                                  [--product 8] [--build 2] [--write] [--pause 5]
                                  [--scenario theme|scale] [--floor 45] [--load 7] [--pipeline]
                                  [--continue]
Dry run unless --write. Every mission of the run carries the same test_id and batch = 1..N;
mission_id = <test_id>-b<k>. Prints, per batch, who moved, who rested and the audit.

--continue (needs --write): the planner remembers the floor. From batch 2 on the request names
only the pallets that move (`continue: true` + `test_id`); starts and resting pallets come from
the last written batch of the run.
"""
import argparse
import json
import sys
import time

import boto3
import requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest


def call(url: str, body: dict, region: str) -> dict:
    session = boto3.Session()
    creds = session.get_credentials().get_frozen_credentials()
    req = AWSRequest(method="POST", url=url, data=json.dumps(body), headers={"Content-Type": "application/json"})
    SigV4Auth(creds, "execute-api", region).add_auth(req)
    r = requests.post(url, data=req.body, headers=dict(req.headers), timeout=300)
    try:
        out = r.json()
    except ValueError:
        out = {"raw": r.text[:500]}
    out["_http"] = r.status_code
    return out


def plan_batches(product: int, build: int, batches: int):
    """Yields, per batch, a function state -> carriers list. State: {cid: {"cell": [x, y], "at": area}}."""
    prod_ids = [f"s{i + 1}" for i in range(product)]
    build_ids = [f"e{i + 1}" for i in range(build)]
    extra_ids = [f"x{i + 1}" for i in range(max(2, product // 3))]
    next_induct = [f"e{i + 1}" for i in range(build, build * 4)]

    def b1(state):  # everybody fresh: stow pallets go and WAIT at load, build pallets enter and WAIT at build
        return [{"carrier_id": c, "flow": "stow_load"} for c in prod_ids] + \
               [{"carrier_id": c, "flow": "induct_build"} for c in build_ids]

    def b2(state):  # load pallets come back; a few NEW stow pallets cycle; build pallets rest at build
        out = []
        for c, st in state.items():
            if st["at"] == "load":
                out.append({"carrier_id": c, "flow": "load_stow", "start": st["cell"]})
            else:
                out.append({"carrier_id": c, "start": st["cell"], "parked": True})
        out += [{"carrier_id": c, "flow": "stow_load_stow"} for c in extra_ids]
        return out

    def b3(state):  # build pallets leave at pick; NEW induct pallets appear; half of the stow pallets cycle again
        out, movers = [], 0
        for c, st in state.items():
            if st["at"] == "build":
                out.append({"carrier_id": c, "flow": "build_pick", "start": st["cell"]})
            elif st["at"] == "stow" and movers < max(1, len(prod_ids) // 2):
                out.append({"carrier_id": c, "flow": "stow_load", "start": st["cell"]})
                movers += 1
            else:
                out.append({"carrier_id": c, "start": st["cell"], "parked": True})
        fresh = [c for c in next_induct if c not in state][:max(1, len(build_ids))]
        out += [{"carrier_id": c, "flow": "induct_build"} for c in fresh]
        return out

    def b4(state):  # the ones waiting at load come back, the others rest; build pallets wait
        out = []
        for c, st in state.items():
            if st["at"] == "load":
                out.append({"carrier_id": c, "flow": "load_stow", "start": st["cell"]})
            else:
                out.append({"carrier_id": c, "start": st["cell"], "parked": True})
        return out

    cycle = [b1, b2, b3, b4]
    for k in range(batches):
        yield cycle[k % len(cycle)] if k < len(cycle) else cycle[2 + (k % 2)]


def scale_batch(k: int, state: dict, last_moved: dict, floor: int, load: int, build: int, cyclers: int = 0):
    """Carriers of batch k for the `scale` scenario and the mission-level extras."""
    if k == 1:
        # Two sets of product pallets in the front row: set A goes to load now, set B rests and
        # goes next; the rest of the floor is parked in the back rows and never moves.
        carriers = [{"carrier_id": f"s{i + 1}", "flow": "stow_load"} for i in range(load)] + \
                   [{"carrier_id": f"s{load + i + 1}", "parked": True} for i in range(load)] + \
                   [{"carrier_id": f"x{i + 1}", "flow": "stow_load_stow"} for i in range(cyclers)] +                    [{"carrier_id": f"e{i + 1}", "flow": "induct_build"} for i in range(build)]
        return carriers, {"parked_carriers": max(0, floor - 2 * load - cyclers - build)}
    out = []
    for c, st in state.items():                      # leaving pallets first: they free the stations
        if st["at"] == "load":
            out.append({"carrier_id": c, "flow": "load_stow", "start": st["cell"]})
    for c, st in state.items():
        if st["at"] == "build":
            out.append({"carrier_id": c, "flow": "build_pick", "start": st["cell"]})
    for c, st in state.items():                      # a few pallets do the whole cycle in every batch
        if c.startswith("x"):
            out.append({"carrier_id": c, "flow": "stow_load_stow", "start": st["cell"]})
    resting = sorted((c for c, st in state.items() if st["at"] == "stow" and not c.startswith("x")),
                     key=lambda c: last_moved.get(c, 0))
    # the front-row pallets that rested longest go to load and wait (a pallet in a back row
    # cannot leave until the ones in front of it move)
    go = set([c for c in resting if state[c].get("front_row")][:load])
    for c in resting:
        if c in go:
            out.append({"carrier_id": c, "flow": "stow_load", "start": state[c]["cell"]})
        else:
            out.append({"carrier_id": c, "start": state[c]["cell"], "parked": True})
    first = 1 + (k - 1) * build
    out += [{"carrier_id": f"e{first + i}", "flow": "induct_build"} for i in range(build)]
    return out, {}


def next_state(res: dict) -> dict:
    """Floor after a batch, from its summary: where everybody is now; pallets that left at pick
    are gone; a pallet the planner could not move is still where it started."""
    area_of = lambda code: {"S": "stow", "L": "load", "B": "build", "I": "induct", "P": "pick"}.get(str(code)[:1], "stow")
    state = {}
    for c in res["carriers"]:
        if c["status"] == "failed":
            state[c["carrier_id"]] = {"cell": c["start_cell"], "at": area_of(c.get("start_area", "S")), "front_row": False}
            continue
        if c["status"] == "parked":
            last_area = area_of(c.get("start_area", "S"))
        else:
            last_area = c["legs"][-1]["area"] if c["legs"] else area_of(c.get("start_area", "S"))
        if last_area in ("pick", "queue"):
            continue   # left the floor
        state[c["carrier_id"]] = {"cell": c["end_cell"], "at": last_area, "front_row": c.get("front_row")}
    return state


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--test-id", required=True)
    ap.add_argument("--batches", type=int, default=4)
    ap.add_argument("--product", type=int, default=8)
    ap.add_argument("--build", type=int, default=2)
    ap.add_argument("--write", action="store_true", help="persist every batch (default: dry run)")
    ap.add_argument("--pause", type=float, default=3.0, help="seconds between batches (distinct timestamps for the player)")
    ap.add_argument("--budget", type=float, default=None)
    ap.add_argument("--scenario", choices=("theme", "scale"), default="theme")
    ap.add_argument("--floor", type=int, default=45, help="scale: pallets on the floor")
    ap.add_argument("--load", type=int, default=7, help="scale: pallets that wait at load per batch (7 = 20 moving per batch)")
    ap.add_argument("--cyclers", type=int, default=0, help="scale: pallets that go to load and come back in every batch")
    ap.add_argument("--pipeline", action="store_true", help="overlap waves inside a batch")
    ap.add_argument("--continue", dest="cont", action="store_true",
                    help="the planner remembers the floor: from batch 2 on, send only the pallets that move")
    a = ap.parse_args()
    if a.cont and not a.write:
        ap.error("--continue reads the floor from the last WRITTEN batch: add --write")

    state = {}   # cid -> {"cell": [x, y], "at": area}
    last_moved = {}   # cid -> last batch in which it moved (scale: who rested longest goes next)
    builders = list(plan_batches(a.product, a.build, a.batches))
    for k in range(1, a.batches + 1):
        extras = {}
        if a.scenario == "scale":
            carriers, extras = scale_batch(k, state, last_moved, a.floor, a.load, a.build, a.cyclers)
        else:
            carriers = builders[k - 1](state)
        mission = {"carriers": carriers, "test_id": a.test_id, "batch_no": k,
                   "mission_id": f"{a.test_id}-b{k}", "write": bool(a.write), **extras}
        if a.cont:
            # only who moves, without positions: the planner knows where everybody is
            movers = carriers if k == 1 else [{x: v for x, v in c.items() if x != "start"} for c in carriers if not c.get("parked")]
            mission = {"carriers": movers, "test_id": a.test_id, "continue": True, "write": True, **extras}
        if a.pipeline:
            mission["pipeline"] = True
        if a.budget:
            mission["budget_s"] = a.budget
        t0 = time.time()
        out = call(a.url, {"mission": mission}, a.region)
        dt = time.time() - t0
        if out.get("_http") != 200:
            print(f"batch {k}: HTTP {out.get('_http')} {json.dumps(out)[:400]}")
            sys.exit(1)
        res = out["response"]
        for leg in res.get("legs", []):
            for f in leg.get("failed_detail", []):
                print(f"   failed in wave {leg['wave']} leg {leg['leg']}: {f.get('carrier_id')} {f.get('code')} {str(f.get('reason'))[:120]}")
        moved = [c for c in res["carriers"] if c["status"] == "planned" and c["start_cell"] != c["end_cell"]]
        rested = [c for c in res["carriers"] if c["status"] == "parked" or c["start_cell"] == c["end_cell"]]
        for c in moved:
            last_moved[c["carrier_id"]] = k
        print(f"batch {k}: {dt:.1f}s status={res['status']} moving={len(moved)} resting={len(rested)} "
              f"failed={res['failed_carriers']} at_once={res['audit']['max_moving_at_once']} "
              f"min_gap={res['audit']['min_gap_inches']} cold_start={res.get('cold_start')} solver={res['solver_time_s']}s")
        state = next_state(res)
        where = {}
        for v in state.values():
            where[v["at"]] = where.get(v["at"], 0) + 1
        print(f"   on floor: {len(state)} {where}" if len(state) > 16 else f"   on floor: { {k2: v['at'] for k2, v in state.items()} }")
        if a.cont:
            print(f"   sent {len(mission['carriers'])} carriers; planner: continued_from={res.get('continued_from')} floor={res.get('floor')}")
        time.sleep(a.pause)


if __name__ == "__main__":
    main()

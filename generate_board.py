#!/usr/bin/env python
"""Build board.json for the Draft Caddie PWA.

Runs the PAR engine (par_model.py) once — empirical replacement levels + a Monte
Carlo dynasty simulation — and writes a single static JSON the browser app loads.
The expensive part (5-year foundations + 5000-trajectory sim per player) is baked
in here; the app only does the light per-pick roster-fit math itself.

Run locally:  python generate_board.py
In CI:        same — needs numpy (pip install in the workflow).
"""
import json
from pathlib import Path

import value_model as vm

HERE = Path(__file__).resolve().parent
# Keep all caches (player map + PAR foundations) local to this repo / CI workspace.
vm.CACHE = HERE / ".cache"

import par_model as pm  # noqa: E402  (after vm.CACHE override)

CONFIG = HERE / "fantasy-football-config.json"
OUT = HERE / "board.json"
FIT_WEIGHT = 0.5  # live score = dyn_par + FIT_WEIGHT * marginal lineup fit (roster need)


def main():
    cfg = json.loads(CONFIG.read_text())
    rows, F = pm.build_board(cfg)  # priced + simulated, sorted by dyn_par, ranked

    sl = cfg.get("_sleeper", {})
    board = {
        "meta": {
            "league_name": cfg["league"].get("name"),
            "season": cfg.get("season_year"),
            "teams": cfg["league"]["teams"],
            "rounds": sl.get("draft_rounds"),
            "draft_id": sl.get("draft_id"),
            "our_uid": sl.get("my_user_id"),
            "fit_weight": FIT_WEIGHT,
            "starters": vm.STARTERS,
            "flex": vm.FLEX,
            "flex_pos": list(vm.FLEX_POS),
            "replacement": F["replacement"],
            "tier_break_frac": 0.10,
            "model": "PAR (points above replacement) + Monte Carlo dynasty sim",
            "generated_at": F["built_at"],
        },
        "players": [
            {
                "player_id": r["player_id"],
                "name": r["name"],
                "pos": r["pos"],
                "team": r["team"],
                "age": r["age"],
                "pts": r["pts"],
                "par_now": r["par_now"],
                "par_now_wk": r["par_now_wk"],
                "dyn_par": r["dyn_par"],
                "par_floor": r["par_floor"],
                "par_ceil": r["par_ceil"],
                "par_med": r["par_med"],
                "adp": r["adp"] if isinstance(r["adp"], (int, float)) and r["adp"] < 900 else None,
                "rank": r["rank"],
            }
            for r in rows
        ],
    }

    OUT.write_text(json.dumps(board, separators=(",", ":")), encoding="utf-8")
    print(f"Wrote {OUT}  ({len(board['players'])} players priced)")
    print(f"{'#':>3} {'Player':22} {'Pos':3} {'Age':>4} {'PAR/wk':>7} {'DynPAR':>7} {'Floor':>6} {'Ceil':>6}")
    for r in rows[:12]:
        print(f"{r['rank']:>3} {r['name'][:22]:22} {r['pos']:3} {r['age']:>4} "
              f"{r['par_now_wk']:>7.1f} {r['dyn_par']:>7.0f} {r['par_floor']:>6.0f} {r['par_ceil']:>6.0f}")


if __name__ == "__main__":
    main()

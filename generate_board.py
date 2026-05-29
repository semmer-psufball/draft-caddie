#!/usr/bin/env python
"""Build board.json for the Draft Caddie PWA.

Reuses the league-rules pricing engine (value_model.py) verbatim, then writes a
single static JSON file the browser app loads. The app does the light per-pick
math (marginal lineup value) itself; everything expensive (projections ->
league points -> age curve -> VOR) is baked in here.

Run locally:  python generate_board.py
In CI:        same — uses stdlib only, no pip installs.
"""
import json
from pathlib import Path

import value_model as vm

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "fantasy-football-config.json"
OUT = HERE / "board.json"

# Keep the Sleeper player-map cache local to this repo (CI-friendly).
vm.CACHE = HERE / ".cache"


def main():
    cfg = json.loads(CONFIG.read_text())
    teams = cfg["league"]["teams"]

    # --- identical pipeline to live_draft.prepare_rows ---
    rows = vm.build_players(cfg)
    base_dyn = vm.replacement(rows, "dyn", teams)
    base_ann = vm.replacement(rows, "pts", teams)
    for r in rows:
        r["vor"] = round(r["dyn"] - base_dyn[r["pos"]], 1)
        r["vor_now"] = round(r["pts"] - base_ann[r["pos"]], 1)
    rows.sort(key=lambda r: -r["vor"])
    for i, r in enumerate(rows, 1):
        r["rank"] = i

    sl = cfg.get("_sleeper", {})
    board = {
        "meta": {
            "league_name": cfg["league"].get("name"),
            "season": cfg.get("season_year"),
            "teams": teams,
            "rounds": sl.get("draft_rounds"),
            "draft_id": sl.get("draft_id"),
            "our_uid": sl.get("my_user_id"),
            "vor_weight": 0.6,
            "starters": vm.STARTERS,
            "flex": vm.FLEX,
            "flex_pos": list(vm.FLEX_POS),
            "tier_break_frac": 0.08,
            "generated_at": str(vm.date.today()),
        },
        "players": [
            {
                "player_id": r["player_id"],
                "name": r["name"],
                "pos": r["pos"],
                "team": r["team"],
                "age": r["age"],
                "pts": r["pts"],
                "dyn": r["dyn"],
                "vor": r["vor"],
                "vor_now": r["vor_now"],
                "adp": r["adp"] if isinstance(r["adp"], (int, float)) and r["adp"] < 900 else None,
                "rank": r["rank"],
            }
            for r in rows
        ],
    }

    OUT.write_text(json.dumps(board, separators=(",", ":")), encoding="utf-8")
    print(f"Wrote {OUT}  ({len(board['players'])} players priced)")
    top = board["players"][:10]
    for r in top:
        print(f"  {r['rank']:>2} {r['name'][:22]:22} {r['pos']:3} {r['age']:>4}y  "
              f"pts={r['pts']:>6}  vor={r['vor']:>6}")


if __name__ == "__main__":
    main()

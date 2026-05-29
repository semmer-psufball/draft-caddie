#!/usr/bin/env python
"""Dynasty valuation engine for the EA 2021 DYNASTY league.

Prices every player under THIS league's rules and produces:
  - a static dynasty board (league points -> age curve -> VOR), and
  - a live draft score (marginal value added to OUR optimal starting lineup,
    which automatically penalizes stacking a position).

Data is pulled live from Sleeper (projections + player ages), so it self-updates.

Usage:
  python value_model.py                      # write the full board to research\\
  python value_model.py --my "Brock Bowers,Trey McBride"   # live: best picks for us now
  python value_model.py --drafted "<names>"  # remove players already taken by others

Layers:
  1. League points  = apply config scoring_settings to projected stats
                       (TE premium falls out via the per-rec bonus stat).
  2. Age premium     = multi-year discounted sum using a per-position aging curve.
  3. VOR             = points minus the leaguewide replacement baseline (scarcity).
  4. Marginal value  = increase in OUR optimal starting lineup (the stacking penalty).
"""
import argparse
import json
import sys
import time
import urllib.request
import urllib.parse
from datetime import date
from pathlib import Path

PROJ_API = "https://api.sleeper.com/projections/nfl"
PLAYERS_API = "https://api.sleeper.app/v1/players/nfl"
CONFIG = Path.home() / ".claude" / "fantasy-football-config.json"
CACHE = Path(__file__).resolve().parent.parent.parent.parent / ".cache"
SEASON_START = date(2026, 9, 1)
HORIZON = 5          # dynasty look-ahead seasons
DISCOUNT = 0.9       # per-year discount for future production

# Fraction-of-peak production by age, per position (dynasty aging curves).
CURVES = {
    "RB": {21: .95, 22: 1.0, 23: 1.0, 24: 1.0, 25: .97, 26: .90, 27: .80,
           28: .66, 29: .50, 30: .36, 31: .22, 32: .12, 33: .06, 34: .03, 35: .015},
    "WR": {20: .80, 21: .86, 22: .92, 23: .97, 24: 1.0, 25: 1.0, 26: 1.0,
           27: 1.0, 28: .96, 29: .90, 30: .80, 31: .68, 32: .54, 33: .40,
           34: .28, 35: .18, 36: .11, 37: .06, 38: .03},
    "TE": {21: .62, 22: .72, 23: .82, 24: .90, 25: .97, 26: 1.0, 27: 1.0,
           28: 1.0, 29: .96, 30: .90, 31: .80, 32: .68, 33: .55, 34: .42,
           35: .30, 36: .20, 37: .12, 38: .06, 39: .03},
    "QB": {22: .85, 23: .90, 24: .95, 25: 1.0, 26: 1.0, 27: 1.0, 28: 1.0, 29: 1.0,
           30: 1.0, 31: 1.0, 32: .98, 33: .96, 34: .93, 35: .89, 36: .83, 37: .75,
           38: .64, 39: .50, 40: .35, 41: .22, 42: .12, 43: .06},
}
STARTERS = {"QB": 1, "RB": 2, "WR": 2, "TE": 1}
FLEX = 2
FLEX_POS = ("RB", "WR", "TE")


def fetch_json(url):
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    })
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def load_players():
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / "sleeper_players_nfl.json"
    if not f.exists() or (time.time() - f.stat().st_mtime) > 7 * 86400:
        f.write_text(json.dumps(fetch_json(PLAYERS_API)))
    return json.loads(f.read_text())


def age_of(p):
    bd = p.get("birth_date")
    if bd:
        try:
            y, m, d = map(int, bd.split("-"))
            return round((SEASON_START - date(y, m, d)).days / 365.25, 1)
        except Exception:
            pass
    if p.get("age"):
        return float(p["age"])
    if p.get("years_exp") is not None:      # rough fallback: ~22 at rookie year
        return 22.0 + float(p["years_exp"])
    return 26.0


def curve(pos, age):
    tbl = CURVES.get(pos)
    if not tbl:
        return 1.0
    a = int(round(age))
    lo, hi = min(tbl), max(tbl)
    if a <= lo:
        return tbl[lo]
    if a >= hi:
        return tbl[hi]
    return tbl.get(a) or tbl[min(tbl, key=lambda k: abs(k - a))]


def dynasty_points(pos, age, annual_pts):
    """Discounted multi-year production, scaled by the aging curve."""
    base = max(curve(pos, age), 0.45)
    total = 0.0
    for t in range(HORIZON + 1):
        ratio = min(curve(pos, age + t) / base, 1.20)
        total += annual_pts * ratio * (DISCOUNT ** t)
    return round(total, 1)


def league_points(stats, scoring, pos):
    """Apply league scoring to projected stats. The per-rec TE bonus is a stat
    field (bonus_rec_te) for TEs, so the generic dot-product handles the premium."""
    return round(sum(v * stats.get(k, 0) for k, v in scoring.items() if k in stats), 1)


def build_players(cfg):
    scoring = cfg["league"]["scoring_settings_raw"]
    pmeta = load_players()
    rows = []
    for pos in ("QB", "RB", "WR", "TE"):
        url = f"{PROJ_API}/{cfg['season_year']}?" + urllib.parse.urlencode(
            {"season_type": "regular", "position[]": pos, "order_by": "pts_half_ppr"}, doseq=True
        )
        for rec in fetch_json(url):
            stats = rec.get("stats") or {}
            pts = league_points(stats, scoring, pos)
            if pts <= 0:
                continue
            pid = rec.get("player_id")
            meta = pmeta.get(pid, {})
            age = age_of(meta or rec.get("player", {}))
            pl = rec.get("player", {})
            name = f"{pl.get('first_name','')} {pl.get('last_name','')}".strip() or pid
            rows.append({
                "player_id": pid,
                "name": name, "pos": pos, "team": pl.get("team"), "age": age,
                "pts": pts, "dyn": dynasty_points(pos, age, pts),
                "adp": stats.get("adp_dynasty_half_ppr"),
            })
    return rows


def replacement(rows, key, teams):
    """Replacement baseline per position = best non-started player on key,
    after filling every team's optimal QB/RB/WR/TE + FLEX lineup."""
    by_pos = {p: sorted([r for r in rows if r["pos"] == p], key=lambda r: -r[key]) for p in STARTERS}
    started = {p: set() for p in STARTERS}
    for p, n in STARTERS.items():
        for r in by_pos[p][: n * teams]:
            started[p].add(id(r))
    flex_pool = sorted(
        [r for p in FLEX_POS for r in by_pos[p] if id(r) not in started[p]],
        key=lambda r: -r[key],
    )
    for r in flex_pool[: FLEX * teams]:
        started[r["pos"]].add(id(r))
    base = {}
    for p in STARTERS:
        rest = [r for r in by_pos[p] if id(r) not in started[p]]
        base[p] = rest[0][key] if rest else by_pos[p][-1][key]
    return base


def lineup_points(roster):
    """Best legal starting lineup points from a roster (uses annual pts)."""
    by_pos = {p: sorted([r for r in roster if r["pos"] == p], key=lambda r: -r["pts"]) for p in STARTERS}
    used, total = set(), 0.0
    for p, n in STARTERS.items():
        for r in by_pos[p][:n]:
            used.add(id(r)); total += r["pts"]
    flex = sorted([r for r in roster if r["pos"] in FLEX_POS and id(r) not in used], key=lambda r: -r["pts"])
    for r in flex[:FLEX]:
        total += r["pts"]
    return total


def explain(cfg):
    """Print every knob the model uses, in a human-readable form."""
    L = cfg["league"]
    print("\n== MODEL RULES ==")
    print(f"League: {L.get('name')}  |  {L['teams']} teams  |  {L['scoring']}")
    print(f"Starters: {STARTERS} + FLEX {FLEX} of {FLEX_POS}")
    print("\n-- SCORING (applied to live Sleeper projections) --")
    for k, v in sorted(L["scoring_settings_raw"].items(), key=lambda kv: -abs(kv[1])):
        if v == 0:
            continue
        tag = ""
        if k == "bonus_rec_te":
            tag = "  <-- TE PREMIUM (TEs effectively full PPR)"
        elif k == "rec":
            tag = "  (Half-PPR base)"
        print(f"  {k:18} {v:>7}{tag}")
    print("\n-- AGING CURVES (fraction of peak by age) --")
    for pos in ("QB", "RB", "WR", "TE"):
        ages = [22, 24, 26, 28, 30, 32, 34, 36, 38]
        print(f"  {pos}: " + "  ".join(f"{a}y={curve(pos, a):.2f}" for a in ages))
    print(f"\n-- DYNASTY WINDOW --\n  HORIZON  = {HORIZON} seasons (multi-year sum)"
          f"\n  DISCOUNT = {DISCOUNT}/yr (future seasons weighted less)")
    print("\n-- DRAFT-NIGHT BLEND --")
    print("  DraftScore = marginal_lineup_value + VOR_WEIGHT * VOR")
    print("  default VOR_WEIGHT = 0.6  (override with --vor-weight on either script)")
    print("    higher -> value long-term/trade assets/youth more")
    print("    lower  -> value this-year starting lineup more")
    print(f"\nTo tune curves or constants, edit:\n  {Path(__file__).resolve()}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--my", default="", help="comma-separated players already on OUR roster")
    ap.add_argument("--drafted", default="", help="comma-separated players taken by others")
    ap.add_argument("--top", type=int, default=40)
    ap.add_argument("--vor-weight", type=float, default=0.6,
                    help="DraftScore = marg + W*VOR (default 0.6)")
    ap.add_argument("--explain", action="store_true",
                    help="print the model's scoring rules, aging curves, and knobs, then exit")
    args = ap.parse_args()

    cfg = json.loads(CONFIG.read_text())
    if args.explain:
        explain(cfg); return
    teams = cfg["league"]["teams"]
    rows = build_players(cfg)

    base_dyn = replacement(rows, "dyn", teams)
    base_ann = replacement(rows, "pts", teams)
    for r in rows:
        r["vor"] = round(r["dyn"] - base_dyn[r["pos"]], 1)
        r["vor_now"] = round(r["pts"] - base_ann[r["pos"]], 1)
    rows.sort(key=lambda r: -r["vor"])
    for i, r in enumerate(rows, 1):
        r["rank"] = i

    mine = [n.strip().lower() for n in args.my.split(",") if n.strip()]
    gone = set(n.strip().lower() for n in args.drafted.split(",") if n.strip())
    my_roster = [r for r in rows if r["name"].lower() in mine]

    if mine or gone:
        # LIVE MODE: rank available players by marginal value added to OUR lineup,
        # blended with dynasty asset value. Stacking is penalized automatically.
        taken = gone | set(mine)
        avail = [r for r in rows if r["name"].lower() not in taken]
        cur = lineup_points(my_roster)
        for r in avail:
            r["marg"] = round(lineup_points(my_roster + [r]) - cur, 1)
            # DraftScore: marginal starting value + dynasty asset value (youth/scarcity)
            r["score"] = round(r["marg"] + args.vor_weight * r["vor"], 1)
        avail.sort(key=lambda r: -r["score"])
        print(f"OUR ROSTER ({len(my_roster)}): " + ", ".join(f"{r['name']}({r['pos']})" for r in my_roster) or "OUR ROSTER: (empty)")
        print(f"\nTOP {args.top} VALUE PICKS FOR US NOW  [DraftScore = marginal lineup + 0.6*VOR]\n")
        print(f"{'#':>3} {'Player':22} {'Pos':3} {'Age':>4} {'Pts':>6} {'Marg':>6} {'VOR':>6} {'Score':>6}")
        for i, r in enumerate(avail[: args.top], 1):
            print(f"{i:>3} {r['name'][:22]:22} {r['pos']:3} {r['age']:>4} {r['pts']:>6} {r['marg']:>6} {r['vor']:>6} {r['score']:>6}")
        return

    # BOARD MODE: write the full dynasty price board.
    out = CACHE.parent / "research" / f"{date.today()} Value Board.md"
    lines = [
        f"# Dynasty Value Board — {cfg['league'].get('name')} ({date.today()})",
        f"*{teams}-team, {cfg['league']['scoring']}, 1QB. Model: league pts → {HORIZON}-yr age-curve → VOR. "
        f"Source: Sleeper projections {cfg['season_year']}. ADP = Sleeper dynasty half-PPR (market reference).*",
        "",
        "| # | Player | Pos | Age | ProjPts | DynPts | VOR | VOR(now) | Mkt ADP |",
        "|--:|--------|-----|----:|--------:|-------:|----:|---------:|--------:|",
    ]
    for r in rows[: max(args.top, 80)]:
        adp = f"{r['adp']:.1f}" if isinstance(r["adp"], (int, float)) and r["adp"] < 900 else "—"
        lines.append(f"| {r['rank']} | {r['name']} | {r['pos']} | {r['age']} | {r['pts']} | {r['dyn']} | {r['vor']} | {r['vor_now']} | {adp} |")
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {out}  ({len(rows)} players priced)\n")
    print(f"{'#':>3} {'Player':22} {'Pos':3} {'Age':>4} {'Pts':>6} {'Dyn':>7} {'VOR':>6}  MktADP")
    for r in rows[:20]:
        adp = f"{r['adp']:.1f}" if isinstance(r["adp"], (int, float)) and r["adp"] < 900 else "—"
        print(f"{r['rank']:>3} {r['name'][:22]:22} {r['pos']:3} {r['age']:>4} {r['pts']:>6} {r['dyn']:>7} {r['vor']:>6}  {adp}")


if __name__ == "__main__":
    main()

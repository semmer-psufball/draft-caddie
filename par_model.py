#!/usr/bin/env python
"""PAR — empirical, simulation-based dynasty valuation for the EA 2021 DYNASTY league.

A WAR-style model. Instead of pricing one projection year against a hand-tuned curve,
this engine:

  1. EMPIRICAL FOUNDATIONS (5 seasons of ACTUAL results, 2021-2025, our scoring):
       - positional scoring curves (what WR1..WRn actually scored, averaged),
       - replacement level = best player NOT startable league-wide after filling all
         12 optimal lineups (1QB/2RB/2WR/1TE + 2 FLEX),
       - blended age curves (empirical fraction-of-peak-by-age + researched curves),
       - year-over-year production volatility (boom/bust + injury), and
       - age/position drop-out hazard (career-end / cliff risk).

  2. PAR NOW = projected points - replacement, expressed per week (the WAR/game stat).

  3. DYNASTY PAR via MONTE CARLO: simulate thousands of future trajectories per player
     (age decline + volatility + drop-out), sum each trajectory's PAR (floored at 0) with
     a tiny win-now discount, and report the EXPECTED value plus floor (p10) / ceiling (p90).

Reuses value_model for the projection + scoring pipeline so league rules stay identical.

Usage:
  python par_model.py                 # print the dynasty board (rank by expected PAR)
  python par_model.py --now           # rank by this-year PAR/wk instead
  python par_model.py --explain       # show the empirical foundations the model derived
  python par_model.py --rebuild       # force-refresh the 5-year foundations cache
"""
import argparse
import json
import sys
import time
import urllib.parse
from datetime import date
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import value_model as vm  # projection pipeline, scoring, STARTERS/FLEX, age curves

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

STATS_API = "https://api.sleeper.com/stats/nfl"
SEASONS = [2021, 2022, 2023, 2024, 2025]
FOUND_MAX_AGE = 7 * 86400


def _found_cache():
    """Resolved at call time so callers can override vm.CACHE before use."""
    return vm.CACHE / "par_foundations.json"

# --- simulation knobs ---
N_SIMS = 5000
HORIZON = 5          # future seasons simulated (t = 0..HORIZON)
WIN_NOW = 0.03       # gentle per-year "a point now wins now" discount
SEED = 20260529      # fixed -> reproducible board (daily commit only on real changes)
GAMES_WK = 14        # fantasy regular-season weeks (PAR/wk denominator)
AGE_BLEND = 0.5      # weight on empirical age curve vs researched (value_model.CURVES)


# ---------------------------------------------------------------- helpers
def age_in_season(birth_date, year):
    try:
        y, m, d = map(int, birth_date.split("-"))
        return (date(year, 9, 1) - date(y, m, d)).days / 365.25
    except Exception:
        return None


def fetch_actuals(season, pos, scoring):
    url = f"{STATS_API}/{season}?" + urllib.parse.urlencode(
        {"season_type": "regular", "position[]": pos, "order_by": "pts_half_ppr"}, doseq=True)
    out = []
    for rec in vm.fetch_json(url):
        s = rec.get("stats") or {}
        if (s.get("gp") or 0) < 1:
            continue
        out.append({"pid": rec.get("player_id"),
                    "pts": round(sum(v * s.get(k, 0) for k, v in scoring.items() if k in s), 1),
                    "gp": int(s.get("gp", 0))})
    return out


# ---------------------------------------------------------------- foundations
def build_foundations(cfg):
    """Derive every empirical input from 5 years of actuals. Returns a plain dict."""
    scoring = cfg["league"]["scoring_settings_raw"]
    teams = cfg["league"]["teams"]
    meta = vm.load_players()
    positions = list(vm.STARTERS)

    # gather (season,pos,pid,pts,age) records
    recs = []
    for season in SEASONS:
        for pos in positions:
            for r in fetch_actuals(season, pos, scoring):
                p = meta.get(r["pid"], {})
                age = age_in_season(p.get("birth_date"), season)
                recs.append({"season": season, "pos": pos, "pid": r["pid"],
                             "pts": r["pts"], "age": age})

    # 1) positional curves: avg season points by finish rank, across years
    pos_curve = {}
    for pos in positions:
        by_rank = {}
        for season in SEASONS:
            ranked = sorted((r["pts"] for r in recs if r["pos"] == pos and r["season"] == season),
                            reverse=True)
            for i, pts in enumerate(ranked):
                by_rank.setdefault(i, []).append(pts)
        pos_curve[pos] = [round(sum(v) / len(v), 1) for _, v in sorted(by_rank.items())]

    # 2) replacement: greedy fill of all optimal lineups on the avg curves
    pool = [{"pos": p, "pts": pts} for p in positions for pts in pos_curve[p]]
    by_pos = {p: sorted([x for x in pool if x["pos"] == p], key=lambda x: -x["pts"]) for p in positions}
    started = {p: vm.STARTERS[p] * teams for p in positions}
    flex_rem = sorted([x for p in vm.FLEX_POS for x in by_pos[p][started[p]:]],
                      key=lambda x: -x["pts"])
    for x in flex_rem[: vm.FLEX * teams]:
        started[x["pos"]] += 1
    replacement, repl_rank = {}, {}
    for p in positions:
        idx = min(started[p], len(by_pos[p]) - 1)
        replacement[p] = round(by_pos[p][idx]["pts"], 1)
        repl_rank[p] = idx + 1

    # 3) empirical age curve (fraction of own peak by age), blended with researched curves
    by_pid = {}
    for r in recs:
        if r["age"] is not None and r["pts"] > 0:
            by_pid.setdefault((r["pid"], r["pos"]), []).append((r["age"], r["pts"]))
    emp = {p: {} for p in positions}
    for (pid, pos), seasons in by_pid.items():
        if len(seasons) < 2:
            continue
        peak = max(pts for _, pts in seasons)
        if peak < 60:
            continue
        for age, pts in seasons:
            emp[pos].setdefault(round(age), []).append(pts / peak)
    age_curve = {}
    SHRINK = 8  # small age-buckets lean on the researched curve (tames survivorship noise)
    for pos in positions:
        ages = list(range(20, 41))
        researched = {a: vm.curve(pos, a) for a in ages}
        blended = {}
        for a in ages:
            samples = emp[pos].get(a)
            if samples:
                e = sum(samples) / len(samples)
                w = AGE_BLEND * len(samples) / (len(samples) + SHRINK)
            else:
                e, w = researched[a], 0.0
            blended[a] = w * e + (1 - w) * researched[a]
        # enforce a clean unimodal shape: non-decreasing up to the researched peak,
        # non-increasing after it (kills "QBs peak at 34" survivorship artifacts)
        peak = max(ages, key=lambda a: researched[a])
        for a in range(peak + 1, max(ages) + 1):
            blended[a] = min(blended[a], blended[a - 1])
        for a in range(peak - 1, min(ages) - 1, -1):
            blended[a] = min(blended[a], blended[a + 1])
        mx = max(blended.values()) or 1.0
        age_curve[pos] = {a: round(blended[a] / mx, 4) for a in ages}

    def curve_at(pos, age, table=None):
        tbl = (table or age_curve)[pos]
        a = int(round(age))
        lo, hi = min(tbl), max(tbl)
        return tbl[lo] if a <= lo else tbl[hi] if a >= hi else tbl[a]

    # 4) volatility (survivor residuals) + 5) drop-out hazard by age
    idx = {(r["season"], r["pid"]): r["pts"] for r in recs}
    vol = {p: [] for p in positions}
    drop_hits = {p: {} for p in positions}   # age -> [n_dropout, n_total]
    for r in recs:
        if r["age"] is None or r["season"] == SEASONS[-1]:
            continue
        pos = r["pos"]
        if r["pts"] < replacement[pos]:       # only judge startable players
            continue
        nxt = idx.get((r["season"] + 1, r["pid"]))
        a = round(r["age"])
        bucket = drop_hits[pos].setdefault(a, [0, 0])
        bucket[1] += 1
        if nxt is None or nxt < replacement[pos]:
            bucket[0] += 1                     # dropped below replacement / left
        else:
            drift = curve_at(pos, r["age"] + 1) / max(curve_at(pos, r["age"]), 1e-6)
            exp = r["pts"] * drift
            if exp > 0:
                vol[pos].append(nxt / exp)     # survivor residual
    # normalize residuals to mean 1 so age curve carries the drift, eps only the spread
    for pos in positions:
        arr = vol[pos]
        m = sum(arr) / len(arr) if arr else 1.0
        vol[pos] = [round(x / m, 4) for x in arr] if arr else [1.0]
    dropout = {}
    for pos in positions:
        dropout[pos] = {a: round(min(0.6, max(0.01, v[0] / v[1])), 4)
                        for a, v in drop_hits[pos].items() if v[1] >= 3}

    return {
        "seasons": SEASONS, "teams": teams,
        "pos_curve": pos_curve, "replacement": replacement, "repl_rank": repl_rank,
        "age_curve": {p: {str(a): v for a, v in age_curve[p].items()} for p in positions},
        "vol": vol, "dropout": {p: {str(a): v for a, v in dropout[p].items()} for p in positions},
        "built_at": str(date.today()),
    }


def load_foundations(cfg, rebuild=False):
    vm.CACHE.mkdir(parents=True, exist_ok=True)
    cache = _found_cache()
    if (not rebuild and cache.exists()
            and (time.time() - cache.stat().st_mtime) < FOUND_MAX_AGE):
        return json.loads(cache.read_text())
    F = build_foundations(cfg)
    cache.write_text(json.dumps(F))
    return F


def _curve_fn(F):
    tbls = {p: {int(a): v for a, v in F["age_curve"][p].items()} for p in F["age_curve"]}

    def at(pos, age):
        tbl = tbls.get(pos)
        if not tbl:
            return 1.0
        a = int(round(age))
        lo, hi = min(tbl), max(tbl)
        return tbl[lo] if a <= lo else tbl[hi] if a >= hi else tbl[a]
    return at


def _dropout_fn(F):
    tbls = {p: {int(a): v for a, v in F["dropout"][p].items()} for p in F["dropout"]}

    def at(pos, age):
        tbl = tbls.get(pos) or {}
        if not tbl:
            return 0.05
        a = int(round(age))
        if a in tbl:
            return tbl[a]
        near = min(tbl, key=lambda k: abs(k - a))    # nearest observed age
        base = tbl[near]
        return min(0.6, base + 0.03 * max(0, a - near))  # rise past oldest observed
    return at


# ---------------------------------------------------------------- simulation
def simulate(rows, F):
    """Attach par_now, par_now_wk, dyn_par (mean), par_floor (p10), par_ceil (p90)."""
    curve = _curve_fn(F)
    dropf = _dropout_fn(F)
    repl = F["replacement"]
    vol = {p: np.asarray(F["vol"][p], dtype=float) for p in F["vol"]}
    rng = np.random.default_rng(SEED)
    disc = np.array([(1 - WIN_NOW) ** t for t in range(HORIZON + 1)])

    for r in rows:
        pos, base, age = r["pos"], r["pts"], r["age"]
        rep = repl.get(pos)
        if rep is None or base <= 0:
            r["par_now"] = r["par_now_wk"] = r["dyn_par"] = r["par_floor"] = r["par_ceil"] = r["par_med"] = 0.0
            continue
        r["par_now"] = round(base - rep, 1)
        r["par_now_wk"] = round((base - rep) / GAMES_WK, 1)

        a0 = max(curve(pos, age), 1e-6)
        alive = np.ones(N_SIMS, dtype=bool)
        total = np.zeros(N_SIMS)
        varr = vol[pos]
        for t in range(HORIZON + 1):
            age_t = age + t
            drift = curve(pos, age_t) / a0
            if t == 0:
                eps = np.ones(N_SIMS)
            else:
                eps = rng.choice(varr, N_SIMS)
                alive &= rng.random(N_SIMS) > dropf(pos, age_t)
            pts_t = np.where(alive, base * drift * eps, 0.0)
            total += np.maximum(pts_t - rep, 0.0) * disc[t]
        r["dyn_par"] = round(float(total.mean()), 1)
        r["par_med"] = round(float(np.percentile(total, 50)), 1)
        r["par_floor"] = round(float(np.percentile(total, 10)), 1)
        r["par_ceil"] = round(float(np.percentile(total, 90)), 1)
    return rows


def build_board(cfg, rebuild=False):
    F = load_foundations(cfg, rebuild=rebuild)
    rows = vm.build_players(cfg)
    simulate(rows, F)
    rows.sort(key=lambda r: -r["dyn_par"])
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return rows, F


# ---------------------------------------------------------------- CLI
def explain(F):
    print(f"\n== PAR MODEL FOUNDATIONS (built {F['built_at']} from {F['seasons']}) ==")
    print(f"\n-- REPLACEMENT LEVEL ({F['teams']} teams, fill all optimal lineups) --")
    for p in vm.STARTERS:
        print(f"  {p}: {p}{F['repl_rank'][p]} = {F['replacement'][p]:.0f} pts/season "
              f"({F['replacement'][p]/GAMES_WK:.1f}/wk)")
    print("\n-- 5-YR POSITIONAL CURVE (season pts by finish) --")
    for p in vm.STARTERS:
        c = F["pos_curve"][p]
        ranks = [1, 3, 6, 12, 18, 24, 36]
        print(f"  {p}: " + "  ".join(f"{p}{r}={c[r-1]:.0f}" for r in ranks if r - 1 < len(c)))
    print("\n-- BLENDED AGE CURVE (fraction of peak) --")
    for p in vm.STARTERS:
        ac = {int(a): v for a, v in F["age_curve"][p].items()}
        ages = [22, 24, 26, 28, 30, 32, 34]
        print(f"  {p}: " + "  ".join(f"{a}y={ac.get(a,0):.2f}" for a in ages))
    print("\n-- DROP-OUT HAZARD (P fall below replacement next yr) --")
    for p in vm.STARTERS:
        dz = {int(a): v for a, v in F["dropout"][p].items()}
        ages = sorted(dz)
        print(f"  {p}: " + "  ".join(f"{a}y={dz[a]:.0%}" for a in ages[::2]) if dz else f"  {p}: (n/a)")
    print(f"\n-- SIM: {N_SIMS} trajectories, {HORIZON}-yr horizon, "
          f"win-now discount {WIN_NOW:.0%}/yr, floor PAR at 0 --")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--now", action="store_true", help="rank by this-year PAR/wk instead of dynasty PAR")
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--explain", action="store_true")
    ap.add_argument("--rebuild", action="store_true", help="force-refresh the 5-yr foundations cache")
    args = ap.parse_args()

    cfg = json.loads((Path.home() / ".claude" / "fantasy-football-config.json").read_text())
    rows, F = build_board(cfg, rebuild=args.rebuild)
    if args.explain:
        explain(F); return

    if args.now:
        rows = sorted(rows, key=lambda r: -r["par_now_wk"])
    label = "THIS-YEAR PAR/wk" if args.now else "DYNASTY PAR (expected, p10->p90)"
    print(f"\nTOP {args.top} — ranked by {label}\n")
    print(f"{'#':>3} {'Player':22} {'Pos':3} {'Age':>4} {'Pts':>5} {'PAR/wk':>7} "
          f"{'DynPAR':>7} {'Floor':>6} {'Ceil':>6}")
    for i, r in enumerate(rows[: args.top], 1):
        print(f"{i:>3} {r['name'][:22]:22} {r['pos']:3} {r['age']:>4} {r['pts']:>5.0f} "
              f"{r['par_now_wk']:>7.1f} {r['dyn_par']:>7.0f} {r['par_floor']:>6.0f} {r['par_ceil']:>6.0f}")


if __name__ == "__main__":
    main()

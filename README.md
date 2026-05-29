# Draft Caddie 🏈

A tiny, installable web app (PWA) that gives **live dynasty draft recommendations**
for the *EA 2021 DYNASTY* Sleeper league during a rolling/slow draft.

Open it, tap **Refresh**, and it pulls the latest picks straight from Sleeper and
tells you who to take next — tuned to this league's exact rules (Half-PPR,
TE-premium, 12 teams, 1QB/2RB/2WR/1TE + 2 FLEX) with dynasty age curves and VOR.

**Live app:** https://semmer-psufball.github.io/draft-caddie/

## How it works (no server needed)

- `generate_board.py` runs the **PAR engine** (`par_model.py`) once and writes
  **`board.json`**. PAR = *Points Above Replacement* (WAR for fantasy):
  - **Empirical foundations** from 5 years of actual results (2021–2025) under league
    scoring: positional scoring curves + the true replacement level (the best player
    *not* startable league-wide after filling all 12 lineups + flex).
  - **PAR now** = projected points − replacement, per week.
  - **Dynasty value** via a **Monte Carlo simulation**: thousands of future
    trajectories per player (production variance, injury, age decline, bust risk),
    summed as expected PAR with floor (p10) / ceiling (p90). Seeded for reproducibility.
  This is the slow part and doesn't change pick-to-pick.
- The browser app (`index.html` + `app.js`) loads `board.json`, then on every
  **Refresh** fetches live picks from the public Sleeper API and recomputes
  recommendations **in the browser**: it removes drafted players, builds your roster
  from your slot's picks, and ranks the rest by
  `score = dynasty PAR + roster-fit` (marginal starting-lineup PAR, so a position you
  already have stacked is down-weighted).
- A daily GitHub Action (`.github/workflows/refresh-board.yml`) regenerates
  `board.json` so values stay fresh over a multi-week draft. The Refresh button always
  reflects the latest picks regardless.

> Note on updates: the service worker is **network-first**, so app/code changes deploy
> immediately when online (the cache is only an offline fallback).

## Install on your iPad

1. Open the live URL above in **Safari**.
2. Tap the **Share** icon → **Add to Home Screen** → **Add**.
3. Launch "Caddie" from your home screen. Tap **Refresh** whenever you want the
   latest board.

## Run / develop locally

```bash
python generate_board.py          # rebuild board.json (uses stdlib only)
python -m http.server 8000        # then open http://localhost:8000
```

`make_icons.py` (needs `pip install pillow`) regenerates the app icons; run it only
if you want to change the artwork.

## Updating the model

`value_model.py` here is a **vendored copy** of the engine from the
`fantasy-football` project's `dynasty-football` skill (the source of truth). If the
model changes there, copy it back over and the daily job picks it up.

# Draft Caddie 🏈

A tiny, installable web app (PWA) that gives **live dynasty draft recommendations**
for the *EA 2021 DYNASTY* Sleeper league during a rolling/slow draft.

Open it, tap **Refresh**, and it pulls the latest picks straight from Sleeper and
tells you who to take next — tuned to this league's exact rules (Half-PPR,
TE-premium, 12 teams, 1QB/2RB/2WR/1TE + 2 FLEX) with dynasty age curves and VOR.

**Live app:** https://semmer-psufball.github.io/draft-caddie/

## How it works (no server needed)

- `generate_board.py` runs the valuation engine (`value_model.py`) once and writes
  **`board.json`** — every player priced under league rules (projected points →
  multi-year age curve → value over replacement). This is the slow part, and it
  doesn't change pick-to-pick.
- The browser app (`index.html` + `app.js`) loads `board.json`, then on every
  **Refresh** fetches live picks from the public Sleeper API and recomputes
  recommendations **in the browser**: it removes drafted players, builds your roster
  from your slot's picks, and ranks the rest by
  `score = marginal lineup value + 0.6 × VOR` (so stacking a position is penalized).
- A daily GitHub Action (`.github/workflows/refresh-board.yml`) regenerates
  `board.json` so projections stay fresh over a multi-week draft. The Refresh button
  always reflects the latest picks regardless.

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

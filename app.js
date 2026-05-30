"use strict";

// Draft Caddie — client-side draft assistant.
// Loads the precomputed value board (board.json), fetches LIVE picks from the
// Sleeper API on demand, and recomputes recommendations entirely in the browser.
// The scoring math is a faithful port of par_model.py / value_model.py.

const SLEEPER = "https://api.sleeper.app/v1";
const COLLAPSED_PER_POS = 10;                          // rows shown before "Show more"
const POS_CAP = { QB: 32, RB: 80, WR: 90, TE: 45 };    // max rows when expanded
const WATCHLIST_KEY = "caddie_watchlist_v1";
const STARS_VERSION_KEY = "caddie_stars_version";
// Curated target board — merged into your watchlist once per version bump (any manual
// removals you make afterward are respected). Bump STARS_VERSION to push a new batch.
const STARS_VERSION = 2;
const DEFAULT_STARS = [
  // WR (top need): Jameson Williams, Zay Flowers, Brian Thomas Jr, DeVonta Smith,
  //               Rome Odunze, Parker Washington, (+ Burden, McMillan from before)
  "8148", "9997", "11631", "7525", "11620", "9487", "12519", "12526",
  // QB (young/value darts — wait then pounce): Jaxson Dart, Caleb Williams, Trevor Lawrence
  "12508", "11560", "7523",
  // TE (have Fannin; upside + taxi stash): LaPorta, Pitts, Kraft, Oronde Gadsden, Sadiq
  "10859", "7553", "9484", "12493", "13330",
  // RB (not a need — flag only as value-if-they-slide): Judkins, Skattebo, Tuten
  "12512", "12481", "12490",
];

let BOARD = null;          // { meta, players: [...] }
let BYID = null;           // player_id -> player row
let WATCH = null;          // Set of starred player_ids (persisted)
let LAST = null;           // { draft, picks } — kept so star/show-more re-render without refetch
const EXPANDED = { QB: false, RB: false, WR: false, TE: false };

const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const setStatus = (msg, isErr) => {
  const el = $("#status");
  el.textContent = msg || "";
  el.classList.toggle("error", !!isErr);
};

// ---------- watchlist (localStorage) ----------
function loadWatch() {
  let set;
  try { set = new Set(JSON.parse(localStorage.getItem(WATCHLIST_KEY) || "[]")); }
  catch { set = new Set(); }
  let appliedV = 0;
  try { appliedV = +(localStorage.getItem(STARS_VERSION_KEY) || 0); } catch {}
  if (appliedV < STARS_VERSION) {             // merge the latest curated batch exactly once
    DEFAULT_STARS.forEach((id) => set.add(id));
    try {
      localStorage.setItem(WATCHLIST_KEY, JSON.stringify([...set]));
      localStorage.setItem(STARS_VERSION_KEY, String(STARS_VERSION));
    } catch {}
  }
  return set;
}
function saveWatch() {
  try { localStorage.setItem(WATCHLIST_KEY, JSON.stringify([...WATCH])); } catch {}
}
function toggleStar(pid) {
  if (WATCH.has(pid)) WATCH.delete(pid); else WATCH.add(pid);
  saveWatch();
  if (LAST) render(LAST.draft, LAST.picks);
}
function starHTML(pid) {
  const on = WATCH.has(pid);
  return `<span class="star${on ? " on" : ""}" data-pid="${esc(pid)}" role="button" ` +
    `aria-label="${on ? "remove from" : "add to"} watchlist">${on ? "★" : "☆"}</span>`;
}

// ---------- snake-draft position math (port of live_draft.py:39-60) ----------
function overallPick(round_, slot, teams) {
  return round_ % 2 === 1
    ? (round_ - 1) * teams + slot
    : (round_ - 1) * teams + (teams - slot + 1);
}
function slotForPick(pickNo, teams) {
  const r = Math.floor((pickNo - 1) / teams) + 1;
  const pos = ((pickNo - 1) % teams) + 1;
  return r % 2 === 1 ? pos : teams - pos + 1;
}
function ourNextPick(nextNo, ourSlot, teams) {
  let r = Math.floor((nextNo - 1) / teams) + 1;
  while (true) {
    const ours = overallPick(r, ourSlot, teams);
    if (ours >= nextNo) return { round: r, overall: ours, away: ours - nextNo };
    r += 1;
  }
}

// ---------- starting-lineup PAR (roster-need signal) ----------
// Value the optimal starting lineup in PAR units (points above replacement), not raw
// points — so a QB's high replacement level isn't mistaken for roster value. A below-
// replacement starter contributes 0 (you'd never actually start them).
function lineupPAR(roster, meta) {
  const starters = meta.starters;          // {QB:1,RB:2,WR:2,TE:1}
  const flexPos = meta.flex_pos;           // ["RB","WR","TE"]
  const val = (r) => Math.max(r.par_now || 0, 0);
  const used = new Set();
  let total = 0;
  for (const pos of Object.keys(starters)) {
    const pool = roster.filter((r) => r.pos === pos).sort((a, b) => val(b) - val(a));
    for (const r of pool.slice(0, starters[pos])) { used.add(r.player_id); total += val(r); }
  }
  const flex = roster
    .filter((r) => flexPos.includes(r.pos) && !used.has(r.player_id))
    .sort((a, b) => val(b) - val(a));
  for (const r of flex.slice(0, meta.flex)) total += val(r);
  return total;
}

// ---------- ranked recommendations ----------
function scoreAvail(myPids, draftedPids) {
  const meta = BOARD.meta;
  const taken = new Set([...myPids, ...draftedPids]);
  const myRoster = BOARD.players.filter((r) => myPids.has(r.player_id));
  const avail = BOARD.players.filter((r) => !taken.has(r.player_id));
  const cur = lineupPAR(myRoster, meta);
  const fit = meta.fit_weight ?? 0.5;
  const scored = avail.map((r) => {
    const marg = lineupPAR([...myRoster, r], meta) - cur;   // roster need
    return { row: r, marg, score: r.dyn_par + fit * marg };
  });
  scored.sort((a, b) => b.score - a.score);
  return { scored, myRoster };
}

// ---------- tier marking (port of live_draft.py:_tier_marked) ----------
function tierMark(players, frac) {
  if (!players.length) return players;
  const top = players[0].dyn_par || 1;
  players.forEach((p, i) => {
    p._tier = i > 0 && players[i - 1].dyn_par - p.dyn_par > frac * top;
  });
  return players;
}

// ---------- data loading ----------
async function loadBoard() {
  if (BOARD) return BOARD;
  const res = await fetch("board.json", { cache: "no-cache" });
  if (!res.ok) throw new Error(`board.json ${res.status}`);
  BOARD = await res.json();
  BYID = new Map(BOARD.players.map((r) => [r.player_id, r]));
  return BOARD;
}

async function fetchDraft() {
  const id = BOARD.meta.draft_id;
  const opts = { cache: "no-store" };
  const [draft, picks] = await Promise.all([
    fetch(`${SLEEPER}/draft/${id}`, opts).then((r) => r.json()),
    fetch(`${SLEEPER}/draft/${id}/picks`, opts).then((r) => r.json()),
  ]);
  picks.sort((a, b) => a.pick_no - b.pick_no);
  return { draft, picks };
}

// ---------- rendering ----------
function fmt(n) { return Math.round(n); }
function ageClass(age) { return age == null ? "" : age < 25 ? "young" : age > 29 ? "old" : ""; }
// market ADP vs our value rank: green if the market drafts him notably later than we
// rank him (a value/steal), orange if the market reaches earlier than we would.
function adpClass(row) {
  if (row.adp == null) return "";
  const edge = row.adp - row.rank;
  return edge >= 6 ? "steal" : edge <= -6 ? "reach" : "";
}
function adpText(row) { return row.adp == null ? "—" : String(fmt(row.adp)); }

function render(draft, picks) {
  LAST = { draft, picks };
  const meta = BOARD.meta;
  const teams = draft.settings?.teams || meta.teams;
  const rounds = draft.settings?.rounds || meta.rounds;
  const order = draft.draft_order || {};
  let ourSlot = order[meta.our_uid] ||
    (picks.find((p) => p.picked_by === meta.our_uid) || {}).draft_slot || null;

  const draftedPids = new Set(picks.map((p) => p.player_id));
  const myPids = new Set(picks.filter((p) => p.draft_slot === ourSlot).map((p) => p.player_id));

  const { scored, myRoster } = scoreAvail(myPids, draftedPids);
  const scoreByPid = new Map(scored.map((s) => [s.row.player_id, s.score]));

  const nextNo = picks.length ? picks[picks.length - 1].pick_no + 1 : 1;
  const complete = draft.status === "complete";
  const onClock = !complete && slotForPick(nextNo, teams) === ourSlot;
  const np = ourSlot ? ourNextPick(nextNo, ourSlot, teams) : null;

  // subtitle
  $("#subtitle").textContent =
    `${meta.league_name} · slot ${ourSlot ?? "?"} · pick ${Math.min(nextNo, teams * rounds)}/${teams * rounds}`;

  // clock banner
  const clock = $("#clock");
  if (complete) {
    clock.innerHTML = `<div class="done">Draft complete</div>`;
  } else if (onClock) {
    clock.innerHTML = `<div class="onclock">★ YOU'RE ON THE CLOCK ★</div>`;
  } else if (np) {
    clock.innerHTML =
      `<div class="next">You pick next: R${np.round} · overall #${np.overall} · ` +
      `<b>${np.away}</b> pick${np.away === 1 ? "" : "s"} away</div>`;
  } else {
    clock.innerHTML = "";
  }

  // roster
  $("#rostercount").textContent = `(${myRoster.length})`;
  const roster = $("#roster");
  if (!myRoster.length) {
    roster.innerHTML = `<span class="empty">empty</span>`;
  } else {
    const ord = { QB: 0, RB: 1, WR: 2, TE: 3 };
    roster.innerHTML = myRoster
      .slice()
      .sort((a, b) => (ord[a.pos] ?? 9) - (ord[b.pos] ?? 9))
      .map((r) => `<span class="chip ${esc(r.pos.toLowerCase())}">${esc(r.name)}` +
        `<em>${esc(r.pos)}${fmt(r.age)}</em></span>`)
      .join("");
  }

  // watchlist panel
  renderWatchlist(draftedPids, scoreByPid);

  // top picks
  $("#toppicks tbody").innerHTML = scored.slice(0, 10).map(({ row }, i) =>
    `<tr class="${i === 0 ? "best" : ""}">` +
    `<td>${i + 1}${i === 0 ? " ★" : ""}</td>` +
    `<td class="pname">${starHTML(row.player_id)} ${esc(row.name)}</td>` +
    `<td>${esc(row.pos)}</td><td>${row.age}</td>` +
    `<td class="val">${fmt(row.dyn_par)}</td>` +
    `<td class="rng">${fmt(row.par_floor)}–${fmt(row.par_ceil)}</td>` +
    `<td class="adp ${adpClass(row)}">${adpText(row)}</td></tr>`
  ).join("");

  // best available by position (collapsible)
  $("#positions").innerHTML = ["QB", "RB", "WR", "TE"].map((pos) => {
    const full = BOARD.players
      .filter((r) => r.pos === pos && !draftedPids.has(r.player_id))
      .sort((a, b) => b.dyn_par - a.dyn_par)
      .slice(0, POS_CAP[pos] || 40);
    const expanded = EXPANDED[pos];
    let visible = full.slice(0, expanded ? full.length : COLLAPSED_PER_POS);
    // when collapsed, still surface any starred-available guys beyond the cut
    if (!expanded) {
      const extra = full.slice(COLLAPSED_PER_POS).filter((r) => WATCH.has(r.player_id));
      visible = visible.concat(extra);
    }
    tierMark(visible, meta.tier_break_frac);
    const maxDyn = visible.length ? visible[0].dyn_par || 1 : 1;
    const rows = visible.map((r) => {
      const w = Math.max(8, Math.round((Math.max(r.dyn_par, 0) / maxDyn) * 100));
      const star = WATCH.has(r.player_id) ? " starred" : "";
      return `<div class="player${r._tier ? " tier-break" : ""}${star}">` +
        starHTML(r.player_id) +
        `<span class="pn">${esc(r.name)}</span>` +
        `<span class="ag ${ageClass(r.age)}">${r.age}y</span>` +
        `<span class="pt">${fmt(r.dyn_par)}</span>` +
        `<span class="padp ${adpClass(r)}">${adpText(r)}</span>` +
        `<span class="bar" style="width:${w}%"></span></div>`;
    }).join("");
    const moreCount = full.length - COLLAPSED_PER_POS;
    const moreBtn = moreCount > 0
      ? `<button class="showmore" data-pos="${pos}">` +
        (expanded ? "Show less" : `Show more (${moreCount})`) + `</button>`
      : "";
    return `<div class="col"><h3>${pos} <span class="ct">(${full.length})</span></h3>${rows}${moreBtn}</div>`;
  }).join("");

  // recent picks (last 10)
  $("#recent tbody").innerHTML = picks.slice(-10).reverse().map((p) => {
    const m = p.metadata || {};
    const name = `${m.first_name || ""} ${m.last_name || ""}`.trim() ||
      (BYID.get(p.player_id) || {}).name || p.player_id;
    const mine = p.draft_slot === ourSlot ? ' class="ours"' : "";
    return `<tr${mine}><td>${p.pick_no}</td><td>R${p.round}</td>` +
      `<td>${p.draft_slot}</td><td>${esc(name)}</td><td>${esc(m.position || (BYID.get(p.player_id) || {}).pos || "")}</td></tr>`;
  }).join("");

  $("#app").hidden = false;
}

function renderWatchlist(draftedPids, scoreByPid) {
  const panel = $("#watchpanel");
  const rows = [...WATCH].map((pid) => BYID.get(pid)).filter(Boolean);
  if (!rows.length) { panel.hidden = true; return; }
  panel.hidden = false;
  const avail = rows.filter((r) => !draftedPids.has(r.player_id))
    .sort((a, b) => (scoreByPid.get(b.player_id) ?? b.dyn_par) - (scoreByPid.get(a.player_id) ?? a.dyn_par));
  const gone = rows.filter((r) => draftedPids.has(r.player_id));
  $("#watchcount").textContent =
    `(${avail.length} available${gone.length ? `, ${gone.length} gone` : ""})`;
  const row = (r, isGone) =>
    `<div class="wrow${isGone ? " gone" : ""}">` +
    starHTML(r.player_id) +
    `<span class="wn">${esc(r.name)}</span>` +
    `<span class="wpos ${esc(r.pos.toLowerCase())}">${esc(r.pos)}</span>` +
    `<span class="wage ${isGone ? "" : ageClass(r.age)}">${isGone ? "drafted" : r.age + "y"}</span>` +
    (isGone ? "" : `<span class="wdp">${fmt(r.dyn_par)}</span>` +
      `<span class="wc rng">${fmt(r.par_floor)}–${fmt(r.par_ceil)}</span>`) +
    `</div>`;
  $("#watchlist").innerHTML =
    avail.map((r) => row(r, false)).join("") + gone.map((r) => row(r, true)).join("");
}

// ---------- refresh flow ----------
let refreshing = false;
async function refresh() {
  if (refreshing) return;
  refreshing = true;
  const btn = $("#refresh");
  btn.classList.add("busy");
  setStatus("Fetching latest picks…");
  try {
    await loadBoard();
    const { draft, picks } = await fetchDraft();
    render(draft, picks);
    const t = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    setStatus(`Updated ${t} · ${picks.length} picks made · board ${BOARD.meta.generated_at}`);
  } catch (err) {
    console.error(err);
    setStatus(`Couldn't refresh: ${err.message}. Check your connection and try again.`, true);
  } finally {
    btn.classList.remove("busy");
    refreshing = false;
  }
}

// ---------- add-to-home-screen hint (Safari, not yet installed) ----------
function maybeShowA2HS() {
  const isStandalone = window.navigator.standalone === true ||
    window.matchMedia("(display-mode: standalone)").matches;
  const isIOS = /iphone|ipad|ipod/i.test(navigator.userAgent);
  if (isIOS && !isStandalone) {
    const bar = $("#a2hs");
    bar.hidden = false;
    $("#a2hs-close").addEventListener("click", () => (bar.hidden = true));
  }
}

// ---------- init ----------
WATCH = loadWatch();
$("#refresh").addEventListener("click", refresh);
// one delegated handler for star toggles + show-more buttons
document.addEventListener("click", (e) => {
  const star = e.target.closest(".star");
  if (star) { toggleStar(star.dataset.pid); return; }
  const more = e.target.closest(".showmore");
  if (more) {
    EXPANDED[more.dataset.pos] = !EXPANDED[more.dataset.pos];
    if (LAST) render(LAST.draft, LAST.picks);
  }
});
maybeShowA2HS();
refresh();

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => navigator.serviceWorker.register("sw.js").catch(() => {}));
}

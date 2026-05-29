"use strict";

// Draft Caddie — client-side draft assistant.
// Loads the precomputed value board (board.json), fetches LIVE picks from the
// Sleeper API on demand, and recomputes recommendations entirely in the browser.
// The scoring math is a faithful port of value_model.py / live_draft.py.

const SLEEPER = "https://api.sleeper.app/v1";
const COLS_PER_POS = 16;

let BOARD = null;          // { meta, players: [...] }
let BYID = null;           // player_id -> player row

const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const setStatus = (msg, isErr) => {
  const el = $("#status");
  el.textContent = msg || "";
  el.classList.toggle("error", !!isErr);
};

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

// ---------- lineup value (port of value_model.py:168) ----------
function lineupPoints(roster, meta) {
  const starters = meta.starters;          // {QB:1,RB:2,WR:2,TE:1}
  const flexPos = meta.flex_pos;           // ["RB","WR","TE"]
  const used = new Set();
  let total = 0;
  for (const pos of Object.keys(starters)) {
    const pool = roster.filter((r) => r.pos === pos).sort((a, b) => b.pts - a.pts);
    for (const r of pool.slice(0, starters[pos])) { used.add(r.player_id); total += r.pts; }
  }
  const flex = roster
    .filter((r) => flexPos.includes(r.pos) && !used.has(r.player_id))
    .sort((a, b) => b.pts - a.pts);
  for (const r of flex.slice(0, meta.flex)) total += r.pts;
  return total;
}

// ---------- ranked recommendations (port of live_draft.py:79) ----------
function scoreAvail(myPids, draftedPids) {
  const meta = BOARD.meta;
  const taken = new Set([...myPids, ...draftedPids]);
  const myRoster = BOARD.players.filter((r) => myPids.has(r.player_id));
  const avail = BOARD.players.filter((r) => !taken.has(r.player_id));
  const cur = lineupPoints(myRoster, meta);
  const scored = avail.map((r) => {
    const marg = lineupPoints([...myRoster, r], meta) - cur;
    return { row: r, marg, score: marg + meta.vor_weight * r.vor };
  });
  scored.sort((a, b) => b.score - a.score);
  return { scored, myRoster };
}

// ---------- tier marking (port of live_draft.py:_tier_marked) ----------
function tierMark(players, frac) {
  if (!players.length) return players;
  const top = players[0].dyn || 1;
  players.forEach((p, i) => {
    p._tier = i > 0 && players[i - 1].dyn - p.dyn > frac * top;
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

function render(draft, picks) {
  const meta = BOARD.meta;
  const teams = draft.settings?.teams || meta.teams;
  const rounds = draft.settings?.rounds || meta.rounds;
  const order = draft.draft_order || {};
  let ourSlot = order[meta.our_uid] ||
    (picks.find((p) => p.picked_by === meta.our_uid) || {}).draft_slot || null;

  const draftedPids = new Set(picks.map((p) => p.player_id));
  const myPids = new Set(picks.filter((p) => p.draft_slot === ourSlot).map((p) => p.player_id));

  const { scored, myRoster } = scoreAvail(myPids, draftedPids);

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

  // top picks
  $("#toppicks tbody").innerHTML = scored.slice(0, 10).map(({ row, marg, score }, i) =>
    `<tr class="${i === 0 ? "best" : ""}">` +
    `<td>${i + 1}${i === 0 ? " ★" : ""}</td>` +
    `<td class="pname">${esc(row.name)}</td><td>${esc(row.pos)}</td><td>${row.age}</td>` +
    `<td>${fmt(row.pts)}</td><td>${marg.toFixed(0)}</td>` +
    `<td>${fmt(row.vor)}</td><td class="score">${fmt(score)}</td></tr>`
  ).join("");

  // best available by position
  const posWrap = $("#positions");
  posWrap.innerHTML = ["QB", "RB", "WR", "TE"].map((pos) => {
    const pool = tierMark(
      BOARD.players
        .filter((r) => r.pos === pos && !draftedPids.has(r.player_id))
        .sort((a, b) => b.dyn - a.dyn)
        .slice(0, COLS_PER_POS),
      meta.tier_break_frac
    );
    const maxDyn = pool.length ? pool[0].dyn || 1 : 1;
    const rows = pool.map((r) => {
      const w = Math.max(8, Math.round((r.dyn / maxDyn) * 100));
      return `<div class="player${r._tier ? " tier-break" : ""}">` +
        `<span class="pn">${esc(r.name)}</span>` +
        `<span class="ag ${ageClass(r.age)}">${r.age}y</span>` +
        `<span class="pt">${fmt(r.pts)}</span>` +
        `<span class="bar" style="width:${w}%"></span></div>`;
    }).join("");
    return `<div class="col"><h3>${pos} <span class="ct">(${pool.length})</span></h3>${rows}</div>`;
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
$("#refresh").addEventListener("click", refresh);
maybeShowA2HS();
refresh();

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => navigator.serviceWorker.register("sw.js").catch(() => {}));
}

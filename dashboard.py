"""A live web dashboard for the meeting organizer.

Runs inside the bot's own asyncio loop on aiohttp, which discord.py already
depends on, so there is nothing extra to install. The page is a single
self-contained document; it polls /api/state for fresh data.

Host and port come from the caller: on Render that means 0.0.0.0 and $PORT.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable

from aiohttp import web

log = logging.getLogger("meetingbot.dashboard")

StateProvider = Callable[[], Awaitable[dict]]

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FTC Meeting Organizer</title>
<style>
  *, *::before, *::after { box-sizing: border-box; }
  :root {
    --bg: #0d1017; --panel: #151a23; --panel-2: #1b2230; --line: #252d3b;
    --text: #e6ecf5; --muted: #8b97ab; --accent: #f5c518; --accent-2: #a855f7;
    --blue: #38bdf8; --green: #34d399; --rose: #fb7185; --orange: #fb923c;
    --radius: 14px;
  }
  html, body { height: 100%; margin: 0; }
  body {
    background: var(--bg); color: var(--text); overflow: hidden;
    font: 15px/1.5 "Segoe UI", Inter, system-ui, -apple-system, sans-serif;
  }
  .app { display: grid; grid-template-columns: 250px 1fr; height: 100vh; }

  /* ---------- sidebar ---------- */
  .side {
    background: var(--panel); border-right: 1px solid var(--line);
    padding: 22px 16px; display: flex; flex-direction: column; gap: 26px;
    overflow-y: auto;
  }
  .brand { display: flex; align-items: center; gap: 11px; }
  .brand .dot {
    width: 34px; height: 34px; border-radius: 10px; flex: none;
    background: linear-gradient(135deg, var(--accent), var(--orange));
    display: grid; place-items: center; font-size: 18px;
  }
  .brand h1 { font-size: 15px; margin: 0; line-height: 1.25; }
  .brand span { color: var(--muted); font-size: 12px; }

  .sect { display: flex; flex-direction: column; gap: 6px; }
  .sect > .label {
    color: var(--muted); font-size: 11px; letter-spacing: .09em;
    text-transform: uppercase; padding: 0 10px 4px;
  }
  .nav {
    display: flex; align-items: center; gap: 10px; padding: 9px 11px;
    border-radius: 9px; cursor: pointer; color: var(--muted);
    border: 1px solid transparent; font-size: 14px; text-align: left;
    background: none; width: 100%; font-family: inherit;
  }
  .nav:hover { background: var(--panel-2); color: var(--text); }
  .nav.on { background: var(--panel-2); color: var(--text); border-color: var(--line); }
  .nav .ic { width: 18px; text-align: center; }

  .stats { display: grid; gap: 9px; }
  .stat {
    background: var(--panel-2); border: 1px solid var(--line);
    border-radius: 11px; padding: 11px 13px;
  }
  .stat b { display: block; font-size: 21px; line-height: 1.2; }
  .stat span { color: var(--muted); font-size: 11.5px; }

  /* ---------- main ---------- */
  .main { display: flex; flex-direction: column; min-width: 0; overflow: hidden; }
  .top {
    display: flex; align-items: center; gap: 14px; flex-wrap: wrap;
    padding: 16px 26px; border-bottom: 1px solid var(--line); background: var(--panel);
  }
  .top h2 { margin: 0; font-size: 21px; min-width: 190px; }
  .btn {
    background: var(--panel-2); color: var(--text); border: 1px solid var(--line);
    border-radius: 9px; padding: 7px 13px; cursor: pointer;
    font-size: 13.5px; font-family: inherit;
  }
  .btn:hover { border-color: var(--muted); }
  .btn.pri { background: var(--accent); color: #1a1400; border-color: transparent; font-weight: 600; }
  .spacer { flex: 1; }
  .live { color: var(--muted); font-size: 12px; display: flex; align-items: center; gap: 7px; }
  .pulse {
    width: 8px; height: 8px; border-radius: 50%; background: var(--green);
    animation: pulse 2s infinite;
  }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: .25; } }

  .scroll { overflow: auto; padding: 22px 26px 34px; flex: 1; }
  .view { display: none; }
  .view.on { display: block; }

  /* ---------- month calendar ---------- */
  .dow {
    display: grid; grid-template-columns: repeat(7, 1fr); gap: 9px;
    margin-bottom: 9px;
  }
  .dow div {
    color: var(--muted); font-size: 11.5px; letter-spacing: .07em;
    text-transform: uppercase; text-align: center; padding-bottom: 2px;
  }
  .grid { display: grid; grid-template-columns: repeat(7, 1fr); gap: 9px; }
  .cell {
    background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius);
    min-height: 116px; padding: 9px 10px; display: flex; flex-direction: column; gap: 6px;
    position: relative; overflow: hidden;
  }
  .cell.pad { background: transparent; border-color: transparent; }
  .cell.today { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent) inset; }
  .cell .n { font-size: 13.5px; color: var(--muted); font-weight: 600; }
  .cell.today .n { color: var(--accent); }
  .cell .heat {
    position: absolute; inset: auto 0 0 0; height: 3px; background: var(--green);
  }
  .chip {
    background: var(--panel-2); border-left: 3px solid var(--accent);
    border-radius: 6px; padding: 4px 7px; font-size: 11.5px;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .chip.p { border-left-color: var(--green); color: var(--muted); }
  .chip time { color: var(--muted); font-size: 10.5px; display: block; }
  .cell .more { color: var(--muted); font-size: 10.5px; }

  /* ---------- heatmap ---------- */
  .heatwrap { overflow-x: auto; }
  .hm { border-collapse: separate; border-spacing: 5px; min-width: 560px; width: 100%; }
  .hm th { color: var(--muted); font-size: 11.5px; font-weight: 500; text-transform: uppercase; }
  .hm th.row { text-align: right; padding-right: 8px; white-space: nowrap; width: 96px; }
  .hm td {
    border-radius: 8px; height: 42px; text-align: center; font-size: 13px;
    background: var(--panel); border: 1px solid var(--line); font-variant-numeric: tabular-nums;
  }
  .hm td.z { color: #3a4557; }
  .hm td.best { outline: 2px solid var(--accent); outline-offset: -2px; }

  /* ---------- lists ---------- */
  /* align-items:start so a tall card doesn't stretch its whole row */
  .cards {
    display: grid; gap: 11px; align-items: start;
    grid-template-columns: repeat(auto-fill, minmax(290px, 1fr));
  }
  .card {
    background: var(--panel); border: 1px solid var(--line); border-left: 4px solid var(--accent);
    border-radius: var(--radius); padding: 13px 15px;
  }
  .card h4 { margin: 0 0 5px; font-size: 15px; }
  .card .meta { color: var(--muted); font-size: 12.5px; }
  .card .who { color: var(--muted); font-size: 12px; margin-top: 7px; line-height: 1.45; }
  .rank { float: right; font-size: 17px; }
  .empty { color: var(--muted); text-align: center; padding: 56px 20px; }
  .hint { color: var(--muted); font-size: 13px; margin: 0 0 14px; }

  /* ---------- best-times cards ---------- */
  .bcard {
    background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius);
    padding: 14px 16px;
  }
  .bcard.win { border-color: var(--accent); background: linear-gradient(180deg, rgba(245,197,24,.07), transparent); }
  .bhead { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }
  .bwhen { font-size: 15.5px; font-weight: 650; }
  .btime { color: var(--accent); font-size: 13.5px; font-weight: 600; margin-top: 1px; }
  .bcount { text-align: right; line-height: 1.15; }
  .bcount b { font-size: 20px; }
  .bcount b span { color: var(--muted); font-size: 13px; font-weight: 500; }
  .bcount > span { display: block; color: var(--muted); font-size: 10.5px; text-transform: uppercase; letter-spacing: .06em; }
  .bbar { height: 5px; border-radius: 3px; background: var(--panel-2); margin: 11px 0 9px; overflow: hidden; }
  .bbar i { display: block; height: 100%; background: var(--green); border-radius: 3px; }
  .bfoot { display: flex; align-items: center; gap: 9px; flex-wrap: wrap; }
  .pill {
    background: var(--panel-2); border: 1px solid var(--line); color: var(--muted);
    border-radius: 999px; padding: 2px 9px; font-size: 11.5px; white-space: nowrap;
  }
  .bfoot .who { color: var(--muted); font-size: 12.5px; margin: 0; }
  .bcard.sel { border-color: var(--blue); box-shadow: 0 0 0 1px var(--blue) inset; }
  .cards.dim .bcard { opacity: .62; }
  .cards.dim .btime { color: var(--muted); }

  /* ---------- season calendar ---------- */
  .months { display: grid; gap: 22px; grid-template-columns: repeat(auto-fill, minmax(290px, 1fr)); }
  .mon { background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius); padding: 14px; }
  .mon h4 { margin: 0 0 10px; font-size: 14.5px; }
  .mgrid { display: grid; grid-template-columns: repeat(7, 1fr); gap: 4px; }
  .mgrid .dh { color: var(--muted); font-size: 10px; text-align: center; text-transform: uppercase; }
  .mday {
    aspect-ratio: 1; border-radius: 7px; display: grid; place-items: center;
    font-size: 12px; background: var(--panel-2); color: var(--muted);
    border: 1px solid transparent; position: relative;
  }
  .mday.pad { background: transparent; }
  .mday.pick { cursor: pointer; }
  .mday.today { border-color: var(--accent); color: var(--accent); font-weight: 700; }
  .mday.has { color: #06281d; font-weight: 650; }
  .mday.ev::after {
    content: ""; position: absolute; bottom: 3px; width: 4px; height: 4px;
    border-radius: 50%; background: var(--accent);
  }

  /* ---------- selected day ---------- */
  .cell { cursor: pointer; }
  .cell.pad { cursor: default; }
  .cell.sel, .mday.sel {
    border-color: var(--blue);
    box-shadow: 0 0 0 2px var(--blue) inset;
  }
  .mday.sel { color: var(--text); font-weight: 700; }
  .daybar { display: flex; align-items: center; gap: 9px; flex-wrap: wrap; margin-bottom: 16px; }
  .daybar input[type="date"] {
    background: var(--panel-2); border: 1px solid var(--line); color: var(--text);
    border-radius: 9px; padding: 7px 11px; font: inherit; font-size: 14px;
    color-scheme: dark;
  }
  .daybar input[type="date"]:focus { outline: none; border-color: var(--accent); }
  .dayhead {
    background: var(--panel); border: 1px solid var(--line); border-left: 4px solid var(--blue);
    border-radius: var(--radius); padding: 15px 18px; margin-bottom: 18px;
  }
  .dayhead .lbl {
    color: var(--muted); font-size: 11px; letter-spacing: .09em; text-transform: uppercase;
  }
  .dayhead .big { font-size: 24px; font-weight: 700; margin: 3px 0 9px; }
  .dayhead .facts { display: flex; gap: 8px; flex-wrap: wrap; }
  .hours { display: grid; gap: 6px; }
  .hrow {
    display: grid; grid-template-columns: 132px 54px 1fr; align-items: center; gap: 12px;
    background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
    padding: 8px 13px;
  }
  .hrow.none { opacity: .45; }
  .hrow .when { font-variant-numeric: tabular-nums; font-size: 13px; white-space: nowrap; }
  .hrow .num { font-size: 13px; color: var(--muted); text-align: right; }
  .hrow .num b { color: var(--text); font-size: 15px; }
  .hrow .bar { height: 8px; border-radius: 4px; background: var(--panel-2); overflow: hidden; }
  .hrow .bar i { display: block; height: 100%; background: var(--green); border-radius: 4px; }
  .hrow .who { grid-column: 1 / -1; color: var(--muted); font-size: 12px; }

  /* ---------- personal editor ---------- */
  .namebar { display: flex; align-items: center; gap: 10px; margin-bottom: 16px; flex-wrap: wrap; }
  .namebar input {
    background: var(--panel-2); border: 1px solid var(--line); color: var(--text);
    border-radius: 9px; padding: 8px 12px; font: inherit; font-size: 14px; min-width: 220px;
  }
  .namebar input:focus { outline: none; border-color: var(--accent); }
  .tabs { display: flex; gap: 8px; margin-bottom: 16px; }
  .offday { cursor: pointer; }
  .offday.off { background: var(--rose); color: #2b0710; font-weight: 700; }
  .ed td { cursor: pointer; user-select: none; transition: background .08s; }
  .ed td:hover { border-color: var(--muted); }
  .ed td.on { background: var(--green); color: #06281d; font-weight: 700; border-color: transparent; }
  .ed th.col { cursor: pointer; }
  .ed th.col:hover { color: var(--text); text-decoration: underline; }
  /* ---------- week grid: one column per real date ---------- */
  .weekwrap { overflow-x: auto; }
  .week {
    display: grid; grid-template-columns: 76px repeat(7, minmax(74px, 1fr)); gap: 4px;
    min-width: 620px;
    /* the grid is painted by dragging, so the browser must not treat a drag
       across it as a scroll gesture */
    touch-action: none; user-select: none;
  }
  .wk-h { text-align: center; padding: 2px 0 7px; position: relative; }
  .wk-h .dw { color: var(--muted); font-size: 10.5px; letter-spacing: .07em; }
  .wk-h .dd { font-size: 13.5px; font-weight: 650; }
  .wk-h.today .dw, .wk-h.today .dd { color: var(--accent); }
  .wk-h.pick { cursor: pointer; }
  .wk-h.pick:hover .dd { text-decoration: underline; }
  .wk-reset {
    background: none; border: none; color: var(--blue); cursor: pointer;
    font: inherit; font-size: 11px; padding: 0; line-height: 1;
  }
  .wk-reset:hover { text-decoration: underline; }
  .wk-t {
    color: var(--muted); font-size: 11px; text-align: right; padding-right: 7px;
    line-height: 30px; white-space: nowrap; font-variant-numeric: tabular-nums;
  }
  .wk-c {
    height: 30px; border-radius: 6px; background: var(--panel-2);
    border: 1px solid var(--line); cursor: pointer;
  }
  .wk-c:hover { border-color: var(--muted); }
  /* two tones: the faded one is inherited from the weekly pattern, the solid
     one was set for this date specifically */
  .wk-c.on { border-color: transparent; background: rgba(52,211,153,.42); }
  .wk-c.on.own { background: var(--green); }
  .wk-c.away { background: rgba(251,113,133,.22); border-color: transparent; cursor: not-allowed; }
  .wk-c.past { opacity: .28; pointer-events: none; }
  .wk-c.out { visibility: hidden; pointer-events: none; }
  .key {
    display: inline-block; width: 11px; height: 11px; border-radius: 3px;
    vertical-align: -1px; margin: 0 3px 0 10px;
  }
  .key.k-week { background: rgba(52,211,153,.42); }
  .key.k-date { background: var(--green); }
  .key.k-away { background: rgba(251,113,133,.22); }
  .ovrow {
    display: flex; align-items: center; justify-content: space-between; gap: 12px;
    background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
    padding: 9px 13px; margin-bottom: 7px; flex-wrap: wrap;
  }
  .ovrow.on { border-color: var(--blue); }
  .ovrow b { font-size: 14px; }
  .ovrow .hrs { color: var(--muted); font-size: 12.5px; }

  .savebar { display: flex; align-items: center; gap: 11px; margin-top: 18px; flex-wrap: wrap; }
  .savebar .hint { margin: 0; }
  h3.sec { font-size: 13px; color: var(--muted); text-transform: uppercase;
           letter-spacing: .08em; margin: 26px 0 11px; }
  h3.sec:first-child { margin-top: 0; }

  @media (max-width: 820px) {
    .app { grid-template-columns: 1fr; }
    .side { display: none; }
    .cell { min-height: 82px; }
  }
</style>
</head>
<body>
<div class="app">
  <aside class="side">
    <div class="brand">
      <div class="dot">🤖</div>
      <div><h1>Meeting Organizer</h1><span id="tz">—</span></div>
    </div>

    <div class="sect">
      <div class="label">Views</div>
      <button class="nav" data-view="me" id="nav-me" hidden><span class="ic">✏️</span> My availability</button>
      <button class="nav on" data-view="cal"><span class="ic">📅</span> Calendar</button>
      <button class="nav" data-view="day"><span class="ic">🕐</span> Selected day</button>
      <button class="nav" data-view="season"><span class="ic">🗓️</span> Season</button>
      <button class="nav" data-view="heat"><span class="ic">📊</span> Weekly pattern</button>
      <button class="nav" data-view="best"><span class="ic">⭐</span> Best times</button>
      <button class="nav" data-view="team"><span class="ic">👥</span> Team</button>
    </div>

    <div class="sect">
      <div class="label">At a glance</div>
      <div class="stats">
        <div class="stat"><b id="s-people">0</b><span>members submitted</span></div>
        <div class="stat"><b id="s-events">0</b><span>upcoming events</span></div>
        <div class="stat"><b id="s-best">—</b><span id="s-best-sub">best slot</span></div>
      </div>
    </div>
  </aside>

  <main class="main">
    <div class="top">
      <h2 id="title">Loading…</h2>
      <button class="btn" id="prev">‹</button>
      <button class="btn pri" id="today">Today</button>
      <button class="btn" id="next">›</button>
      <div class="spacer"></div>
      <div class="live"><span class="pulse"></span><span id="upd">connecting…</span></div>
    </div>

    <div class="scroll">
      <section class="view on" id="v-cal">
        <div class="dow"><div>Mon</div><div>Tue</div><div>Wed</div><div>Thu</div>
                         <div>Fri</div><div>Sat</div><div>Sun</div></div>
        <div class="grid" id="grid"></div>
      </section>
      <section class="view" id="v-day">
        <div class="daybar">
          <button class="btn" id="d-prev">‹ Previous day</button>
          <label for="datepick" class="hint" style="margin:0">Date</label>
          <input type="date" id="datepick">
          <button class="btn pri" id="d-today">Today</button>
          <button class="btn" id="d-next">Next day ›</button>
        </div>
        <div id="daybody"></div>
      </section>
      <section class="view" id="v-season"><div class="months" id="months"></div></section>
      <section class="view" id="v-me">
        <div class="namebar">
          <label for="myname">Display name</label>
          <input id="myname" maxlength="32" placeholder="Your name">
        </div>
        <div class="tabs">
          <button class="btn pri" id="tab-week">Weekly hours</button>
          <button class="btn" id="tab-dates">Specific dates</button>
          <button class="btn" id="tab-off">Days I'm away</button>
        </div>
        <p class="hint" id="me-hint"></p>
        <div id="pane-week"><div class="heatwrap"><table class="hm ed" id="ed"></table></div></div>
        <div id="pane-dates" hidden>
          <div class="daybar">
            <button class="btn" id="wk-prev">‹ Previous week</button>
            <button class="btn pri" id="wk-today">This week</button>
            <button class="btn" id="wk-next">Next week ›</button>
            <span id="wk-range" class="hint" style="margin:0"></span>
          </div>
          <p class="hint" id="wk-legend">
            <span class="key k-week"></span> from your weekly hours
            <span class="key k-date"></span> set for this date
            <span class="key k-away"></span> away all day
            — click or drag to paint, click a date to fill the whole day.
          </p>
          <div class="weekwrap"><div class="week" id="week"></div></div>
          <h3 class="sec">Dates with their own hours</h3>
          <div id="ov-list"></div>
        </div>
        <div id="pane-off" hidden><div class="months" id="offmonths"></div></div>
        <div class="savebar">
          <button class="btn pri" id="save">Save</button>
          <button class="btn" id="clearall">Clear weekly hours</button>
          <span id="savemsg" class="hint"></span>
        </div>
      </section>
      <section class="view" id="v-heat">
        <p class="hint" id="heat-hint"></p>
        <div class="heatwrap"><table class="hm" id="hm"></table></div>
      </section>
      <section class="view" id="v-best"><div id="best"></div></section>
      <section class="view" id="v-team"><div class="cards" id="team"></div></section>
    </div>
  </main>
</div>

<script>
const $ = (id) => document.getElementById(id);
const ACCENTS = ["#f5c518", "#a855f7", "#38bdf8", "#34d399", "#fb7185", "#fb923c"];
const MONTHS = ["January","February","March","April","May","June","July",
                "August","September","October","November","December"];
let state = null;
let cursor = new Date();   // which month the calendar is showing
let view = "cal";

const hue = (s) => ACCENTS[Math.abs([...String(s)].reduce((a,c)=>a+c.charCodeAt(0),0)) % ACCENTS.length];
const clock = (iso) => new Date(iso).toLocaleTimeString([], {hour:"numeric", minute:"2-digit"});
const ymd = (d) => `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;

// Noon keeps the parse away from midnight, where a DST shift could roll a
// date-only string back into the previous day.
const atNoon = (key) => new Date(key + "T12:00:00");
// "Monday, August 10, 2026" -- the whole point of the date selector.
const fullLabel = (key) => atNoon(key).toLocaleDateString([],
  {weekday:"long", month:"long", day:"numeric", year:"numeric"});

const VIEWS = ["cal", "day", "season", "heat", "best", "team", "me"];

// The date the whole dashboard is focused on. Free hours, the day view and the
// calendar highlight all follow it, and it survives a refresh via the hash.
let selected = null;
let dayInfo = null;
let dayReq = 0;   // guards against a slow response for an older date landing last
// Present only on a personal edit link; the plain public URL has no key and
// therefore never gets the editor.
const KEY = new URLSearchParams(location.search).get("key");
let mine = null;
let mePane = "week";
let wkStart = null;  // Monday of the week the date grid is showing
let paintTo = null;  // during a drag: the value being painted into each cell

function setView(v, push) {
  view = VIEWS.includes(v) ? v : "cal";
  document.querySelectorAll(".nav").forEach(n => n.classList.toggle("on", n.dataset.view === view));
  document.querySelectorAll(".view").forEach(s => s.classList.toggle("on", s.id === "v-" + view));
  const stepper = view === "cal" || view === "day";
  ["prev","today","next"].forEach(id => $(id).style.display = stepper ? "" : "none");
  if (push) location.hash = view;   // deep-linkable, and survives a refresh
  render();
}

// The three top-bar buttons move whichever unit the current view is about.
function step(delta) {
  if (view === "day") shiftSelected(delta);
  else cursor = new Date(cursor.getFullYear(), cursor.getMonth() + delta, 1);
  render();
}

function jumpToday() {
  if (view === "day") setSelected(state ? state.today : iso(new Date()));
  else { cursor = new Date(); render(); }
}

function render() {
  if (!state) return;
  $("tz").textContent = state.timezone;
  $("s-people").textContent = state.totalPeople;
  $("s-events").textContent = state.events.length;
  const top = state.best[0];
  $("s-best").textContent = top ? `${top.count}/${state.totalPeople}` : "—";
  $("s-best-sub").textContent = top
    ? `${atNoon(top.date).toLocaleDateString([], {weekday:"short", month:"short", day:"numeric"})} · ${top.label}`
    : "no data yet";

  if (view === "day") renderDay();
  else if (view === "cal") renderMonth();
  else if (view === "season") renderSeason();
  else if (view === "heat") renderHeat();
  else if (view === "best") renderBest();
  else if (view === "me") renderMe();
  else renderTeam();
}

const iso = (d) => `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,"0")}-${String(d.getDate()).padStart(2,"0")}`;

// ---------- selected date ----------
function setSelected(key, {show = false} = {}) {
  if (!key) return;
  const changed = key !== selected;
  selected = key;
  if ($("datepick").value !== key) $("datepick").value = key;
  if (changed) { dayInfo = null; loadDay(); }
  if (show) setView("day", true); else render();
}

function shiftSelected(days) {
  const d = atNoon(selected || state.today);
  d.setDate(d.getDate() + days);
  setSelected(iso(d));
}

// Free hours are asked for by exact date, so two Mondays with different
// away-lists give different answers. Re-run on every poll as well, so the
// numbers on screen stay live without the user touching anything.
async function loadDay() {
  if (!selected) return;
  const token = ++dayReq;
  let next;
  try {
    const r = await fetch("/api/day?date=" + encodeURIComponent(selected), {cache: "no-store"});
    if (!r.ok) throw new Error(r.status);
    next = await r.json();
  } catch (e) {
    next = {date: selected, error: "could not load that date"};
  }
  if (token !== dayReq) return;    // a newer date was picked while we waited
  dayInfo = next;
  if (view === "day") render();
}

function eventsOn(key) {
  return state.events.filter(e => iso(new Date(e.start)) === key);
}

function renderDay() {
  const key = selected || state.today;
  $("title").textContent = fullLabel(key);
  const info = dayInfo && dayInfo.date === key ? dayInfo : null;

  let html = `<div class="dayhead">
    <div class="lbl">Selected date</div>
    <div class="big">${esc(info ? info.label : fullLabel(key))}</div>
    <div class="facts">
      <span class="pill">${key}</span>
      ${key === state.today ? `<span class="pill">Today</span>` : ""}
      ${info ? `<span class="pill">${info.peak}/${info.totalPeople} free at best</span>
                <span class="pill">${info.freeHours} free hour${info.freeHours === 1 ? "" : "s"}</span>` : ""}
      ${info && info.away.length ? `<span class="pill">${info.away.length} away</span>` : ""}
      ${info && (info.custom || []).length ? `<span class="pill">${info.custom.length} on date-specific hours</span>` : ""}
    </div>
    ${KEY && key >= state.today && key <= state.seasonEnd ? `<div style="margin-top:11px">
      <button class="btn" data-edit-date="${key}">Set my hours for this date</button></div>` : ""}
  </div>`;

  if (!info) {
    html += `<div class="empty">${dayInfo && dayInfo.error ? esc(dayInfo.error) : "Loading that date…"}</div>`;
    $("daybody").innerHTML = html;
    return;
  }

  const evs = eventsOn(key);
  if (evs.length) {
    html += `<h3 class="sec">Events on this date (${evs.length})</h3><div class="cards">` +
      evs.map(e => `<div class="card" style="border-left-color:${hue(e.name)}">
        <h4>${esc(e.name)}</h4>
        <div class="meta">${clock(e.start)} – ${clock(e.end)} · 👥 ${e.count}</div>
        ${e.location ? `<div class="who">📍 ${esc(e.location)}</div>` : ""}
      </div>`).join("") + `</div>`;
  }

  html += `<h3 class="sec">Free hours on ${esc(info.label)}</h3>`;
  if (!info.peak) {
    html += `<div class="empty">Nobody is free on this date.</div>`;
  } else {
    html += `<div class="hours">` + info.hours.map(h => {
      const pct = info.totalPeople ? Math.round(h.count / info.totalPeople * 100) : 0;
      return `<div class="hrow${h.count ? "" : " none"}">
        <div class="when">${h.start} – ${h.end}</div>
        <div class="num"><b>${h.count}</b>/${info.totalPeople}</div>
        <div class="bar"><i style="width:${pct}%"></i></div>
        ${h.count ? `<div class="who">${esc(h.people.join(", "))}</div>` : ""}
      </div>`;
    }).join("") + `</div>`;

    html += `<h3 class="sec">Longest windows</h3><div class="cards">` +
      info.windows.map(w => `<div class="bcard">
        <div class="bhead">
          <div>
            <div class="bwhen">${w.startLabel} – ${w.endLabel}</div>
            <div class="btime">${w.span} hour window</div>
          </div>
          <div class="bcount"><b>${w.count}<span>/${info.totalPeople}</span></b><span>free</span></div>
        </div>
        <div class="bfoot"><span class="who">${esc(w.people.join(", "))}</span></div>
      </div>`).join("") + `</div>`;
  }

  if ((info.custom || []).length) {
    html += `<h3 class="sec">Hours set just for this date (${info.custom.length})</h3>
             <p class="hint">${esc(info.custom.join(", "))} — these people set hours for this
             date specifically, so it differs from their usual ${esc(info.weekday)}.</p>`;
  }
  if (info.away.length) {
    html += `<h3 class="sec">Away this date (${info.away.length})</h3>
             <p class="hint">${esc(info.away.join(", "))}</p>`;
  }
  $("daybody").innerHTML = html;
}

// One month block, shared by the season overview and the days-off picker.
function monthBlock(y, m, cell) {
  const first = new Date(y, m, 1);
  const lead = (first.getDay() + 6) % 7;
  const days = new Date(y, m + 1, 0).getDate();
  let g = ["M","T","W","T","F","S","S"].map(d => `<div class="dh">${d}</div>`).join("");
  for (let i = 0; i < lead; i++) g += `<div class="mday pad"></div>`;
  for (let d = 1; d <= days; d++) g += cell(new Date(y, m, d), d);
  return `<div class="mon"><h4>${MONTHS[m]} ${y}</h4><div class="mgrid">${g}</div></div>`;
}

// Every month from today through the end of the season.
function seasonMonths() {
  const start = new Date(state.today + "T12:00:00");
  const end = new Date(state.seasonEnd + "T12:00:00");
  const out = [];
  let y = start.getFullYear(), m = start.getMonth();
  while (y < end.getFullYear() || (y === end.getFullYear() && m <= end.getMonth())) {
    out.push([y, m]);
    if (++m > 11) { m = 0; y++; }
  }
  return out;
}

function renderSeason() {
  $("title").textContent = "Season at a glance";
  const totals = state.dayTotals || {};
  const peak = Math.max(1, ...Object.values(totals).map(t => t.peak));
  const evDays = new Set(state.events.map(e => iso(new Date(e.start))));
  $("months").innerHTML = seasonMonths().map(([y, m]) => monthBlock(y, m, (date, d) => {
    const key = iso(date);
    const t = totals[key];
    const cls = ["mday", "pick"];
    if (key === state.today) cls.push("today");
    if (key === selected) cls.push("sel");
    if (t) cls.push("has");
    if (evDays.has(key)) cls.push("ev");
    const bg = t ? `background:rgba(52,211,153,${(0.18 + 0.72 * t.peak / peak).toFixed(2)});` : "";
    const tip = t ? `${t.peak}/${state.totalPeople} free · ${t.label}` : "nobody free";
    return `<div class="${cls.join(" ")}" data-date="${key}" style="${bg}"
      title="${fullLabel(key)} — ${tip}">${d}</div>`;
  })).join("");
}

async function loadMine() {
  if (!KEY) return;
  $("nav-me").hidden = false;
  try {
    const r = await fetch("/api/me?key=" + encodeURIComponent(KEY), {cache: "no-store"});
    mine = r.ok ? await r.json() : {error: (await r.json().catch(() => ({}))).error || "link not accepted"};
  } catch (e) {
    mine = {error: "could not reach the bot"};
  }
  if (mine && !mine.error) {
    mine.avail = mine.avail || {};
    mine.dates = mine.dates || {};
    if (!wkStart) wkStart = mondayOf(mine.today || state?.today || iso(new Date()));
  }
}

function renderMe() {
  if (!mine) { $("title").textContent = "My availability"; return; }
  if (mine.error) {
    $("title").textContent = "My availability";
    $("me-hint").textContent = mine.error + " — run /dashboard in Discord for a fresh link.";
    $("ed").innerHTML = "";
    $("save").style.display = $("clearall").style.display = "none";
    return;
  }
  $("title").textContent = "Editing " + mine.name;
  if ($("myname") !== document.activeElement) $("myname").value = mine.name === "Unknown" ? "" : mine.name;
  renderOff();
  renderWeek();
  $("me-hint").textContent = {
    off: "Your weekly hours apply every week. Click any date you're away and you'll be left out of that day only.",
    dates: "Pick your free times on real dates — free 5-8 this Tuesday but 3-4 the next. Whatever you paint here replaces your weekly hours on that date only.",
    week: "Click any slot to mark yourself free. Click a day name to toggle the whole column.",
  }[mePane];
  if (mePane !== "week") return;
  let html = "<tr><th></th>" +
    state.days.map(d => `<th class="col" data-day="${d}">${d.slice(0,3)}</th>`).join("") + "</tr>";
  state.hourLabels.forEach((label, h) => {
    html += `<tr><th class="row">${label}</th>`;
    state.days.forEach(day => {
      const on = (mine.avail[day] || []).includes(h);
      html += `<td class="${on ? "on" : ""}" data-day="${day}" data-h="${h}">${on ? "✓" : ""}</td>`;
    });
    html += "</tr>";
  });
  $("ed").innerHTML = html;
}

function renderOff() {
  if (!state) return;
  const off = new Set(mine.off || []);
  $("offmonths").innerHTML = seasonMonths().map(([y, m]) => monthBlock(y, m, (date, d) => {
    const key = iso(date);
    const past = key < state.today;
    const cls = ["mday", "offday"];
    if (off.has(key)) cls.push("off");
    if (key === state.today) cls.push("today");
    return `<div class="${cls.join(" ")}" data-date="${key}"
      style="${past ? "opacity:.25;pointer-events:none" : ""}" title="${key}">${d}</div>`;
  })).join("");
}

function toggleOff(key) {
  mine.off = mine.off || [];
  const i = mine.off.indexOf(key);
  if (i === -1) mine.off.push(key); else mine.off.splice(i, 1);
  mine.off.sort();
  renderOff();
}

// ---------- date-specific hours: a week grid of real dates ----------
// mine.dates[key] replaces the weekly pattern on that one date. An empty array
// is a real value ("free for none of it"), so absence is the only way to say
// "just use my weekly hours" -- hence the delete in clearOverride.
// The tab's controls stay in the DOM even when the link didn't check out, so
// every entry point checks that there is actually a record to edit.
const editable = () => !!(state && mine && !mine.error && mine.dates && wkStart);
const weeklyHoursFor = (key) => mine.avail[state.days[(atNoon(key).getDay() + 6) % 7]] || [];
// What this date resolves to today: its own hours if it has them, else weekly.
const effectiveHours = (key) =>
  mine.dates[key] !== undefined ? mine.dates[key] : weeklyHoursFor(key);
const isAway = (key) => (mine.off || []).includes(key);
const editableDate = (key) => key >= state.today && key <= state.seasonEnd;

// Monday of the week containing `key`.
function mondayOf(key) {
  const d = atNoon(key);
  d.setDate(d.getDate() - ((d.getDay() + 6) % 7));
  return iso(d);
}

function weekKeys() {
  const out = [];
  const d = atNoon(wkStart);
  for (let i = 0; i < 7; i++) { out.push(iso(d)); d.setDate(d.getDate() + 1); }
  return out;
}

function renderWeek() {
  if (!editable() || mePane !== "dates") return;
  // A background poll re-renders every 15s; doing that mid-drag would swap out
  // the nodes the drag is riding on. The pointerup handler re-renders anyway.
  if (paintTo !== null) return;
  const keys = weekKeys();
  const first = atNoon(keys[0]), last = atNoon(keys[6]);
  $("wk-range").textContent =
    `${first.toLocaleDateString([], {month:"long", day:"numeric"})} – ` +
    `${last.toLocaleDateString([], {month:"long", day:"numeric", year:"numeric"})}`;

  let html = `<div></div>`;   // empty corner above the time gutter
  keys.forEach(key => {
    const d = atNoon(key);
    const own = mine.dates[key] !== undefined;
    const usable = editableDate(key);
    html += `<div class="wk-h${key === state.today ? " today" : ""}${usable ? " pick" : ""}"
      ${usable ? `data-col="${key}"` : ""} title="${usable ? "Fill or clear " + fullLabel(key) : fullLabel(key)}">
      <div class="dw">${d.toLocaleDateString([], {weekday:"short"}).toUpperCase()}</div>
      <div class="dd">${d.toLocaleDateString([], {month:"short", day:"numeric"})}</div>
      ${own && usable ? `<button class="wk-reset" data-reset="${key}"
        title="Go back to my weekly hours on this date">↺ weekly</button>` : ""}
    </div>`;
  });

  state.hourLabels.forEach((label, h) => {
    html += `<div class="wk-t">${(state.hourStarts || [])[h] || label.split("-")[0]}</div>`;
    keys.forEach(key => {
      const cls = ["wk-c"];
      if (key > state.seasonEnd) cls.push("out");
      else if (!editableDate(key)) cls.push("past");
      if (isAway(key)) cls.push("away");
      else {
        if (effectiveHours(key).includes(h)) cls.push("on");
        if (mine.dates[key] !== undefined) cls.push("own");
      }
      html += `<div class="${cls.join(" ")}" data-key="${key}" data-h="${h}"></div>`;
    });
  });
  $("week").innerHTML = html;
  renderOvList();
}

function renderOvList() {
  const keys = Object.keys(mine.dates).sort();
  const shown = weekKeys();
  $("ov-list").innerHTML = keys.length ? keys.map(k => {
    const list = mine.dates[k];
    const text = list.length ? list.map(h => state.hourLabels[h]).join(", ") : "free for none of it";
    return `<div class="ovrow${shown.includes(k) ? " on" : ""}">
      <div><b>${esc(fullLabel(k))}</b><div class="hrs">${esc(text)}</div></div>
      <div>
        <button class="btn" data-open="${k}">Show week</button>
        <button class="btn" data-drop="${k}">Use weekly</button>
      </div>
    </div>`;
  }).join("") : `<p class="hint">No date-specific hours yet — paint any date above to give it its own.</p>`;
}

function showWeekOf(key) {
  if (!key) return;
  wkStart = mondayOf(key);
  renderWeek();
}

function shiftWeek(weeks) {
  if (!editable()) return;
  const d = atNoon(wkStart);
  d.setDate(d.getDate() + weeks * 7);
  // Stay inside the season: past weeks aren't editable and weeks beyond the end
  // would just be a blank grid.
  const key = iso(d);
  const lo = mondayOf(state.today), hi = mondayOf(state.seasonEnd);
  wkStart = key < lo ? lo : key > hi ? hi : key;
  renderWeek();
}

// Painting writes an override for the touched date, seeded from whatever that
// date currently resolves to -- so adjusting one Tuesday never means re-entering
// the whole day. Returns true when something actually changed.
function setCell(key, h, on) {
  if (!editable() || !editableDate(key) || isAway(key)) return false;
  const list = effectiveHours(key).slice();
  const i = list.indexOf(h);
  if (on && i === -1) list.push(h);
  else if (!on && i !== -1) list.splice(i, 1);
  else if (mine.dates[key] !== undefined) return false;   // already an override, no change
  list.sort((a, b) => a - b);
  mine.dates[key] = list;
  return true;
}

function toggleColumn(key) {
  if (!editable() || !editableDate(key) || isAway(key)) return;
  const full = effectiveHours(key).length === state.hourLabels.length;
  mine.dates[key] = full ? [] : state.hourLabels.map((_, i) => i);
  renderWeek();
}

function clearOverride(key) {
  if (!editable()) return;
  delete mine.dates[key];
  renderWeek();
}

function setPane(which) {
  mePane = which;
  $("pane-week").hidden = which !== "week";
  $("pane-dates").hidden = which !== "dates";
  $("pane-off").hidden = which !== "off";
  $("tab-week").className = "btn" + (which === "week" ? " pri" : "");
  $("tab-dates").className = "btn" + (which === "dates" ? " pri" : "");
  $("tab-off").className = "btn" + (which === "off" ? " pri" : "");
  renderMe();
}

function toggleSlot(day, h) {
  const list = mine.avail[day] || (mine.avail[day] = []);
  const i = list.indexOf(h);
  if (i === -1) list.push(h); else list.splice(i, 1);
  list.sort((a, b) => a - b);
  if (!list.length) delete mine.avail[day];
  renderMe();
}

function toggleDay(day) {
  const full = (mine.avail[day] || []).length === state.hourLabels.length;
  if (full) delete mine.avail[day];
  else mine.avail[day] = state.hourLabels.map((_, i) => i);
  renderMe();
}

async function saveMine() {
  $("savemsg").textContent = "Saving…";
  try {
    const r = await fetch("/api/me", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        key: KEY,
        avail: mine.avail,
        dates: mine.dates || {},
        off: mine.off || [],
        name: $("myname").value.trim() || undefined,
      }),
    });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.status);
    const saved = await r.json();
    mine.name = saved.name;
    // Adopt what the server actually stored -- it drops past dates and anything
    // out of season, so the editor should stop showing them as saved.
    mine.dates = saved.dates || {};
    mine.off = saved.off || [];
    $("savemsg").textContent = "Saved ✓";
    await poll();                       // pull the team-wide numbers back in
    setTimeout(() => $("savemsg").textContent = "", 2500);
  } catch (e) {
    $("savemsg").textContent = "Could not save: " + e.message;
  }
}

function renderMonth() {
  $("title").textContent = `${MONTHS[cursor.getMonth()]} ${cursor.getFullYear()}`;
  const y = cursor.getFullYear(), m = cursor.getMonth();
  const first = new Date(y, m, 1);
  const lead = (first.getDay() + 6) % 7;           // Monday-first
  const days = new Date(y, m + 1, 0).getDate();
  const todayKey = ymd(new Date());

  // events bucketed by calendar day
  const byDay = {};
  state.events.forEach(e => {
    const d = new Date(e.start);
    if (d.getFullYear() === y && d.getMonth() === m) (byDay[d.getDate()] ||= []).push(e);
  });
  // Availability per *date*, not per weekday, so a day someone marked
  // themselves away on reads differently from the same weekday next week.
  const totals = state.dayTotals || {};
  const maxCount = Math.max(1, ...Object.values(totals).map(t => t.peak));

  let html = "";
  for (let i = 0; i < lead; i++) html += `<div class="cell pad"></div>`;
  for (let d = 1; d <= days; d++) {
    const date = new Date(y, m, d);
    const key = iso(date);
    const evs = byDay[d] || [];
    const p = totals[key];
    const isToday = ymd(date) === todayKey;

    let inner = `<div class="n">${d}</div>`;
    evs.slice(0, 2).forEach(e => {
      inner += `<div class="chip" style="border-left-color:${hue(e.name)}">` +
               `${esc(e.name)}<time>${clock(e.start)}–${clock(e.end)} · 👥 ${e.count}</time></div>`;
    });
    if (evs.length > 2) inner += `<div class="more">+${evs.length - 2} more</div>`;
    if (!evs.length && p) {
      inner += `<div class="chip p">${p.peak}/${state.totalPeople} free<time>${p.label}</time></div>`;
    }
    if (p) {
      inner += `<div class="heat" style="width:${Math.round(p.peak / maxCount * 100)}%;` +
               `opacity:${0.25 + 0.65 * (p.peak / maxCount)}"></div>`;
    }
    html += `<div class="cell${isToday ? " today" : ""}${key === selected ? " sel" : ""}"
      data-date="${key}" title="${fullLabel(key)}">${inner}</div>`;
  }
  $("grid").innerHTML = html;
}

function renderHeat() {
  // Deliberately weekday-shaped, and labelled as such: the numbers here are the
  // standing weekly pattern, not what a particular date works out to.
  $("title").textContent = "Weekly pattern";
  $("heat-hint").innerHTML =
    `The standing weekly arrangement — it ignores days people marked themselves away. ` +
    `For one exact date, open <b>Selected day</b>` +
    (selected ? ` (currently ${esc(fullLabel(selected))})` : "") + `.`;
  const max = Math.max(1, ...Object.values(state.counts).flatMap(o => Object.values(o)));
  let html = "<tr><th></th>" + state.days.map(d => `<th>${d.slice(0,3)}</th>`).join("") + "</tr>";
  state.hourLabels.forEach((label, h) => {
    html += `<tr><th class="row">${label}</th>`;
    state.days.forEach(day => {
      const n = (state.counts[day] || {})[h] || 0;
      const isBest = state.best.length && state.best[0].day === day && state.best[0].hour === h;
      const bg = n ? `background:rgba(52,211,153,${(0.14 + 0.72 * n / max).toFixed(2)});` : "";
      html += `<td class="${n ? "" : "z"}${isBest ? " best" : ""}" style="${bg}">${n || "·"}</td>`;
    });
    html += "</tr>";
  });
  $("hm").innerHTML = html;
}

function renderBest() {
  $("title").textContent = "Best meeting times";
  if (!state.best.length) {
    $("best").innerHTML = `<div class="empty">No availability submitted yet — open the <b>My availability</b> tab to add yours.</div>`;
    return;
  }
  const top = state.best[0].count;
  // Everyone-can-make-it windows are the answer; the rest are fallbacks, so
  // they get a quieter treatment instead of competing for attention.
  const full = state.best.filter(b => b.count === top);
  const rest = state.best.filter(b => b.count !== top);

  const card = (b, i) => {
    const pct = Math.round(b.count / state.totalPeople * 100);
    return `
    <div class="bcard${i === 0 ? " win" : ""}${b.date === selected ? " sel" : ""}"
         data-date="${b.date}" title="Open ${fullLabel(b.date)}" style="cursor:pointer">
      <div class="bhead">
        <div>
          <div class="bwhen">${fullLabel(b.date)}</div>
          <div class="btime">${b.label}</div>
        </div>
        <div class="bcount"><b>${b.count}<span>/${state.totalPeople}</span></b><span>free</span></div>
      </div>
      <div class="bbar"><i style="width:${pct}%"></i></div>
      <div class="bfoot">
        <span class="pill">${b.span}h window</span>
        <span class="who">${esc(b.people.join(", "))}</span>
      </div>
    </div>`;
  };

  $("best").innerHTML =
    `<h3 class="sec">Everyone available (${full.length})</h3>
     <div class="cards">${full.map(card).join("")}</div>` +
    (rest.length ? `<h3 class="sec">Partial turnout</h3>
     <div class="cards dim">${rest.map(b => card(b, -1)).join("")}</div>` : "");
}

function renderTeam() {
  $("title").textContent = "Team";
  $("team").innerHTML = state.people.length ? state.people.map(p => {
    const rows = state.days.filter(d => p.avail[d]).map(d =>
      `<div>${d} — ${p.avail[d].map(h => state.hourLabels[h]).join(", ")}</div>`).join("");
    const dates = Object.keys(p.dates || {}).sort();
    const extra = dates.length
      ? `<div style="margin-top:6px">${dates.length} date${dates.length === 1 ? "" : "s"} with
         their own hours: ${esc(dates.map(k => fullLabel(k)).join("; "))}</div>`
      : "";
    return `<div class="card" style="border-left-color:${hue(p.name)}">
      <h4>${esc(p.name)}</h4><div class="who">${rows || "no weekly hours saved"}${extra}</div></div>`;
  }).join("") : `<div class="empty">Nobody has submitted availability yet.</div>`;
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, c =>
    ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}

async function poll() {
  try {
    const r = await fetch("/api/state", {cache: "no-store"});
    if (!r.ok) throw new Error(r.status);
    state = await r.json();
    // Default to today, but never move a date the user has chosen.
    if (!selected) { selected = state.today; $("datepick").value = selected; }
    $("upd").textContent = "updated " + new Date().toLocaleTimeString([], {hour:"numeric", minute:"2-digit"});
    render();
    loadDay();     // keep the selected date's free hours as live as everything else
  } catch (e) {
    $("upd").textContent = "bot offline — retrying";
  }
}

document.querySelectorAll(".nav").forEach(n => n.onclick = () => setView(n.dataset.view, true));
$("prev").onclick = () => step(-1);
$("next").onclick = () => step(1);
$("today").onclick = jumpToday;
addEventListener("hashchange", () => setView(location.hash.slice(1), false));

// Any date anywhere in the dashboard is a way to select that date.
$("grid").onclick = (e) => {
  const cell = e.target.closest(".cell[data-date]");
  if (cell) setSelected(cell.dataset.date, {show: true});
};
$("months").onclick = (e) => {
  const cell = e.target.closest(".mday[data-date]");
  if (cell) setSelected(cell.dataset.date, {show: true});
};
$("best").onclick = (e) => {
  const cell = e.target.closest(".bcard[data-date]");
  if (cell) setSelected(cell.dataset.date, {show: true});
};
// Delegated so the button works even while the day's numbers are still loading.
$("daybody").onclick = (e) => {
  const jump = e.target.closest("[data-edit-date]");
  if (!jump) return;
  showWeekOf(jump.dataset.editDate);
  setPane("dates");
  setView("me", true);
};
$("datepick").onchange = () => setSelected($("datepick").value);
$("d-prev").onclick = () => shiftSelected(-1);
$("d-next").onclick = () => shiftSelected(1);
$("d-today").onclick = () => setSelected(state ? state.today : iso(new Date()));

$("ed").onclick = (e) => {
  const cell = e.target.closest("td[data-day]");
  if (cell) return toggleSlot(cell.dataset.day, +cell.dataset.h);
  const col = e.target.closest("th.col");
  if (col) toggleDay(col.dataset.day);
};
$("offmonths").onclick = (e) => {
  const cell = e.target.closest(".offday");
  if (cell) toggleOff(cell.dataset.date);
};
$("tab-week").onclick = () => setPane("week");
$("tab-dates").onclick = () => setPane("dates");
$("tab-off").onclick = () => setPane("off");
// Drag-painting. Cells are repainted in place during the drag rather than
// re-rendering the grid, because replacing the nodes mid-drag would break the
// pointerover stream the drag depends on.
function paintCell(cell) {
  if (paintTo === null || !cell) return;
  const {key, h} = cell.dataset;
  if (!setCell(key, +h, paintTo)) return;
  cell.classList.toggle("on", paintTo);
  cell.classList.add("own");
}

$("week").addEventListener("pointerdown", (e) => {
  const reset = e.target.closest("[data-reset]");
  if (reset) return clearOverride(reset.dataset.reset);
  const head = e.target.closest(".wk-h[data-col]");
  if (head) return toggleColumn(head.dataset.col);
  const cell = e.target.closest(".wk-c[data-key]");
  if (!cell || cell.classList.contains("away")) return;
  e.preventDefault();
  // Touch implicitly captures the pointer to the first element, which would
  // stop every other cell from seeing the drag.
  try { cell.releasePointerCapture(e.pointerId); } catch (err) {}
  paintTo = !cell.classList.contains("on");
  paintCell(cell);
});
$("week").addEventListener("pointerover", (e) => {
  if (paintTo !== null) paintCell(e.target.closest(".wk-c[data-key]"));
});
// Ends the drag wherever it finishes, including outside the grid or the window.
const endPaint = () => {
  if (paintTo === null) return;
  paintTo = null;
  renderWeek();     // refresh the headers' ↺ markers and the summary list
};
addEventListener("pointerup", endPaint);
addEventListener("pointercancel", endPaint);

$("ov-list").onclick = (e) => {
  const open = e.target.closest("[data-open]");
  if (open) return showWeekOf(open.dataset.open);
  const drop = e.target.closest("[data-drop]");
  if (drop) clearOverride(drop.dataset.drop);
};
$("wk-prev").onclick = () => shiftWeek(-1);
$("wk-next").onclick = () => shiftWeek(1);
$("wk-today").onclick = () => showWeekOf(state.today);
$("save").onclick = saveMine;
$("clearall").onclick = () => { mine.avail = {}; renderMe(); };

(async () => {
  await loadMine();
  // Land straight on the editor when someone opens their personal link.
  setView(location.hash.slice(1) || (KEY ? "me" : "cal"), false);
  await poll();
  setInterval(poll, 15000);
})();
</script>
</body>
</html>
"""


async def start_dashboard(
    get_state: StateProvider,
    host: str,
    port: int,
    get_user: Callable[[str], Awaitable[dict | None]] | None = None,
    save_user: Callable[[str, dict, list, str | None, dict], Awaitable[dict | None]] | None = None,
    get_day: Callable[[str], Awaitable[dict | None]] | None = None,
) -> web.AppRunner | None:
    """Serve the dashboard on the running event loop. Returns None if it can't bind.

    get_user/save_user back the personal editor. Both take an edit key and
    return None when it doesn't check out, which the routes turn into a 403 --
    the public link can read everything but can't write anything.

    get_day answers the date selector: free hours for one specific date, which
    is too much data to ship for every date in the season up front.
    """
    app = web.Application()

    async def page(_request: web.Request) -> web.Response:
        return web.Response(text=PAGE, content_type="text/html")

    async def health(_request: web.Request) -> web.Response:
        """Cheap liveness probe for an external uptime monitor.

        Deliberately touches nothing -- no data load, no Discord calls -- so a
        ping every few minutes costs essentially nothing and can't fail because
        some other part of the bot is unhappy.
        """
        return web.Response(text="ok", content_type="text/plain")

    async def state(_request: web.Request) -> web.Response:
        try:
            return web.json_response(await get_state())
        except Exception:
            log.exception("Failed to build dashboard state")
            return web.json_response({"error": "internal"}, status=500)

    async def day(request: web.Request) -> web.Response:
        if get_day is None:
            return web.json_response({"error": "unavailable"}, status=404)
        try:
            result = await get_day(request.query.get("date", ""))
        except Exception:
            log.exception("Failed to build the day view")
            return web.json_response({"error": "internal"}, status=500)
        if result is None:
            return web.json_response({"error": "bad date"}, status=400)
        return web.json_response(result)

    async def me_get(request: web.Request) -> web.Response:
        if get_user is None:
            return web.json_response({"error": "editing disabled"}, status=404)
        try:
            result = await get_user(request.query.get("key", ""))
        except Exception:
            log.exception("Failed to read personal availability")
            return web.json_response({"error": "internal"}, status=500)
        if result is None:
            return web.json_response({"error": "invalid or expired link"}, status=403)
        return web.json_response(result)

    async def me_post(request: web.Request) -> web.Response:
        if save_user is None:
            return web.json_response({"error": "editing disabled"}, status=404)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "malformed request"}, status=400)
        if not isinstance(body, dict) or not isinstance(body.get("avail"), dict):
            return web.json_response({"error": "malformed request"}, status=400)
        dates = body.get("dates")
        if dates is not None and not isinstance(dates, dict):
            return web.json_response({"error": "malformed request"}, status=400)
        try:
            result = await save_user(
                str(body.get("key", "")),
                body["avail"],
                body.get("off") or [],
                body.get("name"),
                dates or {},
            )
        except Exception:
            log.exception("Failed to save personal availability")
            return web.json_response({"error": "could not save"}, status=500)
        if result is None:
            return web.json_response({"error": "invalid or expired link"}, status=403)
        return web.json_response(result)

    app.add_routes([
        web.get("/", page),
        # web.get also answers HEAD, which is what most uptime monitors send.
        web.get("/health", health),
        web.get("/api/state", state),
        web.get("/api/day", day),
        web.get("/api/me", me_get),
        web.post("/api/me", me_post),
    ])

    runner = web.AppRunner(app)
    await runner.setup()
    try:
        await web.TCPSite(runner, host, port).start()
    except OSError as exc:
        # A busy port shouldn't stop the Discord half of the bot from running.
        log.error("Dashboard could not bind to %s:%s (%s)", host, port, exc)
        await runner.cleanup()
        return None

    log.info("Dashboard running at http://%s:%s", host, port)
    return runner

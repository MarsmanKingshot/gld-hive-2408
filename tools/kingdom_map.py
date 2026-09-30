#!/usr/bin/env python3
"""Build a searchable kingdom map from a MightPulse city snapshot.

Reads the JSON that https://mightpulse.com/map?kingdom=2408 loads from
/api/map?kid=2408 (every city with alliance tag and X:Y) and the Mapper's
static layer (mountains, lakes, permanent structures), and writes one
self-contained HTML page: all cities drawn on the terrain, the top N alliances
coloured and labelled at their hives, a search box for X:Y, alliance tag or
player name, a ranked alliance list, and a countdown to the next scan
MightPulse will accept (30 minutes after the last one).

Usage (from the repository root):
  python3 tools/kingdom_map.py                      # rebuild from the stored snapshot
  python3 tools/kingdom_map.py --fetch              # download a fresh snapshot first
  python3 tools/kingdom_map.py --fetch --request-scan   # ask MightPulse for a new scan, wait, then fetch
  python3 tools/kingdom_map.py --top 50 --out x.html

The page reads status.json (written next to the page with --status) to show
the timer and to reload itself once a newer scan has been published. The
GitHub Actions workflow in the site repository runs this every 15 minutes.
"""
import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.request
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAPSHOT = os.path.join(ROOT, "alliance", "kingdom", "snapshot_2408.json")
STATIC = os.path.join(ROOT, "alliance", "search", "static_layer.json")
OUT = os.path.join(ROOT, "alliance", "kingdom", "kingdom_map.html")
BASE = "https://mightpulse.com"
SIZE = 1200

# Zone squares (x and y ranges, inclusive), from the Mapper's ZONES_KINGSHOT.
ZONES = [
    ("Plains", 300, 899),
    ("Fertile Lands", 450, 749),
    ("Ruins", 552, 647),
    ("King's zone", 586, 613),
]


def http(path, method="GET", kid=2408):
    req = urllib.request.Request(BASE + path, method=method, headers={
        "User-Agent": "Mozilla/5.0 (kingdom map builder for the GLD alliance)",
        "Referer": f"{BASE}/map?kingdom={kid}",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=180) as r:
        return r.read()


def fetch(kid, path):
    data = http(f"/api/map?kid={kid}", kid=kid)
    json.loads(data)  # validate before overwriting
    with open(path, "wb") as f:
        f.write(data)
    print(f"fetched {len(data)/1e6:.1f} MB to {path}")


def status(kid, token=""):
    return json.loads(http(f"/api/map/update/status?kid={kid}&token={token}", kid=kid))


def request_scan(kid, wait=900):
    """Press MightPulse's "Update map" for this kingdom and wait for the scan to finish."""
    st = status(kid)
    job = st.get("job") or {}
    if job.get("status") in ("queued", "running"):
        print("a scan is already", job["status"])
    elif max(st.get("cooldown_remaining_sec") or 0, st.get("ip_cooldown_remaining_sec") or 0) > 0:
        print(f"cooldown: {st.get('cooldown_remaining_sec')} s for the kingdom, {st.get('ip_cooldown_remaining_sec')} s for this IP; not requesting")
        return st
    else:
        r = json.loads(http(f"/api/map/update?kid={kid}&token=", method="POST", kid=kid))
        print("requested:", {k: r.get(k) for k in ("error", "message", "job_id", "status", "queue_position")})
        if r.get("error"):
            return st
    token = ""
    end = time.time() + wait
    while time.time() < end:
        time.sleep(20)
        st = status(kid, token)
        job = st.get("job") or {}
        print("  ", job.get("status") or "idle", job.get("queue_position") or "", flush=True)
        if job.get("status") not in ("queued", "running"):
            break
    return st


def rle(cells):
    """Row-major run-length encoding of a set of (x, y) cells: [start, len, ...]."""
    idx = sorted(y * SIZE + x for x, y in cells)
    runs = []
    for i in idx:
        if runs and runs[-2] + runs[-1] == i:
            runs[-1] += 1
        else:
            runs += [i, 1]
    return runs


def clusters(pts, radius=25, min_size=5, max_clusters=4):
    """Greedy hive finder: densest 10-tile cell, take everything within radius
    of it, label the mean, repeat. Returns [(cx, cy, n), ...] largest first."""
    pts = list(pts)
    out = []
    while pts and len(out) < max_clusters:
        cells = Counter((x // 10, y // 10) for x, y in pts)
        (cx, cy), n = cells.most_common(1)[0]
        if n < 2 and out:
            break
        centre = (cx * 10 + 5, cy * 10 + 5)
        near = [p for p in pts if abs(p[0] - centre[0]) <= radius and abs(p[1] - centre[1]) <= radius]
        if len(near) < min_size:
            break
        mx = sum(p[0] for p in near) / len(near)
        my = sum(p[1] for p in near) / len(near)
        out.append((round(mx), round(my), len(near)))
        pts = [p for p in pts if p not in near]
    return out


def build(snapshot, static, top, out):
    d = json.load(open(snapshot))
    st = json.load(open(static))
    kid = d["kid"]
    stamp = d["stats"]["location_updated_at"]
    when = dt.datetime.fromtimestamp(stamp, dt.timezone.utc)

    names = {a["aid"]: a["name"] for a in d.get("alliances", [])}
    power = Counter()
    count = Counter()
    abbr = {}
    pts = defaultdict(list)
    for c in d["cities"]:
        aid = c.get("aid")
        if not aid:
            continue
        power[aid] += c.get("power") or 0
        count[aid] += 1
        abbr[aid] = c.get("alliance_abbr") or "?"
        pts[aid].append((c["x"], c["y"]))
    order = sorted(power, key=lambda a: -power[a])
    aindex = {aid: i for i, aid in enumerate(order)}
    alliances = []
    for i, aid in enumerate(order):
        cl = clusters(pts[aid]) if i < top else clusters(pts[aid], max_clusters=2)
        alliances.append({"tag": abbr[aid], "name": names.get(aid, abbr[aid]), "n": count[aid],
                          "power": power[aid], "clusters": cl})

    cities = []
    for c in d["cities"]:
        cities.append([c["x"], c["y"], aindex.get(c.get("aid"), -1), c.get("tc") or 0,
                       c.get("power") or 0, c.get("nick_name") or ""])
    cities.sort(key=lambda r: (r[2] if r[2] >= 0 else 10**6))

    data = {
        "kid": kid, "stamp": stamp, "when": when.strftime("%d %b %Y %H:%M UTC"), "top": top,
        "alliances": alliances, "cities": cities,
        "terrain": {"mountains": rle(map(tuple, st["mountains"])), "lakes": rle(map(tuple, st["lakes"]))},
        "structures": [[b[0], b[1], b[2]] for b in st["buildings"]],
        "zones": ZONES, "size": SIZE, "mightpulse": f"{BASE}/map?kingdom={kid}",
    }
    html = (TEMPLATE.replace("__KID__", str(kid)).replace("__WHEN__", data["when"]).replace("__TOP__", str(top))
            .replace("__DATA__", json.dumps(data, separators=(",", ":"), ensure_ascii=False)))
    with open(out, "w") as f:
        f.write(html)
    print(f"{len(cities)} cities, {len(alliances)} alliances ({top} coloured), snapshot {data['when']}")
    print(f"wrote {out} ({os.path.getsize(out)/1e6:.1f} MB)")
    return stamp


def write_status(path, kid, st, stamp):
    """Only fields that change when a scan happens, so the file (and the site) stays put between scans."""
    job = st.get("job") or {}
    rec = {
        "kid": kid,
        "snapshot_time": stamp,
        "last_finished_at": st.get("last_finished_at"),
        "cooldown_total_sec": st.get("cooldown_total_sec") or 1800,
        "job_status": job.get("status") if job.get("status") in ("queued", "running") else None,
        "updates_paused": bool(st.get("updates_paused")),
    }
    with open(path, "w") as f:
        json.dump(rec, f)
    print("status:", rec)


TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kingdom __KID__ alliance map</title>
<style>
:root { --bg:#14161c; --panel:#1d2029; --line:#2e323f; --text:#e6e8ee; --muted:#9aa0b0; --accent:#ffd166; --ok:#7bd88f; }
* { box-sizing:border-box; }
html, body { margin:0; height:100%; background:var(--bg); color:var(--text); font:14px/1.4 -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
body { display:flex; flex-direction:column; height:100vh; height:100dvh; }
header { display:flex; flex-wrap:wrap; gap:6px 14px; align-items:center; padding:6px 12px; background:var(--panel); border-bottom:1px solid var(--line); }
header h1 { font-size:16px; margin:0; font-weight:600; }
.status { display:flex; flex-wrap:wrap; gap:4px 14px; align-items:center; padding:5px 12px; background:#181b23; border-bottom:1px solid var(--line); font-size:12px; color:var(--muted); }
.status b { color:var(--text); font-weight:600; }
#timer { color:var(--accent); font-variant-numeric:tabular-nums; }
#timer.ready { color:var(--ok); }
#tip { display:none; width:100%; color:var(--text); }
#search { flex:1 1 220px; min-width:180px; padding:7px 10px; border-radius:6px; border:1px solid var(--line); background:#0f1116; color:var(--text); font-size:15px; }
#search:focus { outline:none; border-color:var(--accent); }
main { flex:1; display:flex; min-height:0; }
#mapwrap { flex:1; position:relative; min-width:0; }
canvas { display:block; width:100%; height:100%; touch-action:none; cursor:crosshair; }
#readout { position:absolute; left:8px; bottom:8px; background:rgba(20,22,28,.85); padding:4px 8px; border-radius:6px; font-size:12px; color:var(--muted); pointer-events:none; }
#info { position:absolute; right:8px; top:8px; max-width:280px; background:rgba(29,32,41,.95); border:1px solid var(--line); border-radius:8px; padding:8px 10px; font-size:13px; display:none; }
#info b { color:var(--accent); }
#info .close { float:right; cursor:pointer; color:var(--muted); margin-left:8px; }
#info ul { margin:4px 0 0; padding-left:16px; }
aside { width:320px; background:var(--panel); border-left:1px solid var(--line); display:flex; flex-direction:column; min-height:0; }
aside .head { padding:8px 10px; border-bottom:1px solid var(--line); display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
aside .head label { font-size:12px; color:var(--muted); display:flex; align-items:center; gap:4px; white-space:nowrap; }
#list { overflow:auto; flex:1; }
.row { display:grid; grid-template-columns:28px 14px 44px 1fr 34px 62px; gap:6px; align-items:center; padding:5px 10px; border-bottom:1px solid #22252f; cursor:pointer; font-size:13px; }
.row:hover, .row.sel { background:#262a36; }
.row .rk { color:var(--muted); text-align:right; }
.row .chip { width:12px; height:12px; border-radius:3px; }
.row .tag { font-weight:600; }
.row .nm { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; color:var(--muted); }
.row .n { text-align:right; color:var(--muted); }
.row .at { text-align:right; font-variant-numeric:tabular-nums; }
#toggle { display:none; }
@media (max-width: 760px) {
  main { flex-direction:column; }
  aside { width:100%; border-left:0; border-top:1px solid var(--line); height:38%; }
  aside.hidden { display:none; }
  #toggle { display:inline-block; }
  #info { max-width:220px; }
}
button { background:#2a2e3b; color:var(--text); border:1px solid var(--line); border-radius:6px; padding:5px 9px; font-size:12px; cursor:pointer; }
button.primary { background:#3a3f52; border-color:#4a506a; }
</style>
</head>
<body>
<header>
  <h1>Kingdom __KID__ alliance map</h1>
  <input id="search" placeholder="Search: 756:470, an alliance tag, or a player name" autocomplete="off">
  <button id="btnReset">Whole map</button>
  <button id="toggle">List</button>
</header>
<div class="status">
  <span>Cities as of <b id="when">__WHEN__</b></span>
  <span id="timer">checking when the next scan can run…</span>
  <button id="btnRefresh" class="primary">Refresh</button>
  <span>Top __TOP__ alliances coloured. Alliance buildings are not in the data.</span>
  <span id="tip"></span>
</div>
<main>
  <div id="mapwrap">
    <canvas id="map"></canvas>
    <div id="readout">move over the map for coordinates</div>
    <div id="info"></div>
  </div>
  <aside id="aside">
    <div class="head">
      <input id="filter" placeholder="filter list" style="flex:1;min-width:100px;padding:5px 8px;border-radius:6px;border:1px solid var(--line);background:#0f1116;color:var(--text)">
      <label><input type="checkbox" id="showAll"> all cities</label>
    </div>
    <div id="list"></div>
  </aside>
</main>
<script>
const D = __DATA__;
const N = D.size, TOP = D.top;
const A = D.alliances, C = D.cities;

// ---- colours: golden-angle hues, three lightness bands, for the top N
function colour(i) {
  if (i < 0) return null;
  if (i >= TOP) return '#8a8f9c';
  const h = (i * 137.508) % 360, l = [56, 44, 66][i % 3], s = [78, 88, 70][(i >> 1) % 3];
  return `hsl(${h.toFixed(1)} ${s}% ${l}%)`;
}
const COL = A.map((_, i) => colour(i));
function colourOf(i) { return i < TOP ? COL[i] : '#ffd166'; }   // an alliance outside the top N shows in the accent colour when focused

// ---- tile index for hover and X:Y search (2x2 footprint, X:Y is the lower-left tile)
const tileCity = new Map();
C.forEach((c, k) => { for (let dx = 0; dx < 2; dx++) for (let dy = 0; dy < 2; dy++) tileCity.set((c[0] + dx) * N + c[1] + dy, k); });
const terrain = new Uint8Array(N * N);
function unrle(runs, v) { for (let i = 0; i < runs.length; i += 2) terrain.fill(v, runs[i], runs[i] + runs[i + 1]); }
unrle(D.terrain.mountains, 1); unrle(D.terrain.lakes, 2);
const structTile = new Map();
D.structures.forEach(s => { for (let dx = 0; dx < s[2]; dx++) for (let dy = 0; dy < s[2]; dy++) structTile.set((s[0] + dx) * N + s[1] + dy, s); });

// ---- offscreen base image: 1 px per tile, row 0 = y 1199 (north up)
const base = document.createElement('canvas'); base.width = N; base.height = N;
const bctx = base.getContext('2d');
let showAll = false, focus = -1;
function drawBase() {
  bctx.fillStyle = '#1b1e26'; bctx.fillRect(0, 0, N, N);            // Badlands
  const zf = { 'Plains': '#232733', 'Fertile Lands': '#25302a', 'Ruins': '#2c2a2a', "King's zone": '#332c22' };
  for (const [name, lo, hi] of D.zones) { bctx.fillStyle = zf[name]; bctx.fillRect(lo, N - 1 - hi, hi - lo + 1, hi - lo + 1); }
  const img = bctx.getImageData(0, 0, N, N), px = img.data;
  for (let y = 0; y < N; y++) for (let x = 0; x < N; x++) {
    const t = terrain[y * N + x]; if (!t) continue;
    const o = ((N - 1 - y) * N + x) * 4;
    if (t === 1) { px[o] = 92; px[o + 1] = 94; px[o + 2] = 100; } else { px[o] = 56; px[o + 1] = 96; px[o + 2] = 150; }
  }
  const dim = focus >= 0;
  for (const c of C) {
    const a = c[2];
    let col;
    if (a === focus) col = rgb(colourOf(a));
    else if (a < 0) { if (!showAll) continue; col = [70, 74, 84]; }
    else if (a >= TOP) { if (!showAll) continue; col = [138, 143, 156]; }
    else col = rgb(COL[a]);
    if (dim && a !== focus) col = col.map(v => Math.round(v * 0.35 + 20));
    for (let dx = 0; dx < 2; dx++) for (let dy = 0; dy < 2; dy++) {
      const X = c[0] + dx, Y = c[1] + dy; if (X >= N || Y >= N) continue;
      const o = ((N - 1 - Y) * N + X) * 4; px[o] = col[0]; px[o + 1] = col[1]; px[o + 2] = col[2]; px[o + 3] = 255;
    }
  }
  for (const s of D.structures) {
    for (let dx = 0; dx < s[2]; dx++) for (let dy = 0; dy < s[2]; dy++) {
      const o = ((N - 1 - (s[1] + dy)) * N + s[0] + dx) * 4; px[o] = 230; px[o + 1] = 200; px[o + 2] = 120; px[o + 3] = 255;
    }
  }
  bctx.putImageData(img, 0, 0);
}
const _cv = document.createElement('canvas').getContext('2d');
function rgb(css) { _cv.fillStyle = css; const h = _cv.fillStyle; return [parseInt(h.slice(1, 3), 16), parseInt(h.slice(3, 5), 16), parseInt(h.slice(5, 7), 16)]; }

// ---- view: scale = screen px per tile, (ox, oy) = screen position of tile (0, 1199)'s top-left
const cv = document.getElementById('map'), ctx = cv.getContext('2d');
let W = 0, H = 0, scale = 1, ox = 0, oy = 0, dpr = 1;
let hover = null, pin = null, marker = null;
function resize() {
  dpr = window.devicePixelRatio || 1;
  W = cv.clientWidth; H = cv.clientHeight; cv.width = W * dpr; cv.height = H * dpr;
  render();
}
function fitAll() { scale = Math.min(W, H) / N; ox = (W - N * scale) / 2; oy = (H - N * scale) / 2; render(); }
function toScreen(x, y) { return [ox + x * scale, oy + (N - 1 - y) * scale]; }   // top-left of tile
function toTile(sx, sy) { return [Math.floor((sx - ox) / scale), N - 1 - Math.floor((sy - oy) / scale)]; }
function centreOn(x, y, s) { if (s) scale = s; ox = W / 2 - (x + 0.5) * scale; oy = H / 2 - (N - 1 - y + 0.5) * scale; render(); }

function render() {
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.fillStyle = '#0f1116'; ctx.fillRect(0, 0, W, H);
  ctx.imageSmoothingEnabled = scale < 1;
  ctx.drawImage(base, ox, oy, N * scale, N * scale);
  ctx.lineWidth = 1;
  for (const [name, lo, hi] of D.zones) {
    const [sx, sy] = toScreen(lo, hi); ctx.strokeStyle = 'rgba(255,255,255,.18)';
    ctx.strokeRect(sx, sy, (hi - lo + 1) * scale, (hi - lo + 1) * scale);
  }
  const step = scale >= 4 ? 50 : 100;
  ctx.strokeStyle = 'rgba(255,255,255,.07)'; ctx.beginPath();
  for (let g = 0; g <= N; g += step) { const [sx] = toScreen(g, 0); ctx.moveTo(sx, oy); ctx.lineTo(sx, oy + N * scale); const [, sy] = toScreen(0, g); ctx.moveTo(ox, sy); ctx.lineTo(ox + N * scale, sy); }
  ctx.stroke();
  if (scale >= 2) {
    ctx.fillStyle = 'rgba(255,255,255,.35)'; ctx.font = '10px sans-serif';
    for (let g = 0; g <= N; g += step) { const [sx, sy] = toScreen(g, g); ctx.fillText(g, sx + 2, Math.min(H - 4, Math.max(12, oy + 12))); ctx.fillText(g, Math.max(2, ox + 2), sy - 2); }
  }
  ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
  const labelMin = scale >= 1.2 ? 5 : scale >= 0.6 ? 12 : 25;
  const labelled = focus >= 0 ? [focus] : [...Array(Math.min(TOP, A.length)).keys()];
  for (const i of labelled) {
    const a = A[i];
    a.clusters.forEach((cl, j) => {
      if (cl[2] < labelMin && !(j === 0 && (i < 30 || i === focus) && scale >= 0.4)) return;
      const [sx, sy] = toScreen(cl[0], cl[1]);
      const big = j === 0 && (i < 30 || i === focus);
      ctx.font = (big ? 'bold 13px' : '11px') + ' sans-serif';
      const t = a.tag, w = ctx.measureText(t).width + 8;
      ctx.fillStyle = 'rgba(0,0,0,.65)'; ctx.fillRect(sx - w / 2, sy - 9, w, 18);
      ctx.fillStyle = colourOf(i); ctx.fillText(t, sx, sy);
    });
  }
  for (const h of [hover, pin]) if (h) {
    const [sx, sy] = toScreen(h.x, h.y); const s = h.size || 1; const [sx2, sy2] = toScreen(h.x, h.y + s - 1);
    ctx.strokeStyle = h === pin ? '#ffd166' : '#ffffff'; ctx.lineWidth = 2;
    ctx.strokeRect(sx - 1, sy2 - 1, s * scale + 2, s * scale + 2);
  }
  if (marker) {
    const [sx, sy] = toScreen(marker[0], marker[1]); const cx = sx + scale / 2, cy = sy + scale / 2;
    ctx.strokeStyle = '#ffd166'; ctx.lineWidth = 2; ctx.beginPath();
    ctx.arc(cx, cy, Math.max(10, scale), 0, Math.PI * 2); ctx.moveTo(cx - 18, cy); ctx.lineTo(cx + 18, cy); ctx.moveTo(cx, cy - 18); ctx.lineTo(cx, cy + 18); ctx.stroke();
  }
}

// ---- interaction
let drag = null, pinchDist = 0;
cv.addEventListener('pointerdown', e => { drag = { x: e.clientX, y: e.clientY, ox, oy, moved: false }; cv.setPointerCapture(e.pointerId); });
cv.addEventListener('pointermove', e => {
  if (drag) {
    const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
    if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
    ox = drag.ox + dx; oy = drag.oy + dy; render(); return;
  }
  const r = cv.getBoundingClientRect(); const [x, y] = toTile(e.clientX - r.left, e.clientY - r.top);
  hover = describe(x, y); showReadout(x, y, hover); render();
});
cv.addEventListener('pointerup', e => {
  if (drag && !drag.moved) {
    const r = cv.getBoundingClientRect(); const [x, y] = toTile(e.clientX - r.left, e.clientY - r.top);
    pin = describe(x, y); showInfo(pin ? infoHtml(pin) : `<b>${x}:${y}</b><br>${what(x, y)}`);
  }
  drag = null; render();
});
cv.addEventListener('wheel', e => {
  e.preventDefault();
  const r = cv.getBoundingClientRect(), mx = e.clientX - r.left, my = e.clientY - r.top;
  const f = Math.exp(-e.deltaY * 0.0015), ns = Math.min(40, Math.max(0.2, scale * f));
  ox = mx - (mx - ox) * ns / scale; oy = my - (my - oy) * ns / scale; scale = ns; render();
}, { passive: false });
cv.addEventListener('touchstart', e => { if (e.touches.length === 2) { drag = null; pinchDist = dist(e.touches); } }, { passive: true });
cv.addEventListener('touchmove', e => {
  if (e.touches.length !== 2) return; e.preventDefault();
  const d = dist(e.touches), r = cv.getBoundingClientRect();
  const mx = (e.touches[0].clientX + e.touches[1].clientX) / 2 - r.left, my = (e.touches[0].clientY + e.touches[1].clientY) / 2 - r.top;
  const ns = Math.min(40, Math.max(0.2, scale * d / pinchDist)); pinchDist = d;
  ox = mx - (mx - ox) * ns / scale; oy = my - (my - oy) * ns / scale; scale = ns; render();
}, { passive: false });
function dist(t) { return Math.hypot(t[0].clientX - t[1].clientX, t[0].clientY - t[1].clientY); }

function visible(c) { return c[2] === focus || (c[2] >= 0 && c[2] < TOP) || showAll; }
function describe(x, y) {
  if (x < 0 || y < 0 || x >= N || y >= N) return null;
  const k = tileCity.get(x * N + y);
  if (k !== undefined && visible(C[k])) { const c = C[k]; return { kind: 'city', x: c[0], y: c[1], size: 2, c }; }
  const s = structTile.get(x * N + y);
  if (s) return { kind: 'structure', x: s[0], y: s[1], size: s[2] };
  return null;
}
function what(x, y) {
  const t = terrain[y * N + x];
  const zone = D.zones.slice().reverse().find(z => x >= z[1] && x <= z[2] && y >= z[1] && y <= z[2]);
  return (t === 1 ? 'mountain' : t === 2 ? 'lake' : 'open ground') + ', ' + (zone ? zone[0] : 'Badlands');
}
function fmtP(p) { return p >= 1e9 ? (p / 1e9).toFixed(2) + 'B' : (p / 1e6).toFixed(1) + 'M'; }
function infoHtml(h) {
  if (h.kind === 'city') {
    const c = h.c, a = c[2] >= 0 ? A[c[2]] : null;
    return `<b>${esc(c[5])}</b> at ${c[0]}:${c[1]}<br>${a ? `[${esc(a.tag)}] ${esc(a.name)} (rank ${c[2] + 1})` : 'no alliance'}<br>Town hall ${c[3]}, power ${fmtP(c[4])}`;
  }
  return `<b>Permanent structure</b> ${h.x}:${h.y}, ${h.size}x${h.size}`;
}
function showReadout(x, y, h) {
  const el = document.getElementById('readout');
  if (x < 0 || y < 0 || x >= N || y >= N) { el.textContent = ''; return; }
  el.textContent = `${x}:${y}  ` + (h ? (h.kind === 'city' ? `${h.c[5]} [${h.c[2] >= 0 ? A[h.c[2]].tag : '-'}]` : 'structure') : what(x, y));
}
function showInfo(html) { const el = document.getElementById('info'); el.innerHTML = `<span class="close" onclick="hideInfo()">✕</span>` + html; el.style.display = 'block'; }
function hideInfo() { document.getElementById('info').style.display = 'none'; pin = null; render(); }
function esc(s) { return String(s).replace(/[&<>"]/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[ch])); }

// ---- search
function search(q) {
  q = q.trim(); if (!q) return;
  const m = q.match(/^\s*(\d{1,4})\s*[:,\s]\s*(\d{1,4})\s*$/);
  if (m) {
    const x = +m[1], y = +m[2];
    if (x >= N || y >= N) { showInfo(`<b>${x}:${y}</b> is outside the map (0 to ${N - 1})`); return; }
    marker = [x, y]; setFocus(-1); centreOn(x, y, Math.max(scale, 8));
    const h = describe(x, y);
    const near = new Map();
    for (const c of C) if (Math.abs(c[0] - x) <= 10 && Math.abs(c[1] - y) <= 10 && c[2] >= 0) near.set(c[2], (near.get(c[2]) || 0) + 1);
    const list = [...near].sort((p, q) => q[1] - p[1]).slice(0, 6).map(([i, n]) => `<li><span style="color:${COL[i]}">${esc(A[i].tag)}</span> ${n} cities</li>`).join('');
    showInfo(`<b>${x}:${y}</b>: ${h ? (h.kind === 'city' ? `city of ${esc(h.c[5])}` : 'permanent structure') : what(x, y)}<br>Within 10 tiles:<ul>${list || '<li>no alliance cities</li>'}</ul>`);
    return;
  }
  let i = A.findIndex(a => a.tag === q);
  if (i < 0) i = A.findIndex(a => a.tag.toLowerCase() === q.toLowerCase());
  if (i < 0) i = A.findIndex(a => a.name.toLowerCase().includes(q.toLowerCase()));
  if (i >= 0) { gotoAlliance(i); return; }
  const hits = [];
  for (const c of C) if (c[5].toLowerCase().includes(q.toLowerCase())) { hits.push(c); if (hits.length >= 12) break; }
  if (!hits.length) { showInfo(`Nothing found for "${esc(q)}"`); return; }
  const c = hits[0]; marker = [c[0], c[1]]; setFocus(-1); centreOn(c[0], c[1], Math.max(scale, 8));
  showInfo(`<b>${hits.length}${hits.length >= 12 ? '+' : ''} player${hits.length > 1 ? 's' : ''}</b><ul>` + hits.map(c => `<li><a href="#" onclick="jump(${c[0]},${c[1]});return false" style="color:var(--text)">${esc(c[5])}</a> ${c[0]}:${c[1]} [${c[2] >= 0 ? esc(A[c[2]].tag) : '-'}]</li>`).join('') + '</ul>');
}
function jump(x, y) { marker = [x, y]; centreOn(x, y, Math.max(scale, 8)); }
function gotoAlliance(i) {
  const a = A[i]; setFocus(i);
  const cl = a.clusters[0];
  if (cl) { marker = null; centreOn(cl[0], cl[1], Math.max(scale, 3)); }
  else if (a.n) { const c = C.find(c => c[2] === i); marker = [c[0], c[1]]; centreOn(c[0], c[1], Math.max(scale, 3)); }
  const rows = a.clusters.map(c => `<li><a href="#" onclick="jump(${c[0]},${c[1]});return false" style="color:var(--text)">${c[0]}:${c[1]}</a> ${c[2]} cities</li>`).join('');
  showInfo(`<b>[${esc(a.tag)}] ${esc(a.name)}</b> rank ${i + 1}<br>${a.n} cities, power ${fmtP(a.power)}<br>Hives:<ul>${rows || '<li>no cluster of 5 or more</li>'}</ul>`);
  document.querySelectorAll('.row').forEach(r => r.classList.toggle('sel', +r.dataset.i === i));
  const sel = document.querySelector('.row.sel'); if (sel) sel.scrollIntoView({ block: 'nearest' });
}
function setFocus(i) { if (focus !== i) { focus = i; drawBase(); } }

// ---- list
function buildList() {
  const f = document.getElementById('filter').value.trim().toLowerCase();
  const el = document.getElementById('list'); el.innerHTML = '';
  A.forEach((a, i) => {
    if (f ? !(a.tag.toLowerCase().includes(f) || a.name.toLowerCase().includes(f)) : (i >= TOP && !showAll)) return;
    const cl = a.clusters[0];
    const r = document.createElement('div'); r.className = 'row'; r.dataset.i = i;
    r.innerHTML = `<span class="rk">${i + 1}</span><span class="chip" style="background:${COL[i]}"></span><span class="tag">${esc(a.tag)}</span><span class="nm" title="${esc(a.name)}">${esc(a.name)}</span><span class="n">${a.n}</span><span class="at">${cl ? cl[0] + ':' + cl[1] : '-'}</span>`;
    r.onclick = () => gotoAlliance(i);
    el.appendChild(r);
  });
}
document.getElementById('filter').addEventListener('input', buildList);
document.getElementById('search').addEventListener('keydown', e => { if (e.key === 'Enter') search(e.target.value); });
document.getElementById('btnReset').onclick = () => { setFocus(-1); marker = null; hideInfo(); fitAll(); };
document.getElementById('toggle').onclick = () => { document.getElementById('aside').classList.toggle('hidden'); resize(); };
document.getElementById('showAll').onchange = e => { showAll = e.target.checked; drawBase(); buildList(); render(); };
window.addEventListener('resize', resize);

// ---- refresh: the timer and the reload once a newer scan is published
let status = null;
const COOLDOWN = 1800;
function fmtT(s) { s = Math.max(0, Math.round(s)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`; }
function tick() {
  const el = document.getElementById('timer');
  if (!status) return;
  if (status.updates_paused) { el.className = ''; el.textContent = 'MightPulse has paused map updates'; return; }
  if (status.job_status) { el.className = ''; el.textContent = 'MightPulse is scanning the kingdom now, this page reloads when it is done'; return; }
  const left = (status.last_finished_at || 0) + (status.cooldown_total_sec || COOLDOWN) - Date.now() / 1000;
  if (left > 0) { el.className = ''; el.textContent = `Next scan can be requested in ${fmtT(left)}`; }
  else { el.className = 'ready'; el.textContent = 'A new scan can be requested now'; }
}
async function pollStatus() {
  try {
    const r = await fetch('status.json?_=' + Date.now(), { cache: 'no-store' });
    if (!r.ok) throw 0;
    status = await r.json(); tick();
    if (status.snapshot_time && status.snapshot_time > D.stamp + 1) {
      const key = 'reloaded-' + Math.floor(status.snapshot_time);
      let done = false; try { done = sessionStorage.getItem(key); } catch (e) {}
      if (!done) { try { sessionStorage.setItem(key, '1'); } catch (e) {} location.replace(location.pathname + '?v=' + Math.floor(status.snapshot_time) + location.hash); }
    }
  } catch (e) {
    if (!status) document.getElementById('timer').textContent = 'The refresh timer is only available on the published page.';
  }
}
document.getElementById('btnRefresh').onclick = () => {
  const tip = document.getElementById('tip');
  tip.style.display = 'block';
  const left = status ? (status.last_finished_at || 0) + (status.cooldown_total_sec || COOLDOWN) - Date.now() / 1000 : 0;
  tip.innerHTML = (left > 0 ? `MightPulse only scans a kingdom once every 30 minutes; wait for the timer. ` : ``) +
    `The scan runs on <a href="${D.mightpulse}" target="_blank" rel="noopener" style="color:var(--accent)">MightPulse</a>: press <b>Update map</b> there and wait for it to finish (a few minutes). This page shows the new scan after its next rebuild and reloads by itself when that happens.`;
  window.open(D.mightpulse, '_blank', 'noopener');
};
setInterval(tick, 1000); setInterval(pollStatus, 60000); pollStatus();

drawBase(); buildList(); resize(); fitAll();
// deep link: #q=756:470 or #q=GLD
const hq = new URLSearchParams(location.hash.slice(1)).get('q');
if (hq) { document.getElementById('search').value = hq; search(hq); }
</script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--kid", type=int, default=2408)
    ap.add_argument("--snapshot", default=SNAPSHOT)
    ap.add_argument("--static", default=STATIC)
    ap.add_argument("--top", type=int, default=100)
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--status", default=None, help="write status.json (scan time, cooldown) for the page's timer")
    ap.add_argument("--fetch", action="store_true", help="download a fresh snapshot from MightPulse first")
    ap.add_argument("--request-scan", action="store_true", help="press MightPulse's Update map and wait before fetching")
    a = ap.parse_args()
    st = None
    if a.request_scan:
        st = request_scan(a.kid)
    if a.fetch:
        fetch(a.kid, a.snapshot)
    stamp = build(a.snapshot, a.static, a.top, a.out)
    if a.status:
        if st is None or a.fetch:
            try:
                st = status(a.kid)
            except Exception as e:  # keep the last status rather than failing the build
                print("status not available:", e, file=sys.stderr)
                st = json.load(open(a.status)) if os.path.exists(a.status) else {}
                st = {"last_finished_at": st.get("last_finished_at"), "cooldown_total_sec": st.get("cooldown_total_sec")}
        write_status(a.status, a.kid, st, stamp)


if __name__ == "__main__":
    main()

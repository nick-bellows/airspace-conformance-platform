// Plan-view display.
//
// Polls /v1/tracks once a second and draws what it finds. M4 replaces the poll
// with a WebSocket and adds the alert layer; the drawing code stays.
//
// The view auto-fits to whatever traffic is present rather than being told
// where to centre, so any scenario renders without configuration. The fit is
// smoothed across frames because snapping the scale every time an aircraft
// enters or leaves is unreadable.

const EARTH_RADIUS_NM = 3440.0695;
const POLL_INTERVAL_MS = 1000;
const TRAIL_LENGTH = 90; // seconds of history kept per track, client-side
const FIT_SMOOTHING = 0.12; // 0 = frozen view, 1 = snap instantly

const canvas = document.getElementById("scope");
const ctx = canvas.getContext("2d");
const listEl = document.getElementById("track-list");
const pillEl = document.getElementById("pill-link");
const tracksEl = document.getElementById("stat-tracks");
const updatedEl = document.getElementById("stat-updated");

/** @type {Map<string, {track: object, trail: Array<{lat:number, lon:number}>}>} */
const state = new Map();
let view = null; // {lat, lon, nmPerPixel}

// ---------------------------------------------------------------------------
// Geometry - the same equirectangular projection the Python side uses, so the
// picture and the conflict maths agree about what "near" means.
// ---------------------------------------------------------------------------

function toLocal(refLat, refLon, lat, lon) {
  let dLon = ((lon - refLon + 180) % 360) - 180;
  const meanLat = (((refLat + lat) / 2) * Math.PI) / 180;
  return {
    east: (dLon * Math.PI / 180) * Math.cos(meanLat) * EARTH_RADIUS_NM,
    north: ((lat - refLat) * Math.PI / 180) * EARTH_RADIUS_NM,
  };
}

function resize() {
  const ratio = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = Math.round(rect.width * ratio);
  canvas.height = Math.round(rect.height * ratio);
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
}

function fitView(tracks) {
  if (tracks.length === 0) return view;

  const lats = tracks.map((t) => t.lat);
  const lons = tracks.map((t) => t.lon);
  const centreLat = (Math.min(...lats) + Math.max(...lats)) / 2;
  const centreLon = (Math.min(...lons) + Math.max(...lons)) / 2;

  // Measure the two axes separately. Scaling both from a single span would fit
  // the traffic into the smaller canvas dimension and waste the rest of the
  // screen, which on a wide window is most of it.
  let eastSpanNm = 10;
  let northSpanNm = 10;
  for (const t of tracks) {
    const { east, north } = toLocal(centreLat, centreLon, t.lat, t.lon);
    eastSpanNm = Math.max(eastSpanNm, Math.abs(east) * 2);
    northSpanNm = Math.max(northSpanNm, Math.abs(north) * 2);
  }

  const rect = canvas.getBoundingClientRect();
  // Margin leaves room for the data block drawn to the right of each symbol.
  const target = {
    lat: centreLat,
    lon: centreLon,
    nmPerPixel: Math.max(
      (eastSpanNm * 1.3) / rect.width,
      (northSpanNm * 1.35) / rect.height,
      0.02, // floor: never zoom in so far that noise looks like manoeuvring
    ),
  };
  if (view === null) return target;

  const k = FIT_SMOOTHING;
  return {
    lat: view.lat + (target.lat - view.lat) * k,
    lon: view.lon + (target.lon - view.lon) * k,
    nmPerPixel: view.nmPerPixel + (target.nmPerPixel - view.nmPerPixel) * k,
  };
}

function project(lat, lon) {
  const rect = canvas.getBoundingClientRect();
  const { east, north } = toLocal(view.lat, view.lon, lat, lon);
  return {
    x: rect.width / 2 + east / view.nmPerPixel,
    y: rect.height / 2 - north / view.nmPerPixel, // screen y grows downward
  };
}

// ---------------------------------------------------------------------------
// Drawing
// ---------------------------------------------------------------------------

function styles() {
  const css = getComputedStyle(document.documentElement);
  return {
    grid: css.getPropertyValue("--grid").trim(),
    ink: css.getPropertyValue("--ink").trim(),
    inkDim: css.getPropertyValue("--ink-dim").trim(),
    track: css.getPropertyValue("--track").trim(),
    stale: css.getPropertyValue("--track-stale").trim(),
  };
}

function drawRangeRings(theme) {
  const rect = canvas.getBoundingClientRect();
  const cx = rect.width / 2;
  const cy = rect.height / 2;

  // Pick a ring spacing that yields a handful of rings at the current scale.
  const targetRings = 4;
  const rough = (view.nmPerPixel * Math.min(rect.width, rect.height)) / 2 / targetRings;
  const step = [5, 10, 20, 25, 50, 100, 200].find((s) => s >= rough) || 200;

  ctx.strokeStyle = theme.grid;
  ctx.fillStyle = theme.inkDim;
  ctx.lineWidth = 1;
  ctx.font = "10px ui-monospace, monospace";

  for (let ring = step; ring <= step * 8; ring += step) {
    const radius = ring / view.nmPerPixel;
    if (radius > Math.hypot(rect.width, rect.height)) break;
    ctx.beginPath();
    ctx.arc(cx, cy, radius, 0, Math.PI * 2);
    ctx.stroke();
    ctx.fillText(`${ring} NM`, cx + 4, cy - radius - 4);
  }

  ctx.beginPath();
  ctx.moveTo(cx, 0);
  ctx.lineTo(cx, rect.height);
  ctx.moveTo(0, cy);
  ctx.lineTo(rect.width, cy);
  ctx.stroke();
}

function drawTrail(entry, theme) {
  if (entry.trail.length < 2) return;
  ctx.strokeStyle = theme.grid;
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  entry.trail.forEach((p, i) => {
    const { x, y } = project(p.lat, p.lon);
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();
}

function drawTrack(track, theme) {
  const { x, y } = project(track.lat, track.lon);
  const colour = track.state === "coasting" ? theme.stale : theme.track;

  // Chevron pointing along the aircraft's track. Bearings are clockwise from
  // north; canvas rotation is clockwise from the +x axis, hence the -90.
  const heading = ((track.track_deg - 90) * Math.PI) / 180;
  ctx.save();
  ctx.translate(x, y);
  ctx.rotate(heading);
  ctx.fillStyle = colour;
  ctx.beginPath();
  ctx.moveTo(7, 0);
  ctx.lineTo(-5, 4.5);
  ctx.lineTo(-2.5, 0);
  ctx.lineTo(-5, -4.5);
  ctx.closePath();
  ctx.fill();
  ctx.restore();

  // Speed vector: where dead reckoning puts it in one minute. This is the
  // physics baseline the trajectory model learns a correction to, drawn.
  const oneMinuteNm = track.ground_speed_kt / 60;
  const rad = (track.track_deg * Math.PI) / 180;
  ctx.strokeStyle = colour;
  ctx.globalAlpha = 0.45;
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(x, y);
  ctx.lineTo(
    x + (Math.sin(rad) * oneMinuteNm) / view.nmPerPixel,
    y - (Math.cos(rad) * oneMinuteNm) / view.nmPerPixel,
  );
  ctx.stroke();
  ctx.globalAlpha = 1;

  // Data block: callsign, flight level, ground speed - the three fields on a
  // real one, in the same order.
  const flightLevel = String(Math.round(track.altitude_ft / 100)).padStart(3, "0");
  ctx.fillStyle = theme.ink;
  ctx.font = "11px ui-monospace, monospace";
  ctx.fillText(track.callsign || track.icao24, x + 11, y - 5);
  ctx.fillStyle = theme.inkDim;
  ctx.fillText(`${flightLevel}  ${Math.round(track.ground_speed_kt)}`, x + 11, y + 7);
}

function render() {
  const theme = styles();
  const rect = canvas.getBoundingClientRect();
  ctx.clearRect(0, 0, rect.width, rect.height);
  if (view === null) return;

  drawRangeRings(theme);
  for (const entry of state.values()) drawTrail(entry, theme);
  for (const entry of state.values()) drawTrack(entry.track, theme);
}

// ---------------------------------------------------------------------------
// Data
// ---------------------------------------------------------------------------

function updateList() {
  const entries = [...state.values()].sort((a, b) =>
    (a.track.callsign || "").localeCompare(b.track.callsign || ""),
  );
  listEl.replaceChildren(
    ...entries.map((entry) => {
      const t = entry.track;
      const li = document.createElement("li");
      li.className = `state-${t.state}`;

      const callsign = document.createElement("span");
      callsign.className = "callsign";
      callsign.textContent = t.callsign || t.icao24;

      const detail = document.createElement("span");
      detail.className = "detail";
      const level = String(Math.round(t.altitude_ft / 100)).padStart(3, "0");
      const vertical =
        Math.abs(t.vertical_rate_fpm) < 100
          ? "level"
          : `${t.vertical_rate_fpm > 0 ? "climb" : "desc"} ${Math.abs(Math.round(t.vertical_rate_fpm))}`;
      detail.textContent = `FL${level} · ${Math.round(t.ground_speed_kt)} kt · ${Math.round(t.track_deg).toString().padStart(3, "0")}° · ${vertical}`;

      li.append(callsign, detail);
      return li;
    }),
  );
}

function ingest(payload) {
  const seen = new Set();
  for (const track of payload.tracks) {
    seen.add(track.track_id);
    const entry = state.get(track.track_id) || { track, trail: [] };
    entry.track = track;
    entry.trail.push({ lat: track.lat, lon: track.lon });
    if (entry.trail.length > TRAIL_LENGTH) entry.trail.shift();
    state.set(track.track_id, entry);
  }
  // Anything the server no longer reports has aged out of the live window.
  for (const id of [...state.keys()]) if (!seen.has(id)) state.delete(id);

  view = fitView(payload.tracks);
  tracksEl.textContent = `${payload.count} track${payload.count === 1 ? "" : "s"}`;
  updatedEl.textContent = new Date(payload.generated_at).toLocaleTimeString();
  updateList();
  render();
}

function setLink(stateName, label) {
  pillEl.dataset.state = stateName;
  pillEl.textContent = label;
}

async function poll() {
  try {
    const response = await fetch("/v1/tracks");
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    ingest(await response.json());
    setLink("live", "live");
  } catch (error) {
    // Keep the last picture on screen but say so, rather than blanking the
    // display or - worse - leaving it looking current.
    setLink("down", "no data");
    console.warn("poll failed", error);
  }
}

window.addEventListener("resize", () => {
  resize();
  render();
});

resize();
poll();
setInterval(poll, POLL_INTERVAL_MS);

// The Last Seat - browser client. Talks only to the JSON API on the same origin.
"use strict";

const $ = (sel) => document.querySelector(sel);
const REFRESH_MS = 2000;
const ACTIVE = new Set(["HELD", "CONFIRMED"]);

const state = {
  events: [],
  eventId: null,
  seats: new Map(),        // seat_number -> seat object from the API
  clockOffset: 0,          // server time minus browser time, in ms
  me: "",
  current: null,           // the reservation shown in the side panel
  bookings: [],
  lastAction: null,        // {method, path, body, key} for "resend with same key"
  builtFor: null,          // event id the seat grid was built for
};

// ---------------------------------------------------------------- utilities

function store(key, value) {
  try { localStorage.setItem(key, value); } catch { /* private mode: ignore */ }
}
function load(key) {
  try { return localStorage.getItem(key); } catch { return null; }
}
function newKey() {
  if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
  return "k-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2);
}
function serverNow() { return Date.now() + state.clockOffset; }
function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
let toastTimer;
function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 3200);
}

// ---------------------------------------------------------------- API calls

async function api(method, path, body, { key, quiet = false } = {}) {
  const headers = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (key) headers["Idempotency-Key"] = key;
  let res, data = null;
  try {
    res = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  } catch (err) {
    if (!quiet) log(method, path, 0, { error: { code: "NETWORK_ERROR", message: String(err) } }, false, key);
    return { status: 0, ok: false, data: null, replayed: false };
  }
  try { data = await res.json(); } catch { data = null; }
  const replayed = res.headers.get("Idempotent-Replayed") === "true";
  if (!quiet) log(method, path, res.status, data, replayed, key);
  return { status: res.status, ok: res.ok, data, replayed };
}

// A state-changing action: new Idempotency-Key, remembered for "resend".
async function act(method, path, body) {
  const key = newKey();
  state.lastAction = { method, path, body, key };
  $("#replay").disabled = false;
  const r = await api(method, path, body, { key });
  await refresh();
  return r;
}

// ---------------------------------------------------------------- request log

const logEntries = [];
function log(method, path, status, data, replayed, key) {
  const outcome = data && (data.result || (data.error && data.error.code)) || "";
  logEntries.unshift({ method, path, status, data, replayed, key, outcome, at: new Date() });
  logEntries.length = Math.min(logEntries.length, 40);
  renderLog();
}
function renderLog() {
  const ol = $("#log");
  ol.innerHTML = logEntries.map((e, i) => {
    const cls = e.status >= 500 || e.status === 0 ? "s5" : e.status >= 400 ? "s4" : "s2";
    const short = e.path.replace("/api/v1", "").replace(/[0-9a-f]{8}-[0-9a-f-]{27}/g, (u) => u.slice(0, 8) + "...");
    return `<li data-i="${i}"><span class="code ${cls}">${e.status || "ERR"}</span>` +
      `<span class="what">${e.method} ${esc(short)} ${esc(e.outcome)}</span>` +
      (e.replayed ? `<span class="tag">replayed</span>` : "") + `</li>`;
  }).join("");
}
$("#log").addEventListener("click", (ev) => {
  const li = ev.target.closest("li");
  if (!li) return;
  const e = logEntries[Number(li.dataset.i)];
  const pre = $("#log-detail");
  pre.hidden = false;
  pre.textContent = `${e.method} ${e.path}\n` + (e.key ? `Idempotency-Key: ${e.key}\n` : "") +
    `-> ${e.status}${e.replayed ? " (Idempotent-Replayed: true)" : ""}\n\n` + JSON.stringify(e.data, null, 2);
});
$("#replay").addEventListener("click", async () => {
  const a = state.lastAction;
  if (!a) return;
  const r = await api(a.method, a.path, a.body, { key: a.key });
  toast(r.replayed ? "Same key: the server replayed its stored answer. Nothing new was created."
                   : `Sent again: ${r.status}`);
  await refresh();
});

// ---------------------------------------------------------------- seat map

function buildGrid(total) {
  const grid = $("#seat-grid");
  const parts = [];
  for (let n = 1; n <= total; n++) parts.push(`<button class="seat" data-n="${n}" aria-label="Seat ${n}">${n}</button>`);
  grid.innerHTML = parts.join("");
  state.builtFor = state.eventId;
}

function myActiveSeats() {
  const mine = new Map();
  for (const r of state.bookings) {
    if (r.event_id === state.eventId && ACTIVE.has(r.effective_status)) mine.set(r.seat_number, r);
  }
  return mine;
}

function renderSeats() {
  const mine = myActiveSeats();
  for (const btn of $("#seat-grid").children) {
    const n = Number(btn.dataset.n);
    const seat = state.seats.get(n);
    const status = seat ? seat.status : "AVAILABLE";
    const cls = mine.has(n) ? "mine" : status === "HELD" ? "held" : status === "CONFIRMED" ? "confirmed" : "";
    const want = "seat" + (cls ? " " + cls : "");
    if (btn.className.replace(" flash", "") !== want) btn.className = want;
    btn.setAttribute("aria-label", `Seat ${n}, ${mine.has(n) ? "yours" : status.toLowerCase()}`);
  }
  if (lastHovered) $("#hover-readout").textContent = describeSeat(lastHovered);
}
let lastHovered = null;

function describeSeat(n) {
  const seat = state.seats.get(n);
  if (!seat) return "";
  const mine = myActiveSeats().get(n);
  if (mine) return `Seat ${n} · yours (${mine.effective_status.toLowerCase()})`;
  if (seat.status === "HELD") {
    const left = Math.max(0, Math.round((Date.parse(seat.held_until) - serverNow()) / 1000));
    return `Seat ${n} · held by someone else, frees in ${left}s unless they pay`;
  }
  if (seat.status === "CONFIRMED") return `Seat ${n} · booked`;
  return `Seat ${n} · available, click to hold`;
}

$("#seat-grid").addEventListener("mouseover", (ev) => {
  const btn = ev.target.closest(".seat");
  if (!btn) return;
  lastHovered = Number(btn.dataset.n);
  $("#hover-readout").textContent = describeSeat(lastHovered);
});
$("#seat-grid").addEventListener("click", async (ev) => {
  const btn = ev.target.closest(".seat");
  if (!btn) return;
  const n = Number(btn.dataset.n);
  const mine = myActiveSeats().get(n);
  if (mine) { await selectReservation(mine.id); return; }
  if (!validMe()) return;
  const r = await act("POST", "/api/v1/reservations", { event_id: state.eventId, seat_number: n, customer_ref: state.me });
  if (r.status === 201) {
    btn.classList.add("flash");
    await selectReservation(r.data.reservation.id);
    toast(`Seat ${n} is held for you. Pay before the timer runs out.`);
  } else if (r.data && r.data.error) {
    toast(r.data.error.message);
  }
});

// ---------------------------------------------------------------- reservation panel

async function selectReservation(id) {
  const r = await api("GET", `/api/v1/reservations/${id}`, undefined, { quiet: true });
  if (r.ok) { state.current = r.data; renderReservation(); renderBookings(); }
}

function pendingPayment(res) {
  return (res.payments || []).find((p) => p.status === "PENDING");
}

let renderedResSig = null;
function renderReservation() {
  const box = $("#reservation");
  const res = state.current;
  // Re-render only when something visible changed, so buttons are not swapped out under a click.
  const sig = res ? JSON.stringify([res.id, res.effective_status, res.payments.map((p) => p.status)]) : "none";
  if (sig === renderedResSig) return;
  renderedResSig = sig;
  if (!res) { box.className = "reservation empty"; box.textContent = "No seat selected yet."; return; }
  box.className = "reservation";
  const st = res.effective_status;
  const pending = pendingPayment(res);
  const held = st === "HELD";
  let actions = "";
  if (held && !pending) {
    actions = `
      <button class="btn good" data-pay="SUCCESS">Pay: success</button>
      <button class="btn bad" data-pay="FAILURE">Pay: card declined</button>
      <button class="btn" data-pay="DELAYED">Pay: bank is slow</button>
      <button class="btn" data-cancel>Cancel hold</button>`;
  } else if (held && pending) {
    actions = `
      <button class="btn good" data-callback="SUCCEEDED">Bank says yes</button>
      <button class="btn bad" data-callback="FAILED">Bank says no</button>
      <button class="btn" data-cancel>Cancel hold</button>`;
  } else if (st === "CONFIRMED") {
    actions = `<button class="btn" data-cancel>Cancel booking (refund)</button>`;
  }
  const lateCb = pending || (res.payments || []).find((p) => p.status === "VOIDED" && p.scenario === "DELAYED");
  if (!held && lateCb) {
    actions += `<button class="btn" data-callback="SUCCEEDED" data-pid="${lateCb.id}" title="Simulate the bank answering after the hold ended">Late "yes" from bank</button>`;
  }
  const pays = (res.payments || []).map((p) =>
    `<li><span>#${p.attempt_number} ${p.payment_type} · ${p.scenario}</span><span class="chip ${p.status}">${p.status}</span></li>`
  ).join("");
  box.innerHTML = `
    <div class="res-top">
      <div class="res-seat">Seat ${res.seat_number}</div>
      <span class="chip ${st}">${st}</span>
    </div>
    <div class="countdown" id="countdown"></div>
    <div class="actions">${actions || '<span class="muted">This reservation is finished. Pick another seat.</span>'}</div>
    ${pays ? `<ul class="payments">${pays}</ul>` : ""}
    <p class="muted small">Reservation ${esc(res.id)}</p>`;
  tickCountdown();
}

function tickCountdown() {
  const el = document.getElementById("countdown");
  const res = state.current;
  if (!el || !res) return;
  if (res.effective_status === "HELD") {
    const left = Math.max(0, (Date.parse(res.expires_at) - serverNow()) / 1000);
    const m = Math.floor(left / 60), s = Math.floor(left % 60);
    el.innerHTML = `Hold expires in <b>${m}:${String(s).padStart(2, "0")}</b>`;
    if (left <= 0) refresh();
  } else if (res.effective_status === "CONFIRMED") {
    el.textContent = "Booked. Enjoy the concert!";
  } else if (res.effective_status === "EXPIRED") {
    el.textContent = "The hold ran out before payment, so the seat was released.";
  } else {
    el.textContent = "Cancelled. The seat is free for others.";
  }
}

$("#reservation").addEventListener("click", async (ev) => {
  const res = state.current;
  if (!res) return;
  const b = ev.target.closest("button");
  if (!b) return;
  let r;
  if (b.dataset.pay) {
    r = await act("POST", `/api/v1/reservations/${res.id}/confirm`, { scenario: b.dataset.pay });
  } else if (b.dataset.callback) {
    const pid = b.dataset.pid || (pendingPayment(res) || {}).id;
    r = await act("POST", `/api/v1/payments/${pid}/simulate-callback`, { outcome: b.dataset.callback });
  } else if ("cancel" in b.dataset) {
    r = await act("POST", `/api/v1/reservations/${res.id}/cancel`);
  }
  if (r && r.data) toast(r.data.message || (r.data.error && r.data.error.message) || `HTTP ${r.status}`);
  await selectReservation(res.id);
});

// ---------------------------------------------------------------- bookings

function renderBookings() {
  const ul = $("#bookings");
  const html = !state.bookings.length
    ? `<li class="empty">Nothing yet. Click a seat to start.</li>`
    : state.bookings.map((r) => {
      const sel = state.current && state.current.id === r.id ? " selected" : "";
      return `<li class="${sel}" data-id="${r.id}"><span>Event ${r.event_id} · Seat ${r.seat_number}</span>` +
        `<span class="chip ${r.effective_status}">${r.effective_status}</span></li>`;
    }).join("");
  if (ul.innerHTML !== html) ul.innerHTML = html;
}
$("#bookings").addEventListener("click", (ev) => {
  const li = ev.target.closest("li[data-id]");
  if (li) selectReservation(li.dataset.id);
});

// ---------------------------------------------------------------- stampede demo

$("#stampede-go").addEventListener("click", async () => {
  const seat = Number($("#stampede-seat").value);
  const count = Math.min(300, Math.max(2, Number($("#stampede-count").value) || 100));
  const s = state.seats.get(seat);
  if (!s) { toast("That seat does not exist for this event."); return; }
  if (s.status !== "AVAILABLE") { toast(`Seat ${seat} is not available; pick a free seat.`); return; }
  const btn = $("#stampede-go");
  btn.disabled = true;
  $("#stampede-result").textContent = `Sending ${count} requests...`;
  const tag = Math.random().toString(36).slice(2, 6).toUpperCase();
  const started = performance.now();
  const results = await Promise.all(Array.from({ length: count }, (_, i) =>
    api("POST", "/api/v1/reservations",
      { event_id: state.eventId, seat_number: seat, customer_ref: `RUSH${tag}-${String(i + 1).padStart(3, "0")}` },
      { key: newKey(), quiet: true })));
  const ms = Math.round(performance.now() - started);
  const tally = {};
  for (const r of results) tally[r.status] = (tally[r.status] || 0) + 1;
  const winner = results.find((r) => r.status === 201);
  const parts = Object.keys(tally).sort().map((k) => `${k} × ${tally[k]}`).join(", ");
  $("#stampede-result").innerHTML =
    `<div class="big">${esc(parts)}</div>` +
    `<div>${winner ? `Winner: ${esc(winner.data.reservation.customer_ref)}. ` : ""}` +
    `${tally[409] || 0} students were told the seat is taken. ${ms} ms total.</div>`;
  log("POST", "/api/v1/reservations", winner ? 201 : 409, { result: `stampede: ${parts}` }, false);
  btn.disabled = false;
  await refresh();
});

// ---------------------------------------------------------------- identity, events, refresh

function validMe() {
  if (/^[A-Za-z0-9_.@:-]{1,128}$/.test(state.me)) return true;
  toast("Your id may use letters, digits and _ . @ : - only.");
  $("#me").focus();
  return false;
}
$("#me").addEventListener("change", async () => {
  state.me = $("#me").value.trim();
  if (!validMe()) return;
  store("lastseat.me", state.me);
  state.current = null;
  renderReservation();
  await refresh();
});

$("#event-select").addEventListener("change", async () => {
  state.eventId = Number($("#event-select").value);
  store("lastseat.event", String(state.eventId));
  state.current = null;
  renderReservation();
  await refresh();
});

async function loadEvents() {
  const r = await api("GET", "/api/v1/events", undefined, { quiet: true });
  if (!r.ok) return;
  state.events = r.data.events;
  const saved = Number(load("lastseat.event"));
  const pick = state.events.find((e) => e.id === saved) || state.events[0];
  $("#event-select").innerHTML = state.events.map((e) =>
    `<option value="${e.id}">${esc(e.name)} (${e.total_seats} seats)</option>`).join("");
  if (pick) { state.eventId = pick.id; $("#event-select").value = String(pick.id); }
}

let refreshing = false;
async function refresh() {
  if (refreshing || !state.eventId) return;
  refreshing = true;
  try {
    const [seats, ev, mine] = await Promise.all([
      api("GET", `/api/v1/events/${state.eventId}/seats`, undefined, { quiet: true }),
      api("GET", `/api/v1/events/${state.eventId}`, undefined, { quiet: true }),
      /^[A-Za-z0-9_.@:-]{1,128}$/.test(state.me)
        ? api("GET", `/api/v1/customers/${encodeURIComponent(state.me)}/bookings`, undefined, { quiet: true })
        : Promise.resolve({ ok: false }),
    ]);
    if (seats.ok) {
      if (state.builtFor !== state.eventId) {
        lastHovered = null;
        $("#hover-readout").textContent = "Click an available seat to hold it.";
      }
      state.clockOffset = Date.parse(seats.data.as_of) - Date.now();
      state.seats = new Map(seats.data.seats.map((s) => [s.seat_number, s]));
      if (state.builtFor !== state.eventId) buildGrid(seats.data.count);
    }
    if (ev.ok) {
      const s = ev.data.seat_summary;
      $("#summary").innerHTML = `<span><b>${s.available}</b> available</span><span><b>${s.held}</b> held</span>` +
        `<span><b>${s.confirmed}</b> booked</span><span>of ${s.total}</span>`;
    }
    state.bookings = mine.ok ? mine.data.reservations : [];
    if (state.current) {
      const fresh = await api("GET", `/api/v1/reservations/${state.current.id}`, undefined, { quiet: true });
      if (fresh.ok) state.current = fresh.data;
    }
    renderSeats();
    renderBookings();
    renderReservation();
  } finally {
    refreshing = false;
  }
}

(async function start() {
  state.me = load("lastseat.me") || `22BCE${Math.floor(1000 + Math.random() * 9000)}`;
  store("lastseat.me", state.me);
  $("#me").value = state.me;
  renderLog();
  await loadEvents();
  await refresh();
  setInterval(refresh, REFRESH_MS);
  setInterval(tickCountdown, 250);
})();

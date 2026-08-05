/* dash — widget UI.
 * Layout (order + hidden) persisted in localStorage, no login.
 * Every widget: {id, title, full?, refresh, render(el, data)}.
 */
"use strict";

const LS_KEY = "dash.widgets.v1";
const POLL = 10000;       // default widget refresh
const FEED_POLL = 5000;   // live feed refresh

/* ------------------------------------------------------------- widgets */

async function jget(url) {
  const r = await fetch(url, { headers: { Accept: "application/json" } });
  if (!r.ok) throw new Error(url + " -> " + r.status);
  return r.json();
}

const fmtTime = (s) => {
  if (!s) return "—";
  const d = new Date(s);
  return isNaN(d) ? String(s) : d.toLocaleTimeString([], { hour12: false });
};

const WIDGETS = {
  cluster: {
    title: "Cluster",
    refresh: POLL,
    async load() { return jget("/api/overview"); },
    render(el, d) {
      if (d.error) { el.innerHTML = `<div class="err">${d.error}</div>`; return; }
      const nodes = d.nodes || [];
      const okNodes = nodes.filter(n => n.ready !== false).length;
      const deps = d.deployments || [];
      const okDeps = deps.filter(x => x.ready >= x.desired).length;
      const pods = d.pods || [];
      const running = pods.filter(p => p.phase === "Running").length;
      el.innerHTML = `
        <div class="hero">
          <div class="stat"><b class="${okNodes === nodes.length && nodes.length ? 'ok' : 'bad'}">${okNodes}/${nodes.length}</b><span>nodes</span></div>
          <div class="stat"><b class="${okDeps === deps.length && deps.length ? 'ok' : 'bad'}">${okDeps}/${deps.length}</b><span>deploys</span></div>
          <div class="stat"><b>${running}/${pods.length}</b><span>pods run</span></div>
          <div class="stat"><b>${(d.namespaces || []).length}</b><span>namespaces</span></div>
        </div>
        ${nodes.map(n => `
          <div class="row">
            <span class="pill"><span class="dot ${n.ready === false ? 'bad' : 'ok'}"></span>${n.ready === false ? 'DOWN' : 'UP'}</span>
            <span class="grow svc-name">${n.name}</span>
            <span class="muted mono">${n.ip}</span>
            <span class="muted">${n.role}</span>
          </div>`).join("")}
        ${deps.filter(x => x.ready < x.desired).slice(0, 6).map(x => `
          <div class="row"><span class="pill"><span class="dot bad"></span>${x.ready}/${x.desired}</span>
          <span class="grow svc-name">${x.name}</span><span class="muted">${x.namespace}</span></div>`).join("")}`;
    },
  },

  services: {
    title: "Services · web UIs",
    full: true,
    refresh: POLL,
    async load() { return jget("/api/services"); },
    render(el, d) {
      const list = d.services || [];
      if (list.error) { el.innerHTML = `<div class="err">${list.error}</div>`; return; }
      const up = list.filter(s => s.probe && s.probe.up).length;
      el.innerHTML = `
        <div class="hero">
          <div class="stat"><b class="ok">${up}</b><span>up</span></div>
          <div class="stat"><b class="bad">${list.length - up}</b><span>down</span></div>
          <div class="stat"><b>${list.length}</b><span>routes</span></div>
        </div>
        ${list.map(s => {
          const p = s.probe || {};
          const cls = p.up ? "ok" : (p.status ? "warn" : "bad");
          const label = p.up ? `${p.status} ${p.ms}ms` : (p.status ? `${p.status}` : "unreachable");
          return `<div class="row">
            <span class="pill"><span class="dot ${cls}"></span>${label}</span>
            <span class="grow"><span class="svc-name">${s.service}</span><br><span class="svc-host">${s.host}</span></span>
            <span class="muted">${s.namespace}</span>
          </div>`;
        }).join("")}`;
    },
  },

  health: {
    title: "Health checks",
    full: true,
    refresh: POLL,
    async load() { return jget("/api/services"); },
    render(el, d) {
      const list = d.services || [];
      const rows = list.map(s => {
        const p = s.probe || {};
        const cls = p.up ? "ok" : (p.status ? "warn" : "bad");
        const status = p.up ? "OK" : (p.status ? `HTTP ${p.status}` : "DOWN");
        const detail = p.detail && p.detail.length > 80 ? p.detail.slice(0, 80) + "…" : (p.detail || "");
        return `<div class="row">
          <span class="pill"><span class="dot ${cls}"></span>${status}</span>
          <span class="grow"><span class="svc-name">${s.service}</span>
            ${detail ? `<br><span class="muted">${detail}</span>` : ""}</span>
          <span class="right muted mono">${p.ms != null ? p.ms + "ms" : ""}</span>
        </div>`;
      }).join("");
      el.innerHTML = rows || `<div class="empty">no services</div>`;
    },
  },

  queues: {
    title: "Worker queues · live",
    full: true,
    refresh: FEED_POLL,
    async load() { return jget("/api/queues"); },
    render(el, d) {
      const dl = d.downloads || {};
      const act = d.actions || {};
      let html = `<div class="hero">`;
      if (dl.counts) {
        for (const st of ["queued", "resolving", "downloading", "done", "error"]) {
          if (dl.counts[st]) {
            const cls = st === "error" ? "bad" : (st === "done" ? "ok" : "warn");
            html += `<div class="stat"><b class="${cls}">${dl.counts[st]}</b><span>${st}</span></div>`;
          }
        }
      } else if (dl.error) {
        html += `<div class="err">${dl.error}</div>`;
      }
      html += `</div>`;
      if (act.by_status) {
        html += `<div class="row"><span class="muted">Actions:</span>
          <span class="pill"><span class="dot warn"></span>${act.by_status.running || 0} running</span>
          <span class="pill"><span class="dot"></span>${act.by_status.queued || 0} queued</span>
          <span class="pill"><span class="dot ok"></span>${act.by_conclusion.success || 0} success</span>
          <span class="pill"><span class="dot bad"></span>${act.by_conclusion.failure || 0} fail</span></div>`;
      }
      if (act.recent && act.recent.length) {
        html += `<div class="feed">${act.recent.map(r => `
          <div class="row"><span class="pill"><span class="dot ${r.conclusion === "success" ? "ok" : (r.conclusion === "failure" ? "bad" : "warn")}"></span>#${r.run_number || r.id}</span>
          <span class="grow mono">${r.sha || ""}</span><span class="muted">${r.status}</span></div>`).join("")}</div>`;
      }
      el.innerHTML = html;
    },
  },

  gitea: {
    title: "Gitea · commits & builds",
    full: true,
    refresh: POLL,
    async load() { return jget("/api/gitea"); },
    render(el, d) {
      if (d.error) { el.innerHTML = `<div class="err">${d.error}</div>`; return; }
      const repos = d.repos || [];
      if (!repos.length) { el.innerHTML = `<div class="empty">no repos</div>`; return; }
      el.innerHTML = repos.map(r => {
        const lastRun = (r.runs || [])[0];
        const runBadge = lastRun
          ? `<span class="pill"><span class="dot ${lastRun.conclusion === "success" ? "ok" : (lastRun.conclusion === "failure" ? "bad" : "warn")}"></span>#${lastRun.run_number || lastRun.id} ${lastRun.status || ""}</span>`
          : `<span class="muted">no builds</span>`;
        const commits = (r.commits || []).slice(0, 3).map(c =>
          `<div class="row"><span class="mono muted">${c.sha}</span><span class="grow">${c.message}</span><span class="muted">${fmtTime(c.date)}</span></div>`).join("");
        return `<div class="widget-inner">
          <div class="row"><span class="grow svc-name">${r.name}</span>${runBadge}</div>
          ${commits}
        </div>`;
      }).join("");
    },
  },

  telemetry: {
    title: "Telemetry · live feed",
    full: true,
    refresh: FEED_POLL,
    async load() { return jget("/api/telemetry?limit=60"); },
    render(el, d) {
      if (d.error) { el.innerHTML = `<div class="err">${d.error}</div>`; return; }
      const rows = d.rows || [];
      el.innerHTML = `<div class="feed">${
        rows.map(r => `<div class="row">
          <span class="t">${fmtTime(r.ts)}</span>
          <span class="muted mono">${r.ip || ""}</span>
          <span class="b">${(r.body || "").replace(/</g, "&lt;")}</span>
        </div>`).join("") || `<div class="empty">no telemetry yet</div>`}</div>`;
    },
  },
};

/* ------------------------------------------------------- layout engine */

function loadState() {
  try {
    const raw = JSON.parse(localStorage.getItem(LS_KEY) || "{}");
    return { order: Array.isArray(raw.order) ? raw.order : [], hidden: Array.isArray(raw.hidden) ? raw.hidden : [] };
  } catch { return { order: [], hidden: [] }; }
}
function saveState(s) { localStorage.setItem(LS_KEY, JSON.stringify(s)); }

const grid = document.getElementById("grid");
const tray = document.getElementById("add-tray");
const connDot = document.getElementById("conn-dot");
const clock = document.getElementById("clock");
const lastUpd = document.getElementById("last-updated");

const state = loadState();
const defaultOrder = Object.keys(WIDGETS);
// merge: anything the user hid stays hidden; new widgets appear at the end
let order = [...new Set([...state.order, ...defaultOrder])].filter(id => WIDGETS[id]);
let hidden = new Set(state.hidden.filter(id => WIDGETS[id]));

const cards = new Map();   // id -> {el, body, timer, data}

function renderClock() {
  clock.textContent = new Date().toLocaleTimeString([], { hour12: false });
}
setInterval(renderClock, 1000);

async function refreshWidget(id) {
  const w = WIDGETS[id];
  const card = cards.get(id);
  if (!w || !card || hidden.has(id)) return;
  try {
    const data = await w.load();
    card.data = data;
    card.body.innerHTML = `<div class="spin" style="margin:8px"></div>`;
    w.render(card.body, data);
    connDot.classList.add("ok"); connDot.classList.remove("bad");
    lastUpd.textContent = "updated " + new Date().toLocaleTimeString([], { hour12: false });
  } catch (e) {
    card.body.innerHTML = `<div class="err">${e.message}</div>`;
    connDot.classList.add("bad"); connDot.classList.remove("ok");
  }
}

function buildCard(id) {
  const w = WIDGETS[id];
  const card = document.createElement("section");
  card.className = "widget" + (w.full ? " full" : "");
  card.dataset.id = id;
  card.innerHTML = `
    <div class="widget-head">
      <h2>${w.title}</h2>
      <div class="widget-tools">
        <button class="btn mini" data-act="up" title="Move up">▲</button>
        <button class="btn mini" data-act="down" title="Move down">▼</button>
        <button class="btn mini" data-act="hide" title="Hide">✕</button>
      </div>
    </div>
    <div class="widget-body"><div class="spin" style="margin:8px"></div></div>`;
  card.querySelectorAll("[data-act]").forEach(btn => {
    btn.addEventListener("click", () => {
      const act = btn.dataset.act;
      if (act === "hide") { setHidden(id, true); return; }
      move(id, act === "up" ? -1 : 1);
    });
  });
  return card;
}

function setHidden(id, hide) {
  if (hide) hidden.add(id); else hidden.delete(id);
  saveState({ order, hidden: [...hidden] });
  renderGrid();
  if (!hide) refreshWidget(id);
}

function move(id, delta) {
  const i = order.indexOf(id);
  const j = i + delta;
  if (i < 0 || j < 0 || j >= order.length) return;
  order.splice(i, 1);
  order.splice(j, 0, id);
  saveState({ order, hidden: [...hidden] });
  renderGrid();
}

function renderGrid() {
  grid.innerHTML = "";
  cards.clear();
  const visible = order.filter(id => !hidden.has(id));
  for (const id of visible) {
    const card = buildCard(id);
    grid.appendChild(card);
    cards.set(id, { el: card, body: card.querySelector(".widget-body") });
  }
  // add tray with hidden widgets
  tray.innerHTML = "";
  const hiddenIds = order.filter(id => hidden.has(id));
  if (hiddenIds.length) {
    hiddenIds.forEach(id => {
      const b = document.createElement("button");
      b.className = "btn";
      b.textContent = "＋ " + WIDGETS[id].title;
      b.addEventListener("click", () => setHidden(id, false));
      tray.appendChild(b);
    });
    tray.classList.remove("hidden");
  } else {
    tray.classList.add("hidden");
  }
}

function startPolling() {
  for (const id of order) {
    if (hidden.has(id)) continue;
    refreshWidget(id);
    const w = WIDGETS[id];
    setInterval(() => refreshWidget(id), w.refresh);
  }
}

document.getElementById("btn-add").addEventListener("click", () => tray.classList.toggle("hidden"));
document.getElementById("btn-reset").addEventListener("click", () => {
  localStorage.removeItem(LS_KEY);
  location.reload();
});

renderGrid();
startPolling();

/* Barkeep's Ledger — front-of-house single-page app (no build step). */

const TOKEN_KEY = "ledger_token";
const LOC_KEY = "ledger_active_loc";
let CONFIG = {};
let VENDOR_NAMES = null;  // per-store autocomplete cache; reset on store switch
// The store this device is viewing. Sent as X-Location-Id on every request so the
// backend scopes per-request (no shared global "current store"). Per-device.
let ACTIVE_LOC = Number(localStorage.getItem(LOC_KEY)) || null;

/* ---------- category taxonomy (Category Type -> Category) ---------- */
let CATEGORIES = null;   // cache of /api/categories
async function loadCategories() {
  if (!CATEGORIES) CATEGORIES = await api("GET", "/api/categories");
  return CATEGORIES;
}
const TYPE_CLASS = {
  "Food": "type-Food", "Beer": "type-Beer", "Wine": "type-Wine",
  "Liquor": "type-Liquor", "N/A Bev": "type-Bev", "Other": "type-Other",
};
function typePill(t) {
  return `<span class="pill ${TYPE_CLASS[t] || "type-Other"}">${esc(t || "—")}</span>`;
}
const STATUS_LABEL = {
  processing: "Processing", action_required: "Action Req", closed: "Closed",
  reviewed: "Reviewed", new: "New",
};
function statusPill(s) {
  s = s || "closed";
  return `<span class="pill st-${esc(s)}">${esc(STATUS_LABEL[s] || s)}</span>`;
}

/* ---------- tiny helpers ---------- */
const $ = (sel, root = document) => root.querySelector(sel);
const view = () => $("#view");
const money = (n) => (n == null ? "—" : "$" + Number(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
const pct = (n) => (n == null ? "—" : Number(n).toFixed(1) + "%");
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/* ---------- recipe unit conversion (mirrors units.py) ---------- */
const RECIPE_UNITS = ["oz", "ml", "L", "tsp", "tbsp", "cup", "pt", "qt", "gal", "shot", "dash", "each", "g", "kg", "lb"];
// Keep these in lockstep with units.py (a parity test asserts the alias sets match).
const _UNIT_VOL = { ml: 1, milliliter: 1, millilitre: 1, cc: 1, l: 1000, liter: 1000, litre: 1000, oz: 29.5735, floz: 29.5735, "fl oz": 29.5735, ounce: 29.5735, tsp: 4.92892, tbsp: 14.7868, dash: 0.92, cup: 236.588, pt: 473.176, pint: 473.176, qt: 946.353, quart: 946.353, gal: 3785.41, gallon: 3785.41, shot: 44.3603, jigger: 44.3603 };
const _UNIT_WT = { g: 1, gram: 1, gm: 1, kg: 1000, kilo: 1000, kilogram: 1000, lb: 453.592, lbs: 453.592, pound: 453.592 };
const _UNIT_CT = { each: 1, ea: 1, unit: 1, ct: 1, count: 1, piece: 1 };
function _unitFactor(u) {
  u = (u || "").trim().toLowerCase();
  if (u in _UNIT_VOL) return ["v", _UNIT_VOL[u]];
  if (u in _UNIT_WT) return ["w", _UNIT_WT[u]];
  if (u in _UNIT_CT) return ["c", _UNIT_CT[u]];
  return [null, null];
}
function convertUnit(qty, from, to) {
  const [fd, ff] = _unitFactor(from), [td, tf] = _unitFactor(to);
  if (!fd || !td || fd !== td) return null;
  return (qty * ff) / tf;
}

function el(html) {
  const t = document.createElement("template");
  t.innerHTML = html.trim();
  // Single root → return the element (callers may query/listen on it).
  // Multiple roots → return the fragment so appendChild adds them all.
  return t.content.childElementCount === 1 ? t.content.firstElementChild : t.content;
}

let toastTimer;
function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), 2600);
}

async function api(method, path, body, isForm) {
  const headers = {};
  const token = localStorage.getItem(TOKEN_KEY);
  if (token) headers.Authorization = "Bearer " + token;
  if (ACTIVE_LOC) headers["X-Location-Id"] = String(ACTIVE_LOC);
  const opts = { method, headers };
  if (body != null) {
    if (isForm) { opts.body = body; }
    else { headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  }
  const res = await fetch(path, opts);
  if (res.status === 401) {
    localStorage.removeItem(TOKEN_KEY);
    showGate();
    throw new Error("unauthorized");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw Object.assign(new Error(data.error || ("HTTP " + res.status)), { data });
  return data;
}

// Fetch a file with the same auth + active-store headers as api() (a plain
// <a download> link would carry neither), then save it via a Blob URL.
async function download(path, filename) {
  const headers = {};
  const token = localStorage.getItem(TOKEN_KEY);
  if (token) headers.Authorization = "Bearer " + token;
  if (ACTIVE_LOC) headers["X-Location-Id"] = String(ACTIVE_LOC);
  const res = await fetch(path, { headers });
  if (res.status === 401) { localStorage.removeItem(TOKEN_KEY); showGate(); throw new Error("unauthorized"); }
  if (!res.ok) throw new Error("HTTP " + res.status);
  const url = URL.createObjectURL(await res.blob());
  const a = document.createElement("a");
  a.href = url; a.download = filename;
  document.body.appendChild(a); a.click(); a.remove();
  // Defer the revoke a tick: a.click() only schedules the download, so revoking
  // in the same tick can cancel it / yield an empty file in some browsers.
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

/* ---------- date helpers ---------- */
const iso = (d) => d.toISOString().slice(0, 10);
function weekStart(d = new Date()) { const x = new Date(d); const day = (x.getDay() + 6) % 7; x.setDate(x.getDate() - day); return x; }
function monthStart(d = new Date()) { return new Date(d.getFullYear(), d.getMonth(), 1); }

// Keep a From/To pair coherent: the picker is constrained (To can't precede From
// and vice-versa), and a typed reversed range is clamped + flagged. `onChange`
// fires only after the range is made coherent.
function linkDates(startEl, endEl, onChange) {
  const sync = () => {
    endEl.min = startEl.value || "";
    startEl.max = endEl.value || "";
  };
  const handle = (e) => {
    if (startEl.value && endEl.value && startEl.value > endEl.value) {
      if (e && e.target === startEl) endEl.value = startEl.value;
      else startEl.value = endEl.value;
      toast("From can’t be after To — adjusted.");
    }
    sync();
    if (onChange) onChange();
  };
  sync();
  startEl.addEventListener("change", handle);
  endEl.addEventListener("change", handle);
}

/* ============================================================
   AUTH GATE
   ============================================================ */
function showGate() {
  $("#app").classList.add("hidden");
  $("#gate").classList.remove("hidden");
}
function showApp() {
  $("#gate").classList.add("hidden");
  $("#app").classList.remove("hidden");
  initLocationSwitch();
  if (!location.hash) location.hash = "#/dashboard";
  else route();
}

/* ---------- location switcher (topbar) ---------- */
async function initLocationSwitch() {
  const sel = $("#loc-switch");
  try {
    const d = await api("GET", "/api/locations");
    // Adopt the server's default store if this device hasn't chosen a valid one.
    if (d.active && (!ACTIVE_LOC || !d.locations.some((l) => l.id === ACTIVE_LOC))) {
      ACTIVE_LOC = d.active;
      localStorage.setItem(LOC_KEY, ACTIVE_LOC);
    }
    sel.innerHTML = d.locations.map((l) =>
      `<option value="${l.id}" ${l.id === ACTIVE_LOC ? "selected" : ""}>${esc(l.name)}</option>`).join("");
    sel.onchange = async () => {
      ACTIVE_LOC = Number(sel.value);
      localStorage.setItem(LOC_KEY, ACTIVE_LOC);   // per-device choice, sent as X-Location-Id
      VENDOR_NAMES = null;   // vendors are per-store — drop the other store's autocomplete cache
      try {
        await api("PUT", "/api/active-location", { location_id: ACTIVE_LOC });  // persist default
        CONFIG = await api("GET", "/api/config").catch(() => CONFIG);
        route();  // re-render the current screen against the new active store
      } catch (e) { toast(e.message); }
    };
  } catch (e) { sel.style.display = "none"; }
}
$("#gate-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#gate-err").textContent = "";
  try {
    const r = await api("POST", "/api/login", { password: $("#gate-pw").value });
    localStorage.setItem(TOKEN_KEY, r.token || "");
    await boot();
  } catch (err) {
    $("#gate-err").textContent = err.message || "Wrong passcode.";
  }
});

/* ============================================================
   NAV + ROUTER
   ============================================================ */
const NAV = [
  { tab: "dashboard", icon: "\u{1F3E0}", label: "Home", href: "#/dashboard" },
  { tab: "orders", icon: "\u{1F9FE}", label: "Orders", href: "#/orders" },
  { tab: "performance", icon: "\u{1F4CA}", label: "Performance", href: "#/performance/category",
    children: [
      { label: "Category Report", href: "#/performance/category" },
      { label: "Controllable P&L", href: "#/performance/pl" },
      { label: "Sales", href: "#/performance/sales" },
      { label: "Price Movers", href: "#/performance/movers" },
    ] },
  { tab: "vendors", icon: "\u{1F69A}", label: "Vendors", href: "#/vendors",
    children: [
      { label: "Vendors", href: "#/vendors" },
      { label: "Vendor Items", href: "#/vendors/items" },
    ] },
  { tab: "products", icon: "\u{1F376}", label: "Products", href: "#/products",
    children: [
      { label: "All Products", href: "#/products" },
      { label: "Recipes", href: "#/recipes" },
      { label: "Categories", href: "#/categories" },
      { label: "New Item Review", href: "#/products/new" },
      { label: "Purchase Report", href: "#/products/purchase" },
    ] },
  { tab: "inventory", icon: "\u{1F4E6}", label: "Inventory", href: "#/inventory",
    children: [
      { label: "Stock", href: "#/inventory" },
      { label: "Count", href: "#/count" },
    ] },
  { tab: "settings", icon: "⚙", label: "Settings", href: "#/settings" },
];

// Map a route name -> the nav section it belongs under (for active highlight).
const SECTION_OF = {
  dashboard: "dashboard", orders: "orders", invoice: "orders",
  performance: "performance", vendors: "vendors", vendor: "vendors",
  products: "products", product: "products", recipes: "products",
  categories: "products",
  inventory: "inventory", count: "inventory", settings: "settings",
};

const ROUTES = {
  dashboard: renderDashboard,
  orders: renderOrders,
  invoice: renderInvoiceDetail,
  performance: renderPerformance,
  vendors: renderVendors,
  vendor: renderVendorDetail,
  products: renderProducts,
  product: renderProductDetail,
  recipes: renderRecipes,
  categories: renderCategoriesAdmin,
  inventory: renderInventory,
  count: renderCount,
  settings: renderSettings,
};

function buildNav() {
  const section = SECTION_OF[(location.hash.replace(/^#\//, "").split("/")[0]) || "dashboard"];
  const sub = location.hash.replace(/^#/, "");
  $("#nav").innerHTML = NAV.map((n) => {
    const active = n.tab === section;
    let html = `<a class="nav-link ${active ? "active" : ""}" href="${n.href}">
      <span class="ic">${n.icon}</span><span>${n.label}</span></a>`;
    if (n.children && active) {
      html += `<div class="nav-sub">` + n.children.map((c) =>
        `<a href="${c.href}" class="${c.href === sub ? "active" : ""}">${c.label}</a>`).join("") + `</div>`;
    }
    return html;
  }).join("");

  // Mobile bottom tab bar: top-level sections only, quick switching (CSS hides
  // it on desktop). Mirrors the sidebar's active section.
  const tb = $("#tabbar");
  if (tb) {
    tb.innerHTML = NAV.map((n) =>
      `<a href="${n.href}" class="${n.tab === section ? "active" : ""}">
        <span class="ic">${n.icon}</span><b>${esc(n.label)}</b></a>`).join("");
  }
}

function route() {
  const parts = (location.hash.replace(/^#\//, "") || "dashboard").split("/");
  const name = parts[0];
  const fn = ROUTES[name] || renderDashboard;
  buildNav();
  closeDrawer();
  view().innerHTML = "";
  fn(parts.slice(1));
  window.scrollTo(0, 0);
}
window.addEventListener("hashchange", route);

// Navigate, re-rendering even when the target hash equals the current one
// (forms opened imperatively leave the hash unchanged, so a plain assignment
// would fire no hashchange and the view would stay stuck on the form).
function go(hash) {
  if (location.hash === hash) route();
  else location.hash = hash;
}

function closeDrawer() {
  $("#sidebar").classList.remove("open");
  $("#scrim").classList.add("hidden");
}
function openDrawer() {
  $("#sidebar").classList.add("open");
  $("#scrim").classList.remove("hidden");
}
$("#navtoggle").addEventListener("click", openDrawer);
$("#scrim").addEventListener("click", closeDrawer);

/* ---------- theme toggle (light/dark, persisted per device) ---------- */
const THEME_KEY = "ledger_theme";
function effectiveTheme() {
  const set = document.documentElement.getAttribute("data-theme");
  if (set) return set;
  return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}
function applyThemeToggleIcon() {
  const btn = $("#theme-toggle");
  if (btn) btn.textContent = effectiveTheme() === "dark" ? "◐" : "◑";
}
$("#theme-toggle").addEventListener("click", () => {
  const next = effectiveTheme() === "dark" ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", next);
  try { localStorage.setItem(THEME_KEY, next); } catch (e) {}
  applyThemeToggleIcon();
});
applyThemeToggleIcon();

function loading() { view().innerHTML = '<div class="spinner"></div>'; }

/* ============================================================
   DATA TABLE (sortable, searchable; cards on mobile)
   ============================================================ */
function dataTable(columns, rows, opts = {}) {
  const { search = false, empty = "Nothing here yet.", initialSort = null, footer = null } = opts;
  const wrap = el(`<div class="dtable-wrap"></div>`);
  let q = "", sortKey = initialSort ? initialSort.key : null, sortDir = initialSort ? (initialSort.dir || 1) : 1;
  if (search) {
    const s = el(`<input class="dt-search" placeholder="Search…">`);
    s.addEventListener("input", () => { q = s.value.toLowerCase(); draw(); });
    wrap.appendChild(s);
  }
  const host = el(`<div class="dtable-scroll"></div>`);
  wrap.appendChild(host);

  const raw = (row, c) => (c.sortVal ? c.sortVal(row) : row[c.key]);
  function cmp(a, b) {
    const na = parseFloat(a), nb = parseFloat(b);
    if (!isNaN(na) && !isNaN(nb)) return na - nb;
    return String(a == null ? "" : a).localeCompare(String(b == null ? "" : b));
  }
  function view_rows() {
    let r = rows;
    if (q) r = r.filter((row) => columns.some((c) => String(raw(row, c) ?? "").toLowerCase().includes(q)));
    if (sortKey) {
      const c = columns.find((x) => x.key === sortKey);
      r = [...r].sort((a, b) => cmp(raw(a, c), raw(b, c)) * sortDir);
    }
    return r;
  }
  function draw() {
    const r = view_rows();
    const head = columns.map((c) => {
      const sortable = c.sortable !== false;
      const aria = sortKey === c.key ? ` aria-sort="${sortDir > 0 ? "ascending" : "descending"}"` : "";
      return `<th class="${c.align === "right" ? "r" : ""} ${sortable ? "sortable" : ""}"${sortable ? ' tabindex="0" role="button"' : ""}${aria} data-k="${esc(c.key)}">${esc(c.label)}${sortKey === c.key ? (sortDir > 0 ? " ▲" : " ▼") : ""}</th>`;
    }).join("");
    const body = r.length ? r.map((row) =>
      `<tr ${row._href ? `data-href="${esc(row._href)}" tabindex="0" role="link"` : ""}>` + columns.map((c) =>
        `<td class="${c.align === "right" ? "r" : ""} ${c.cls || ""}" data-label="${esc(c.label)}">${c.fmt ? c.fmt(row) : esc(row[c.key] ?? "")}</td>`).join("") + `</tr>`).join("")
      : `<tr><td class="dt-empty" colspan="${columns.length}">${esc(empty)}</td></tr>`;
    const foot = footer ? `<tfoot><tr>${columns.map((c) =>
      `<td class="${c.align === "right" ? "r" : ""}" data-label="${esc(c.label)}">${footer[c.key] != null ? footer[c.key] : ""}</td>`).join("")}</tr></tfoot>` : "";
    host.innerHTML = `<table class="dtable"><thead><tr>${head}</tr></thead><tbody>${body}</tbody>${foot}</table>`;
    host.querySelectorAll("th.sortable").forEach((th) => {
      const sort = () => {
        const k = th.dataset.k;
        if (sortKey === k) sortDir = -sortDir; else { sortKey = k; sortDir = 1; }
        draw();
      };
      th.addEventListener("click", sort);
      th.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); sort(); } });
    });
    host.querySelectorAll("tr[data-href]").forEach((tr) => {
      const go = () => { location.hash = tr.dataset.href; };
      tr.addEventListener("click", go);
      tr.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); } });
    });
  }
  draw();
  return wrap;
}

/* ============================================================
   DASHBOARD
   ============================================================ */
let dashRange = { start: iso(weekStart()), end: iso(new Date()), key: "week" };

async function renderDashboard() {
  const v = view();
  v.appendChild(el(`
    <div>
      <div class="chips" id="range-chips">
        <button class="chip" data-k="week">This Week</button>
        <button class="chip" data-k="month">This Month</button>
        <button class="chip" data-k="7">Last 7 Days</button>
        <button class="chip" data-k="30">Last 30</button>
        <button class="chip" data-k="custom">Custom</button>
      </div>
      <div id="custom-range" class="daterow hidden">
        <label class="fld"><span>From</span><input type="date" id="d-start"></label>
        <label class="fld"><span>To</span><input type="date" id="d-end"></label>
        <button class="btn btn-brass btn-sm" id="d-go">Go</button>
      </div>
      <div id="dash-body"></div>
    </div>`));

  const chips = $("#range-chips");
  chips.addEventListener("click", (e) => {
    const b = e.target.closest(".chip"); if (!b) return;
    setRange(b.dataset.k);
  });
  linkDates($("#d-start"), $("#d-end"));  // keep custom From/To coherent
  $("#d-go").addEventListener("click", () => {
    if (!$("#d-start").value || !$("#d-end").value) { toast("Pick both From and To."); return; }
    dashRange = { start: $("#d-start").value, end: $("#d-end").value, key: "custom" };
    loadDash();
  });
  setRange(dashRange.key, true);
}

function setRange(key, silent) {
  const today = new Date();
  if (key === "week") dashRange = { start: iso(weekStart()), end: iso(today), key };
  else if (key === "month") dashRange = { start: iso(monthStart()), end: iso(today), key };
  else if (key === "7") dashRange = { start: iso(new Date(Date.now() - 6 * 864e5)), end: iso(today), key };
  else if (key === "30") dashRange = { start: iso(new Date(Date.now() - 29 * 864e5)), end: iso(today), key };
  else key = "custom";
  document.querySelectorAll("#range-chips .chip").forEach((c) =>
    c.classList.toggle("active", c.dataset.k === dashRange.key));
  $("#custom-range").classList.toggle("hidden", dashRange.key !== "custom");
  if (dashRange.key === "custom") {
    $("#d-start").value = dashRange.start || iso(weekStart());
    $("#d-end").value = dashRange.end || iso(new Date());
  }
  if (!silent || dashRange.key !== "custom") loadDash();
}

async function loadDash() {
  const body = $("#dash-body");
  body.innerHTML = '<div class="spinner"></div>';
  let d;
  try {
    d = await api("GET", `/api/dashboard?start=${dashRange.start}&end=${dashRange.end}`);
  } catch (e) { body.innerHTML = `<p class="err">${esc(e.message)}</p>`; return; }

  const tCogs = d.targets.cogs, tLabor = d.targets.labor;
  const cogsCls = ratingClass(d.cogs_pct, tCogs);
  const laborCls = ratingClass(d.labor_pct, tLabor);
  const primeTarget = tCogs + tLabor;
  const primeCls = ratingClass(d.prime_pct, primeTarget);

  body.innerHTML = "";
  if (!d.square_configured) {
    body.appendChild(el(`<div class="note">Square isn&rsquo;t connected yet — sales &amp; labor read $0.
      Add your access token in <a href="#/settings" class="linkbtn">Settings</a>.</div>`));
  } else if (d.sales_error || d.labor_error) {
    body.appendChild(el(`<div class="note">Square says: ${esc(d.sales_error || d.labor_error)}</div>`));
  }
  if (d.labor_warning) {
    body.appendChild(el(`<div class="note">${esc(d.labor_warning)}</div>`));
  }

  // Proactive price-increase alerts, fetched separately so a hiccup here never
  // breaks the dashboard. Only shown when there's something to flag.
  try {
    const pa = await api("GET", "/api/alerts/price-increases");
    if (pa.count) {
      const ac = el(`<div class="card"><div class="card-band">⚠ Price Increases
        <span>${pa.count}</span></div><div class="card-body" id="pa"></div></div>`);
      const box = ac.querySelector("#pa");
      box.appendChild(el(`<p class="muted" style="font-size:.82rem;margin:0 0 .4rem">Vendor items up
        ${pct(pa.min_pct)}+ vs their prior price, purchased in the last ${pa.lookback_days} days.</p>`));
      pa.alerts.forEach((a) => box.appendChild(el(
        `<div class="kv"><span>${esc(a.name)} <span class="muted">&middot; ${esc(a.vendor)}</span></span>
          <b class="bad">${money(a.old_price)} &rarr; ${money(a.new_price)}
          <span class="muted">(+${pct(a.change_pct)})</span></b></div>`)));
      body.appendChild(ac);
    }
  } catch (e) { /* best-effort: alerts never block the dashboard */ }

  body.appendChild(el(`
    <div class="stat-grid">
      <div class="stat wide accent-ind">
        <div class="label">Net Sales &middot; Square</div>
        <div class="value">${money(d.sales)}</div>
        <div class="sub">${d.orders} orders &middot; ${esc(rangeLabel())}</div>
      </div>

      <div class="stat accent-ox">
        <div class="label">COGS</div>
        <div class="value ${cogsCls}">${pct(d.cogs_pct)}</div>
        <div class="sub">${money(d.cogs)} &middot; ${d.cogs_method === "usage" ? "usage-based" : "purchases"}</div>
        ${bar(d.cogs_pct, tCogs)}
      </div>

      <div class="stat accent-grn">
        <div class="label">Labor</div>
        <div class="value ${laborCls}">${pct(d.labor_pct)}</div>
        <div class="sub">${money(d.labor)} &middot; ${Number(d.labor_hours).toFixed(1)} hrs</div>
        ${bar(d.labor_pct, tLabor)}
      </div>

      <div class="stat wide">
        <div class="label">Prime Cost</div>
        <div class="value ${primeCls}">${pct(d.prime_pct)}</div>
        <div class="sub">${money(d.prime)} (COGS + Labor) &middot; target ${primeTarget}%</div>
        ${bar(d.prime_pct, primeTarget)}
      </div>
    </div>`));

  // Purchases breakdown
  const cats = Object.entries(d.purchases_by_category || {});
  const card = el(`<div class="card"><div class="card-band">Purchases Logged
      <span>${money(d.purchases)}</span></div><div class="card-body" id="pb"></div></div>`);
  const pb = card.querySelector("#pb");
  if (!cats.length) pb.appendChild(el(`<p class="muted center">No invoices in this range.</p>`));
  else cats.sort((a, b) => b[1] - a[1]).forEach(([c, amt]) =>
    pb.appendChild(el(`<div class="kv"><span>${typePill(c)}</span><b>${money(amt)}</b></div>`)));
  body.appendChild(card);

  if (d.usage_period) {
    const up = d.usage_period;
    body.appendChild(el(`<div class="note">Usage-based COGS, measured between your counts
      (${up.start} &rarr; ${up.end}): open ${money(d.begin_inventory.value)} + buys
      ${money(up.purchases)} &minus; close ${money(d.end_inventory.value)} = <b>${money(d.cogs)}</b>.
      <span class="muted">&ldquo;Buys&rdquo; covers the count window, not just the selected range.</span></div>`));
  } else {
    body.appendChild(el(`<div class="note muted">Tip: take an inventory count at the start and end of a
      period and COGS switches from purchases-based to true usage-based automatically.</div>`));
  }
}

function rangeLabel() {
  const labels = { week: "this week", month: "this month", "7": "last 7 days", "30": "last 30 days" };
  return labels[dashRange.key] || `${dashRange.start} → ${dashRange.end}`;
}
function ratingClass(val, target) {
  if (val == null) return "";
  if (val <= target) return "good";
  if (val <= target * 1.1) return "warn";
  return "bad";
}
function bar(val, target) {
  if (val == null) return "";
  const max = Math.max(target * 1.6, val, 1);
  const w = Math.min((val / max) * 100, 100);
  const tickPos = Math.min((target / max) * 100, 100);
  return `<div class="bar ${val > target ? "over" : ""}"><span style="width:${w}%"></span>
    <i class="tick" style="left:${tickPos}%"></i></div>`;
}

/* ============================================================
   INVOICES
   ============================================================ */
async function renderOrders() {
  const v = view();
  v.appendChild(el(`
    <h2 class="section section-head">Orders</h2>
    <div class="btn-row">
      <button class="btn btn-brass" id="snap">&#x1F4F7; Photograph Invoice</button>
      <button class="btn btn-ghost" id="manual">Enter by Hand</button>
    </div>
    <input type="file" id="file" accept="image/*" capture="environment" class="hidden">
    <div class="filters">
      <label class="fld"><span>From</span><input type="date" id="o-start"></label>
      <label class="fld"><span>To</span><input type="date" id="o-end"></label>
      <label class="fld"><span>Vendor</span><select id="o-vendor"><option value="">All</option></select></label>
      <label class="fld"><span>Status</span><select id="o-status">
        <option value="">All</option><option value="processing">Processing</option>
        <option value="action_required">Action Required</option><option value="closed">Closed</option>
      </select></label>
    </div>
    <div id="ord-list"><div class="spinner"></div></div>`));

  $("#snap").addEventListener("click", () => $("#file").click());
  $("#manual").addEventListener("click", () => openInvoiceForm(null, null));
  $("#file").addEventListener("change", onPhoto);

  try {
    const vendors = await api("GET", "/api/vendors");
    $("#o-vendor").insertAdjacentHTML("beforeend",
      vendors.map((vd) => `<option value="${esc(vd.name)}">${esc(vd.name)}</option>`).join(""));
  } catch (e) { /* filter is optional */ }

  linkDates($("#o-start"), $("#o-end"), loadOrders);
  ["o-vendor", "o-status"].forEach((id) => $("#" + id).addEventListener("change", loadOrders));
  await loadOrders();
}

async function loadOrders() {
  const box = $("#ord-list");
  box.innerHTML = '<div class="spinner"></div>';
  const qs = new URLSearchParams();
  if ($("#o-start").value) qs.set("start", $("#o-start").value);
  if ($("#o-end").value) qs.set("end", $("#o-end").value);
  if ($("#o-vendor").value) qs.set("vendor", $("#o-vendor").value);
  if ($("#o-status").value) qs.set("status", $("#o-status").value);
  try {
    const list = await api("GET", "/api/invoices?" + qs.toString());
    box.innerHTML = "";
    const total = list.reduce((s, iv) => s + (iv.total || 0), 0);
    const table = dataTable([
      { key: "invoice_date", label: "Date", fmt: (r) => esc(r.invoice_date || "—") },
      { key: "vendor", label: "Vendor", cls: "strong", fmt: (r) => esc(r.vendor || "Unknown") },
      { key: "invoice_number", label: "Invoice #", fmt: (r) => esc(r.invoice_number || "—") },
      { key: "status", label: "Status", fmt: (r) => statusPill(r.status) },
      { key: "payment_account", label: "Payment", fmt: (r) => esc(r.payment_account || "—") },
      { key: "total", label: "Total", align: "right", fmt: (r) => money(r.total) },
    ], list.map((iv) => ({ ...iv, _href: `#/invoice/${iv.id}` })), {
      search: true, empty: "No orders match these filters.",
      initialSort: { key: "invoice_date", dir: -1 },
      footer: { vendor: `${list.length} orders`, total: `<b>${money(total)}</b>` },
    });
    box.appendChild(table);
  } catch (e) { box.innerHTML = `<p class="err">${esc(e.message)}</p>`; }
}

async function onPhoto(e) {
  const file = e.target.files[0];
  if (!file) return;
  e.target.value = "";
  view().innerHTML = `<div class="card"><div class="card-band">Reading invoice…</div>
    <div class="card-body center"><div class="spinner"></div>
    <p class="muted">Claude is reading the slip. This takes a few seconds.</p></div></div>`;
  const fd = new FormData();
  fd.append("image", file);
  try {
    const r = await api("POST", "/api/invoices/parse", fd, true);
    openInvoiceForm(r.parsed, r.image_path);
  } catch (err) {
    // Even on parse failure we may have stored the image — let them log it manually.
    const imgPath = err.data && err.data.image_path;
    toast(err.message || "Couldn't read that one.");
    openInvoiceForm(null, imgPath, err.message);
  }
}

// parsed: AI/blank data for a NEW invoice. existing: a saved invoice to EDIT
// (PUT instead of POST, prefilled, no duplicate prompt).
async function openInvoiceForm(parsed, imagePath, warn, existing) {
  await loadCategories();
  const editing = !!existing;
  const src = existing || parsed ||
    { vendor: "", invoice_date: "", invoice_number: "", subtotal: null, tax: null, total: null, line_items: [] };
  imagePath = editing ? src.image_path : imagePath;
  // Saved line items carry category_id; the picker keys on the category name.
  const catNameById = {};
  (CATEGORIES || []).forEach((c) => { catNameById[c.id] = c.name; });
  const lineItems = (src.line_items || []).map((li) =>
    ({ ...li, category: li.category != null ? li.category : (catNameById[li.category_id] || null) }));
  const v = view();
  v.innerHTML = "";
  v.appendChild(el(`
    <h2 class="section section-head">${editing ? "Edit Invoice" : "Confirm Invoice"}</h2>
    ${warn ? `<div class="note">${esc(warn)} — enter the details by hand.</div>` : ""}
    ${imagePath ? `<img class="thumb-lg" src="/uploads/${esc(imagePath)}" alt="invoice">` : ""}
    <div class="card"><div class="card-body">
      <label class="fld"><span>Vendor</span><input id="f-vendor" list="vendor-list" value="${esc(src.vendor || "")}"><datalist id="vendor-list"></datalist></label>
      <div class="row2">
        <label class="fld"><span>Date</span><input type="date" id="f-date" value="${esc(src.invoice_date || "")}"></label>
        <label class="fld"><span>Invoice #</span><input id="f-num" value="${esc(src.invoice_number || "")}"></label>
      </div>
      <div class="row2">
        <label class="fld"><span>Status</span><select id="f-status">
          <option value="closed">Closed</option>
          <option value="processing">Processing</option>
          <option value="action_required">Action Required</option></select></label>
        <label class="fld"><span>Payment Account</span><input id="f-pay" value="${esc(src.payment_account || "")}" placeholder="A/P"></label>
      </div>
      <div class="row3">
        <label class="fld"><span>Subtotal</span><input type="number" step="0.01" id="f-sub" value="${num(src.subtotal)}"></label>
        <label class="fld"><span>Tax</span><input type="number" step="0.01" id="f-tax" value="${num(src.tax)}"></label>
        <label class="fld"><span>Total</span><input type="number" step="0.01" id="f-total" value="${num(src.total)}"></label>
      </div>
    </div></div>

    <div class="card"><div class="card-band">Line Items <button class="btn btn-sm btn-ghost" id="add-line">+ Add</button></div>
      <div class="card-body" id="lines"></div></div>
    <div id="recon" class="recon"></div>

    <div class="btn-row">
      <button class="btn btn-grn btn-block" id="save-inv">${editing ? "Save Changes" : "Save to Ledger"}</button>
    </div>
    <button class="btn btn-ghost btn-block" id="cancel-inv" style="margin-top:.5rem">Cancel</button>
  `));

  $("#f-status").value = src.status || "closed";
  const lines = $("#lines");
  lineItems.forEach((li) => lines.appendChild(lineRow(li)));
  if (!lineItems.length) lines.appendChild(lineRow({}));

  // Live reconciliation: do the line items add up to the invoice? Lines may be
  // tax-exclusive (sum to subtotal) or tax-inclusive (sum to total), so match
  // EITHER. Mirrors the backend _reconcile.
  // Work in integer cents (like the backend _reconcile) so the line sum and the
  // comparison are exact and don't drift below the penny in float.
  const cents = (x) => Math.round((Number(x) || 0) * 100);
  const reconRead = () => {
    // Mirror the server: reconcile only the named rows that will actually be
    // saved, and gate on whether any exist (not on the sum being zero).
    const named = [...lines.querySelectorAll(".lrow")]
      .filter((r) => r.querySelector(".li-name").value.trim());
    const lineC = named.reduce((s, r) => s + cents(f(r.querySelector(".li-total").value)), 0);
    const sub = f($("#f-sub").value), tax = f($("#f-tax").value) || 0, tot = f($("#f-total").value);
    const targets = [];
    if (sub != null) targets.push(cents(sub));
    if (tot != null) { targets.push(cents(tot)); if (tax) targets.push(cents(tot) - cents(tax)); }
    const box = $("#recon");
    if (!targets.length || !named.length) { box.innerHTML = ""; box.className = "recon"; return; }
    let expectedC = targets[0];
    targets.forEach((t) => { if (Math.abs(lineC - t) < Math.abs(lineC - expectedC)) expectedC = t; });
    const deltaC = lineC - expectedC;
    // Exact-integer tolerance (>=2c or 0.5%), x1000 so it matches the backend
    // _reconcile bit-for-bit (no Math.round vs Python round half-even drift).
    const ok = Math.abs(deltaC) * 1000 <= Math.max(2000, Math.abs(expectedC) * 5);
    box.className = "recon " + (ok ? "recon-ok" : "recon-warn");
    box.innerHTML = ok
      ? `✓ Line items add up (${money(lineC / 100)})`
      : `⚠ Line items total ${money(lineC / 100)}, invoice is ${money(expectedC / 100)} — off by ${money(Math.abs(deltaC) / 100)}`;
  };

  $("#add-line").addEventListener("click", () => { lines.appendChild(lineRow({})); reconRead(); });
  lines.addEventListener("input", reconRead);
  lines.addEventListener("click", (e) => { if (e.target.classList.contains("li-del")) setTimeout(reconRead, 0); });
  ["f-sub", "f-tax", "f-total"].forEach((id) => $("#" + id).addEventListener("input", reconRead));
  $("#cancel-inv").addEventListener("click", () => { go(editing ? `#/invoice/${src.id}` : "#/orders"); });
  fillVendorDatalist();
  reconRead();

  $("#save-inv").addEventListener("click", async () => {
    const items = [...lines.querySelectorAll(".lrow")].map((r) => ({
      name: r.querySelector(".li-name").value,
      qty: f(r.querySelector(".li-qty").value),
      unit: r.querySelector(".li-unit").value || null,
      unit_cost: f(r.querySelector(".li-cost").value),
      total: f(r.querySelector(".li-total").value),
      category: r.querySelector(".li-cat").value || null,
    })).filter((x) => x.name.trim());
    const payload = {
      vendor: $("#f-vendor").value, invoice_date: $("#f-date").value,
      invoice_number: $("#f-num").value, status: $("#f-status").value,
      payment_account: $("#f-pay").value,
      subtotal: f($("#f-sub").value), tax: f($("#f-tax").value), total: f($("#f-total").value),
      image_path: imagePath || null, line_items: items,
      raw_json: parsed ? JSON.stringify(parsed) : "",
    };
    if (editing) {
      try {
        await api("PUT", `/api/invoices/${src.id}`, payload);
        toast("Invoice updated.");
        go(`#/invoice/${src.id}`);
      } catch (e) { toast(e.message); }
      return;
    }
    const done = () => { toast("Logged to the ledger."); go("#/orders"); };
    try {
      await api("POST", "/api/invoices", payload);
      done();
    } catch (e) {
      if (e.data && e.data.error === "duplicate") {
        const dup = e.data.duplicate || {};
        const tag = [dup.invoice_number && `#${dup.invoice_number}`,
                     dup.invoice_date && `dated ${dup.invoice_date}`,
                     dup.total != null && `for ${money(dup.total)}`].filter(Boolean).join(", ");
        if (!confirm(`Looks like this invoice is already logged${tag ? ` (${tag})` : ""}.\n\nSave it anyway?`)) return;
        try {
          await api("POST", "/api/invoices", { ...payload, confirm_duplicate: true });
          done();
        } catch (e2) { toast(e2.message); }
        return;
      }
      toast(e.message);
    }
  });
}

// <option> list grouped by category type, for the per-line category picker.
function catOptionsHTML(selected) {
  if (!CATEGORIES) return "";
  const byType = {};
  CATEGORIES.forEach((c) => (byType[c.category_type] = byType[c.category_type] || []).push(c));
  return `<option value="">— category —</option>` + Object.entries(byType).map(([t, cs]) =>
    `<optgroup label="${esc(t)}">` + cs.map((c) =>
      `<option value="${esc(c.name)}" ${c.name === selected ? "selected" : ""}>${esc(c.name)}</option>`).join("") + `</optgroup>`).join("");
}

function lineRow(li) {
  const r = el(`<div class="lrow" style="display:grid;grid-template-columns:1fr auto;gap:.4rem;align-items:start;margin-bottom:.6rem;border-bottom:1px solid var(--border);padding-bottom:.5rem">
    <input class="li-name" placeholder="Item" value="${esc(li.name || "")}">
    <button class="btn btn-sm btn-ghost li-del" title="remove">&times;</button>
    <div class="row3" style="grid-column:1 / -1">
      <input class="li-qty" type="number" step="0.01" placeholder="qty" value="${num(li.qty)}">
      <input class="li-unit" placeholder="unit" value="${esc(li.unit || "")}">
      <input class="li-cost" type="number" step="0.01" placeholder="unit $" value="${num(li.unit_cost)}">
    </div>
    <div class="row2" style="grid-column:1 / -1">
      <input class="li-total" type="number" step="0.01" placeholder="line total" value="${num(li.total)}">
      <select class="li-cat">${catOptionsHTML(li.category)}</select>
    </div>
  </div>`);
  r.querySelector(".li-del").addEventListener("click", () => r.remove());
  return r;
}

async function renderInvoiceDetail(parts) {
  loading();
  const id = parts[0];
  try {
    const [iv, cats] = await Promise.all([api("GET", `/api/invoices/${id}`), loadCategories()]);
    const catName = {};
    cats.forEach((c) => { catName[c.id] = c.name; });
    const recon = iv.reconciliation;
    const v = view();
    v.innerHTML = "";
    v.appendChild(el(`
      <h2 class="section section-head">${esc(iv.vendor || "Invoice")}</h2>
      ${iv.image_path ? `<img class="thumb-lg" src="/uploads/${esc(iv.image_path)}" alt="">` : ""}
      <div class="card"><div class="card-body">
        <div class="kv"><span>Date</span><b>${esc(iv.invoice_date || "—")}</b></div>
        <div class="kv"><span>Invoice #</span><b>${esc(iv.invoice_number || "—")}</b></div>
        <div class="kv"><span>Status</span>${statusPill(iv.status)}</div>
        <div class="kv"><span>Payment</span><b>${esc(iv.payment_account || "—")}</b></div>
        <div class="kv"><span>Subtotal</span><b>${money(iv.subtotal)}</b></div>
        <div class="kv"><span>Tax</span><b>${money(iv.tax)}</b></div>
        <div class="kv"><span>Total</span><b>${money(iv.total)}</b></div>
      </div></div>
      ${recon && recon.ok === false ? `<div class="recon recon-warn" style="margin-top:.6rem">
        ⚠ Line items add up to ${money(recon.line_sum)}, but the invoice is ${money(recon.expected)}
        (off by ${money(Math.abs(recon.delta))}). Tap Edit to fix.</div>` : ""}
      ${iv.line_items.length ? `<div class="card"><div class="card-band">Items</div><div class="card-body">
        ${iv.line_items.map((li) => `<div class="kv"><span>${esc(li.name)}${li.qty ? ` <span class="muted">×${li.qty} ${esc(li.unit || "")}</span>` : ""}${li.category_id ? ` <span class="muted">· ${esc(catName[li.category_id] || "")}</span>` : ""}</span><b>${money(li.total)}</b></div>`).join("")}
      </div></div>` : ""}
      <button class="btn btn-brass btn-block" id="edit-inv" style="margin-top:.6rem">Edit Invoice</button>
      <button class="btn btn-ox btn-block" id="del-inv" style="margin-top:.5rem">Delete Invoice</button>
      <button class="btn btn-ghost btn-block" id="back" style="margin-top:.5rem">Back</button>`));
    $("#edit-inv").addEventListener("click", () => openInvoiceForm(null, null, null, iv));
    $("#back").addEventListener("click", () => { location.hash = "#/orders"; });
    $("#del-inv").addEventListener("click", async () => {
      if (!confirm("Delete this invoice?")) return;
      await api("DELETE", `/api/invoices/${id}`);
      toast("Removed.");
      location.hash = "#/orders";
    });
  } catch (e) { view().innerHTML = `<p class="err">${esc(e.message)}</p>`; }
}

/* ============================================================
   INVENTORY
   ============================================================ */
async function renderInventory() {
  const v = view();
  v.appendChild(el(`
    <h2 class="section section-head">Inventory</h2>
    <div class="btn-row">
      <button class="btn btn-brass" id="add-item">+ New Item</button>
      <button class="btn btn-ghost" id="order-list">Order List</button>
      <a class="btn btn-grn" href="#/count" style="text-decoration:none;text-align:center">Take Count</a>
    </div>
    <div id="inv-box"><div class="spinner"></div></div>`));
  $("#add-item").addEventListener("click", () => openItemEditor(null));
  $("#order-list").addEventListener("click", showOrderList);
  await loadInventory();
}

async function loadInventory() {
  const box = $("#inv-box");
  try {
    const items = await api("GET", "/api/products");
    box.innerHTML = "";
    if (!items.length) { box.appendChild(el(`<p class="empty">No items yet.<br>Add your first item to set a par.</p>`)); return; }
    const byCat = {};
    items.forEach((it) => (byCat[it.category_name || "Uncategorized"] = byCat[it.category_name || "Uncategorized"] || []).push(it));
    Object.keys(byCat).sort().forEach((cat) => {
      box.appendChild(el(`<h2 class="section section-head">${esc(cat)}</h2>`));
      byCat[cat].forEach((it) => {
        const under = (it.last_count || 0) <= (it.par_level || 0);
        const row = el(`<div class="row-item">
          <div class="grow">
            <div class="ttl">${esc(it.name)}</div>
            <div class="meta">par ${fmtQty(it.par_level)} · on hand ${fmtQty(it.last_count)} ${esc(it.unit || "")} · ${money(it.unit_cost)}/${esc(it.unit || "ea")}</div>
          </div>
          ${under ? `<span class="pill wine">Low</span>` : ""}
          <button class="btn btn-sm btn-ghost edit">Edit</button>
        </div>`);
        row.querySelector(".edit").addEventListener("click", () => openItemEditor(it));
        box.appendChild(row);
      });
    });
  } catch (e) { box.innerHTML = `<p class="err">${esc(e.message)}</p>`; }
}

async function openItemEditor(it) {
  const isNew = !it;
  it = it || { name: "", category_id: null, unit: "bottle", par_level: 0, last_count: 0, unit_cost: 0, vendor: "" };
  const cats = await loadCategories();
  const byType = {};
  cats.forEach((c) => (byType[c.category_type] = byType[c.category_type] || []).push(c));
  const catOpts = `<option value="">— category —</option>` + Object.entries(byType).map(([t, cs]) =>
    `<optgroup label="${esc(t)}">` + cs.map((c) =>
      `<option value="${c.id}" ${c.id === it.category_id ? "selected" : ""}>${esc(c.name)}</option>`).join("") + `</optgroup>`).join("");
  const v = view();
  v.innerHTML = "";
  v.appendChild(el(`
    <h2 class="section section-head">${isNew ? "New Item" : "Edit Item"}</h2>
    <div class="card"><div class="card-body">
      <label class="fld"><span>Name</span><input id="i-name" value="${esc(it.name)}"></label>
      <div class="row2">
        <label class="fld"><span>Category</span><select id="i-cat">${catOpts}</select></label>
        <label class="fld"><span>Unit</span><input id="i-unit" value="${esc(it.unit)}" placeholder="bottle, case, keg…"></label>
      </div>
      <div class="row3">
        <label class="fld"><span>Par</span><input type="number" step="0.5" id="i-par" value="${num(it.par_level)}"></label>
        <label class="fld"><span>On Hand</span><input type="number" step="0.5" id="i-cnt" value="${num(it.last_count)}"></label>
        <label class="fld"><span>Unit $</span><input type="number" step="0.01" id="i-cost" value="${num(it.unit_cost)}"></label>
      </div>
      <div class="row2">
        <label class="fld"><span>Size / unit</span><input type="number" step="0.01" id="i-size" value="${num(it.size_qty)}" placeholder="e.g. 750"></label>
        <label class="fld"><span>Size unit</span><input id="i-sunit" list="unit-list" value="${esc(it.size_unit || "")}" placeholder="ml, oz, each…">
          <datalist id="unit-list">${RECIPE_UNITS.map((u) => `<option value="${u}">`).join("")}</datalist></label>
      </div>
      <p class="muted" style="font-size:.78rem;margin:.1rem 0 0">Content of one purchase unit (a 750 ml bottle ⇒ 750 / ml). Lets recipes cost a 1.5 oz pour.</p>
      <label class="fld"><span>Vendor</span><input id="i-vendor" list="vendor-list" value="${esc(it.vendor || "")}"><datalist id="vendor-list"></datalist></label>
    </div></div>
    <button class="btn btn-grn btn-block" id="save-item">Save</button>
    ${isNew ? "" : `<button class="btn btn-ox btn-block" id="del-item" style="margin-top:.5rem">Delete Item</button>`}
    <button class="btn btn-ghost btn-block" id="cancel-item" style="margin-top:.5rem">Cancel</button>`));

  $("#cancel-item").addEventListener("click", () => { location.hash = "#/inventory"; });
  fillVendorDatalist();
  $("#save-item").addEventListener("click", async () => {
    const catId = $("#i-cat").value ? Number($("#i-cat").value) : null;
    const cat = cats.find((c) => c.id === catId);
    const payload = {
      name: $("#i-name").value.trim(), category_id: catId, category: cat ? cat.name : null,
      unit: $("#i-unit").value.trim(),
      par_level: f($("#i-par").value, 0), last_count: f($("#i-cnt").value, 0),
      unit_cost: f($("#i-cost").value, 0), vendor: $("#i-vendor").value.trim(),
      size_qty: f($("#i-size").value, null), size_unit: $("#i-sunit").value.trim() || null,
    };
    if (!payload.name) { toast("Give it a name."); return; }
    try {
      if (isNew) await api("POST", "/api/products", payload);
      else await api("PUT", `/api/products/${it.id}`, payload);
      toast("Saved.");
      location.hash = "#/inventory";
    } catch (e) { toast(e.message); }
  });
  if (!isNew) $("#del-item").addEventListener("click", async () => {
    if (!confirm("Remove this item from inventory?")) return;
    await api("DELETE", `/api/products/${it.id}`);
    toast("Removed.");
    location.hash = "#/inventory";
  });
}

async function showOrderList() {
  loading();
  try {
    const g = await api("GET", "/api/inventory/order-guide");
    const v = view();
    v.innerHTML = `<h2 class="section section-head">Order Guide</h2>`;
    if (!g.item_count) {
      v.appendChild(el(`<p class="empty">Everything&rsquo;s at or above par. Nothing to order.</p>`));
    } else {
      v.appendChild(el(`<div class="btn-row">
        <button class="btn btn-ghost btn-sm" id="exp-order">&#x2B07; Order Guide (CSV)</button>
      </div>`));
      $("#exp-order").addEventListener("click", () =>
        download("/api/export/order-guide.csv", "order-guide.csv")
          .catch((e) => toast("Export failed: " + e.message)));
      // One card per vendor, so each is a ready-to-send order.
      g.vendors.forEach((vd) => {
        const card = el(`<div class="card"><div class="card-band">${esc(vd.vendor)}
          <span>${money(vd.subtotal)}</span></div><div class="card-body" id="ord"></div></div>`);
        const ord = card.querySelector("#ord");
        vd.items.forEach((it) => ord.appendChild(el(
          `<div class="kv"><span>${esc(it.name)} <span class="muted">need ${fmtQty(it.order_qty)} ${esc(it.unit || "")}
            &middot; on hand ${fmtQty(it.on_hand)}/${fmtQty(it.par)}</span></span>
            <b>${money(it.line_cost)}</b></div>`)));
        v.appendChild(card);
      });
      v.appendChild(el(`<div class="kv total-row" style="margin-top:.4rem"><span><b>Total</b></span>
        <b>${money(g.grand_total)}</b></div>`));
    }
    v.appendChild(el(`<button class="btn btn-ghost btn-block" id="back" style="margin-top:.6rem">Back to Stock</button>`));
    $("#back").addEventListener("click", () => { location.hash = "#/inventory"; });
  } catch (e) { view().innerHTML = `<p class="err">${esc(e.message)}</p>`; }
}

/* ============================================================
   COUNT (walk-around)
   ============================================================ */
async function renderCount() {
  loading();
  let items;
  try { items = await api("GET", "/api/products"); }
  catch (e) { view().innerHTML = `<p class="err">${esc(e.message)}</p>`; return; }

  const v = view();
  v.innerHTML = "";
  if (!items.length) {
    v.appendChild(el(`<p class="empty">Add items to inventory first, then come back to count.</p>`));
    v.appendChild(el(`<a class="btn btn-brass btn-block" href="#/inventory" style="text-decoration:none;text-align:center">Go to Inventory</a>`));
    return;
  }

  v.appendChild(el(`<h2 class="section section-head">Count Inventory</h2>
    <p class="muted" style="font-size:.82rem;margin-top:-.3rem">Tap to adjust each count. Starts from your last numbers.</p>`));

  const byCat = {};
  items.forEach((it) => (byCat[it.category_name || "Uncategorized"] = byCat[it.category_name || "Uncategorized"] || []).push(it));
  const state = {};
  Object.keys(byCat).sort().forEach((cat) => {
    v.appendChild(el(`<h2 class="section section-head">${esc(cat)}</h2>`));
    byCat[cat].forEach((it) => {
      state[it.id] = Number(it.last_count || 0);
      const under = (it.last_count || 0) <= (it.par_level || 0);
      const row = el(`<div class="count-row ${under ? "under" : ""}" data-id="${it.id}">
        <div class="grow"><div class="ttl">${esc(it.name)}</div>
          <div class="meta">par ${fmtQty(it.par_level)} ${esc(it.unit || "")}</div></div>
        <div class="stepper">
          <button class="dec" type="button">&minus;</button>
          <input class="cval" inputmode="decimal" value="${fmtQty(it.last_count)}">
          <button class="inc" type="button">+</button>
        </div></div>`);
      const input = row.querySelector(".cval");
      const sync = (val) => { state[it.id] = val; input.value = fmtQty(val);
        row.classList.toggle("under", val <= (it.par_level || 0)); };
      row.querySelector(".inc").addEventListener("click", () => sync(round1((state[it.id] || 0) + 1)));
      row.querySelector(".dec").addEventListener("click", () => sync(Math.max(0, round1((state[it.id] || 0) - 1))));
      input.addEventListener("change", () => sync(Math.max(0, f(input.value, 0))));
      v.appendChild(row);
    });
  });

  v.appendChild(el(`<div style="height:60px"></div>`));
  const action = el(`<div class="sticky-action">
    <button class="btn btn-grn btn-block" id="save-count">Finish &amp; Record Count</button></div>`);
  document.body.appendChild(action);

  const cleanup = () => action.remove();
  $("#save-count", action).addEventListener("click", async () => {
    const lines = Object.entries(state).map(([id, qty]) => ({ item_id: Number(id), qty }));
    const note = prompt("Label this count (optional):", "") || "";
    try {
      const r = await api("POST", "/api/counts", { lines, note });
      cleanup();
      toast(`Count recorded — ${money(r.value)} on hand.`);
      location.hash = "#/inventory";
    } catch (e) { toast(e.message); }
  });
  // Remove the floating button when we navigate away.
  window.addEventListener("hashchange", cleanup, { once: true });
}

/* ============================================================
   SETTINGS
   ============================================================ */
async function renderSettings() {
  loading();
  let cfg;
  try { cfg = await api("GET", "/api/config"); CONFIG = cfg; }
  catch (e) { view().innerHTML = `<p class="err">${esc(e.message)}</p>`; return; }

  const v = view();
  v.innerHTML = "";
  v.appendChild(el(`
    <h2 class="section section-head">Settings</h2>

    <div class="card"><div class="card-band">Targets</div><div class="card-body">
      <div class="row2">
        <label class="fld"><span>Target COGS %</span><input type="number" id="s-cogs" value="${esc(cfg.target_cogs_pct)}"></label>
        <label class="fld"><span>Target Labor %</span><input type="number" id="s-labor" value="${esc(cfg.target_labor_pct)}"></label>
      </div>
      <div class="row2">
        <label class="fld"><span>Default Hourly Wage $</span><input type="number" step="0.01" id="s-wage" value="${esc(cfg.default_hourly_wage)}"></label>
        <label class="fld"><span>Price Alert Threshold %</span><input type="number" step="0.5" id="s-palert" value="${esc(cfg.price_alert_pct)}"></label>
      </div>
      <p class="muted" style="font-size:.82rem;margin:.3rem 0 0">Wage is applied to Square shifts with no wage recorded (e.g. tipped staff) so Labor% isn&rsquo;t understated (0 = leave at $0). The alert threshold flags a vendor item on the dashboard when its price jumps that much.</p>
    </div></div>

    <div class="card"><div class="card-band">Sales Mix &middot; per period</div><div class="card-body">
      <p class="muted" style="font-size:.82rem;margin-top:0">Your actual sales mix (% of sales by type) for a period. Powers income on the Controllable P&amp;L.</p>
      <div class="row2">
        <label class="fld"><span>Period From</span><input type="date" id="mx-start" value="${iso(monthStart())}"></label>
        <label class="fld"><span>Period To</span><input type="date" id="mx-end" value="${iso(new Date())}"></label>
      </div>
      <div id="mx-fields"><div class="spinner"></div></div>
      <button class="btn btn-grn btn-block" id="save-mix" style="margin-top:.5rem">Save Sales Mix</button>
    </div></div>

    <div class="card"><div class="card-band">Square &middot; Sales &amp; Labor
      <span class="pill">${cfg.square_configured ? "connected" : "off"}</span></div>
      <div class="card-body">
      <label class="fld"><span>Access Token ${cfg.has_square_token ? "(stored — leave blank to keep)" : ""}</span>
        <input id="s-token" type="password" placeholder="${cfg.has_square_token ? "••••••••" : "Square access token"}"></label>
      <div class="row2">
        <label class="fld"><span>Environment</span><select id="s-env">
          <option value="production" ${cfg.square_env === "production" ? "selected" : ""}>Production</option>
          <option value="sandbox" ${cfg.square_env === "sandbox" ? "selected" : ""}>Sandbox</option></select></label>
        <label class="fld"><span>API Version</span><input id="s-ver" value="${esc(cfg.square_version)}"></label>
      </div>
      <label class="fld"><span>Location</span>
        <select id="s-loc"><option value="${esc(cfg.square_location_id)}">${cfg.square_location_id ? esc(cfg.square_location_id) : "— pick after loading —"}</option></select></label>
      <button class="btn btn-ghost btn-sm" id="load-locs">Load Locations</button>
    </div></div>

    <div class="card"><div class="card-band">Invoice Reader (Claude)
      <span class="pill">${cfg.ai_key_present ? "key set" : "no key"}</span></div>
      <div class="card-body">
      <label class="fld"><span>Model</span>
        <select id="s-model">
          ${["claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5"].map((m) =>
            `<option value="${m}" ${m === cfg.ai_model ? "selected" : ""}>${m}</option>`).join("")}
        </select></label>
      ${cfg.ai_key_present ? "" : `<div class="note">Set <code>ANTHROPIC_API_KEY</code> in the server environment to read invoice photos.</div>`}
      <p class="muted" style="font-size:.78rem">Opus is most accurate; Sonnet &amp; Haiku are cheaper per invoice.</p>
    </div></div>

    <div class="card"><div class="card-band">Data &amp; Backups</div><div class="card-body">
      <p class="muted" style="font-size:.82rem;margin-top:0">The ledger backs up automatically at startup and every few hours to <code>data/backups/</code> (the last 14 are kept). Snapshot on demand here.</p>
      <button class="btn btn-ghost btn-block" id="backup-now">Back Up Now</button>
    </div></div>

    <button class="btn btn-brass btn-block" id="save-settings">Save Settings</button>
    <button class="btn btn-ghost btn-block" id="logout" style="margin-top:.6rem">Sign Out</button>
    <p class="center muted" style="margin-top:1rem;font-size:.75rem">Barkeep&rsquo;s Ledger · self-hosted</p>`));

  // --- Sales mix editor ---
  async function loadMix() {
    const box = $("#mx-fields");
    box.innerHTML = '<div class="spinner"></div>';
    try {
      const d = await api("GET", `/api/sales-mix?start=${$("#mx-start").value}&end=${$("#mx-end").value}`);
      const sum = d.category_types.reduce((s, t) => s + (Number(d.mix[t]) || 0), 0);
      box.innerHTML = `<div class="row3">${d.category_types.map((t) =>
        `<label class="fld"><span>${esc(t)}</span><input type="number" step="0.1" class="mx-in" data-t="${esc(t)}" value="${num(d.mix[t])}"></label>`).join("")}</div>
        <p class="muted" id="mx-sum" style="font-size:.8rem">Total: ${sum.toFixed(1)}%</p>`;
      box.querySelectorAll(".mx-in").forEach((i) => i.addEventListener("input", () => {
        const s = [...box.querySelectorAll(".mx-in")].reduce((a, x) => a + (Number(x.value) || 0), 0);
        $("#mx-sum").textContent = `Total: ${s.toFixed(1)}%` + (Math.abs(s - 100) > 0.1 ? " (should be ~100%)" : "");
      }));
    } catch (e) { box.innerHTML = `<p class="err">${esc(e.message)}</p>`; }
  }
  linkDates($("#mx-start"), $("#mx-end"), loadMix);
  loadMix();
  $("#save-mix").addEventListener("click", async () => {
    const mix = {};
    $("#mx-fields").querySelectorAll(".mx-in").forEach((i) => { mix[i.dataset.t] = Number(i.value) || 0; });
    try {
      await api("PUT", `/api/sales-mix?start=${$("#mx-start").value}&end=${$("#mx-end").value}`, { mix });
      toast("Sales mix saved.");
    } catch (e) { toast(e.message); }
  });

  $("#load-locs").addEventListener("click", async (e) => {
    e.target.disabled = true; e.target.textContent = "Loading…";
    // Save a freshly-typed token first so "Load Locations" can use it without
    // needing a separate Save step.
    if ($("#s-token").value.trim()) {
      await api("POST", "/api/settings", {
        square_token: $("#s-token").value.trim(),
        square_env: $("#s-env").value, square_version: $("#s-ver").value,
      }).catch(() => {});
    }
    try {
      const r = await api("GET", "/api/square-locations");
      const sel = $("#s-loc");
      if (r.error) { toast(r.error); }
      else { toast(`Connected — ${(r.locations || []).length} location(s).`); }
      sel.innerHTML = (r.locations || []).map((l) =>
        `<option value="${esc(l.id)}" ${l.id === cfg.square_location_id ? "selected" : ""}>${esc(l.name)} (${esc(l.id)})</option>`).join("")
        || `<option value="">none found</option>`;
    } catch (err) { toast(err.message); }
    e.target.disabled = false; e.target.textContent = "Load Locations";
  });

  $("#save-settings").addEventListener("click", async () => {
    const payload = {
      target_cogs_pct: $("#s-cogs").value, target_labor_pct: $("#s-labor").value,
      default_hourly_wage: $("#s-wage").value, price_alert_pct: $("#s-palert").value,
      square_env: $("#s-env").value, square_version: $("#s-ver").value,
      square_location_id: $("#s-loc").value, ai_model: $("#s-model").value,
    };
    if ($("#s-token").value.trim()) payload.square_token = $("#s-token").value.trim();
    const btn = $("#save-settings");
    btn.disabled = true;
    try {
      await api("POST", "/api/settings", payload);
      toast("Settings saved.");
      renderSettings();  // re-render so the status badges & location refresh
    } catch (e) { toast(e.message); btn.disabled = false; }
  });

  $("#backup-now").addEventListener("click", async (e) => {
    e.target.disabled = true;
    try {
      const r = await api("POST", "/api/backup");
      toast(`Backed up — ${r.file}`);
    } catch (err) { toast(err.message); }
    e.target.disabled = false;
  });

  $("#logout").addEventListener("click", () => { localStorage.removeItem(TOKEN_KEY); location.reload(); });
}

/* ============================================================
   VENDORS
   ============================================================ */

async function fillVendorDatalist() {
  const dl = $("#vendor-list");
  if (!dl) return;
  try {
    if (!VENDOR_NAMES) VENDOR_NAMES = (await api("GET", "/api/vendors")).map((v) => v.name);
    dl.innerHTML = VENDOR_NAMES.map((n) => `<option value="${esc(n)}"></option>`).join("");
  } catch (e) { /* autocomplete is a nicety; ignore failures */ }
}

async function renderVendors(parts) {
  if (parts[0] === "items") return renderVendorItems();
  const v = view();
  v.appendChild(el(`
    <h2 class="section section-head">Vendors</h2>
    <div id="v-summary" class="stat-grid"></div>
    <button class="btn btn-brass btn-block" id="add-vendor" style="margin-top:.8rem">+ New Vendor</button>
    <div id="vlist"><div class="spinner"></div></div>`));
  $("#add-vendor").addEventListener("click", () => openVendorEditor(null));

  api("GET", "/api/vendors/summary").then((s) => {
    $("#v-summary").innerHTML = `
      <div class="stat accent-ind"><div class="label">Vendors</div><div class="value">${s.total_vendors}</div></div>
      <div class="stat accent-grn"><div class="label">Vendor Items</div><div class="value">${s.vendor_items}</div></div>
      <div class="stat accent-ox"><div class="label">Invoices</div><div class="value">${s.invoices_processed}</div></div>
      <div class="stat"><div class="label">Total Purchased</div><div class="value" style="font-size:1.7rem">${money(s.total_purchased)}</div></div>`;
  }).catch(() => {});

  try {
    const list = await api("GET", "/api/vendors");
    const box = $("#vlist");
    box.innerHTML = "";
    if (!list.length) { box.appendChild(el(`<p class="empty">No vendors yet.<br>Add your distributors and reps here.</p>`)); return; }
    box.appendChild(dataTable([
      { key: "name", label: "Vendor", cls: "strong", fmt: (r) => `<a href="#/vendor/${r.id}" style="color:var(--accent);text-decoration:none">${esc(r.name)}</a>` },
      { key: "item_count", label: "Items", align: "right" },
      { key: "period_purchases", label: "This Period", align: "right", fmt: (r) => money(r.period_purchases) },
      { key: "last_period_purchases", label: "Last Period", align: "right", fmt: (r) => money(r.last_period_purchases) },
      { key: "year_purchases", label: "This Year", align: "right", fmt: (r) => money(r.year_purchases) },
      { key: "last_order", label: "Last Invoice", align: "right", fmt: (r) => esc(r.last_order || "—") },
    ], list, { search: true, initialSort: { key: "name", dir: 1 } }));
  } catch (e) { $("#vlist").innerHTML = `<p class="err">${esc(e.message)}</p>`; }
}

async function renderVendorItems() {
  const v = view();
  v.appendChild(el(`
    <h2 class="section section-head">Vendor Items</h2>
    <div id="vi-list"><div class="spinner"></div></div>`));
  try {
    const all = await api("GET", "/api/vendor-items");
    const box = $("#vi-list");
    box.innerHTML = "";
    if (!all.length) { box.appendChild(el(`<p class="empty">No vendor items yet.<br>They appear as you log invoices.</p>`)); return; }
    box.appendChild(dataTable([
      { key: "vendor_name", label: "Vendor", fmt: (r) => esc(r.vendor_name || "—") },
      { key: "vendor_item_name", label: "Item", cls: "strong", fmt: (r) => esc(r.vendor_item_name) },
      { key: "product_name", label: "Product", fmt: (r) => esc(r.product_name || "—") },
      { key: "category_name", label: "Category", fmt: (r) => r.category_type ? typePill(r.category_type) + " " + esc(r.category_name || "") : "—" },
      { key: "item_code", label: "Code", fmt: (r) => esc(r.item_code || "—") },
      { key: "last_purchase_price", label: "Last $", align: "right", fmt: (r) => money(r.last_purchase_price) },
      { key: "last_purchase_date", label: "Last Buy", align: "right", fmt: (r) => esc(r.last_purchase_date || "—") },
      { key: "status", label: "Status", fmt: (r) => statusPill(r.status) },
    ], all, { search: true, initialSort: { key: "vendor_item_name", dir: 1 } }));
  } catch (e) { $("#vi-list").innerHTML = `<p class="err">${esc(e.message)}</p>`; }
}

async function renderVendorDetail(parts) {
  loading();
  const id = parts[0];
  let vd;
  try { vd = await api("GET", `/api/vendors/${id}`); }
  catch (e) { view().innerHTML = `<p class="err">${esc(e.message)}</p>`; return; }

  const v = view();
  v.innerHTML = "";
  const contactBtns = [];
  if (vd.phone) contactBtns.push(`<a class="btn btn-grn" href="tel:${esc(vd.phone)}" style="text-decoration:none;text-align:center">&#x1F4DE; Call</a>`);
  if (vd.email) contactBtns.push(`<a class="btn btn-ghost" href="mailto:${esc(vd.email)}" style="text-decoration:none;text-align:center">&#x2709; Email</a>`);

  v.appendChild(el(`
    <h2 class="section section-head">${esc(vd.name)}</h2>
    ${contactBtns.length ? `<div class="btn-row">${contactBtns.join("")}</div>` : ""}

    <div class="card"><div class="card-band">Details</div><div class="card-body">
      ${row("Contact", vd.contact_name)}
      ${row("Phone", vd.phone)}
      ${row("Email", vd.email)}
      ${row("Account #", vd.account_number)}
      ${row("Order days", vd.order_days)}
      ${vd.notes ? `<div style="margin-top:.5rem" class="muted">${esc(vd.notes)}</div>` : ""}
    </div></div>

    <div class="stat-grid">
      <div class="stat accent-ind"><div class="label">Total Spend</div>
        <div class="value">${money(vd.spend)}</div><div class="sub">${vd.invoices.length} invoice(s)</div></div>
      <div class="stat accent-grn"><div class="label">Items Supplied</div>
        <div class="value">${vd.items.length}</div><div class="sub">in your cellar</div></div>
    </div>

    ${vd.invoices.length ? `<div class="card"><div class="card-band">Recent Invoices</div><div class="card-body" id="v-inv"></div></div>` : ""}
    ${vd.items.length ? `<div class="card"><div class="card-band">Items From This Vendor</div><div class="card-body" id="v-items"></div></div>` : ""}

    <button class="btn btn-brass btn-block" id="edit-vendor">Edit Vendor</button>
    <button class="btn btn-ghost btn-block" id="back-vendors" style="margin-top:.5rem">Back to Vendors</button>`));

  const inv = $("#v-inv");
  if (inv) vd.invoices.forEach((iv) => inv.appendChild(el(
    `<a href="#/invoice/${iv.id}" class="kv" style="text-decoration:none;color:inherit">
      <span>${esc(iv.invoice_date || "—")}${iv.invoice_number ? ` <span class="muted">#${esc(iv.invoice_number)}</span>` : ""}</span>
      <b>${money(iv.total)}</b></a>`)));
  const its = $("#v-items");
  if (its) vd.items.forEach((it) => its.appendChild(el(
    `<div class="kv"><span>${esc(it.name)} <span class="muted">par ${fmtQty(it.par_level)} ${esc(it.unit || "")}</span></span><b>${money(it.unit_cost)}</b></div>`)));

  $("#edit-vendor").addEventListener("click", () => openVendorEditor(vd));
  $("#back-vendors").addEventListener("click", () => { location.hash = "#/vendors"; });

  function row(label, val) {
    return `<div class="kv"><span>${label}</span><b style="font-family:var(--f-ui);font-weight:600">${esc(val || "—")}</b></div>`;
  }
}

function openVendorEditor(vd) {
  const isNew = !vd;
  vd = vd || { name: "", contact_name: "", phone: "", email: "", account_number: "", order_days: "", notes: "" };
  const v = view();
  v.innerHTML = "";
  v.appendChild(el(`
    <h2 class="section section-head">${isNew ? "New Vendor" : "Edit Vendor"}</h2>
    <div class="card"><div class="card-body">
      <label class="fld"><span>Name</span><input id="v-name" value="${esc(vd.name)}" placeholder="e.g. Southern Glazer's"></label>
      <label class="fld"><span>Contact / Rep</span><input id="v-contact" value="${esc(vd.contact_name)}"></label>
      <div class="row2">
        <label class="fld"><span>Phone</span><input id="v-phone" type="tel" value="${esc(vd.phone)}"></label>
        <label class="fld"><span>Email</span><input id="v-email" type="email" value="${esc(vd.email)}"></label>
      </div>
      <div class="row2">
        <label class="fld"><span>Account #</span><input id="v-acct" value="${esc(vd.account_number)}"></label>
        <label class="fld"><span>Order Days</span><input id="v-days" value="${esc(vd.order_days)}" placeholder="Tue / Fri"></label>
      </div>
      <label class="fld"><span>Notes</span><textarea id="v-notes" rows="3">${esc(vd.notes || "")}</textarea></label>
    </div></div>
    <button class="btn btn-grn btn-block" id="save-vendor">Save</button>
    ${isNew ? "" : `<button class="btn btn-ox btn-block" id="del-vendor" style="margin-top:.5rem">Delete Vendor</button>`}
    <button class="btn btn-ghost btn-block" id="cancel-vendor" style="margin-top:.5rem">Cancel</button>`));

  $("#cancel-vendor").addEventListener("click", () => { location.hash = isNew ? "#/vendors" : `#/vendor/${vd.id}`; });
  $("#save-vendor").addEventListener("click", async () => {
    const payload = {
      name: $("#v-name").value.trim(), contact_name: $("#v-contact").value.trim(),
      phone: $("#v-phone").value.trim(), email: $("#v-email").value.trim(),
      account_number: $("#v-acct").value.trim(), order_days: $("#v-days").value.trim(),
      notes: $("#v-notes").value.trim(),
    };
    if (!payload.name) { toast("Give the vendor a name."); return; }
    try {
      VENDOR_NAMES = null;  // bust autocomplete cache
      if (isNew) { await api("POST", "/api/vendors", payload); location.hash = "#/vendors"; }
      else { await api("PUT", `/api/vendors/${vd.id}`, payload); location.hash = `#/vendor/${vd.id}`; }
      toast("Saved.");
    } catch (e) { toast(e.message); }
  });
  if (!isNew) $("#del-vendor").addEventListener("click", async () => {
    if (!confirm("Remove this vendor?")) return;
    VENDOR_NAMES = null;
    await api("DELETE", `/api/vendors/${vd.id}`);
    toast("Removed.");
    location.hash = "#/vendors";
  });
}

/* ============================================================
   PERFORMANCE REPORTS
   ============================================================ */
function dateFilterBar(onChange) {
  const def = { start: iso(monthStart()), end: iso(new Date()) };
  const bar = el(`<div class="filters">
    <label class="fld"><span>From</span><input type="date" class="r-start" value="${def.start}"></label>
    <label class="fld"><span>To</span><input type="date" class="r-end" value="${def.end}"></label>
  </div>`);
  const get = () => ({ start: bar.querySelector(".r-start").value, end: bar.querySelector(".r-end").value });
  linkDates(bar.querySelector(".r-start"), bar.querySelector(".r-end"), () => onChange(get()));
  return { bar, get };
}

function renderPerformance(parts) {
  const sub = parts[0] || "category";
  if (sub === "pl") return renderControllablePL();
  if (sub === "sales") return renderSales();
  if (sub === "movers") return renderPriceMovers();
  return renderCategoryReport();
}

async function renderCategoryReport() {
  const v = view();
  v.innerHTML = `<h2 class="section section-head">Category Report</h2>`;
  const body = el(`<div id="cr-body"><div class="spinner"></div></div>`);
  const filter = dateFilterBar(load);
  v.appendChild(filter.bar);
  const expBar = el(`<div class="btn-row">
    <button class="btn btn-ghost btn-sm" id="exp-lines">&#x2B07; Line items (CSV)</button>
    <button class="btn btn-ghost btn-sm" id="exp-sum">&#x2B07; Summary (CSV)</button>
  </div>`);
  v.appendChild(expBar);
  const dl = (endpoint, prefix) => {
    const { start, end } = filter.get();
    download(`/api/export/${endpoint}?start=${start}&end=${end}`, `${prefix}_${start}_${end}.csv`)
      .catch((e) => toast("Export failed: " + e.message));
  };
  expBar.querySelector("#exp-lines").addEventListener("click", () => dl("purchases.csv", "purchases"));
  expBar.querySelector("#exp-sum").addEventListener("click", () => dl("category-summary.csv", "category-summary"));
  v.appendChild(body);
  async function load() {
    body.innerHTML = '<div class="spinner"></div>';
    const { start, end } = filter.get();
    try {
      const d = await api("GET", `/api/reports/category?start=${start}&end=${end}`);
      const cols = d.categories.filter((c) => (d.column_totals[c.id] || 0) !== 0);
      const columns = [
        { key: "invoice_date", label: "Date", fmt: (r) => esc(r.invoice_date || "—") },
        { key: "vendor", label: "Vendor", cls: "strong", fmt: (r) => esc(r.vendor || "—") },
        ...cols.map((c) => ({
          key: "c" + c.id, label: c.name, align: "right",
          fmt: (r) => (r["c" + c.id] ? money(r["c" + c.id]) : ""),
        })),
        { key: "total", label: "Total", align: "right", fmt: (r) => money(r.total) },
      ];
      const rows = d.rows.map((r) => {
        const o = { invoice_date: r.invoice_date, vendor: r.vendor, total: r.total, _href: `#/invoice/${r.id}` };
        cols.forEach((c) => { o["c" + c.id] = r.cells[c.id] || 0; });
        return o;
      });
      const footer = { vendor: `${d.rows.length} orders`, total: `<b>${money(d.grand_total)}</b>` };
      cols.forEach((c) => { footer["c" + c.id] = money(d.column_totals[c.id]); });
      body.innerHTML = "";
      if (!d.rows.length) { body.appendChild(el(`<p class="empty">No invoices in this range.</p>`)); return; }
      body.appendChild(dataTable(columns, rows, { search: true, footer, initialSort: { key: "invoice_date", dir: -1 } }));
    } catch (e) { body.innerHTML = `<p class="err">${esc(e.message)}</p>`; }
  }
  load();
}

async function renderControllablePL() {
  const v = view();
  v.innerHTML = `<h2 class="section section-head">Controllable P&amp;L</h2>`;
  const body = el(`<div id="pl-body"><div class="spinner"></div></div>`);
  const filter = dateFilterBar(load);
  v.appendChild(filter.bar);
  v.appendChild(body);
  async function load() {
    body.innerHTML = '<div class="spinner"></div>';
    const { start, end } = filter.get();
    try {
      const d = await api("GET", `/api/reports/controllable-pl?start=${start}&end=${end}`);
      const row = (label, amt, pctv, cls = "") =>
        `<div class="pl-row ${cls}"><span>${esc(label)}</span>
          <span style="display:flex;gap:.6rem"><span class="pl-amt ${amt < 0 ? "neg" : ""}">${money(amt)}</span>
          <span class="pl-pct">${pctv == null ? "" : pct(pctv)}</span></span></div>`;
      let html = `<div class="card"><div class="card-body pl">`;
      if (!d.square_configured) html += `<div class="note">Square isn’t connected — income &amp; labor read $0. Add it in <a href="#/settings" class="linkbtn">Settings</a>.</div>`;
      else if (!d.mix_set) html += `<div class="note">No sales mix set for this period. Set your actual mix in <a href="#/settings" class="linkbtn">Settings → Sales Mix</a> to split income by category.</div>`;
      html += `<div class="pl-row head">Income</div>`;
      d.income.forEach((i) => { html += row(i.category_type, i.amt, i.pct_of_sales, "sub"); });
      html += row("Total Income", d.total_income, null, "total");
      html += `<div class="pl-row head">Cost of Goods Sold</div>`;
      d.cogs.forEach((t) => {
        html += row(t.category_type, t.type_total, t.type_pct);
        t.categories.forEach((c) => { html += row(c.category, c.amt, c.pct, "sub"); });
      });
      html += row("Total COGS", d.total_cogs, d.total_cogs_pct, "total");
      html += row("Gross Profit", d.gross_profit, d.gross_pct, "total grand");
      html += `<div class="pl-row head">Controllable Expenses</div>`;
      d.expenses.forEach((e) => { html += row(e.name, e.amt, e.pct, "sub"); });
      html += row("Controllable Profit", d.controllable_profit, d.controllable_pct, "total grand");
      html += `</div></div>`;
      body.innerHTML = html;
    } catch (e) { body.innerHTML = `<p class="err">${esc(e.message)}</p>`; }
  }
  load();
}

async function renderSales() {
  loading();
  try {
    const d = await api("GET", "/api/reports/sales");
    const v = view();
    v.innerHTML = `<h2 class="section section-head">Sales</h2>`;
    if (!d.square_configured) v.appendChild(el(`<div class="note">Square isn’t connected — sales read $0. Add it in <a href="#/settings" class="linkbtn">Settings</a>.</div>`));
    v.appendChild(el(`<div class="stat-grid">
      <div class="stat wide accent-ind"><div class="label">Week of ${esc(d.week_of)}</div>
        <div class="value">${money(d.totals.this_week)}</div><div class="sub">this week to date</div></div>
      <div class="stat accent-grn"><div class="label">Period to Date</div><div class="value" style="font-size:1.9rem">${money(d.period_to_date)}</div></div>
      <div class="stat accent-ox"><div class="label">Year to Date</div><div class="value" style="font-size:1.9rem">${money(d.year_to_date)}</div></div>
    </div>`));
    v.appendChild(dataTable([
      { key: "day", label: "Day", sortable: false },
      { key: "this_week", label: "This Week", align: "right", fmt: (r) => r.this_week == null ? "—" : money(r.this_week) },
      { key: "last_week", label: "Last Week", align: "right", fmt: (r) => money(r.last_week) },
      { key: "last_year", label: "Last Year", align: "right", fmt: (r) => money(r.last_year) },
    ], d.days, {
      footer: {
        day: "Total", this_week: `<b>${money(d.totals.this_week)}</b>`,
        last_week: `<b>${money(d.totals.last_week)}</b>`, last_year: `<b>${money(d.totals.last_year)}</b>`,
      },
    }));
  } catch (e) { view().innerHTML = `<p class="err">${esc(e.message)}</p>`; }
}

async function renderPriceMovers() {
  const v = view();
  v.innerHTML = `<h2 class="section section-head">Price Movers</h2>`;
  const body = el(`<div id="pm-body"><div class="spinner"></div></div>`);
  const filter = dateFilterBar(load);
  v.appendChild(filter.bar);
  v.appendChild(body);
  async function load() {
    body.innerHTML = '<div class="spinner"></div>';
    const { start, end } = filter.get();
    try {
      const d = await api("GET", `/api/reports/price-movers?start=${start}&end=${end}`);
      body.innerHTML = "";
      if (!d.movers.length) { body.appendChild(el(`<p class="empty">No price changes in this range.<br>Movers appear once an item is bought at a new price.</p>`)); return; }
      body.appendChild(dataTable([
        { key: "category", label: "Category", fmt: (r) => esc(r.category) },
        { key: "name", label: "Product", cls: "strong", fmt: (r) => esc(r.name) },
        { key: "old_price", label: "Old", align: "right", fmt: (r) => money(r.old_price) },
        { key: "new_price", label: "New", align: "right", fmt: (r) => money(r.new_price) },
        { key: "change_pct", label: "Change", align: "right", fmt: (r) => `<span class="${r.change_pct > 0 ? "neg" : ""}">${r.change_pct > 0 ? "▲" : "▼"} ${pct(Math.abs(r.change_pct))}</span>` },
        { key: "impact", label: "Impact $", align: "right", fmt: (r) => `<b class="${r.impact > 0 ? "neg" : ""}">${money(r.impact)}</b>` },
      ], d.movers, { search: true, initialSort: { key: "impact", dir: -1 },
        footer: { name: `${d.movers.length} items`, impact: `<b>${money(d.total_impact)}</b>` } }));
    } catch (e) { body.innerHTML = `<p class="err">${esc(e.message)}</p>`; }
  }
  load();
}

/* ============================================================
   PRODUCTS
   ============================================================ */
function renderProducts(parts) {
  const sub = parts[0] || "all";
  if (sub === "new") return renderNewItems();
  if (sub === "purchase") return renderPurchaseReport();
  return renderAllProducts();
}

async function renderAllProducts() {
  const v = view();
  v.appendChild(el(`
    <h2 class="section section-head">Products</h2>
    <button class="btn btn-brass btn-block" id="add-product">+ Add Product</button>
    <div class="filters">
      <label class="fld"><span>Type</span><select id="p-type"><option value="">All Types</option></select></label>
      <label class="fld"><span>Category</span><select id="p-cat"><option value="">All Categories</option></select></label>
    </div>
    <div id="p-list"><div class="spinner"></div></div>`));
  $("#add-product").addEventListener("click", () => openItemEditor(null));
  const cats = await loadCategories();
  const types = [...new Set(cats.map((c) => c.category_type))];
  $("#p-type").insertAdjacentHTML("beforeend", types.map((t) => `<option value="${esc(t)}">${esc(t)}</option>`).join(""));
  $("#p-cat").insertAdjacentHTML("beforeend", cats.map((c) => `<option value="${esc(c.name)}">${esc(c.name)}</option>`).join(""));
  $("#p-type").addEventListener("change", load);
  $("#p-cat").addEventListener("change", load);
  async function load() {
    const box = $("#p-list");
    box.innerHTML = '<div class="spinner"></div>';
    const qs = new URLSearchParams();
    if ($("#p-type").value) qs.set("category_type", $("#p-type").value);
    if ($("#p-cat").value) qs.set("category", $("#p-cat").value);
    try {
      const list = await api("GET", "/api/products?" + qs.toString());
      box.innerHTML = "";
      box.appendChild(dataTable([
        { key: "name", label: "Name", cls: "strong", fmt: (r) => `<a href="#/product/${r.id}" style="color:var(--accent);text-decoration:none">${esc(r.name)}</a>` },
        { key: "category_name", label: "Category", fmt: (r) => r.category_type ? typePill(r.category_type) + " " + esc(r.category_name || "") : "—" },
        { key: "report_by_unit", label: "Report By", fmt: (r) => esc(r.report_by_unit || r.unit || "—") },
        { key: "on_inventory", label: "On Inv", fmt: (r) => (r.on_inventory ? "Yes" : "No") },
        { key: "tax_exempt", label: "Tax Exempt", fmt: (r) => (r.tax_exempt ? "Yes" : "No") },
        { key: "unit_cost", label: "Last $", align: "right", fmt: (r) => money(r.unit_cost) },
      ], list, { search: true, empty: "No products match.", initialSort: { key: "name", dir: 1 } }));
    } catch (e) { box.innerHTML = `<p class="err">${esc(e.message)}</p>`; }
  }
  load();
}

async function renderNewItems() {
  const v = view();
  v.appendChild(el(`
    <h2 class="section section-head">New Item Review</h2>
    <p class="muted" style="font-size:.85rem;margin-top:-.3rem">New vendor items from recent invoices. Confirm a category, then approve.</p>
    <div id="ni-list"><div class="spinner"></div></div>`));
  const cats = await loadCategories();
  const opts = (sel) => `<option value="">— category —</option>` + cats.map((c) =>
    `<option value="${c.id}" ${c.id === sel ? "selected" : ""}>${esc(c.category_type)} · ${esc(c.name)}</option>`).join("");
  async function load() {
    const box = $("#ni-list");
    box.innerHTML = '<div class="spinner"></div>';
    try {
      const list = await api("GET", "/api/products/new-items");
      box.innerHTML = "";
      if (!list.length) { box.appendChild(el(`<p class="empty">Nothing to review.<br>New vendor items show up here as you log invoices.</p>`)); return; }
      list.forEach((vi) => {
        const row = el(`<div class="row-item" style="flex-wrap:wrap">
          <div class="grow">
            <div class="ttl">${esc(vi.vendor_item_name)}</div>
            <div class="meta">${esc(vi.vendor_name || "—")}${vi.last_purchase_price != null ? " · " + money(vi.last_purchase_price) : ""}</div>
          </div>
          <select class="ni-cat" style="flex:1 1 180px">${opts(vi.category_id)}</select>
          <button class="btn btn-sm btn-grn ni-ok">Approve</button>
        </div>`);
        row.querySelector(".ni-ok").addEventListener("click", async () => {
          const cid = row.querySelector(".ni-cat").value;
          try {
            await api("POST", `/api/products/new-items/${vi.id}/accept`, cid ? { category_id: Number(cid) } : {});
            toast("Approved.");
            load();
          } catch (e) { toast(e.message); }
        });
        box.appendChild(row);
      });
    } catch (e) { box.innerHTML = `<p class="err">${esc(e.message)}</p>`; }
  }
  load();
}

async function renderPurchaseReport() {
  const v = view();
  v.innerHTML = `<h2 class="section section-head">Purchase Report</h2>`;
  const body = el(`<div id="pr-body"><div class="spinner"></div></div>`);
  const filter = dateFilterBar(load);
  v.appendChild(filter.bar);
  v.appendChild(body);
  async function load() {
    body.innerHTML = '<div class="spinner"></div>';
    const { start, end } = filter.get();
    try {
      const d = await api("GET", `/api/products/purchase-report?start=${start}&end=${end}`);
      body.innerHTML = "";
      if (!d.rows.length) { body.appendChild(el(`<p class="empty">No purchases in this range.</p>`)); return; }
      const total = d.rows.reduce((s, r) => s + (r.spend || 0), 0);
      body.appendChild(dataTable([
        { key: "product", label: "Product", cls: "strong", fmt: (r) => esc(r.product) },
        { key: "category_type", label: "Type", fmt: (r) => r.category_type ? typePill(r.category_type) : "—" },
        { key: "category", label: "Category", fmt: (r) => esc(r.category || "—") },
        { key: "report_by", label: "Report By", fmt: (r) => esc(r.report_by || "—") },
        { key: "units", label: "Units", align: "right", fmt: (r) => fmtQty(r.units) },
        { key: "spend", label: "Spend", align: "right", fmt: (r) => money(r.spend) },
      ], d.rows, { search: true, initialSort: { key: "spend", dir: -1 },
        footer: { product: `${d.rows.length} products`, spend: `<b>${money(total)}</b>` } }));
    } catch (e) { body.innerHTML = `<p class="err">${esc(e.message)}</p>`; }
  }
  load();
}

async function renderProductDetail(parts) {
  loading();
  try {
    const p = await api("GET", `/api/products/${parts[0]}`);
    const v = view();
    v.innerHTML = "";
    v.appendChild(el(`
      <h2 class="section section-head">${esc(p.name)}</h2>
      <div class="card"><div class="card-body">
        <div class="kv"><span>Category</span><span>${p.category_type ? typePill(p.category_type) + " " + esc(p.category_name || "") : "—"}</span></div>
        <div class="kv"><span>Report By</span><b>${esc(p.report_by_unit || p.unit || "—")}</b></div>
        <div class="kv"><span>Accounting Code</span><b>${esc(p.accounting_code || "—")}</b></div>
        <div class="kv"><span>On Inventory</span><b>${p.on_inventory ? "Yes" : "No"}</b></div>
        <div class="kv"><span>Tax Exempt</span><b>${p.tax_exempt ? "Yes" : "No"}</b></div>
        <div class="kv"><span>Last Unit Cost</span><b>${money(p.unit_cost)}</b></div>
        <div class="kv"><span>Vendor</span><b>${esc(p.vendor || "—")}</b></div>
      </div></div>`));
    if (p.purchase_history && p.purchase_history.length) {
      const card = el(`<div class="card"><div class="card-band">Purchase History</div><div class="card-body" id="ph"></div></div>`);
      card.querySelector("#ph").appendChild(dataTable([
        { key: "invoice_date", label: "Date", fmt: (r) => esc(r.invoice_date || "—") },
        { key: "vendor", label: "Vendor", fmt: (r) => esc(r.vendor || "—") },
        { key: "qty", label: "Qty", align: "right", fmt: (r) => fmtQty(r.qty) },
        { key: "unit_cost", label: "Unit $", align: "right", fmt: (r) => money(r.unit_cost) },
        { key: "total", label: "Total", align: "right", fmt: (r) => money(r.total) },
      ], p.purchase_history, {}));
      v.appendChild(card);
    }
    v.appendChild(el(`<button class="btn btn-brass btn-block" id="edit-p" style="margin-top:.6rem">Edit</button>
      <button class="btn btn-ghost btn-block" id="back-p" style="margin-top:.5rem">Back to Products</button>`));
    $("#edit-p").addEventListener("click", () => openItemEditor(p));
    $("#back-p").addEventListener("click", () => { location.hash = "#/products"; });
  } catch (e) { view().innerHTML = `<p class="err">${esc(e.message)}</p>`; }
}

/* ============================================================
   RECIPES / plate costing
   ============================================================ */
function renderRecipes(parts) {
  const sub = parts[0];
  if (sub === "new") return recipeEditor(null);
  if (sub) return recipeEditor(sub);
  return recipeList();
}

async function recipeList() {
  const v = view();
  v.innerHTML = `<h2 class="section section-head">Recipes</h2>`;
  v.appendChild(el(`<div class="btn-row">
    <button class="btn btn-brass btn-sm" id="rec-new">+ New Recipe</button>
    <button class="btn btn-ghost btn-sm" id="rec-exp">&#x2B07; Costing (CSV)</button>
  </div>`));
  $("#rec-new").addEventListener("click", () => { location.hash = "#/recipes/new"; });
  $("#rec-exp").addEventListener("click", () =>
    download("/api/export/recipes.csv", "recipe-costing.csv").catch((e) => toast("Export failed: " + e.message)));
  const body = el(`<div><div class="spinner"></div></div>`);
  v.appendChild(body);
  try {
    const list = await api("GET", "/api/recipes");
    body.innerHTML = "";
    if (!list.length) {
      body.appendChild(el(`<p class="empty">No recipes yet. Add one to cost a menu item.</p>`));
      return;
    }
    const columns = [
      { key: "name", label: "Recipe", cls: "strong", fmt: (r) => esc(r.name) },
      { key: "cost_per_serving", label: "Cost", align: "right", fmt: (r) => money(r.cost_per_serving) },
      { key: "menu_price", label: "Price", align: "right", fmt: (r) => money(r.menu_price) },
      { key: "cost_pct", label: "Cost %", align: "right", fmt: (r) => pct(r.cost_pct) },
      { key: "margin", label: "Margin", align: "right", fmt: (r) => (r.margin == null ? "—" : money(r.margin)) },
    ];
    const rows = list.map((r) => ({ ...r, _href: `#/recipes/${r.id}` }));
    body.appendChild(dataTable(columns, rows, { search: true, initialSort: { key: "cost_pct", dir: -1 } }));
  } catch (e) { body.innerHTML = `<p class="err">${esc(e.message)}</p>`; }
}

async function recipeEditor(id) {
  const v = view();
  v.innerHTML = `<div class="spinner"></div>`;
  let rec = { name: "", menu_price: 0, yield_qty: 1, notes: "", items: [] };
  let products = [];
  try {
    products = await api("GET", "/api/products");
    if (id) rec = await api("GET", `/api/recipes/${id}`);
  } catch (e) { v.innerHTML = `<p class="err">${esc(e.message)}</p>`; return; }

  v.innerHTML = `<h2 class="section section-head">${id ? "Edit" : "New"} Recipe</h2>`;
  v.appendChild(el(`<div class="card"><div class="card-body">
    <label class="fld"><span>Name</span><input id="r-name" value="${esc(rec.name)}"></label>
    <div class="row2">
      <label class="fld"><span>Menu Price $</span><input type="number" step="0.01" id="r-price" value="${num(rec.menu_price)}"></label>
      <label class="fld"><span>Yields (servings)</span><input type="number" step="0.01" id="r-yield" value="${num(rec.yield_qty)}"></label>
    </div>
    <label class="fld"><span>Notes</span><input id="r-notes" value="${esc(rec.notes || "")}"></label>
  </div></div>`));

  const ingCard = el(`<div class="card"><div class="card-band">Ingredients
    <button class="btn btn-sm btn-ghost" id="r-add">+ Add</button></div>
    <div class="card-body" id="r-lines"></div></div>`);
  v.appendChild(ingCard);
  const linesEl = ingCard.querySelector("#r-lines");
  v.appendChild(el(`<datalist id="unit-list">${RECIPE_UNITS.map((u) => `<option value="${u}">`).join("")}</datalist>`));
  const prodOptions = products.map((p) =>
    `<option value="${p.id}">${esc(p.name)} (${money(p.unit_cost)}/${esc(p.unit || "ea")}${p.size_qty ? `, ${p.size_qty}${esc(p.size_unit || "")}` : ""})</option>`).join("");
  const prodById = (pid) => products.find((x) => String(x.id) === String(pid));
  // Round half-to-even, matching Python's round() / money.normalize on the
  // backend, so the live preview equals the saved cost to the penny.
  const bankers = (n) => {
    const fl = Math.floor(n), d = n - fl;
    if (d < 0.5) return fl;
    if (d > 0.5) return fl + 1;
    return fl % 2 === 0 ? fl : fl + 1;
  };
  // Mirror recipes._line_cost: convert qty into the product's size unit and take
  // the fraction of a purchase unit, else fall back to qty * unit_cost.
  const lineCents = (pid, qty, unit) => {
    const p = prodById(pid);
    if (!p) return 0;
    const uc = p.unit_cost || 0;
    if (p.size_qty > 0 && unit && p.size_unit) {
      const used = convertUnit(qty, unit, p.size_unit);
      if (used !== null) return bankers(uc * (used / p.size_qty) * 100);
    }
    return bankers(qty * uc * 100);
  };

  const addRow = (it) => {
    it = it || {};
    const row = el(`<div class="lrow rrow">
      <select class="ri-prod"><option value="">— product —</option>${prodOptions}</select>
      <input class="ri-qty" type="number" step="0.0001" placeholder="qty" value="${num(it.qty)}">
      <input class="ri-unit" list="unit-list" placeholder="unit" value="${esc(it.unit || "")}" style="max-width:5.5rem">
      <button class="btn btn-sm btn-ghost ri-del">&times;</button>
    </div>`);
    if (it.product_id) row.querySelector(".ri-prod").value = String(it.product_id);
    row.querySelector(".ri-del").addEventListener("click", () => { row.remove(); recalc(); });
    row.querySelector(".ri-prod").addEventListener("change", recalc);
    row.querySelector(".ri-qty").addEventListener("input", recalc);
    row.querySelector(".ri-unit").addEventListener("input", recalc);
    linesEl.appendChild(row);
  };
  (rec.items || []).forEach(addRow);
  if (!(rec.items || []).length) addRow();
  ingCard.querySelector("#r-add").addEventListener("click", () => { addRow(); recalc(); });

  const preview = el(`<div class="note" id="r-preview"></div>`);
  v.appendChild(preview);
  // Mirror the backend: cost each line in cents, sum, divide by yield.
  function recalc() {
    let cents = 0;
    linesEl.querySelectorAll(".rrow").forEach((row) => {
      const pid = row.querySelector(".ri-prod").value;
      const qty = Number(row.querySelector(".ri-qty").value) || 0;
      const unit = row.querySelector(".ri-unit").value;
      cents += lineCents(pid, qty, unit);
    });
    const batch = cents / 100;
    const yld = Number($("#r-yield").value) > 0 ? Number($("#r-yield").value) : 1;
    const per = bankers(cents / yld) / 100;
    const price = Number($("#r-price").value) || 0;
    preview.innerHTML = `Batch ${money(batch)} &middot; per serving <b>${money(per)}</b>` +
      (price ? ` &middot; cost ${pct((per / price) * 100)} &middot; margin ${money(price - per)}` : "");
  }
  $("#r-price").addEventListener("input", recalc);
  $("#r-yield").addEventListener("input", recalc);
  recalc();

  v.appendChild(el(`<div class="btn-row" style="margin-top:.6rem">
    <button class="btn btn-brass" id="r-save">Save</button>
    ${id ? '<button class="btn btn-ghost" id="r-del">Delete</button>' : ""}
    <button class="btn btn-ghost" id="r-cancel">Cancel</button>
  </div>`));
  $("#r-cancel").addEventListener("click", () => { location.hash = "#/recipes"; });
  if (id) $("#r-del").addEventListener("click", async () => {
    if (!confirm("Delete this recipe?")) return;
    try { await api("DELETE", `/api/recipes/${id}`); location.hash = "#/recipes"; }
    catch (e) { toast(e.message); }
  });
  $("#r-save").addEventListener("click", async () => {
    const items = [...linesEl.querySelectorAll(".rrow")]
      .map((row) => ({ product_id: row.querySelector(".ri-prod").value || null,
                       qty: Number(row.querySelector(".ri-qty").value) || 0,
                       unit: row.querySelector(".ri-unit").value.trim() }))
      .filter((x) => x.product_id);
    const name = $("#r-name").value.trim();
    if (!name) { toast("Name the recipe."); return; }
    const payload = { name, menu_price: Number($("#r-price").value) || 0,
      yield_qty: Number($("#r-yield").value) || 1, notes: $("#r-notes").value, items };
    try {
      if (id) await api("PUT", `/api/recipes/${id}`, payload);
      else await api("POST", "/api/recipes", payload);
      location.hash = "#/recipes";
    } catch (e) { toast(e.message); }
  });
}

/* ============================================================
   CATEGORIES (taxonomy admin)
   ============================================================ */
async function renderCategoriesAdmin() {
  const v = view();
  const types = Object.keys(TYPE_CLASS);   // Food, Beer, Wine, Liquor, N/A Bev, Other
  v.innerHTML = `<h2 class="section section-head">Categories</h2>`;
  v.appendChild(el(`<div class="card"><div class="card-band">Add Category</div><div class="card-body">
    <div class="row2">
      <label class="fld"><span>Name</span><input id="c-name" placeholder="e.g. Mezcal"></label>
      <label class="fld"><span>Type</span><select id="c-type">${types.map((t) => `<option>${esc(t)}</option>`).join("")}</select></label>
    </div>
    <button class="btn btn-brass btn-sm" id="c-add">Add</button>
  </div></div>`));
  const body = el(`<div id="cat-body"><div class="spinner"></div></div>`);
  v.appendChild(body);

  async function reload() {
    CATEGORIES = null;            // bust the shared cache other screens read
    body.innerHTML = '<div class="spinner"></div>';
    let cats;
    try { cats = await api("GET", "/api/categories"); }
    catch (e) { body.innerHTML = `<p class="err">${esc(e.message)}</p>`; return; }
    body.innerHTML = "";
    types.forEach((t) => {
      const inType = cats.filter((c) => c.category_type === t)
        .sort((a, b) => (a.sort_order - b.sort_order) || a.name.localeCompare(b.name));
      if (!inType.length) return;
      const card = el(`<div class="card"><div class="card-band">${typePill(t)}</div><div class="card-body"></div></div>`);
      const cb = card.querySelector(".card-body");
      inType.forEach((c) => {
        const row = el(`<div class="kv">
          <input class="ce-name" value="${esc(c.name)}" style="flex:1;margin-right:.4rem">
          <select class="ce-type">${types.map((tt) => `<option ${tt === t ? "selected" : ""}>${esc(tt)}</option>`).join("")}</select>
          <button class="btn btn-sm btn-ghost ce-save">Save</button>
          <button class="btn btn-sm btn-ghost ce-del">Archive</button>
        </div>`);
        row.querySelector(".ce-save").addEventListener("click", async () => {
          const name = row.querySelector(".ce-name").value.trim();
          if (!name) { toast("Name can’t be blank."); return; }
          try {
            await api("PUT", `/api/categories/${c.id}`,
              { name, category_type: row.querySelector(".ce-type").value });
            toast("Saved.");
            reload();   // re-bucket under the (possibly new) type; also busts the cache
          } catch (e) { toast(e.message); }
        });
        row.querySelector(".ce-del").addEventListener("click", async () => {
          if (!confirm(`Archive "${c.name}"? Existing invoice lines keep their category.`)) return;
          try { await api("DELETE", `/api/categories/${c.id}`); reload(); }
          catch (e) { toast(e.message); }
        });
        cb.appendChild(row);
      });
      body.appendChild(card);
    });
  }

  $("#c-add").addEventListener("click", async () => {
    const name = $("#c-name").value.trim();
    if (!name) { toast("Name the category."); return; }
    try {
      await api("POST", "/api/categories", { name, category_type: $("#c-type").value });
      $("#c-name").value = "";
      reload();
    } catch (e) { toast(e.message); }
  });
  reload();
}

/* ---------- small format helpers ---------- */
function num(n) { return n == null || n === "" ? "" : n; }
function f(v, d = null) { const n = parseFloat(v); return isNaN(n) ? d : n; }
function round1(n) { return Math.round(n * 100) / 100; }
function fmtQty(n) { n = Number(n || 0); return Number.isInteger(n) ? String(n) : n.toFixed(2).replace(/\.?0+$/, ""); }

/* ============================================================
   BOOT
   ============================================================ */
async function boot() {
  try {
    CONFIG = await api("GET", "/api/config");
  } catch (e) {
    if (e.message === "unauthorized") return; // gate already shown
    CONFIG = {};
  }
  if (CONFIG.auth_required && !localStorage.getItem(TOKEN_KEY)) {
    showGate();
  } else {
    showApp();
  }
}
boot();

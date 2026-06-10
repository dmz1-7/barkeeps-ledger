/* Barkeep's Ledger — front-of-house single-page app (no build step). */

const TOKEN_KEY = "ledger_token";
const CATS = {
  food: "Food", liquor: "Liquor", beer: "Beer", wine: "Wine",
  na_beverage: "N/A Bev", supplies: "Supplies", other: "Other",
};
let CONFIG = {};

/* ---------- tiny helpers ---------- */
const $ = (sel, root = document) => root.querySelector(sel);
const view = () => $("#view");
const money = (n) => (n == null ? "—" : "$" + Number(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
const pct = (n) => (n == null ? "—" : Number(n).toFixed(1) + "%");
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

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

/* ---------- date helpers ---------- */
const iso = (d) => d.toISOString().slice(0, 10);
function weekStart(d = new Date()) { const x = new Date(d); const day = (x.getDay() + 6) % 7; x.setDate(x.getDate() - day); return x; }
function monthStart(d = new Date()) { return new Date(d.getFullYear(), d.getMonth(), 1); }

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
  if (!location.hash) location.hash = "#/dashboard";
  else route();
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
   ROUTER
   ============================================================ */
const ROUTES = {
  dashboard: renderDashboard,
  invoices: renderInvoices,
  invoice: renderInvoiceDetail,
  inventory: renderInventory,
  vendors: renderVendors,
  vendor: renderVendorDetail,
  count: renderCount,
  settings: renderSettings,
};
function route() {
  const parts = (location.hash.replace(/^#\//, "") || "dashboard").split("/");
  const name = parts[0];
  const fn = ROUTES[name] || renderDashboard;
  document.querySelectorAll(".tabbar a").forEach((a) =>
    a.classList.toggle("active", a.dataset.tab === name));
  view().innerHTML = "";
  fn(parts.slice(1));
  window.scrollTo(0, 0);
}
window.addEventListener("hashchange", route);

function loading() { view().innerHTML = '<div class="spinner"></div>'; }

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
  $("#d-go").addEventListener("click", () => {
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
    pb.appendChild(el(`<div class="kv"><span class="pill ${c}">${CATS[c] || c}</span><b>${money(amt)}</b></div>`)));
  body.appendChild(card);

  if (d.begin_inventory && d.end_inventory) {
    body.appendChild(el(`<div class="note">Usage-based COGS using counts:
      open ${money(d.begin_inventory.value)} + buys ${money(d.purchases)} &minus;
      close ${money(d.end_inventory.value)} = <b>${money(d.cogs)}</b>.</div>`));
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
async function renderInvoices() {
  const v = view();
  v.appendChild(el(`
    <h2 class="section section-head">Invoices</h2>
    <div class="btn-row">
      <button class="btn btn-brass" id="snap">&#x1F4F7; Photograph Invoice</button>
      <button class="btn btn-ghost" id="manual">Enter by Hand</button>
    </div>
    <input type="file" id="file" accept="image/*" capture="environment" class="hidden">
    <div id="inv-list"><div class="spinner"></div></div>`));

  $("#snap").addEventListener("click", () => $("#file").click());
  $("#manual").addEventListener("click", () => openInvoiceForm(null, null));
  $("#file").addEventListener("change", onPhoto);

  try {
    const list = await api("GET", "/api/invoices");
    const box = $("#inv-list");
    box.innerHTML = "";
    if (!list.length) { box.appendChild(el(`<p class="empty">No invoices logged yet.<br>Snap your first delivery slip.</p>`)); return; }
    list.forEach((iv) => {
      const row = el(`<a class="row-item" href="#/invoice/${iv.id}" style="text-decoration:none;color:inherit">
        ${iv.image_path ? `<img class="thumb" src="/uploads/${esc(iv.image_path)}" alt="">` : `<span class="ic">&#x1F4C4;</span>`}
        <div class="grow">
          <div class="ttl">${esc(iv.vendor || "Unknown vendor")}</div>
          <div class="meta">${esc(iv.invoice_date || "no date")} ${iv.invoice_number ? "· #" + esc(iv.invoice_number) : ""}</div>
        </div>
        <span class="pill ${iv.category}">${CATS[iv.category] || iv.category}</span>
        <div class="amt">${money(iv.total)}</div>
      </a>`);
      box.appendChild(row);
    });
  } catch (e) { $("#inv-list").innerHTML = `<p class="err">${esc(e.message)}</p>`; }
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

function openInvoiceForm(parsed, imagePath, warn) {
  parsed = parsed || { vendor: "", invoice_date: "", invoice_number: "", category: "other", subtotal: null, tax: null, total: null, line_items: [] };
  const v = view();
  v.innerHTML = "";
  v.appendChild(el(`
    <h2 class="section section-head">Confirm Invoice</h2>
    ${warn ? `<div class="note">${esc(warn)} — enter the details by hand.</div>` : ""}
    ${imagePath ? `<img class="thumb-lg" src="/uploads/${esc(imagePath)}" alt="invoice">` : ""}
    <div class="card"><div class="card-body">
      <label class="fld"><span>Vendor</span><input id="f-vendor" list="vendor-list" value="${esc(parsed.vendor)}"><datalist id="vendor-list"></datalist></label>
      <div class="row2">
        <label class="fld"><span>Date</span><input type="date" id="f-date" value="${esc(parsed.invoice_date)}"></label>
        <label class="fld"><span>Invoice #</span><input id="f-num" value="${esc(parsed.invoice_number)}"></label>
      </div>
      <label class="fld"><span>Category</span>
        <select id="f-cat">${Object.entries(CATS).map(([k, vv]) =>
          `<option value="${k}" ${k === parsed.category ? "selected" : ""}>${vv}</option>`).join("")}</select></label>
      <div class="row3">
        <label class="fld"><span>Subtotal</span><input type="number" step="0.01" id="f-sub" value="${num(parsed.subtotal)}"></label>
        <label class="fld"><span>Tax</span><input type="number" step="0.01" id="f-tax" value="${num(parsed.tax)}"></label>
        <label class="fld"><span>Total</span><input type="number" step="0.01" id="f-total" value="${num(parsed.total)}"></label>
      </div>
    </div></div>

    <div class="card"><div class="card-band">Line Items <button class="btn btn-sm btn-ghost" id="add-line">+ Add</button></div>
      <div class="card-body" id="lines"></div></div>

    <div class="btn-row">
      <button class="btn btn-grn btn-block" id="save-inv">Save to Ledger</button>
    </div>
    <button class="btn btn-ghost btn-block" id="cancel-inv" style="margin-top:.5rem">Cancel</button>
  `));

  const lines = $("#lines");
  (parsed.line_items || []).forEach((li) => lines.appendChild(lineRow(li)));
  if (!(parsed.line_items || []).length) lines.appendChild(lineRow({}));
  $("#add-line").addEventListener("click", () => lines.appendChild(lineRow({})));
  $("#cancel-inv").addEventListener("click", () => { location.hash = "#/invoices"; });
  fillVendorDatalist();

  $("#save-inv").addEventListener("click", async () => {
    const items = [...lines.querySelectorAll(".lrow")].map((r) => ({
      name: r.querySelector(".li-name").value,
      qty: f(r.querySelector(".li-qty").value),
      unit: r.querySelector(".li-unit").value || null,
      unit_cost: f(r.querySelector(".li-cost").value),
      total: f(r.querySelector(".li-total").value),
    })).filter((x) => x.name.trim());
    const payload = {
      vendor: $("#f-vendor").value, invoice_date: $("#f-date").value,
      invoice_number: $("#f-num").value, category: $("#f-cat").value,
      subtotal: f($("#f-sub").value), tax: f($("#f-tax").value), total: f($("#f-total").value),
      image_path: imagePath || null, line_items: items,
      raw_json: parsed ? JSON.stringify(parsed) : "",
    };
    try {
      await api("POST", "/api/invoices", payload);
      toast("Logged to the ledger.");
      location.hash = "#/invoices";
    } catch (e) { toast(e.message); }
  });
}

function lineRow(li) {
  const r = el(`<div class="lrow" style="display:grid;grid-template-columns:1fr auto;gap:.4rem;align-items:start;margin-bottom:.6rem;border-bottom:1px dotted var(--edge);padding-bottom:.5rem">
    <input class="li-name" placeholder="Item" value="${esc(li.name || "")}">
    <button class="btn btn-sm btn-ghost li-del" title="remove">&times;</button>
    <div class="row3" style="grid-column:1 / -1">
      <input class="li-qty" type="number" step="0.01" placeholder="qty" value="${num(li.qty)}">
      <input class="li-unit" placeholder="unit" value="${esc(li.unit || "")}">
      <input class="li-cost" type="number" step="0.01" placeholder="unit $" value="${num(li.unit_cost)}">
    </div>
    <input class="li-total" type="number" step="0.01" placeholder="line total" value="${num(li.total)}" style="grid-column:1 / -1">
  </div>`);
  r.querySelector(".li-del").addEventListener("click", () => r.remove());
  return r;
}

async function renderInvoiceDetail(parts) {
  loading();
  const id = parts[0];
  try {
    const iv = await api("GET", `/api/invoices/${id}`);
    const v = view();
    v.innerHTML = "";
    v.appendChild(el(`
      <h2 class="section section-head">${esc(iv.vendor || "Invoice")}</h2>
      ${iv.image_path ? `<img class="thumb-lg" src="/uploads/${esc(iv.image_path)}" alt="">` : ""}
      <div class="card"><div class="card-body">
        <div class="kv"><span>Date</span><b>${esc(iv.invoice_date || "—")}</b></div>
        <div class="kv"><span>Invoice #</span><b>${esc(iv.invoice_number || "—")}</b></div>
        <div class="kv"><span>Category</span><span class="pill ${iv.category}">${CATS[iv.category] || iv.category}</span></div>
        <div class="kv"><span>Subtotal</span><b>${money(iv.subtotal)}</b></div>
        <div class="kv"><span>Tax</span><b>${money(iv.tax)}</b></div>
        <div class="kv"><span>Total</span><b>${money(iv.total)}</b></div>
      </div></div>
      ${iv.line_items.length ? `<div class="card"><div class="card-band">Items</div><div class="card-body">
        ${iv.line_items.map((li) => `<div class="kv"><span>${esc(li.name)}${li.qty ? ` <span class="muted">×${li.qty} ${esc(li.unit || "")}</span>` : ""}</span><b>${money(li.total)}</b></div>`).join("")}
      </div></div>` : ""}
      <button class="btn btn-ox btn-block" id="del-inv" style="margin-top:.6rem">Delete Invoice</button>
      <button class="btn btn-ghost btn-block" id="back" style="margin-top:.5rem">Back</button>`));
    $("#back").addEventListener("click", () => { location.hash = "#/invoices"; });
    $("#del-inv").addEventListener("click", async () => {
      if (!confirm("Delete this invoice?")) return;
      await api("DELETE", `/api/invoices/${id}`);
      toast("Removed.");
      location.hash = "#/invoices";
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
    const items = await api("GET", "/api/inventory");
    box.innerHTML = "";
    if (!items.length) { box.appendChild(el(`<p class="empty">No items yet.<br>Add your first item to set a par.</p>`)); return; }
    const byCat = {};
    items.forEach((it) => (byCat[it.category] = byCat[it.category] || []).push(it));
    Object.keys(byCat).sort().forEach((cat) => {
      box.appendChild(el(`<h2 class="section section-head">${CATS[cat] || cat}</h2>`));
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

function openItemEditor(it) {
  const isNew = !it;
  it = it || { name: "", category: "liquor", unit: "bottle", par_level: 0, last_count: 0, unit_cost: 0, vendor: "" };
  const v = view();
  v.innerHTML = "";
  v.appendChild(el(`
    <h2 class="section section-head">${isNew ? "New Item" : "Edit Item"}</h2>
    <div class="card"><div class="card-body">
      <label class="fld"><span>Name</span><input id="i-name" value="${esc(it.name)}"></label>
      <div class="row2">
        <label class="fld"><span>Category</span><select id="i-cat">${Object.entries(CATS).map(([k, vv]) =>
          `<option value="${k}" ${k === it.category ? "selected" : ""}>${vv}</option>`).join("")}</select></label>
        <label class="fld"><span>Unit</span><input id="i-unit" value="${esc(it.unit)}" placeholder="bottle, case, keg…"></label>
      </div>
      <div class="row3">
        <label class="fld"><span>Par</span><input type="number" step="0.5" id="i-par" value="${num(it.par_level)}"></label>
        <label class="fld"><span>On Hand</span><input type="number" step="0.5" id="i-cnt" value="${num(it.last_count)}"></label>
        <label class="fld"><span>Unit $</span><input type="number" step="0.01" id="i-cost" value="${num(it.unit_cost)}"></label>
      </div>
      <label class="fld"><span>Vendor</span><input id="i-vendor" list="vendor-list" value="${esc(it.vendor || "")}"><datalist id="vendor-list"></datalist></label>
    </div></div>
    <button class="btn btn-grn btn-block" id="save-item">Save</button>
    ${isNew ? "" : `<button class="btn btn-ox btn-block" id="del-item" style="margin-top:.5rem">Delete Item</button>`}
    <button class="btn btn-ghost btn-block" id="cancel-item" style="margin-top:.5rem">Cancel</button>`));

  $("#cancel-item").addEventListener("click", () => { location.hash = "#/inventory"; });
  fillVendorDatalist();
  $("#save-item").addEventListener("click", async () => {
    const payload = {
      name: $("#i-name").value.trim(), category: $("#i-cat").value, unit: $("#i-unit").value.trim(),
      par_level: f($("#i-par").value, 0), last_count: f($("#i-cnt").value, 0),
      unit_cost: f($("#i-cost").value, 0), vendor: $("#i-vendor").value.trim(),
    };
    if (!payload.name) { toast("Give it a name."); return; }
    try {
      if (isNew) await api("POST", "/api/inventory", payload);
      else await api("PUT", `/api/inventory/${it.id}`, payload);
      toast("Saved.");
      location.hash = "#/inventory";
    } catch (e) { toast(e.message); }
  });
  if (!isNew) $("#del-item").addEventListener("click", async () => {
    if (!confirm("Remove this item from inventory?")) return;
    await api("DELETE", `/api/inventory/${it.id}`);
    toast("Removed.");
    location.hash = "#/inventory";
  });
}

async function showOrderList() {
  loading();
  try {
    const items = await api("GET", "/api/inventory/order-list");
    const v = view();
    v.innerHTML = `<h2 class="section section-head">Order List</h2>`;
    if (!items.length) { v.appendChild(el(`<p class="empty">Everything&rsquo;s at or above par. Nothing to order.</p>`)); }
    else {
      let total = 0;
      const card = el(`<div class="card"><div class="card-band">Below Par <span id="ord-total"></span></div><div class="card-body" id="ord"></div></div>`);
      const ord = card.querySelector("#ord");
      items.forEach((it) => {
        total += it.order_cost;
        ord.appendChild(el(`<div class="kv"><span>${esc(it.name)} <span class="muted">need ${fmtQty(it.order_qty)} ${esc(it.unit || "")}</span></span><b>${money(it.order_cost)}</b></div>`));
      });
      card.querySelector("#ord-total").textContent = money(total);
      v.appendChild(card);
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
  try { items = await api("GET", "/api/inventory"); }
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
  items.forEach((it) => (byCat[it.category] = byCat[it.category] || []).push(it));
  const state = {};
  Object.keys(byCat).sort().forEach((cat) => {
    v.appendChild(el(`<h2 class="section section-head">${CATS[cat] || cat}</h2>`));
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

    <button class="btn btn-brass btn-block" id="save-settings">Save Settings</button>
    <button class="btn btn-ghost btn-block" id="logout" style="margin-top:.6rem">Sign Out</button>
    <p class="center muted" style="margin-top:1rem;font-size:.75rem">Barkeep&rsquo;s Ledger · self-hosted</p>`));

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
      const r = await api("GET", "/api/locations");
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

  $("#logout").addEventListener("click", () => { localStorage.removeItem(TOKEN_KEY); location.reload(); });
}

/* ============================================================
   VENDORS
   ============================================================ */
let VENDOR_NAMES = null;  // cache for the autocomplete datalist

async function fillVendorDatalist() {
  const dl = $("#vendor-list");
  if (!dl) return;
  try {
    if (!VENDOR_NAMES) VENDOR_NAMES = (await api("GET", "/api/vendors")).map((v) => v.name);
    dl.innerHTML = VENDOR_NAMES.map((n) => `<option value="${esc(n)}"></option>`).join("");
  } catch (e) { /* autocomplete is a nicety; ignore failures */ }
}

async function renderVendors() {
  const v = view();
  v.appendChild(el(`
    <h2 class="section section-head">Vendors</h2>
    <button class="btn btn-brass btn-block" id="add-vendor">+ New Vendor</button>
    <div id="vlist"><div class="spinner"></div></div>`));
  $("#add-vendor").addEventListener("click", () => openVendorEditor(null));
  try {
    const list = await api("GET", "/api/vendors");
    const box = $("#vlist");
    box.innerHTML = "";
    if (!list.length) { box.appendChild(el(`<p class="empty">No vendors yet.<br>Add your distributors and reps here.</p>`)); return; }
    list.forEach((vd) => {
      const sub = [vd.contact_name, vd.phone].filter(Boolean).join(" · ") || (vd.order_days ? "Orders " + vd.order_days : "—");
      const row = el(`<a class="row-item" href="#/vendor/${vd.id}" style="text-decoration:none;color:inherit">
        <div class="grow">
          <div class="ttl">${esc(vd.name)}</div>
          <div class="meta">${esc(sub)}</div>
        </div>
        <div style="text-align:right">
          <div class="amt">${money(vd.spend)}</div>
          <div class="meta">${vd.invoice_count} inv</div>
        </div>
      </a>`);
      box.appendChild(row);
    });
  } catch (e) { $("#vlist").innerHTML = `<p class="err">${esc(e.message)}</p>`; }
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
      <span>${esc(iv.invoice_date || "—")} <span class="pill ${iv.category}">${CATS[iv.category] || iv.category}</span></span>
      <b>${money(iv.total)}</b></a>`)));
  const its = $("#v-items");
  if (its) vd.items.forEach((it) => its.appendChild(el(
    `<div class="kv"><span>${esc(it.name)} <span class="muted">par ${fmtQty(it.par_level)} ${esc(it.unit || "")}</span></span><b>${money(it.unit_cost)}</b></div>`)));

  $("#edit-vendor").addEventListener("click", () => openVendorEditor(vd));
  $("#back-vendors").addEventListener("click", () => { location.hash = "#/vendors"; });

  function row(label, val) {
    return `<div class="kv"><span>${label}</span><b style="font-family:var(--f-body);font-weight:600">${esc(val || "—")}</b></div>`;
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

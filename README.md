# Barkeep's Ledger

A lightweight, self-hosted back-office for a bar — think of it as a pocket-sized
[MarginEdge](https://www.marginedge.com/). It connects to **Square** for sales and
labor, reads **invoice photographs** with Claude, and tracks **inventory with par
levels** so you can walk the cellar and count fast. It spits out the two numbers
that matter: **COGS %** and **Labor %** (plus prime cost).

Phone-first, runs on your own machine or a cheap VPS, data stays in a single
SQLite file. Aesthetic: a bright 1930s American diner — cherry red, turquoise,
cream and chrome, a checkerboard trim, a neon-script sign over the door, and
big, easy-to-read numbers.

---

## What it does

| Tab | What it's for |
|-----|---------------|
| **Ledger** (Dashboard) | Net sales (Square), Labor $ and %, COGS $ and %, prime cost — for this week, month, or any range. Target bars show whether you're over or under. |
| **Invoices** | Photograph a delivery slip → Claude reads vendor, date, totals, and line items → you confirm → it's logged. Or enter by hand. |
| **Cellar** (Inventory) | Items with **par levels**, unit costs, and vendors. An **order list** shows everything below par and what it'll cost to restock. |
| **Count** | Walk-around counting: big +/− steppers per item, starts from your last numbers, records a dated snapshot of on-hand value. |
| **Forge** (Settings) | Square token & location, COGS/labor targets, and which Claude model reads invoices. |

**How the numbers are figured**

- **Net sales** — sum of completed Square orders (net of tax) over the range.
- **Labor** — Square shifts × hourly wage, minus unpaid breaks. `Labor % = labor ÷ sales`.
- **COGS** — by default *purchases-based*: the invoices you logged in the range.
  When you have an inventory **count at the start and end** of a range, it
  automatically switches to *usage-based*: `opening inventory + purchases −
  closing inventory`. `COGS % = COGS ÷ sales`.
- **Prime cost** — `COGS + Labor`, the number that makes or breaks a bar.

---

## Quick start

You need **Python 3.9+**. From the project folder:

```bash
cp .env.example .env          # then edit .env — at minimum set ANTHROPIC_API_KEY
./run.sh                      # creates a venv, installs deps, starts the server
```

Or do it by hand:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python app.py
```

Open **http://localhost:8088** on your computer, or `http://<your-machine-ip>:8088`
from your phone on the same network. In Safari/Chrome, use **"Add to Home Screen"**
to get a full-screen app icon (it's a PWA).

---

## Configuration

Everything except the Claude key can be set in-app under **The Forge**, or up front
in `.env`:

| Setting | Where | Notes |
|---------|-------|-------|
| `ANTHROPIC_API_KEY` | `.env` only | Required to read invoice photos. From [console.anthropic.com](https://console.anthropic.com). |
| `APP_PASSWORD` | `.env` | Passcode gate. **Leave blank only on a trusted private network.** Set it before exposing the app publicly. |
| `APP_SECRET` | `.env` | Random string used to sign the login token. |
| Square token + location | Forge or `.env` | See below. |
| Targets, model, Square env | Forge | Stored in the database. |

### Connecting Square

1. Create an access token at the [Square Developer dashboard](https://developer.squareup.com/apps)
   (an app → **Production** → Access Token). It needs **`ORDERS_READ`**,
   **`PAYMENTS_READ`**, **`EMPLOYEES_READ`**, and **`TIMECARDS_READ`** scopes.
2. In **The Forge**, paste the token, choose **Production**, then **Load Locations**
   and pick your bar.
3. Save. The Ledger will start pulling sales and labor.

Use **Sandbox** + a sandbox token to try it without touching live data.

### Choosing the invoice model

In the Forge: **Opus** (default) is the most accurate reader; **Sonnet** and
**Haiku** are cheaper per invoice and usually plenty good for clean photos.

---

## Self-hosting notes

- **Data** lives in `data/ledger.db`; uploaded invoice images in `uploads/`.
  Back these two up and you've backed up everything.
- The bundled server is Flask's development server — fine for one bar on a LAN.
  To put it on the open internet, run it behind a reverse proxy (Caddy/nginx with
  HTTPS) and **set `APP_PASSWORD`**. A production WSGI server is optional:
  `pip install gunicorn` then `gunicorn -b 0.0.0.0:8088 app:app`.
- It's a normal Python app — drop it on any VPS, a Raspberry Pi, or run it in
  Docker (a `python:3.12-slim` base, `pip install -r requirements.txt`, `CMD
  ["python","app.py"]`).

---

## Project layout

```
app.py            Flask app + all API routes + auth
db.py             SQLite schema, settings, connection handling
square_client.py  Square Orders (sales) + Labor (shifts) calls
invoice_ai.py     Invoice photo → structured JSON via Claude vision
cogs.py           COGS / labor % / prime cost math
static/           The phone-friendly web app (HTML/CSS/vanilla JS, no build step)
  index.html, css/style.css, js/app.js, manifest.json, icon.svg
data/             ledger.db (created at runtime)
uploads/          invoice images (created at runtime)
```

No build tooling, no Node, no database server. Copy the folder, run it, done.

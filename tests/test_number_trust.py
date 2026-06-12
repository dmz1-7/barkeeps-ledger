"""Tests for the number-trust fix batch.

Covers the four fixes:
  1. Net sales basis excludes tax / tips / service charges.
  2. The daily-sales cache is never overwritten with zeros on a failed fetch.
  4. Duplicate invoices are caught (and can be overridden).
  5. By-id endpoints are scoped to the active location.

Pure stdlib unittest — no pytest needed:

    .venv/bin/python -m unittest discover -s tests
"""
import csv
import datetime as dt
import glob
import io
import os
import shutil
import sqlite3
import tempfile
import unittest

# Point at a throwaway DB and disable the auth gate BEFORE importing the app, so
# we never touch the real data/ledger.db and the test client isn't challenged.
# Clear APP_SECRET too so the persisted-random-secret path is exercised (the
# user's real .env sets one, which load_dotenv would otherwise import).
os.environ["LEDGER_DB"] = tempfile.mktemp(suffix=".db")
os.environ["APP_PASSWORD"] = ""
os.environ["APP_SECRET"] = ""

import db                       # noqa: E402
import square_client            # noqa: E402
import cogs                     # noqa: E402
import money                    # noqa: E402
import reports                  # noqa: E402
from db import get_db           # noqa: E402
import app as app_module        # noqa: E402

flask_app = app_module.app


class Base(unittest.TestCase):
    def setUp(self):
        os.environ["APP_PASSWORD"] = ""
        os.environ["APP_SECRET"] = ""
        self.tmpdir = tempfile.mkdtemp()          # own dir so backups stay isolated
        self.db_path = os.path.join(self.tmpdir, "ledger.db")
        db.DB_PATH = self.db_path                 # get_db() reads this at call time
        with flask_app.app_context():
            db.init_db()                          # creates schema + seeds DC(1)/NYC(2)
        self.client = flask_app.test_client()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)


class NetSalesBasis(Base):
    def test_strips_tax_tip_and_service(self):
        order = {"net_amounts": {
            "total_money": {"amount": 1180},
            "tax_money": {"amount": 100},
            "tip_money": {"amount": 80},
            "service_charge_money": {"amount": 0},
        }}
        self.assertEqual(square_client._net_sales_cents(order), 1000)

    def test_no_net_amounts_strips_order_level_totals(self):
        # Square Invoice-sourced orders can arrive with no net_amounts; the
        # order-level fields carry the total_ prefix and must still be stripped.
        order = {"total_money": {"amount": 1330},
                 "total_tax_money": {"amount": 130},
                 "total_tip_money": {"amount": 200}}
        self.assertEqual(square_client._net_sales_cents(order), 1000)

    def test_bare_total_money_has_nothing_to_strip(self):
        self.assertEqual(
            square_client._net_sales_cents({"total_money": {"amount": 500}}), 500)


class CacheZeroOverwrite(Base):
    def _configure_square(self):
        db.set_setting("square_token", "x")   # token is global; the location id is per-store now
        get_db().execute("UPDATE locations SET square_location_id='LOC1' WHERE id=?",
                         (db.active_location_id(),))
        get_db().commit()

    def _seed_today(self, value):
        ds = dt.date.today().isoformat()
        get_db().execute(
            "INSERT INTO daily_sales(square_location_id, date, net_sales, fetched_at) "
            "VALUES('LOC1', ?, ?, datetime('now'))", (ds, value))
        get_db().commit()
        return ds

    def test_failed_fetch_does_not_zero_cache(self):
        today = dt.date.today()
        with flask_app.app_context():
            self._configure_square()
            ds = self._seed_today(500.0)
            orig = square_client.get_daily_sales
            square_client.get_daily_sales = lambda s, e: None   # simulate Square error
            try:
                out = square_client.daily_sales_cached(today, today)
            finally:
                square_client.get_daily_sales = orig
            row = get_db().execute(
                "SELECT net_sales FROM daily_sales WHERE square_location_id='LOC1' AND date=?",
                (ds,)).fetchone()
        self.assertEqual(row["net_sales"], 500.0)   # untouched
        self.assertEqual(out.get(ds), 500.0)        # served from cache

    def test_successful_fetch_updates_cache(self):
        today = dt.date.today()
        ds = today.isoformat()
        with flask_app.app_context():
            self._configure_square()
            self._seed_today(500.0)
            orig = square_client.get_daily_sales
            square_client.get_daily_sales = lambda s, e: {ds: 250.0}
            try:
                out = square_client.daily_sales_cached(today, today)
            finally:
                square_client.get_daily_sales = orig
            row = get_db().execute(
                "SELECT net_sales FROM daily_sales WHERE square_location_id='LOC1' AND date=?",
                (ds,)).fetchone()
        self.assertEqual(row["net_sales"], 250.0)
        self.assertEqual(out.get(ds), 250.0)


class DuplicateInvoiceGuard(Base):
    PAYLOAD = {"vendor": "Acme Liquor", "invoice_number": "INV-1",
               "invoice_date": "2026-06-01", "total": 100.0, "line_items": []}

    def test_second_identical_invoice_is_flagged(self):
        r1 = self.client.post("/api/invoices", json=self.PAYLOAD)
        self.assertEqual(r1.status_code, 200)

        r2 = self.client.post("/api/invoices", json=self.PAYLOAD)
        self.assertEqual(r2.status_code, 409)
        self.assertEqual(r2.get_json()["error"], "duplicate")
        self.assertEqual(r2.get_json()["duplicate"]["invoice_number"], "INV-1")

        r3 = self.client.post("/api/invoices", json={**self.PAYLOAD, "confirm_duplicate": True})
        self.assertEqual(r3.status_code, 200)

    def test_different_invoice_not_flagged(self):
        self.client.post("/api/invoices", json=self.PAYLOAD)
        r = self.client.post("/api/invoices",
                             json={**self.PAYLOAD, "invoice_number": "INV-2", "total": 42.0})
        self.assertEqual(r.status_code, 200)

    def test_no_vendor_no_false_positive(self):
        blank = {"vendor": "", "invoice_number": "", "invoice_date": "", "total": None,
                 "line_items": []}
        self.assertEqual(self.client.post("/api/invoices", json=blank).status_code, 200)
        self.assertEqual(self.client.post("/api/invoices", json=blank).status_code, 200)


class LocationScoping(Base):
    def _make_invoice(self, location_id):
        with flask_app.app_context():
            cur = get_db().execute(
                "INSERT INTO invoices(location_id, vendor, invoice_date, total) "
                "VALUES(?, 'V', '2026-06-01', 100.0)", (location_id,))
            get_db().commit()
            return cur.lastrowid

    def test_get_foreign_location_invoice_404(self):
        # Active location defaults to 1 (DC); make an invoice in 2 (NYC).
        nyc = self._make_invoice(2)
        self.assertEqual(self.client.get(f"/api/invoices/{nyc}").status_code, 404)
        # Switching to NYC makes it visible.
        self.client.put("/api/active-location", json={"location_id": 2})
        self.assertEqual(self.client.get(f"/api/invoices/{nyc}").status_code, 200)

    def test_delete_foreign_location_invoice_404_and_survives(self):
        nyc = self._make_invoice(2)
        self.assertEqual(self.client.delete(f"/api/invoices/{nyc}").status_code, 404)
        with flask_app.app_context():
            still = get_db().execute("SELECT 1 FROM invoices WHERE id=?", (nyc,)).fetchone()
        self.assertIsNotNone(still)

    def test_update_foreign_location_product_404_and_unchanged(self):
        with flask_app.app_context():
            cur = get_db().execute(
                "INSERT INTO inventory_items(location_id, name, unit_cost) VALUES(2, 'Gin', 20.0)")
            get_db().commit()
            pid = cur.lastrowid
        r = self.client.put(f"/api/products/{pid}", json={"unit_cost": 999.0})
        self.assertEqual(r.status_code, 404)
        with flask_app.app_context():
            cost = get_db().execute(
                "SELECT unit_cost FROM inventory_items WHERE id=?", (pid,)).fetchone()["unit_cost"]
        self.assertEqual(cost, 20.0)

    def test_accept_new_item_foreign_location_404(self):
        with flask_app.app_context():
            cur = get_db().execute(
                "INSERT INTO vendor_items(location_id, vendor_name, vendor_item_name, status) "
                "VALUES(2, 'V', 'Gin 750', 'new')")
            get_db().commit()
            vid = cur.lastrowid
        r = self.client.post(f"/api/products/new-items/{vid}/accept", json={"category_id": 1})
        self.assertEqual(r.status_code, 404)
        with flask_app.app_context():
            status = get_db().execute(
                "SELECT status FROM vendor_items WHERE id=?", (vid,)).fetchone()["status"]
        self.assertEqual(status, "new")   # untouched

    def test_count_save_ignores_foreign_location_items(self):
        with flask_app.app_context():
            gin = get_db().execute(
                "INSERT INTO inventory_items(location_id, name, unit_cost) VALUES(1,'Gin',10.0)").lastrowid
            rum = get_db().execute(
                "INSERT INTO inventory_items(location_id, name, unit_cost) VALUES(2,'Rum',99.0)").lastrowid
            get_db().commit()
        r = self.client.post("/api/counts",
                             json={"lines": [{"item_id": gin, "qty": 3}, {"item_id": rum, "qty": 5}]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["value"], 30.0)   # only the in-store item: 3 * 10
        with flask_app.app_context():
            gin_lc = get_db().execute(
                "SELECT last_count FROM inventory_items WHERE id=?", (gin,)).fetchone()["last_count"]
            rum_lc = get_db().execute(
                "SELECT last_count FROM inventory_items WHERE id=?", (rum,)).fetchone()["last_count"]
        self.assertEqual(gin_lc, 3.0)
        self.assertIn(rum_lc, (None, 0, 0.0))           # foreign item untouched

    def test_update_active_location_product_ok(self):
        with flask_app.app_context():
            cur = get_db().execute(
                "INSERT INTO inventory_items(location_id, name, unit_cost) VALUES(1, 'Gin', 20.0)")
            get_db().commit()
            pid = cur.lastrowid
        r = self.client.put(f"/api/products/{pid}", json={"unit_cost": 25.0})
        self.assertEqual(r.status_code, 200)
        with flask_app.app_context():
            cost = get_db().execute(
                "SELECT unit_cost FROM inventory_items WHERE id=?", (pid,)).fetchone()["unit_cost"]
        self.assertEqual(cost, 25.0)


class Reconcile(unittest.TestCase):
    def test_lines_match_subtotal(self):
        r = app_module._reconcile(100.0, 8.0, 108.0, [{"total": 60.0}, {"total": 40.0}])
        self.assertTrue(r["ok"])
        self.assertEqual(r["line_sum"], 100.0)
        self.assertEqual(r["expected"], 100.0)

    def test_lines_mismatch_flagged(self):
        r = app_module._reconcile(100.0, 8.0, 108.0, [{"total": 60.0}])
        self.assertFalse(r["ok"])
        self.assertEqual(r["delta"], -40.0)

    def test_no_subtotal_uses_total_minus_tax(self):
        r = app_module._reconcile(None, 5.0, 105.0, [{"total": 100.0}])
        self.assertTrue(r["ok"])
        self.assertEqual(r["expected"], 100.0)

    def test_nothing_to_check_when_no_amounts(self):
        r = app_module._reconcile(None, None, None, [{"total": 50.0}])
        self.assertIsNone(r["ok"])

    def test_tax_inclusive_lines_ok(self):
        # Lines that sum to the grand total (tax-inclusive convention) reconcile.
        r = app_module._reconcile(100.0, 8.0, 108.0, [{"total": 108.0}])
        self.assertTrue(r["ok"])
        self.assertEqual(r["expected"], 108.0)

    def test_header_only_invoice_not_flagged(self):
        # A total with no itemization is "nothing to check", not an error.
        r = app_module._reconcile(None, 8.0, 108.0, [])
        self.assertIsNone(r["ok"])


class InvoiceEdit(Base):
    def _create(self):
        r = self.client.post("/api/invoices", json={
            "vendor": "Acme", "invoice_date": "2026-06-01", "invoice_number": "E-1",
            "subtotal": 100.0, "tax": 8.0, "total": 108.0,
            "line_items": [{"name": "Gin", "total": 100.0}]})
        self.assertEqual(r.status_code, 200)
        return r.get_json()["id"]

    def test_edit_updates_header_and_replaces_lines(self):
        iid = self._create()
        r = self.client.put(f"/api/invoices/{iid}", json={
            "vendor": "Acme Wine", "invoice_date": "2026-06-02", "invoice_number": "E-1",
            "subtotal": 50.0, "tax": 0.0, "total": 50.0,
            "line_items": [{"name": "Rye", "total": 30.0}, {"name": "Soda", "total": 20.0}]})
        self.assertEqual(r.status_code, 200)
        got = self.client.get(f"/api/invoices/{iid}").get_json()
        self.assertEqual(got["vendor"], "Acme Wine")
        self.assertEqual(got["total"], 50.0)
        self.assertEqual(sorted(li["name"] for li in got["line_items"]), ["Rye", "Soda"])
        self.assertTrue(got["reconciliation"]["ok"])

    def test_edit_foreign_location_404(self):
        with flask_app.app_context():
            cur = get_db().execute(
                "INSERT INTO invoices(location_id, vendor, total) VALUES(2, 'NYC', 10.0)")
            get_db().commit()
            fid = cur.lastrowid
        r = self.client.put(f"/api/invoices/{fid}", json={"vendor": "x", "line_items": []})
        self.assertEqual(r.status_code, 404)
        with flask_app.app_context():
            v = get_db().execute("SELECT vendor FROM invoices WHERE id=?", (fid,)).fetchone()["vendor"]
        self.assertEqual(v, "NYC")   # untouched

    def test_editing_old_invoice_does_not_roll_back_latest_price(self):
        self.client.post("/api/invoices", json={
            "vendor": "Acme", "invoice_date": "2026-06-01", "invoice_number": "A",
            "total": 20.0, "line_items": [{"name": "Vodka", "unit_cost": 20.0, "total": 20.0}]})
        self.client.post("/api/invoices", json={
            "vendor": "Acme", "invoice_date": "2026-06-10", "invoice_number": "B",
            "total": 22.0, "line_items": [{"name": "Vodka", "unit_cost": 22.0, "total": 22.0}]})

        def vodka_price():
            with flask_app.app_context():
                return get_db().execute(
                    "SELECT last_purchase_price FROM vendor_items "
                    "WHERE lower(vendor_item_name)='vodka'").fetchone()["last_purchase_price"]

        self.assertEqual(vodka_price(), 22.0)   # newest delivery wins
        with flask_app.app_context():
            old_id = get_db().execute(
                "SELECT id FROM invoices WHERE invoice_number='A'").fetchone()["id"]
        # Editing the OLD invoice must not roll the latest price back to 20.
        self.client.put(f"/api/invoices/{old_id}", json={
            "vendor": "Acme", "invoice_date": "2026-06-01", "invoice_number": "A2",
            "total": 20.0, "line_items": [{"name": "Vodka", "unit_cost": 20.0, "total": 20.0}]})
        self.assertEqual(vodka_price(), 22.0)   # unchanged

    def test_detail_flags_reconciliation_mismatch(self):
        iid = self._create()
        self.client.put(f"/api/invoices/{iid}", json={
            "vendor": "Acme", "subtotal": 100.0, "tax": 0.0, "total": 100.0,
            "line_items": [{"name": "Gin", "total": 60.0}]})
        recon = self.client.get(f"/api/invoices/{iid}").get_json()["reconciliation"]
        self.assertFalse(recon["ok"])
        self.assertEqual(recon["delta"], -40.0)


class Backups(Base):
    def test_backup_creates_valid_snapshot_and_prunes(self):
        with flask_app.app_context():
            db.backup(keep=2)
            db.backup(keep=2)
            last = db.backup(keep=2)
        self.assertTrue(last and os.path.exists(last))
        files = sorted(glob.glob(os.path.join(self.tmpdir, "backups", "ledger-*.db")))
        self.assertEqual(len(files), 2)          # pruned to keep=2
        con = sqlite3.connect(files[-1])         # snapshot is a usable DB
        try:
            n = con.execute("SELECT COUNT(*) FROM locations").fetchone()[0]
        finally:
            con.close()
        self.assertGreaterEqual(n, 2)            # seeded DC + NYC carried over


class AppSecret(Base):
    def test_persisted_random_secret_when_env_blank(self):
        os.environ["APP_SECRET"] = ""
        with flask_app.app_context():
            s1 = app_module._app_secret()
            s2 = app_module._app_secret()
        self.assertEqual(s1, s2)                  # stable across calls (persisted)
        self.assertEqual(len(s1), 64)            # secrets.token_hex(32)
        self.assertNotEqual(s1, "barkeep-secret")  # not the old shipped default

    def test_env_override_wins(self):
        os.environ["APP_SECRET"] = "explicit-secret"
        try:
            with flask_app.app_context():
                self.assertEqual(app_module._app_secret(), "explicit-secret")
        finally:
            os.environ["APP_SECRET"] = ""


class LoginRateLimit(Base):
    def setUp(self):
        super().setUp()
        os.environ["APP_PASSWORD"] = "secret"
        os.environ["APP_SECRET"] = "testsecret"
        app_module._LOGIN_FAILS.clear()

    def tearDown(self):
        os.environ["APP_PASSWORD"] = ""
        os.environ["APP_SECRET"] = ""
        app_module._LOGIN_FAILS.clear()
        super().tearDown()

    def test_lockout_after_max_failures(self):
        for _ in range(app_module._LOGIN_MAX):
            self.assertEqual(
                self.client.post("/api/login", json={"password": "nope"}).status_code, 401)
        # Even the RIGHT passcode is refused once locked out.
        r = self.client.post("/api/login", json={"password": "secret"})
        self.assertEqual(r.status_code, 429)

    def test_success_clears_failures(self):
        for _ in range(app_module._LOGIN_MAX - 1):
            self.client.post("/api/login", json={"password": "nope"})
        r = self.client.post("/api/login", json={"password": "secret"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("token", r.get_json())
        # The counter reset, so a later wrong attempt is a plain 401, not a lockout.
        self.assertEqual(
            self.client.post("/api/login", json={"password": "nope"}).status_code, 401)

    def test_spoofed_forwarded_header_on_direct_path_is_ignored(self):
        # Direct LAN connection (real remote_addr): a rotated CF-Connecting-IP
        # must NOT create fresh buckets, so the per-IP lockout still trips.
        for i in range(app_module._LOGIN_MAX):
            r = self.client.post("/api/login", json={"password": "nope"},
                                 headers={"CF-Connecting-IP": f"1.2.3.{i}"},
                                 environ_base={"REMOTE_ADDR": "10.0.0.5"})
            self.assertEqual(r.status_code, 401)
        r = self.client.post("/api/login", json={"password": "secret"},
                             headers={"CF-Connecting-IP": "9.9.9.9"},
                             environ_base={"REMOTE_ADDR": "10.0.0.5"})
        self.assertEqual(r.status_code, 429)

    def test_global_cap_catches_distributed_spray(self):
        # Through the tunnel (loopback remote_addr) each distinct CF-Connecting-IP
        # is its own bucket, so per-IP never trips — the global cap still bounds it.
        for i in range(app_module._LOGIN_GLOBAL_MAX):
            self.client.post("/api/login", json={"password": "nope"},
                             headers={"CF-Connecting-IP": f"203.0.113.{i}"})
        r = self.client.post("/api/login", json={"password": "secret"},
                             headers={"CF-Connecting-IP": "203.0.113.250"})
        self.assertEqual(r.status_code, 429)


class UploadCap(Base):
    def test_oversized_request_returns_413_json(self):
        old = flask_app.config["MAX_CONTENT_LENGTH"]
        flask_app.config["MAX_CONTENT_LENGTH"] = 1000
        try:
            r = self.client.post("/api/settings", data=b"x" * 2000,
                                  content_type="application/json")
            self.assertEqual(r.status_code, 413)
            self.assertIn("too large", r.get_json()["error"])
        finally:
            flask_app.config["MAX_CONTENT_LENGTH"] = old


class UsageCogs(Base):
    def _count(self, date_str, value):
        with flask_app.app_context():
            get_db().execute("INSERT INTO counts(location_id, taken_at, value) VALUES(1, ?, ?)",
                             (f"{date_str} 12:00:00", value))
            get_db().commit()

    def _invoice(self, date_str, total):
        with flask_app.app_context():
            get_db().execute(
                "INSERT INTO invoices(location_id, vendor, invoice_date, total) VALUES(1,'V',?,?)",
                (date_str, total))
            get_db().commit()

    def test_usage_cogs_uses_count_interval_not_requested_range(self):
        self._count("2026-05-30", 1000.0)   # opening (just before the period)
        self._count("2026-07-02", 800.0)    # closing (just after the period)
        self._invoice("2026-05-20", 999.0)  # before interval  -> excluded
        self._invoice("2026-05-30", 999.0)  # ON opening date  -> excluded (exclusive lower bound)
        self._invoice("2026-06-15", 500.0)  # in interval
        self._invoice("2026-07-02", 200.0)  # ON closing date  -> included
        self._invoice("2026-07-05", 999.0)  # after interval   -> excluded
        with flask_app.app_context():
            s = cogs.summary(dt.date(2026, 6, 1), dt.date(2026, 6, 30))
        self.assertEqual(s["cogs_method"], "usage")
        self.assertEqual(s["usage_period"],
                         {"start": "2026-05-30", "end": "2026-07-02", "purchases": 700.0})
        self.assertEqual(s["cogs"], 900.0)  # 1000 + 700 - 800

    def test_far_counts_fall_back_to_purchases(self):
        self._count("2026-01-01", 1000.0)   # outside the 14-day grace window
        self._count("2026-12-31", 800.0)
        self._invoice("2026-06-15", 500.0)
        with flask_app.app_context():
            s = cogs.summary(dt.date(2026, 6, 1), dt.date(2026, 6, 30))
        self.assertEqual(s["cogs_method"], "purchases")
        self.assertIsNone(s["usage_period"])
        self.assertEqual(s["cogs"], 500.0)


class PriceMovers(Base):
    def _vi(self, vendor, name):
        with flask_app.app_context():
            cur = get_db().execute(
                "INSERT INTO vendor_items(location_id, vendor_name, vendor_item_name, status) "
                "VALUES(1, ?, ?, 'reviewed')", (vendor, name))
            get_db().commit()
            return cur.lastrowid

    def _line(self, date_str, vendor, name, price, qty, vi=None):
        # Real model: the line name IS the vendor-item name. vi links it (or None).
        with flask_app.app_context():
            d = get_db()
            inv = d.execute(
                "INSERT INTO invoices(location_id, vendor, invoice_date, total) VALUES(1,?,?,?)",
                (vendor, date_str, price * qty)).lastrowid
            d.execute(
                "INSERT INTO invoice_items(invoice_id, name, unit_cost, qty, vendor_item_id) "
                "VALUES(?,?,?,?,?)", (inv, name, price, qty, vi))
            d.commit()

    def _movers(self):
        with flask_app.app_context():
            return reports.price_movers(dt.date(2026, 6, 1), dt.date(2026, 6, 30))["movers"]

    def test_pack_sizes_do_not_merge(self):
        # Different packs are distinct vendor-item names -> distinct keys.
        self._line("2026-05-15", "Acme", "Tito 750ml", 20.0, 3)
        self._line("2026-05-15", "Acme", "Tito 1.75L", 40.0, 2)
        self._line("2026-06-10", "Acme", "Tito 750ml", 25.0, 4)   # moved 20 -> 25
        self._line("2026-06-10", "Acme", "Tito 1.75L", 40.0, 1)   # unchanged
        movers = self._movers()
        self.assertEqual(len(movers), 1)
        m = movers[0]
        self.assertEqual((m["old_price"], m["new_price"], m["qty"], m["impact"]), (20.0, 25.0, 4.0, 20.0))

    def test_move_survives_vendor_item_wipe(self):
        # The importer wipes vendor_items (nulling vendor_item_id) then new lines get
        # fresh ids; the (vendor, name) identity must still tie prior to current.
        self._line("2026-05-15", "Acme", "Tito 750ml", 20.0, 3, vi=None)        # prior, unlinked
        vi = self._vi("Acme", "Tito 750ml")
        self._line("2026-06-10", "Acme", "Tito 750ml", 25.0, 4, vi=vi)          # window, linked
        movers = self._movers()
        self.assertEqual(len(movers), 1)
        self.assertEqual((movers[0]["old_price"], movers[0]["new_price"]), (20.0, 25.0))

    def test_blank_vendor_item_name_falls_back_to_invoice_line(self):
        # A vendor_item created with a blank vendor must still unify with an
        # unlinked prior line for the same product (NULLIF -> invoice vendor).
        self._line("2026-05-15", "Acme", "Tito 750ml", 20.0, 3, vi=None)
        vi = self._vi("", "Tito 750ml")                       # blank vendor_name
        self._line("2026-06-10", "Acme", "Tito 750ml", 25.0, 4, vi=vi)
        movers = self._movers()
        self.assertEqual(len(movers), 1)
        self.assertEqual((movers[0]["old_price"], movers[0]["new_price"]), (20.0, 25.0))

    def test_same_name_different_vendor_not_merged(self):
        self._line("2026-05-15", "Acme", "Vodka", 20.0, 2)
        self._line("2026-05-15", "Beta", "Vodka", 30.0, 2)
        self._line("2026-06-10", "Acme", "Vodka", 25.0, 3)   # Acme moved
        self._line("2026-06-10", "Beta", "Vodka", 30.0, 3)   # Beta unchanged
        movers = self._movers()
        self.assertEqual(len(movers), 1)                     # not a cross-vendor merge
        self.assertEqual(movers[0]["name"], "Vodka")
        self.assertEqual((movers[0]["old_price"], movers[0]["new_price"]), (20.0, 25.0))


class LocationHeader(Base):
    """#6 — the active store is resolved per request from the X-Location-Id header,
    not a shared mutable global, so concurrent devices don't cross-contaminate."""

    def test_header_overrides_persisted_default(self):
        with flask_app.app_context():
            db.set_setting("active_location_id", "1")
            nyc = get_db().execute(
                "INSERT INTO invoices(location_id, vendor, total) VALUES(2,'N',5.0)").lastrowid
            get_db().commit()
        # Default store is 1 -> the NYC invoice is invisible...
        self.assertEqual(self.client.get(f"/api/invoices/{nyc}").status_code, 404)
        # ...until this request declares store 2 via the header.
        self.assertEqual(
            self.client.get(f"/api/invoices/{nyc}", headers={"X-Location-Id": "2"}).status_code, 200)

    def test_invalid_header_falls_back_to_default(self):
        with flask_app.app_context():
            db.set_setting("active_location_id", "1")
        for bad in ("999", "abc", ""):
            r = self.client.get("/api/active-location", headers={"X-Location-Id": bad})
            self.assertEqual(r.get_json()["active"], 1)

    def test_square_location_resolves_per_request(self):
        c1 = self.client.get("/api/config", headers={"X-Location-Id": "1"}).get_json()
        c2 = self.client.get("/api/config", headers={"X-Location-Id": "2"}).get_json()
        self.assertEqual(c1["square_location_id"], "LNKNR2A7MBB4K")   # seeded DC
        self.assertEqual(c2["square_location_id"], "LS1WRASW8V02R")   # seeded NYC

    def test_settings_writes_square_id_to_active_store_only(self):
        self.client.post("/api/settings", json={"square_location_id": "NEWNYC"},
                         headers={"X-Location-Id": "2"})
        with flask_app.app_context():
            r1 = get_db().execute("SELECT square_location_id FROM locations WHERE id=1").fetchone()[0]
            r2 = get_db().execute("SELECT square_location_id FROM locations WHERE id=2").fetchone()[0]
        self.assertEqual(r2, "NEWNYC")            # active store updated
        self.assertEqual(r1, "LNKNR2A7MBB4K")     # other store untouched (no global mirror)


class TippedLaborWage(Base):
    """Shifts Square records with no wage (typical for tipped staff) must not
    silently count as $0 labor: they're billed at default_hourly_wage when set
    and always reported back as unwaged so Labor% stays trustworthy."""

    def _shift(self, hours, amount_cents=None):
        start = dt.datetime(2026, 6, 1, 18, 0, 0, tzinfo=dt.timezone.utc)
        end = start + dt.timedelta(hours=hours)
        s = {"start_at": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
             "end_at": end.strftime("%Y-%m-%dT%H:%M:%SZ")}
        if amount_cents is not None:
            s["wage"] = {"hourly_rate": {"amount": amount_cents, "currency": "USD"}}
        return s

    def test_recorded_wage_priced_and_not_unwaged(self):
        cost, hours, unwaged = square_client._shift_cost(self._shift(4, 1500), 0.0)
        self.assertEqual((cost, hours, unwaged), (60.0, 4.0, 0.0))

    def test_missing_wage_no_fallback_is_unwaged(self):
        cost, hours, unwaged = square_client._shift_cost(self._shift(5), 0.0)
        self.assertEqual((cost, hours, unwaged), (0.0, 5.0, 5.0))

    def test_missing_wage_uses_fallback(self):
        cost, hours, unwaged = square_client._shift_cost(self._shift(5), 12.0)
        self.assertEqual((cost, hours, unwaged), (60.0, 5.0, 5.0))

    def test_zero_amount_treated_as_unwaged(self):
        cost, hours, unwaged = square_client._shift_cost(self._shift(3, 0), 10.0)
        self.assertEqual((cost, hours, unwaged), (30.0, 3.0, 3.0))

    def test_default_wage_setting_read(self):
        with flask_app.app_context():
            db.set_setting("default_hourly_wage", "15.50")
            self.assertEqual(square_client._default_wage(), 15.5)
            db.set_setting("default_hourly_wage", "")        # blank => off
            self.assertEqual(square_client._default_wage(), 0.0)
            db.set_setting("default_hourly_wage", "junk")    # unparseable => off
            self.assertEqual(square_client._default_wage(), 0.0)

    def test_default_wage_exposed_in_settings_api(self):
        with flask_app.app_context():
            db.set_setting("default_hourly_wage", "13")
        cfg = self.client.get("/api/config").get_json()
        self.assertEqual(cfg["default_hourly_wage"], "13")
        self.client.post("/api/settings", json={"default_hourly_wage": "14.25"})
        with flask_app.app_context():
            self.assertEqual(db.get_setting("default_hourly_wage"), "14.25")

    def _run_get_labor(self, shifts):
        """get_labor over a stubbed Square response (DC store is seeded with a
        square_location_id; setting a token makes is_configured() true)."""
        from unittest import mock

        class FakeResp:
            def raise_for_status(self): pass
            def json(self): return {"shifts": shifts, "cursor": None}

        with flask_app.app_context():
            db.set_setting("square_token", "tok")
            with mock.patch.object(square_client.requests, "post",
                                   return_value=FakeResp()):
                return square_client.get_labor(dt.date(2026, 6, 1), dt.date(2026, 6, 1))

    def test_get_labor_no_fallback_warns_and_understates_visibly(self):
        info = self._run_get_labor([self._shift(4, 1500), self._shift(5)])
        self.assertEqual(info["labor"], 60.0)           # only the waged shift priced
        self.assertEqual(info["hours"], 9.0)
        self.assertEqual(info["unwaged_hours"], 5.0)
        self.assertEqual(info["unwaged_shifts"], 1)
        self.assertIn("$0 labor", info["warning"])

    def test_get_labor_fallback_prices_unwaged_and_still_discloses(self):
        with flask_app.app_context():
            db.set_setting("default_hourly_wage", "10")
        info = self._run_get_labor([self._shift(4, 1500), self._shift(5)])
        self.assertEqual(info["labor"], 110.0)          # 60 waged + 5h * $10 fallback
        self.assertEqual(info["unwaged_shifts"], 1)
        self.assertIn("estimated", info["warning"])

    def test_get_labor_all_waged_has_no_warning(self):
        info = self._run_get_labor([self._shift(4, 1500)])
        self.assertEqual(info["unwaged_shifts"], 0)
        self.assertIsNone(info["warning"])

    def test_summary_payload_carries_labor_warning_keys(self):
        with flask_app.app_context():
            self.assertIn("labor_warning", cogs.summary(dt.date(2026, 6, 1),
                                                        dt.date(2026, 6, 1)))
            r = reports.controllable_pl(dt.date(2026, 6, 1), dt.date(2026, 6, 1))
            self.assertIn("labor_warning", r)
            self.assertIn("unwaged_hours", r)


class BusinessToday(Base):
    """Default date ranges resolve via the business day in the configured tz, so
    they line up with how sales/labor are bucketed regardless of server tz."""

    def test_after_midnight_before_5am_is_prior_day(self):
        # 2026-06-12 03:00 America/New_York == 07:00 UTC; still 06-11's bar night.
        now = dt.datetime(2026, 6, 12, 7, 0, tzinfo=dt.timezone.utc)
        with flask_app.app_context():
            self.assertEqual(square_client.business_today(now), dt.date(2026, 6, 11))

    def test_after_5am_is_same_day(self):
        # 2026-06-12 06:00 ET == 10:00 UTC; the new business day has rolled over.
        now = dt.datetime(2026, 6, 12, 10, 0, tzinfo=dt.timezone.utc)
        with flask_app.app_context():
            self.assertEqual(square_client.business_today(now), dt.date(2026, 6, 12))

    def test_independent_of_server_local_date(self):
        # 23:30 ET on 06-12 is 03:30 UTC on 06-13: a UTC server's date.today()
        # would say 06-13, but the bar night is still 06-12.
        now = dt.datetime(2026, 6, 13, 3, 30, tzinfo=dt.timezone.utc)
        with flask_app.app_context():
            self.assertEqual(square_client.business_today(now), dt.date(2026, 6, 12))


class MoneyHelpers(unittest.TestCase):
    """money.py routes settled amounts through integer cents so sums and
    comparisons are exact and writes are clean to the penny."""

    def test_to_cents_and_back(self):
        self.assertEqual(money.to_cents("42.50"), 4250)
        self.assertEqual(money.to_cents(None), 0)
        self.assertEqual(money.to_cents("", default=0), 0)
        self.assertEqual(money.to_cents("junk", default=0), 0)
        self.assertEqual(money.to_dollars(4250), 42.5)
        # The classic float trap, made exact.
        self.assertEqual(money.to_cents(0.1) + money.to_cents(0.2), money.to_cents(0.3))

    def test_cents_or_none_keeps_zero_distinct(self):
        self.assertIsNone(money.cents_or_none(None))
        self.assertIsNone(money.cents_or_none(""))
        self.assertIsNone(money.cents_or_none("abc"))
        self.assertEqual(money.cents_or_none(0), 0)       # $0.00 is a real amount

    def test_normalize_cleans_noise_and_preserves_none(self):
        self.assertIsNone(money.normalize(None))
        self.assertIsNone(money.normalize(""))
        self.assertEqual(money.normalize(0.1 + 0.2), 0.3)  # 0.30000000000000004 -> 0.3
        self.assertEqual(money.normalize(12.344), 12.34)
        self.assertEqual(money.normalize(12.346), 12.35)

    def test_half_cent_uses_bankers_rounding(self):
        # 0.125 is exactly representable, so this is deterministic: round() is
        # half-even (consistent with every other round(x, 2) in the app), so
        # 0.125 -> 0.12 not 0.13. Pinned so a switch to half-up is a conscious choice.
        self.assertEqual(money.normalize(0.125), 0.12)
        self.assertEqual(money.to_cents(0.125), 12)

    def test_sum_dollars_is_exact(self):
        self.assertNotEqual(sum([0.1, 0.2]), 0.3)          # the naive way drifts
        self.assertEqual(money.sum_dollars([0.1, 0.2]), 0.3)
        self.assertEqual(money.sum_dollars([0.10] * 10), 1.0)
        self.assertEqual(money.sum_dollars([None, "5.00", 2.5]), 7.5)

    def test_same_money(self):
        self.assertTrue(money.same_money(10.00, 10.004))   # same to the penny
        self.assertFalse(money.same_money(10.00, 10.01))
        self.assertTrue(money.same_money(10.00, 10.01, tol_cents=1))


class ReconcileExact(unittest.TestCase):
    """_reconcile compares in integer cents, so many small lines reconcile
    exactly instead of relying on a final float round to hide drift."""

    def test_many_small_lines_reconcile_exactly(self):
        r = app_module._reconcile(1.00, None, 1.00, [{"total": 0.10}] * 10)
        self.assertTrue(r["ok"])
        self.assertEqual(r["line_sum"], 1.0)
        self.assertEqual(r["delta"], 0.0)

    def test_real_gap_still_flagged(self):
        r = app_module._reconcile(1.00, None, 1.00, [{"total": 0.10}] * 9)  # $0.90
        self.assertFalse(r["ok"])
        self.assertEqual(r["delta"], -0.1)

    def test_tolerance_boundary_at_exact_dollar(self):
        # expected $5.00 -> 0.5% = 2.5c; a 2c gap reconciles, 3c does not. (This
        # is the half-even edge that bit the old round()-based tolerance.)
        self.assertTrue(app_module._reconcile(5.00, None, 5.00, [{"total": 5.02}])["ok"])
        self.assertFalse(app_module._reconcile(5.00, None, 5.00, [{"total": 5.03}])["ok"])

    def test_tax_exclusive_and_inclusive_both_reconcile(self):
        excl = app_module._reconcile(100.0, 8.0, 108.0, [{"total": 100.0}])
        self.assertTrue(excl["ok"])
        self.assertEqual(excl["expected"], 100.0)
        incl = app_module._reconcile(100.0, 8.0, 108.0, [{"total": 108.0}])
        self.assertTrue(incl["ok"])
        self.assertEqual(incl["expected"], 108.0)


class DuplicateCentPrecision(Base):
    """The no-invoice-number fallback dup check matches to the exact penny."""

    def _post(self, total):
        return self.client.post("/api/invoices", json={
            "vendor": "Acme", "invoice_number": "", "invoice_date": "2026-06-01",
            "total": total, "line_items": []})

    def test_same_penny_is_duplicate(self):
        self.assertEqual(self._post(10.00).status_code, 200)
        self.assertEqual(self._post(10.004).status_code, 409)   # rounds to 10.00

    def test_off_by_a_penny_is_not_duplicate(self):
        self.assertEqual(self._post(10.00).status_code, 200)
        self.assertEqual(self._post(10.01).status_code, 200)


class MoneyWriteNormalization(Base):
    """Settled amounts land in the DB clean to the penny, so they sum exactly."""

    def test_invoice_total_stored_to_the_penny_and_sums_exactly(self):
        for _ in range(10):
            self.client.post("/api/invoices", json={
                "vendor": "Drip", "invoice_number": "", "invoice_date": "2026-06-02",
                "total": 0.10, "line_items": [], "confirm_duplicate": True})
        with flask_app.app_context():
            stored = [r["total"] for r in get_db().execute(
                "SELECT total FROM invoices WHERE lower(vendor)='drip'")]
        self.assertEqual(len(stored), 10)
        self.assertTrue(all(t == 0.1 for t in stored))      # clean, not 0.099999…
        self.assertEqual(money.sum_dollars(stored), 1.0)

    def test_line_total_normalized_on_save(self):
        r = self.client.post("/api/invoices", json={
            "vendor": "Drip", "invoice_number": "L1", "invoice_date": "2026-06-03",
            "total": 0.30, "line_items": [{"name": "x", "total": 0.1 + 0.2}]})
        self.assertEqual(r.status_code, 200)
        with flask_app.app_context():
            t = get_db().execute(
                "SELECT total FROM invoice_items WHERE name='x'").fetchone()["total"]
        self.assertEqual(t, 0.3)                            # 0.30000000000000004 cleaned


class PriceAlerts(Base):
    """Proactive price-increase alerts: the latest price jumped >= threshold over
    the prior price, and that purchase is recent. Built on the same stable
    (vendor, item) SKU keying as price_movers."""

    def _d(self, days_ago):
        with flask_app.app_context():
            return (square_client.business_today() - dt.timedelta(days=days_ago)).isoformat()

    def _inv(self, vendor, date, lines):
        """lines: [(name, unit_cost, qty)] at location 1 (the test default)."""
        with flask_app.app_context():
            d = get_db()
            iid = d.execute(
                "INSERT INTO invoices(location_id, vendor, invoice_date, status) "
                "VALUES(1, ?, ?, 'closed')", (vendor, date)).lastrowid
            for name, price, qty in lines:
                d.execute("INSERT INTO invoice_items(invoice_id, name, unit_cost, qty, total) "
                          "VALUES(?,?,?,?,?)", (iid, name, price, qty, (price or 0) * (qty or 0)))
            d.commit()

    def _alerts(self, **kw):
        with flask_app.app_context():
            return reports.price_alerts(**kw)

    def test_increase_above_threshold_alerts_with_impact(self):
        self._inv("Acme", self._d(10), [("Gin", 20.00, 2)])
        self._inv("Acme", self._d(2), [("Gin", 24.00, 3)])      # +20%
        res = self._alerts(min_pct=10)
        self.assertEqual(res["count"], 1)
        a = res["alerts"][0]
        self.assertEqual((a["old_price"], a["new_price"], a["change_pct"]), (20.0, 24.0, 20.0))
        self.assertEqual(a["qty"], 3.0)
        self.assertEqual(a["impact"], 12.0)                     # (24-20) * 3

    def test_increase_below_threshold_ignored(self):
        self._inv("Acme", self._d(10), [("Gin", 20.0, 1)])
        self._inv("Acme", self._d(2), [("Gin", 21.0, 1)])      # +5%
        self.assertEqual(self._alerts(min_pct=10)["count"], 0)

    def test_price_drop_not_alerted(self):
        self._inv("Acme", self._d(10), [("Gin", 24.0, 1)])
        self._inv("Acme", self._d(2), [("Gin", 20.0, 1)])
        self.assertEqual(self._alerts(min_pct=10)["count"], 0)

    def test_recent_jump_but_latest_purchase_stale_ignored(self):
        self._inv("Acme", self._d(80), [("Gin", 20.0, 1)])
        self._inv("Acme", self._d(60), [("Gin", 24.0, 1)])     # jump, but newest is 60d ago
        self.assertEqual(self._alerts(min_pct=10, lookback_days=30)["count"], 0)

    def test_same_item_different_vendor_not_merged(self):
        # Each vendor has only one purchase -> no prior price -> no false alert,
        # proving the (vendor, item) key doesn't merge a hike across vendors.
        self._inv("Acme", self._d(10), [("Gin", 20.0, 1)])
        self._inv("Beta", self._d(2), [("Gin", 24.0, 1)])
        self.assertEqual(self._alerts(min_pct=10)["count"], 0)

    def test_same_day_correction_is_not_a_price_change(self):
        # Two differently-priced lines for the same SKU on the SAME day (an
        # intra-day correction / split) must NOT look like a hike over time.
        self._inv("Acme", self._d(2), [("Gin", 20.00, 1), ("Gin", 24.00, 3)])
        self.assertEqual(self._alerts(min_pct=10)["count"], 0)

    def test_mixed_prices_on_latest_date_suppressed_as_ambiguous(self):
        # Latest date carries two different prices for the SKU -> the current
        # price is undeterminable, so we suppress rather than risk a wrong alert
        # (conservative: never alert on an ambiguous current price).
        self._inv("Acme", self._d(20), [("Gin", 20.00, 1)])
        self._inv("Acme", self._d(2), [("Gin", 24.00, 2), ("Gin", 20.00, 1)])
        self.assertEqual(self._alerts(min_pct=10)["count"], 0)

    def test_oscillation_back_to_baseline_not_alerted(self):
        # $20 -> $24 -> $20: newest is a return to baseline, not an increase.
        self._inv("Acme", self._d(20), [("Gin", 20.0, 1)])
        self._inv("Acme", self._d(10), [("Gin", 24.0, 1)])
        self._inv("Acme", self._d(2), [("Gin", 20.0, 1)])
        self.assertEqual(self._alerts(min_pct=10)["count"], 0)

    def test_endpoint_uses_configured_threshold(self):
        self._inv("Acme", self._d(10), [("Gin", 20.0, 1)])
        self._inv("Acme", self._d(2), [("Gin", 23.0, 1)])      # +15%
        with flask_app.app_context():
            db.set_setting("price_alert_pct", "20")
        self.assertEqual(self.client.get("/api/alerts/price-increases").get_json()["count"], 0)
        with flask_app.app_context():
            db.set_setting("price_alert_pct", "10")
        self.assertEqual(self.client.get("/api/alerts/price-increases").get_json()["count"], 1)

    def test_threshold_exposed_in_config(self):
        self.assertEqual(self.client.get("/api/config").get_json()["price_alert_pct"], "10")


class CsvExports(Base):
    """Bookkeeping CSV exports: location-scoped, date-filtered, properly quoted,
    and summed exactly."""

    def _inv(self, loc, vendor, date, num, lines):
        # lines: [(item, qty, unit, unit_cost, total, category_name)]
        with flask_app.app_context():
            d = get_db()
            iid = d.execute(
                "INSERT INTO invoices(location_id, vendor, invoice_date, invoice_number, status) "
                "VALUES(?,?,?,?, 'closed')", (loc, vendor, date, num)).lastrowid
            for item, qty, unit, uc, total, cat in lines:
                row = d.execute("SELECT id FROM categories WHERE name=?", (cat,)).fetchone()
                d.execute("INSERT INTO invoice_items(invoice_id, name, qty, unit, unit_cost, total, category_id) "
                          "VALUES(?,?,?,?,?,?,?)",
                          (iid, item, qty, unit, uc, total, row["id"] if row else None))
            d.commit()

    def _csv(self, path, loc=1):
        r = self.client.get(path, headers={"X-Location-Id": str(loc)})
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/csv", r.headers["Content-Type"])
        self.assertIn("attachment", r.headers["Content-Disposition"])
        return list(csv.reader(io.StringIO(r.get_data(as_text=True))))

    def test_purchases_csv_header_and_quoted_fields(self):
        self._inv(1, "Acme, Inc.", "2026-06-05", "INV9",
                  [("Gin", 2, "btl", 20.0, 40.0, "Beer Keg")])
        rows = self._csv("/api/export/purchases.csv?start=2026-06-01&end=2026-06-30")
        self.assertEqual(rows[0], ["Invoice Date", "Vendor", "Invoice #", "Status",
                                   "Category Type", "Category", "Item", "Qty", "Unit",
                                   "Unit Cost", "Total"])
        body = rows[1]
        self.assertEqual(body[1], "Acme, Inc.")      # comma stays one field (quoted)
        self.assertEqual(body[4], "Beer")            # category_type
        self.assertEqual(body[5], "Beer Keg")        # category
        self.assertEqual(body[6], "Gin")
        self.assertEqual(body[10], "40.0")

    def test_purchases_csv_scoped_by_range_and_location(self):
        self._inv(1, "InRange", "2026-06-05", "A", [("X", 1, "ea", 10.0, 10.0, "Wine")])
        self._inv(1, "OutOfRange", "2026-05-05", "B", [("Y", 1, "ea", 5.0, 5.0, "Wine")])
        self._inv(2, "OtherStore", "2026-06-06", "C", [("Z", 1, "ea", 9.0, 9.0, "Wine")])
        rows = self._csv("/api/export/purchases.csv?start=2026-06-01&end=2026-06-30", loc=1)
        vendors = [r[1] for r in rows[1:]]
        self.assertIn("InRange", vendors)
        self.assertNotIn("OutOfRange", vendors)      # before the range
        self.assertNotIn("OtherStore", vendors)      # different store

    def test_category_summary_sums_exactly_with_grand_total(self):
        self._inv(1, "V", "2026-06-05", "A",
                  [("a", 1, "ea", 0.1, 0.1, "Wine"), ("b", 1, "ea", 0.1, 0.1, "Wine"),
                   ("c", 1, "ea", 0.1, 0.1, "Wine")])
        rows = self._csv("/api/export/category-summary.csv?start=2026-06-01&end=2026-06-30")
        self.assertEqual(rows[0], ["Category Type", "Category", "Total"])
        wine = [r for r in rows if len(r) >= 2 and r[1] == "Wine"][0]
        self.assertEqual(wine[2], "0.3")             # 0.1*3 summed exactly, not 0.30000000000000004
        total = [r for r in rows if len(r) >= 2 and r[1] == "TOTAL"][0]
        self.assertEqual(total[2], "0.3")

    def test_empty_range_returns_header_only(self):
        purch = self._csv("/api/export/purchases.csv?start=2030-01-01&end=2030-01-31")
        self.assertEqual(len(purch), 1)              # header, no data rows
        summary = self._csv("/api/export/category-summary.csv?start=2030-01-01&end=2030-01-31")
        self.assertEqual(summary[0], ["Category Type", "Category", "Total"])
        self.assertEqual(summary[-1], ["", "TOTAL", "0.0"])   # grand total of nothing

    def test_export_requires_auth(self):
        # The Base harness runs with auth OFF; turn it on and confirm a tokenless
        # request to an export is rejected (these return raw business data).
        os.environ["APP_PASSWORD"] = "secret"
        try:
            for path in ("/api/export/purchases.csv", "/api/export/category-summary.csv",
                         "/api/export/order-guide.csv"):
                self.assertEqual(self.client.get(path).status_code, 401)
        finally:
            os.environ["APP_PASSWORD"] = ""


class OrderGuide(Base):
    """Below-par products grouped by vendor, with suggested order qty (to par)
    and exact per-vendor subtotals — one order sheet per distributor."""

    def _item(self, loc, name, vendor, par, on_hand, unit_cost, unit="ea"):
        with flask_app.app_context():
            d = get_db()
            d.execute(
                "INSERT INTO inventory_items(location_id, name, vendor, unit, par_level, "
                "last_count, unit_cost) VALUES(?,?,?,?,?,?,?)",
                (loc, name, vendor, unit, par, on_hand, unit_cost))
            d.commit()

    def _guide(self):
        with flask_app.app_context():
            return reports.order_guide()

    def test_groups_below_par_by_vendor_with_exact_totals(self):
        self._item(1, "Gin", "Acme", 10, 3, 20.0)     # need 7  -> $140
        self._item(1, "Rum", "Acme", 5, 5, 15.0)      # at par  -> excluded
        self._item(1, "Wine", "Beta", 4, 1, 12.5)     # need 3  -> $37.50
        g = self._guide()
        self.assertEqual(g["item_count"], 2)
        vendors = {v["vendor"]: v for v in g["vendors"]}
        self.assertEqual(set(vendors), {"Acme", "Beta"})
        gin = vendors["Acme"]["items"][0]
        self.assertEqual((gin["order_qty"], gin["line_cost"]), (7.0, 140.0))
        self.assertEqual(vendors["Acme"]["subtotal"], 140.0)
        self.assertEqual(vendors["Beta"]["subtotal"], 37.5)
        self.assertEqual(g["grand_total"], 177.5)

    def test_zero_par_and_at_par_excluded(self):
        self._item(1, "Soda", "Acme", 0, 0, 1.0)      # no par set
        self._item(1, "Tonic", "Acme", 6, 6, 1.0)     # exactly at par
        self.assertEqual(self._guide()["item_count"], 0)

    def test_blank_vendor_bucketed_unassigned(self):
        self._item(1, "Mystery", "", 3, 0, 2.0)
        self.assertEqual(self._guide()["vendors"][0]["vendor"], "Unassigned")

    def test_case_variant_vendors_group_together(self):
        # The app keys vendors case-insensitively everywhere; one distributor
        # must not split into two order sheets over casing.
        self._item(1, "Gin", "Acme", 5, 0, 10.0)
        self._item(1, "Vodka", "acme", 5, 0, 8.0)
        g = self._guide()
        self.assertEqual(len(g["vendors"]), 1)
        self.assertEqual(len(g["vendors"][0]["items"]), 2)
        self.assertEqual(g["vendors"][0]["subtotal"], 90.0)   # 5*10 + 5*8

    def test_par_set_but_never_counted_is_ordered(self):
        # last_count NULL (par set, no count yet) -> still needs ordering.
        with flask_app.app_context():
            get_db().execute(
                "INSERT INTO inventory_items(location_id, name, vendor, par_level, last_count, "
                "unit_cost) VALUES(1, 'NewSku', 'Acme', 4, NULL, 5.0)")
            get_db().commit()
        g = self._guide()
        self.assertEqual(g["item_count"], 1)
        self.assertEqual(g["vendors"][0]["items"][0]["order_qty"], 4.0)

    def test_endpoint_scoped_by_location_header(self):
        self._item(1, "DC-only", "Acme", 5, 0, 1.0)
        self._item(2, "NYC-only", "Acme", 5, 0, 1.0)
        g1 = self.client.get("/api/inventory/order-guide", headers={"X-Location-Id": "1"}).get_json()
        g2 = self.client.get("/api/inventory/order-guide", headers={"X-Location-Id": "2"}).get_json()
        self.assertEqual([i["name"] for v in g1["vendors"] for i in v["items"]], ["DC-only"])
        self.assertEqual([i["name"] for v in g2["vendors"] for i in v["items"]], ["NYC-only"])

    def test_csv_has_item_subtotal_and_total_rows(self):
        self._item(1, "Gin", "Acme", 10, 3, 20.0)     # 7 * $20 = $140
        r = self.client.get("/api/export/order-guide.csv", headers={"X-Location-Id": "1"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/csv", r.headers["Content-Type"])
        rows = list(csv.reader(io.StringIO(r.get_data(as_text=True))))
        self.assertEqual(rows[0], ["Vendor", "Item", "Unit", "Par", "On Hand",
                                   "Order Qty", "Unit Cost", "Line Cost"])
        self.assertEqual((rows[1][1], rows[1][7]), ("Gin", "140.0"))
        sub = [x for x in rows if len(x) > 1 and x[1] == "SUBTOTAL"][0]
        self.assertEqual(sub[7], "140.0")
        total = [x for x in rows if x[0] == "TOTAL"][0]
        self.assertEqual(total[7], "140.0")


class CreditsAndReturns(Base):
    """Vendor credits / returns are negative invoices/lines. They must NET in
    spend reports but must NOT pollute price intelligence (a return's negative
    unit cost isn't a real price)."""

    def _d(self, days_ago):
        with flask_app.app_context():
            return (square_client.business_today() - dt.timedelta(days=days_ago)).isoformat()

    def _inv(self, vendor, date, lines, loc=1):
        # lines: [(name, unit_cost, qty, total, [category_name])]
        with flask_app.app_context():
            d = get_db()
            iid = d.execute(
                "INSERT INTO invoices(location_id, vendor, invoice_date, total, status) "
                "VALUES(?,?,?,?, 'closed')",
                (loc, vendor, date, sum(ln[3] for ln in lines))).lastrowid
            for name, uc, qty, total, *rest in lines:
                cid = None
                if rest and rest[0]:
                    row = d.execute("SELECT id FROM categories WHERE name=?", (rest[0],)).fetchone()
                    cid = row["id"] if row else None
                d.execute("INSERT INTO invoice_items(invoice_id, name, unit_cost, qty, total, category_id) "
                          "VALUES(?,?,?,?,?,?)", (iid, name, uc, qty, total, cid))
            d.commit()

    def test_return_line_does_not_pollute_price_movers(self):
        self._inv("Acme", "2026-05-15", [("Gin", 20.0, 2, 40.0)])    # prior price (before window)
        self._inv("Acme", "2026-06-10", [("Gin", 24.0, 3, 72.0)])    # real move in window
        self._inv("Acme", "2026-06-15", [("Gin", -24.0, -1, -24.0)])  # a return/credit, newest
        with flask_app.app_context():
            res = reports.price_movers(dt.date(2026, 6, 1), dt.date(2026, 6, 30))
        movers = {m["name"]: m for m in res["movers"]}
        self.assertIn("Gin", movers)
        self.assertEqual(movers["Gin"]["new_price"], 24.0)   # the -24 credit was ignored
        self.assertEqual(movers["Gin"]["old_price"], 20.0)
        self.assertEqual(movers["Gin"]["impact"], 12.0)      # (24-20)*3, return qty excluded

    def test_credit_between_purchases_does_not_mask_a_real_alert(self):
        # Without excluding credits, the -20 return would be read as the "prior
        # price" and (being <= 0) suppress the genuine 20 -> 24 hike.
        self._inv("Acme", self._d(30), [("Gin", 20.0, 1, 20.0)])     # real prior
        self._inv("Acme", self._d(10), [("Gin", -20.0, -1, -20.0)])   # a return (credit)
        self._inv("Acme", self._d(2), [("Gin", 24.0, 2, 48.0)])      # real new (+20%)
        with flask_app.app_context():
            res = reports.price_alerts(min_pct=10)
        self.assertEqual(res["count"], 1)
        self.assertEqual((res["alerts"][0]["old_price"], res["alerts"][0]["new_price"]), (20.0, 24.0))

    def test_credit_invoice_nets_in_spend_reports(self):
        self._inv("Acme", "2026-06-05", [("Case", 100.0, 1, 100.0, "Wine")])
        self._inv("Acme", "2026-06-06", [("Credit", -30.0, -1, -30.0, "Wine")])   # vendor credit
        with flask_app.app_context():
            p = cogs.purchases(dt.date(2026, 6, 1), dt.date(2026, 6, 30))
            cr = reports.category_report(dt.date(2026, 6, 1), dt.date(2026, 6, 30))
            pl = reports.controllable_pl(dt.date(2026, 6, 1), dt.date(2026, 6, 30))
        self.assertEqual(p["total"], 70.0)            # 100 - 30 netted (invoice totals)
        self.assertEqual(cr["grand_total"], 70.0)     # category report nets the credit
        self.assertEqual(pl["total_cogs"], 70.0)      # P&L COGS nets the credit

    def test_credit_line_does_not_corrupt_last_purchase_price(self):
        # A purchase sets the SKU's last price; a later credit/return for the same
        # SKU (negative unit_cost) must NOT overwrite it — but may advance the date.
        self.client.post("/api/invoices", json={
            "vendor": "Acme", "invoice_number": "P1", "invoice_date": "2026-06-05", "total": 30.0,
            "line_items": [{"name": "Keg", "unit_cost": 30.0, "qty": 1, "total": 30.0}]})
        self.client.post("/api/invoices", json={
            "vendor": "Acme", "invoice_number": "C1", "invoice_date": "2026-06-10", "total": -30.0,
            "line_items": [{"name": "Keg", "unit_cost": -30.0, "qty": -1, "total": -30.0}]})
        with flask_app.app_context():
            vi = get_db().execute(
                "SELECT last_purchase_price, last_purchase_date FROM vendor_items "
                "WHERE lower(vendor_item_name)='keg'").fetchone()
        self.assertEqual(vi["last_purchase_price"], 30.0)        # not -30
        self.assertEqual(vi["last_purchase_date"], "2026-06-10")  # date still advanced

    def test_sku_first_seen_on_credit_stores_null_last_price(self):
        self.client.post("/api/invoices", json={
            "vendor": "Acme", "invoice_number": "C2", "invoice_date": "2026-06-10", "total": -15.0,
            "line_items": [{"name": "Returns Only", "unit_cost": -15.0, "qty": -1, "total": -15.0}]})
        with flask_app.app_context():
            vi = get_db().execute(
                "SELECT last_purchase_price FROM vendor_items "
                "WHERE lower(vendor_item_name)='returns only'").fetchone()
        self.assertIsNone(vi["last_purchase_price"])             # never a negative "last price"


if __name__ == "__main__":
    unittest.main()

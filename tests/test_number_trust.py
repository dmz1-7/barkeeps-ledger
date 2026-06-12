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
from unittest import mock

# Point at a throwaway DB and disable the auth gate BEFORE importing the app, so
# we never touch the real data/ledger.db and the test client isn't challenged.
# Clear APP_SECRET too so the persisted-random-secret path is exercised (the
# user's real .env sets one, which load_dotenv would otherwise import).
os.environ["LEDGER_DB"] = tempfile.mktemp(suffix=".db")
os.environ["APP_PASSWORD"] = ""
os.environ["APP_SECRET"] = ""
os.environ["ALLOW_OPEN"] = "1"   # tests run open; acknowledge the fail-closed guard
os.environ["LEDGER_DISABLE_BACKUPS"] = "1"   # don't spawn the backup thread/snapshot under tests

import db                       # noqa: E402
import square_client            # noqa: E402
import cogs                     # noqa: E402
import money                    # noqa: E402
import reports                  # noqa: E402
import units                    # noqa: E402
import invoice_ai               # noqa: E402
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
        # blank vendor/number must not false-positive as a duplicate; a valid date
        # is now required (an unvalidated date would silently drop reports).
        blank = {"vendor": "", "invoice_number": "", "invoice_date": "2026-06-01",
                 "total": None, "line_items": []}
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
            "vendor": "Acme", "invoice_date": "2026-06-01", "subtotal": 100.0, "tax": 0.0,
            "total": 100.0, "line_items": [{"name": "Gin", "total": 60.0}]})
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

    def test_throttle_after_max_wrong_guesses(self):
        # The first _LOGIN_MAX wrong guesses are plain 401s; beyond that, further
        # WRONG guesses are throttled with 429.
        for _ in range(app_module._LOGIN_MAX):
            self.assertEqual(
                self.client.post("/api/login", json={"password": "nope"}).status_code, 401)
        self.assertEqual(
            self.client.post("/api/login", json={"password": "nope"}).status_code, 429)

    def test_throttle_short_circuits_before_evaluating_guess(self):
        # A real rate limit: once the window budget is spent, requests are refused
        # with 429 BEFORE the guess is evaluated (not a cosmetic message swap).
        # Deliberate tradeoff: a flood also makes the owner wait out the short window.
        for _ in range(app_module._LOGIN_MAX):
            self.client.post("/api/login", json={"password": "nope"})
        r = self.client.post("/api/login", json={"password": "secret"})
        self.assertEqual(r.status_code, 429)

    def test_success_clears_failures(self):
        for _ in range(app_module._LOGIN_MAX - 1):
            self.client.post("/api/login", json={"password": "nope"})
        r = self.client.post("/api/login", json={"password": "secret"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("token", r.get_json())
        # The counter reset, so a later wrong attempt is a plain 401, not a 429.
        self.assertEqual(
            self.client.post("/api/login", json={"password": "nope"}).status_code, 401)

    def test_rotated_forwarded_ip_does_not_bypass_the_throttle(self):
        # The throttle is global (no per-IP bucket), so rotating CF-Connecting-IP
        # on every request can't escape it — wrong guesses still 429 past the cap.
        for i in range(app_module._LOGIN_MAX):
            self.assertEqual(self.client.post(
                "/api/login", json={"password": "nope"},
                headers={"CF-Connecting-IP": f"203.0.113.{i}"}).status_code, 401)
        r = self.client.post("/api/login", json={"password": "nope"},
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
            iid = get_db().execute(
                "INSERT INTO invoices(location_id, vendor, invoice_date, total) VALUES(1,'V',?,?)",
                (date_str, total)).lastrowid
            get_db().execute("INSERT INTO invoice_items(invoice_id,name,total) VALUES(?,'x',?)",
                             (iid, total))
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


class RecipeCosting(Base):
    """A recipe costs the sum of its ingredient lines (qty x product unit_cost);
    yield gives per-serving cost; menu_price gives cost% and margin. Exact money,
    location-scoped, items replaced on update and cascade-deleted."""

    def _product(self, name, unit_cost, loc=1, unit="bottle"):
        with flask_app.app_context():
            d = get_db()
            pid = d.execute(
                "INSERT INTO inventory_items(location_id, name, unit, unit_cost) "
                "VALUES(?,?,?,?)", (loc, name, unit, unit_cost)).lastrowid
            d.commit()
            return pid

    def _create(self, **kw):
        return self.client.post("/api/recipes", json=kw).get_json()

    def test_cost_margin_and_cost_pct(self):
        gin = self._product("Gin", 20.0)
        verm = self._product("Vermouth", 10.0)
        r = self._create(name="Martini", menu_price=12.0, yield_qty=1, items=[
            {"product_id": gin, "qty": 0.1},      # $2.00
            {"product_id": verm, "qty": 0.05}])   # $0.50
        self.assertEqual(r["batch_cost"], 2.5)
        self.assertEqual(r["cost_per_serving"], 2.5)
        self.assertEqual(r["cost_pct"], 20.8)     # 2.5/12*100
        self.assertEqual(r["margin"], 9.5)        # 12 - 2.5
        self.assertEqual(r["item_count"], 2)

    def test_yield_divides_into_per_serving(self):
        keg = self._product("Keg", 124.0)
        r = self._create(name="Pint", menu_price=6.0, yield_qty=124,
                         items=[{"product_id": keg, "qty": 1}])
        self.assertEqual(r["batch_cost"], 124.0)
        self.assertEqual(r["cost_per_serving"], 1.0)   # 124 / 124
        self.assertEqual(r["cost_pct"], 16.7)

    def test_zero_yield_treated_as_one(self):
        p = self._product("X", 5.0)
        r = self._create(name="Y", menu_price=10.0, yield_qty=0,
                         items=[{"product_id": p, "qty": 2}])
        self.assertEqual(r["cost_per_serving"], 10.0)   # divided by 1, not a crash

    def test_cost_sums_exactly(self):
        p = self._product("Penny", 0.1)
        r = self._create(name="Z", menu_price=1.0, yield_qty=1, items=[
            {"product_id": p, "qty": 1}, {"product_id": p, "qty": 1},
            {"product_id": p, "qty": 1}])
        self.assertEqual(r["batch_cost"], 0.3)          # not 0.30000000000000004

    def test_no_menu_price_leaves_pct_and_margin_null(self):
        p = self._product("P", 5.0)
        r = self._create(name="NoPrice", menu_price=0, yield_qty=1,
                         items=[{"product_id": p, "qty": 1}])
        self.assertIsNone(r["cost_pct"])
        self.assertIsNone(r["margin"])

    def test_deleted_product_line_costs_zero_and_is_flagged(self):
        p = self._product("Temp", 5.0)
        rid = self._create(name="R", menu_price=10.0, yield_qty=1,
                           items=[{"product_id": p, "qty": 2}])["id"]
        with flask_app.app_context():
            get_db().execute("DELETE FROM inventory_items WHERE id=?", (p,))
            get_db().commit()
        r = self.client.get(f"/api/recipes/{rid}").get_json()
        self.assertEqual(r["batch_cost"], 0.0)          # product_id SET NULL -> no cost
        self.assertEqual(r["missing_products"], 1)
        self.assertTrue(r["items"][0]["missing_product"])

    def test_update_replaces_items(self):
        p = self._product("P", 4.0)
        rid = self._create(name="R", menu_price=10, yield_qty=1,
                           items=[{"product_id": p, "qty": 1}])["id"]
        upd = self.client.put(f"/api/recipes/{rid}", json={
            "name": "R2", "menu_price": 8, "yield_qty": 2,
            "items": [{"product_id": p, "qty": 4}]}).get_json()
        self.assertEqual((upd["name"], upd["item_count"]), ("R2", 1))
        self.assertEqual(upd["batch_cost"], 16.0)       # 4 * 4
        self.assertEqual(upd["cost_per_serving"], 8.0)  # / 2

    def test_delete_cascades_items(self):
        p = self._product("P", 4.0)
        rid = self._create(name="R", menu_price=10, yield_qty=1,
                           items=[{"product_id": p, "qty": 1}])["id"]
        self.assertEqual(self.client.delete(f"/api/recipes/{rid}").status_code, 200)
        self.assertEqual(self.client.get(f"/api/recipes/{rid}").status_code, 404)
        with flask_app.app_context():
            n = get_db().execute(
                "SELECT COUNT(*) c FROM recipe_items WHERE recipe_id=?", (rid,)).fetchone()["c"]
        self.assertEqual(n, 0)

    def test_foreign_recipe_is_404(self):
        p = self._product("NycGin", 5.0, loc=2)
        rid = self.client.post("/api/recipes", headers={"X-Location-Id": "2"}, json={
            "name": "NYC", "menu_price": 5, "yield_qty": 1,
            "items": [{"product_id": p, "qty": 1}]}).get_json()["id"]
        self.assertEqual(self.client.get(f"/api/recipes/{rid}",
                                         headers={"X-Location-Id": "1"}).status_code, 404)
        self.assertEqual(self.client.get(f"/api/recipes/{rid}",
                                         headers={"X-Location-Id": "2"}).status_code, 200)

    def test_foreign_product_dropped_not_costed(self):
        # A store-1 recipe must not cost against a store-2 product's price; the
        # foreign id is dropped to an unlinked ($0) line.
        p2 = self._product("NycSpirit", 50.0, loc=2)
        r = self._create(name="Sneaky", menu_price=10.0, yield_qty=1,
                         items=[{"product_id": p2, "qty": 1}])
        self.assertEqual(r["batch_cost"], 0.0)
        self.assertEqual(r["missing_products"], 1)
        self.assertEqual(r["item_count"], 1)

    def test_non_list_items_does_not_crash(self):
        r = self.client.post("/api/recipes", json={
            "name": "Bad", "menu_price": 5, "yield_qty": 1, "items": 123})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["item_count"], 0)

    def test_null_unit_cost_product_costs_zero(self):
        with flask_app.app_context():
            d = get_db()
            pid = d.execute("INSERT INTO inventory_items(location_id, name, unit, unit_cost) "
                            "VALUES(1, 'NoCost', 'ea', NULL)").lastrowid
            d.commit()
        r = self._create(name="R", menu_price=10.0, yield_qty=1,
                         items=[{"product_id": pid, "qty": 5}])
        self.assertEqual(r["batch_cost"], 0.0)        # NULL unit_cost -> $0, no crash

    def _sized_product(self, name, unit_cost, size_qty, size_unit, loc=1, unit="bottle"):
        with flask_app.app_context():
            d = get_db()
            pid = d.execute(
                "INSERT INTO inventory_items(location_id, name, unit, unit_cost, size_qty, size_unit) "
                "VALUES(?,?,?,?,?,?)", (loc, name, unit, unit_cost, size_qty, size_unit)).lastrowid
            d.commit()
            return pid

    def test_conversion_costs_a_fraction_of_the_purchase_unit(self):
        gin = self._sized_product("Gin", 20.0, 750, "ml")   # $20 / 750ml bottle
        r = self._create(name="G&T", menu_price=12.0, yield_qty=1,
                         items=[{"product_id": gin, "qty": 1.5, "unit": "oz"}])
        self.assertEqual(r["batch_cost"], 1.18)             # 20 * (44.36/750)
        self.assertTrue(r["items"][0]["converted"])
        self.assertEqual(r["unconverted_lines"], 0)

    def test_incompatible_unit_costs_zero_and_flags(self):
        gin = self._sized_product("Gin", 20.0, 750, "ml")
        r = self._create(name="Bad", menu_price=12.0, yield_qty=1,
                         items=[{"product_id": gin, "qty": 2, "unit": "g"}])  # g can't -> ml
        # size present but unit won't convert: contribute $0 (flagged), NOT a wildly
        # wrong 2 * $20/bottle that would inflate batch_cost/cost%/margin.
        self.assertEqual(r["batch_cost"], 0.0)
        self.assertFalse(r["items"][0]["converted"])
        self.assertEqual(r["unconverted_lines"], 1)

    def test_whole_purchase_unit_costs_full_price(self):
        gin = self._sized_product("Gin", 20.0, 750, "ml")
        r = self._create(name="WholeBottle", menu_price=30, yield_qty=1,
                         items=[{"product_id": gin, "qty": 750, "unit": "ml"}])
        self.assertEqual(r["batch_cost"], 20.0)             # 750ml of a 750ml bottle = full price

    def test_no_size_uses_raw_qty_fallback(self):
        lime = self._product("Lime", 0.25)                  # no size set
        r = self._create(name="Garnish", menu_price=1.0, yield_qty=1,
                         items=[{"product_id": lime, "qty": 2, "unit": "each"}])
        self.assertEqual(r["batch_cost"], 0.5)              # 2 * $0.25
        # A no-size product priced in its own unit is correct by design — NOT a
        # conversion failure, so it must not be flagged (only genuine mismatches are).
        self.assertEqual(r["unconverted_lines"], 0)

    def test_product_stores_size_fields(self):
        pid = self.client.post("/api/products", json={
            "name": "Gin", "unit": "bottle", "unit_cost": 20, "size_qty": 750,
            "size_unit": "ml"}).get_json()["id"]
        with flask_app.app_context():
            row = get_db().execute(
                "SELECT size_qty, size_unit FROM inventory_items WHERE id=?", (pid,)).fetchone()
        self.assertEqual((row["size_qty"], row["size_unit"]), (750.0, "ml"))

    def test_recipes_csv(self):
        p = self._product("Gin", 20.0)
        self._create(name="Martini", menu_price=12.0, yield_qty=1,
                     items=[{"product_id": p, "qty": 0.1}])
        r = self.client.get("/api/export/recipes.csv")
        self.assertEqual(r.status_code, 200)
        rows = list(csv.reader(io.StringIO(r.get_data(as_text=True))))
        self.assertEqual(rows[0], ["Recipe", "Menu Price", "Yield", "Batch Cost",
                                   "Cost/Serving", "Cost %", "Margin"])
        self.assertEqual((rows[1][0], rows[1][3], rows[1][6]), ("Martini", "2.0", "10.0"))


class CategoriesApi(Base):
    """The category taxonomy admin (shared across stores) — backs the new UI."""

    def test_create_appears_in_list(self):
        r = self.client.post("/api/categories", json={"name": "Mezcal", "category_type": "Liquor"})
        self.assertEqual(r.status_code, 200)
        cats = self.client.get("/api/categories").get_json()
        mez = [c for c in cats if c["name"] == "Mezcal"][0]
        self.assertEqual(mez["category_type"], "Liquor")

    def test_duplicate_name_rejected(self):
        self.client.post("/api/categories", json={"name": "Mezcal", "category_type": "Liquor"})
        r = self.client.post("/api/categories", json={"name": "Mezcal", "category_type": "Liquor"})
        self.assertEqual(r.status_code, 400)

    def test_missing_name_or_type_rejected(self):
        self.assertEqual(self.client.post(
            "/api/categories", json={"name": "", "category_type": "Liquor"}).status_code, 400)
        self.assertEqual(self.client.post(
            "/api/categories", json={"name": "X", "category_type": ""}).status_code, 400)

    def test_update_renames_and_retypes(self):
        cid = self.client.post("/api/categories",
                               json={"name": "Mezcal", "category_type": "Liquor"}).get_json()["id"]
        self.client.put(f"/api/categories/{cid}", json={"name": "Agave", "category_type": "Other"})
        c = [x for x in self.client.get("/api/categories").get_json() if x["id"] == cid][0]
        self.assertEqual((c["name"], c["category_type"]), ("Agave", "Other"))

    def test_archive_removes_from_list(self):
        cid = self.client.post("/api/categories",
                               json={"name": "Mezcal", "category_type": "Liquor"}).get_json()["id"]
        self.client.delete(f"/api/categories/{cid}")
        cats = self.client.get("/api/categories").get_json()
        self.assertFalse(any(c["id"] == cid for c in cats))


class UnitConvert(unittest.TestCase):
    def test_volume(self):
        self.assertAlmostEqual(units.convert(1, "l", "ml"), 1000.0)
        self.assertAlmostEqual(units.convert(1, "oz", "ml"), 29.5735, places=3)
        self.assertAlmostEqual(units.convert(1, "gal", "oz"), 128.0, places=1)

    def test_weight_and_count(self):
        self.assertAlmostEqual(units.convert(1, "kg", "g"), 1000.0)
        self.assertAlmostEqual(units.convert(1, "lb", "g"), 453.592, places=2)
        self.assertEqual(units.convert(3, "each", "ea"), 3.0)

    def test_cross_dimension_and_unknown_return_none(self):
        self.assertIsNone(units.convert(1, "oz", "g"))      # volume vs weight
        self.assertIsNone(units.convert(1, "oz", "each"))   # volume vs count
        self.assertIsNone(units.convert(1, "blarg", "ml"))  # unknown unit

    def test_case_insensitive_and_aliases(self):
        self.assertAlmostEqual(units.convert(1, "OZ", "mL"), 29.5735, places=3)
        self.assertAlmostEqual(units.convert(1, "Liter", "ml"), 1000.0)
        self.assertTrue(units.known("tbsp"))
        self.assertFalse(units.known("nonsense"))


class UnitTableParity(unittest.TestCase):
    """The JS conversion tables (live recipe-cost preview) must match units.py
    exactly, or the preview disagrees with the saved cost for some aliases."""

    def _js_keys(self, varname):
        import re
        path = os.path.join(os.path.dirname(__file__), "..", "static", "js", "app.js")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        m = re.search(r"const " + varname + r"\s*=\s*\{(.*?)\};", src, re.DOTALL)
        self.assertIsNotNone(m, f"{varname} not found in app.js")   # DOTALL: tolerate multi-line literals
        pairs = re.findall(r'(?:"([^"]+)"|([A-Za-z][\w ]*?))\s*:', m.group(1))
        return {(a or b).strip() for a, b in pairs}

    def test_volume_parity(self):
        self.assertEqual(self._js_keys("_UNIT_VOL"), set(units._VOLUME))

    def test_weight_parity(self):
        self.assertEqual(self._js_keys("_UNIT_WT"), set(units._WEIGHT))

    def test_count_parity(self):
        self.assertEqual(self._js_keys("_UNIT_CT"), set(units._COUNT))


# ============================================================================
# Audit-fix regression tests (2026-06-12 full-spectrum audit, 28 findings)
# ============================================================================

_DC_SQID = "LNKNR2A7MBB4K"   # seeded Pubkey DC square_location_id
_LABOR0 = {"labor": 0.0, "hours": 0, "shifts": 0, "unwaged_hours": 0,
           "unwaged_shifts": 0, "warning": None, "error": None}


class UsageCogsDenominator(Base):
    """HIGH fix: usage COGS spans the count interval, so COGS%/prime% must divide
    by THAT interval's sales, not the (shorter) requested range's sales."""

    def _count(self, date_str, value):
        with flask_app.app_context():
            get_db().execute("INSERT INTO counts(location_id, taken_at, value) VALUES(1,?,?)",
                             (f"{date_str} 12:00:00", value))
            get_db().commit()

    def _invoice(self, date_str, total):
        with flask_app.app_context():
            iid = get_db().execute("INSERT INTO invoices(location_id, vendor, invoice_date, total) "
                                   "VALUES(1,'V',?,?)", (date_str, total)).lastrowid
            get_db().execute("INSERT INTO invoice_items(invoice_id,name,total) VALUES(?,'x',?)",
                             (iid, total))
            get_db().commit()

    @staticmethod
    def _full_interval(per_day):
        # complete daily-sales cache covering every day of [b, e] (the basis is
        # only trusted when the cache is complete)
        def f(b, e):
            n = (e - b).days + 1
            return {(b + dt.timedelta(days=i)).isoformat(): per_day for i in range(n)}
        return f

    def test_cogs_pct_uses_interval_sales_not_range_sales(self):
        self._count("2026-05-30", 1000.0)        # opening, just before the range
        self._count("2026-07-02", 800.0)         # closing, just after
        self._invoice("2026-06-15", 700.0)       # in-interval -> usage = 1000+700-800 = 900
        # interval [05-30, 07-02] = 34 days; $100/day, opening day excluded from the
        # denominator (matches the (b_date, e_date] purchase window) -> 33*100 = 3300
        with flask_app.app_context(), \
                mock.patch.object(square_client, "is_configured", return_value=True), \
                mock.patch.object(square_client, "get_sales",
                                  return_value={"sales": 1000.0, "orders": 1, "error": None}), \
                mock.patch.object(square_client, "get_labor", return_value=dict(_LABOR0)), \
                mock.patch.object(square_client, "daily_sales_cached",
                                  side_effect=self._full_interval(100.0)):
            s = cogs.summary(dt.date(2026, 6, 1), dt.date(2026, 6, 30))
        self.assertEqual(s["cogs_method"], "usage")
        self.assertEqual(s["cogs"], 900.0)
        self.assertEqual(s["cogs_sales"], 3300.0)
        self.assertEqual(s["cogs_pct"], round(900 / 3300 * 100, 1))   # interval basis, not 900/1000
        self.assertEqual(s["cogs_sales_basis"], "interval")
        self.assertEqual(s["sales"], 1000.0)      # labor% still uses range sales


class UsageCogsBrackets(Base):
    def _count(self, d, v):
        with flask_app.app_context():
            get_db().execute("INSERT INTO counts(location_id, taken_at, value) VALUES(1,?,?)",
                             (f"{d} 12:00:00", v)); get_db().commit()

    def _invoice(self, d, t):
        with flask_app.app_context():
            get_db().execute("INSERT INTO invoices(location_id, vendor, invoice_date, total) "
                             "VALUES(1,'V',?,?)", (d, t)); get_db().commit()

    def test_short_range_overshoot_falls_back_to_purchases(self):
        self._count("2026-06-01", 1000.0)
        self._count("2026-06-20", 800.0)         # spans far beyond a 2-day range
        self._invoice("2026-06-10", 500.0)
        with flask_app.app_context():
            s = cogs.summary(dt.date(2026, 6, 10), dt.date(2026, 6, 11))
        self.assertEqual(s["cogs_method"], "purchases")

    def test_single_count_falls_back_to_purchases(self):
        self._count("2026-06-15", 1000.0)
        self._invoice("2026-06-10", 500.0)
        with flask_app.app_context():
            s = cogs.summary(dt.date(2026, 6, 1), dt.date(2026, 6, 30))
        self.assertEqual(s["cogs_method"], "purchases")


class CategoryReportUncategorized(Base):
    def test_uncategorized_line_counts_in_grand_total(self):
        with flask_app.app_context():
            d = get_db()
            wine = d.execute("SELECT id FROM categories WHERE name='Wine'").fetchone()["id"]
            iid = d.execute("INSERT INTO invoices(location_id,vendor,invoice_date,total) "
                            "VALUES(1,'V','2026-06-05',150)").lastrowid
            d.execute("INSERT INTO invoice_items(invoice_id,name,total,category_id) VALUES(?,?,?,?)",
                      (iid, "a", 100.0, wine))
            d.execute("INSERT INTO invoice_items(invoice_id,name,total,category_id) VALUES(?,?,?,NULL)",
                      (iid, "b", 50.0))
            d.commit()
            cr = reports.category_report(dt.date(2026, 6, 1), dt.date(2026, 6, 30))
        self.assertEqual(cr["grand_total"], 150.0)          # includes the 50 uncategorized
        self.assertEqual(cr["column_totals"][0], 50.0)      # the Uncategorized column (id 0)
        self.assertTrue(any(c["id"] == 0 and c["name"] == "Uncategorized" for c in cr["categories"]))


class ApiHardening(Base):
    def test_bad_date_param_is_400_json(self):
        r = self.client.get("/api/dashboard?start=not-a-date&end=2026-06-30")
        self.assertEqual(r.status_code, 400)
        self.assertIn("error", r.get_json())

    def test_non_object_json_body_does_not_500(self):
        for ep in ("/api/recipes", "/api/invoices", "/api/counts"):
            self.assertNotEqual(self.client.post(ep, json=[1, 2, 3]).status_code, 500)

    def test_404_is_json_error_shape(self):
        r = self.client.get("/api/recipes/999999")
        self.assertEqual(r.status_code, 404)
        self.assertIn("error", r.get_json())

    def test_array_field_value_does_not_500(self):
        pid = self.client.post("/api/products", json={"name": "X", "unit_cost": 1}).get_json()["id"]
        self.assertNotEqual(self.client.put(f"/api/products/{pid}", json={"unit": ["x"]}).status_code, 500)

    def test_non_string_name_is_rejected_not_500(self):
        r = self.client.post("/api/categories", json={"name": 123, "category_type": "Liquor"})
        self.assertEqual(r.status_code, 400)            # 123 -> "" -> name required -> 400, not 500

    def test_malformed_line_items_does_not_500(self):
        r = self.client.post("/api/invoices", json={"vendor": "V", "invoice_number": "",
                                                    "invoice_date": "2026-06-01", "total": 5,
                                                    "line_items": "oops"})
        self.assertNotEqual(r.status_code, 500)


class AuthGateCoverage(Base):
    def setUp(self):
        super().setUp()
        os.environ["APP_PASSWORD"] = "secret"
        os.environ["APP_SECRET"] = "testsecret"

    def tearDown(self):
        os.environ["APP_PASSWORD"] = ""
        os.environ["APP_SECRET"] = ""
        super().tearDown()

    def test_tokenless_data_endpoints_401(self):
        # /uploads/ serves raw invoice photos — assert it's gated too, not just /api/
        for ep in ("/api/dashboard", "/api/invoices", "/api/counts", "/uploads/x.jpg"):
            self.assertEqual(self.client.get(ep).status_code, 401)

    def test_config_open_but_minimal_pre_login(self):
        j = self.client.get("/api/config").get_json()
        self.assertEqual(j.get("auth_required"), True)
        self.assertNotIn("square_location_id", j)        # don't leak config pre-login
        self.assertNotIn("target_cogs_pct", j)

    def test_valid_token_is_accepted(self):
        token = self.client.post("/api/login", json={"password": "secret"}).get_json()["token"]
        r = self.client.get("/api/invoices", headers={"Authorization": "Bearer " + token})
        self.assertEqual(r.status_code, 200)

    def test_token_invalid_after_secret_rotation(self):
        token = self.client.post("/api/login", json={"password": "secret"}).get_json()["token"]
        os.environ["APP_SECRET"] = "rotated-secret"   # rotating the signing key voids old tokens
        r = self.client.get("/api/invoices", headers={"Authorization": "Bearer " + token})
        self.assertEqual(r.status_code, 401)


class GetSalesPagination(Base):
    def test_sums_net_across_pages_and_converts_cents(self):
        page1 = {"orders": [{"net_amounts": {"total_money": {"amount": 1180},
                                             "tax_money": {"amount": 100},
                                             "tip_money": {"amount": 80}}}], "cursor": "c"}
        page2 = {"orders": [{"net_amounts": {"total_money": {"amount": 1000}}}], "cursor": None}

        class FakeResp:
            def __init__(self, d): self._d = d
            def raise_for_status(self): pass
            def json(self): return self._d

        with flask_app.app_context():
            db.set_setting("square_token", "tok")
            with mock.patch.object(square_client.requests, "post",
                                   side_effect=[FakeResp(page1), FakeResp(page2)]):
                info = square_client.get_sales(dt.date(2026, 6, 1), dt.date(2026, 6, 2))
        self.assertEqual(info["sales"], 20.0)   # (1000 + 1000) cents
        self.assertEqual(info["orders"], 2)


class SquareLocationOutbound(Base):
    def test_get_labor_posts_active_store_location(self):
        captured = {}

        class FakeResp:
            def raise_for_status(self): pass
            def json(self): return {"shifts": [], "cursor": None}

        def fake_post(url, **kw):
            captured["body"] = kw.get("json")
            return FakeResp()

        with flask_app.app_context():
            db.set_setting("square_token", "tok")
            with mock.patch.object(square_client.requests, "post", side_effect=fake_post):
                square_client.get_labor(dt.date(2026, 6, 1), dt.date(2026, 6, 2))
        self.assertEqual(captured["body"]["query"]["filter"]["location_ids"], [_DC_SQID])


class DailySalesStaleWindow(Base):
    def test_only_today_and_yesterday_refetched(self):
        with flask_app.app_context():
            db.set_setting("square_token", "tok")
            today = square_client.business_today()
            d = get_db()
            for n in range(0, 11):                      # cache every day in [today-10, today]
                day = (today - dt.timedelta(days=n)).isoformat()
                d.execute("INSERT INTO daily_sales(square_location_id,date,net_sales,fetched_at) "
                          "VALUES(?,?,?, '2020-01-01')", (_DC_SQID, day, 111.0))
            d.commit()
            calls = {}

            def fake_fetch(start, end):
                calls["start"] = start
                return {}

            with mock.patch.object(square_client, "get_daily_sales", side_effect=fake_fetch):
                res = square_client.daily_sales_cached(today - dt.timedelta(days=10), today)
        self.assertEqual(calls["start"], today - dt.timedelta(days=1))   # only today+yesterday
        old = (today - dt.timedelta(days=10)).isoformat()
        self.assertEqual(res[old], 111.0)                                 # historical served from cache


class ControllablePLValues(Base):
    def test_income_cogs_and_margins(self):
        with flask_app.app_context():
            d = get_db()
            d.execute("INSERT INTO sales_mix(location_id,period_start,period_end,category_type,pct) "
                      "VALUES(1,'2026-06-01','2026-06-30','Wine',100)")
            wine = d.execute("SELECT id FROM categories WHERE name='Wine'").fetchone()["id"]
            iid = d.execute("INSERT INTO invoices(location_id,vendor,invoice_date,total) "
                            "VALUES(1,'V','2026-06-05',300)").lastrowid
            d.execute("INSERT INTO invoice_items(invoice_id,name,total,category_id) VALUES(?,?,?,?)",
                      (iid, "a", 300.0, wine))
            d.commit()
            labor = dict(_LABOR0); labor["labor"] = 100.0
            with mock.patch.object(square_client, "get_sales",
                                   return_value={"sales": 1000.0, "orders": 1, "error": None}), \
                    mock.patch.object(square_client, "get_labor", return_value=labor), \
                    mock.patch.object(square_client, "is_configured", return_value=True):
                pl = reports.controllable_pl(dt.date(2026, 6, 1), dt.date(2026, 6, 30))
        self.assertEqual(pl["total_income"], 1000.0)
        wine_income = [i for i in pl["income"] if i["category_type"] == "Wine"][0]
        self.assertEqual(wine_income["amt"], 1000.0)        # 1000 * 100%
        self.assertEqual(pl["total_cogs"], 300.0)
        self.assertEqual(pl["gross_profit"], 700.0)         # 1000 - 300
        self.assertEqual(pl["controllable_profit"], 600.0)  # 700 - 100 labor


class CsvFormulaSafety(Base):
    def test_formula_prefix_is_neutralized(self):
        with flask_app.app_context():
            d = get_db()
            iid = d.execute("INSERT INTO invoices(location_id,vendor,invoice_date,total) "
                            "VALUES(1,?,'2026-06-05',10)", ("=HYPERLINK(1)",)).lastrowid
            d.execute("INSERT INTO invoice_items(invoice_id,name,total) VALUES(?,?,?)", (iid, "x", 10.0))
            d.commit()
        text = self.client.get("/api/export/purchases.csv?start=2026-06-01&end=2026-06-30").get_data(as_text=True)
        self.assertIn("'=HYPERLINK(1)", text)               # apostrophe-guarded


class CrossStoreProductName(Base):
    def test_foreign_product_name_not_leaked(self):
        with flask_app.app_context():
            d = get_db()
            pid2 = d.execute("INSERT INTO inventory_items(location_id,name) VALUES(2,'SecretNYC')").lastrowid
            d.execute("INSERT INTO vendor_items(location_id,vendor_name,vendor_item_name,product_id,status) "
                      "VALUES(1,'V','item',?, 'reviewed')", (pid2,))
            d.commit()
        rows = self.client.get("/api/vendor-items", headers={"X-Location-Id": "1"}).get_json()
        item = [r for r in rows if r["vendor_item_name"] == "item"][0]
        self.assertIsNone(item["product_name"])             # store-2 product name not surfaced


class SalesMixValidation(Base):
    def _put(self, mix):
        return self.client.put("/api/sales-mix?start=2026-06-01&end=2026-06-30", json={"mix": mix})

    def test_mix_must_total_100(self):
        self.assertEqual(self._put({"Wine": 80}).status_code, 400)
        self.assertEqual(self._put({"Wine": 60, "Beer": 40}).status_code, 200)
        self.assertEqual(self._put({"Wine": 0, "Beer": 0}).status_code, 200)   # all-zero clears


class ImporterParsing(Base):
    def test_import_invoices_attributes_category_columns(self):
        import import_marginedge
        rows = [
            ["Invoice Date", "Invoice #", "Vendor", "Total", "Liquor", "Wine"],
            ["06/05/2026", "INV1", "Acme", "$150.00", "100.00", "50.00"],
        ]
        path = os.path.join(self.tmpdir, "categoryReport.csv")
        with open(path, "w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerows(rows)
        with flask_app.app_context():
            imp = import_marginedge.Importer(get_db(), 1)
            imp.import_invoices(path)
            get_db().commit()
            inv = get_db().execute("SELECT id, total FROM invoices WHERE invoice_number='INV1'").fetchone()
            self.assertEqual(inv["total"], 150.0)
            lines = get_db().execute(
                "SELECT ii.total, c.name AS cat FROM invoice_items ii "
                "LEFT JOIN categories c ON c.id = ii.category_id WHERE ii.invoice_id=?",
                (inv["id"],)).fetchall()
        by_cat = {r["cat"]: r["total"] for r in lines}
        self.assertEqual(by_cat.get("Liquor"), 100.0)
        self.assertEqual(by_cat.get("Wine"), 50.0)


# ============================================================================
# Second audit-fix regression tests (2026-06-12 re-audit, 24 findings)
# ============================================================================

def _product(name, unit_cost, loc=1, **extra):
    with flask_app.app_context():
        cols = "location_id,name,unit_cost" + ("," + ",".join(extra) if extra else "")
        marks = "?,?,?" + ("," + ",".join("?" * len(extra)) if extra else "")
        pid = get_db().execute(f"INSERT INTO inventory_items({cols}) VALUES({marks})",
                               (loc, name, unit_cost, *extra.values())).lastrowid
        get_db().commit()
        return pid


class PrimePctUsageMode(Base):
    """Re-audit HIGH: in usage mode prime% = cogs% + labor% (each correctly
    based), not prime / interval-sales."""

    def _count(self, d, v):
        with flask_app.app_context():
            get_db().execute("INSERT INTO counts(location_id, taken_at, value) VALUES(1,?,?)",
                             (f"{d} 12:00:00", v)); get_db().commit()

    def _invoice(self, d, t):
        with flask_app.app_context():
            iid = get_db().execute("INSERT INTO invoices(location_id,vendor,invoice_date,total) "
                                   "VALUES(1,'V',?,?)", (d, t)).lastrowid
            get_db().execute("INSERT INTO invoice_items(invoice_id,name,total) VALUES(?,'x',?)",
                             (iid, t)); get_db().commit()

    def _summary(self, daily_fn):
        labor = dict(_LABOR0); labor["labor"] = 300.0
        with flask_app.app_context(), \
                mock.patch.object(square_client, "is_configured", return_value=True), \
                mock.patch.object(square_client, "get_sales",
                                  return_value={"sales": 1000.0, "orders": 1, "error": None}), \
                mock.patch.object(square_client, "get_labor", return_value=labor), \
                mock.patch.object(square_client, "daily_sales_cached", side_effect=daily_fn):
            return cogs.summary(dt.date(2026, 6, 1), dt.date(2026, 6, 30))

    def test_prime_pct_is_sum_of_correctly_based_parts(self):
        self._count("2026-05-30", 1000.0)
        self._count("2026-07-02", 800.0)
        self._invoice("2026-06-15", 700.0)            # usage = 900
        # complete cache: 34 days x $100, opening day excluded -> 33*100 = 3300
        s = self._summary(lambda b, e: {(b + dt.timedelta(days=i)).isoformat(): 100.0
                                        for i in range((e - b).days + 1)})
        self.assertEqual(s["cogs_sales_basis"], "interval")
        cogs_pct = round(900 / 3300 * 100, 1)
        self.assertEqual(s["cogs_pct"], cogs_pct)
        self.assertEqual(s["labor_pct"], 30.0)         # 300/1000 (range)
        self.assertEqual(s["prime_pct"], round(cogs_pct + 30.0, 1))   # sum of correctly-based parts

    def test_empty_interval_cache_falls_back_to_range_sales(self):
        self._count("2026-05-30", 1000.0)
        self._count("2026-07-02", 800.0)
        self._invoice("2026-06-15", 700.0)
        s = self._summary(lambda b, e: {})             # Square-down / cold cache
        self.assertEqual(s["cogs_sales_basis"], "range")
        self.assertEqual(s["cogs_sales"], 1000.0)
        self.assertEqual(s["cogs_pct"], 90.0)          # 900/1000

    def test_partial_cache_falls_back_to_range_sales(self):
        self._count("2026-05-30", 1000.0)
        self._count("2026-07-02", 800.0)
        self._invoice("2026-06-15", 700.0)
        # only 2 of 34 interval days cached -> incomplete -> fall back to range
        s = self._summary(lambda b, e: {b.isoformat(): 100.0, e.isoformat(): 100.0})
        self.assertEqual(s["cogs_sales_basis"], "range")
        self.assertEqual(s["cogs_pct"], 90.0)


class RecipeSubCent(Base):
    def test_sub_cent_pours_sum_not_zeroed(self):
        pid = _product("Bitters", 1.0)                 # $1 per "each"
        items = [{"product_id": pid, "qty": 0.004} for _ in range(10)]   # each $0.004
        r = self.client.post("/api/recipes", json={"name": "R", "menu_price": 1,
                                                   "yield_qty": 1, "items": items}).get_json()
        self.assertEqual(r["batch_cost"], 0.04)        # 10 * 0.004, not 0.00


class MoneyNonFinite(unittest.TestCase):
    def test_inf_nan_rejected(self):
        self.assertEqual(money.to_cents(float("inf")), 0)
        self.assertEqual(money.to_cents(float("nan")), 0)
        self.assertEqual(money.to_cents("Infinity"), 0)
        self.assertIsNone(money.cents_or_none(float("inf")))
        self.assertIsNone(money.normalize(float("inf")))


class ApiHardening2(Base):
    def test_f_rejects_non_finite(self):
        self.assertEqual(app_module._f(float("inf"), 0), 0)
        self.assertEqual(app_module._f("Infinity", 0), 0)
        self.assertEqual(app_module._f(float("nan"), 0), 0)

    def test_product_array_field_does_not_500(self):
        self.assertNotEqual(
            self.client.post("/api/products", json={"name": "X", "unit_cost": ["x"]}).status_code, 500)

    def test_count_non_scalar_item_id_does_not_500(self):
        self.assertNotEqual(
            self.client.post("/api/counts", json={"lines": [{"item_id": [1], "qty": 1}]}).status_code, 500)

    def test_active_location_non_scalar_is_400(self):
        self.assertEqual(self.client.put("/api/active-location", json={"location_id": [1]}).status_code, 400)


class VendorItemForeignProduct(Base):
    def test_create_drops_foreign_product(self):
        pid2 = _product("NYConly", 5.0, loc=2)
        rid = self.client.post("/api/vendor-items", headers={"X-Location-Id": "1"},
                               json={"vendor_item_name": "x", "product_id": pid2}).get_json()["id"]
        with flask_app.app_context():
            row = get_db().execute("SELECT product_id FROM vendor_items WHERE id=?", (rid,)).fetchone()
        self.assertIsNone(row["product_id"])


class InvoiceDeleteRecompute(Base):
    def test_deleting_newest_invoice_reverts_last_price(self):
        def post(date, cost):
            return self.client.post("/api/invoices", json={
                "vendor": "Acme", "invoice_number": "", "invoice_date": date, "total": cost,
                "confirm_duplicate": True,
                "line_items": [{"name": "Gin", "unit_cost": cost, "qty": 1, "total": cost}]})
        post("2026-06-01", 20.0)
        newest = post("2026-06-10", 24.0).get_json()["id"]
        with flask_app.app_context():
            p = get_db().execute("SELECT last_purchase_price FROM vendor_items "
                                 "WHERE lower(vendor_item_name)='gin'").fetchone()
            self.assertEqual(p["last_purchase_price"], 24.0)
        self.assertEqual(self.client.delete(f"/api/invoices/{newest}").status_code, 200)
        with flask_app.app_context():
            p = get_db().execute("SELECT last_purchase_price FROM vendor_items "
                                 "WHERE lower(vendor_item_name)='gin'").fetchone()
        self.assertEqual(p["last_purchase_price"], 20.0)   # recomputed from the remaining line


class InvoiceEditPricePropagation(Base):
    def test_editing_latest_invoice_updates_last_price(self):
        rid = self.client.post("/api/invoices", json={
            "vendor": "Acme", "invoice_number": "E1", "invoice_date": "2026-06-10", "total": 20,
            "line_items": [{"name": "Gin", "unit_cost": 20.0, "qty": 1, "total": 20.0}]}).get_json()["id"]
        self.client.put(f"/api/invoices/{rid}", json={
            "vendor": "Acme", "invoice_number": "E1", "invoice_date": "2026-06-10", "total": 26,
            "line_items": [{"name": "Gin", "unit_cost": 26.0, "qty": 1, "total": 26.0}]})
        with flask_app.app_context():
            p = get_db().execute("SELECT last_purchase_price FROM vendor_items "
                                 "WHERE lower(vendor_item_name)='gin'").fetchone()
        self.assertEqual(p["last_purchase_price"], 26.0)


class SquareErrorSoft(Base):
    def test_get_sales_and_labor_soften_request_errors(self):
        import requests as rq
        with flask_app.app_context():
            db.set_setting("square_token", "tok")
            with mock.patch.object(square_client.requests, "post",
                                   side_effect=rq.RequestException("boom")):
                s = square_client.get_sales(dt.date(2026, 6, 1), dt.date(2026, 6, 2))
                lab = square_client.get_labor(dt.date(2026, 6, 1), dt.date(2026, 6, 2))
        self.assertEqual(s["sales"], 0.0)
        self.assertIsNotNone(s["error"])
        self.assertEqual(lab["labor"], 0.0)
        self.assertIsNotNone(lab["error"])


class DupInvoiceCrossDate(Base):
    def test_same_vendor_and_number_different_date_still_flagged(self):
        base = {"vendor": "Acme", "invoice_number": "INV1", "total": 10, "line_items": []}
        self.client.post("/api/invoices", json={**base, "invoice_date": "2026-06-01"})
        r = self.client.post("/api/invoices", json={**base, "invoice_date": "2026-07-01"})
        self.assertEqual(r.status_code, 409)           # strong signal is vendor+number


class SchemaColumns(Base):
    def test_added_columns_present_after_init(self):
        with flask_app.app_context():
            ri = {r["name"] for r in get_db().execute("PRAGMA table_info(recipe_items)")}
            inv = {r["name"] for r in get_db().execute("PRAGMA table_info(inventory_items)")}
        self.assertIn("unit", ri)
        self.assertIn("size_qty", inv)
        self.assertIn("size_unit", inv)


# ============================================================================
# Third audit-fix regression tests (2026-06-12, 3rd re-audit)
# ============================================================================

class CogsBasisConsistency(Base):
    """All three reports derive COGS from the same line-item basis, so the
    dashboard, Category Report, and P&L agree for the same period even when line
    items don't reconcile to the invoice grand total."""

    def test_dashboard_category_and_pl_agree(self):
        with flask_app.app_context():
            d = get_db()
            wine = d.execute("SELECT id FROM categories WHERE name='Wine'").fetchone()["id"]
            # grand total 150 (incl. tax/fees) but only 120 of categorized line items
            iid = d.execute("INSERT INTO invoices(location_id,vendor,invoice_date,total) "
                            "VALUES(1,'V','2026-06-05',150)").lastrowid
            d.execute("INSERT INTO invoice_items(invoice_id,name,total,category_id) VALUES(?,?,?,?)",
                      (iid, "a", 120.0, wine))
            d.commit()
            purch = cogs.purchases(dt.date(2026, 6, 1), dt.date(2026, 6, 30))
            cr = reports.category_report(dt.date(2026, 6, 1), dt.date(2026, 6, 30))
        self.assertEqual(purch["total"], 120.0)              # line-item basis, not 150
        self.assertEqual(cr["grand_total"], 120.0)
        self.assertEqual(round(sum(purch["by_category"].values()), 2), 120.0)  # header = breakdown


class InvoiceUpdateHardening(Base):
    def _create(self, items):
        return self.client.post("/api/invoices", json={
            "vendor": "Acme", "invoice_number": "U1", "invoice_date": "2026-06-10",
            "total": 20, "line_items": items}).get_json()["id"]

    def test_malformed_line_item_on_update_does_not_500(self):
        rid = self._create([{"name": "Gin", "unit_cost": 20, "qty": 1, "total": 20}])
        r = self.client.put(f"/api/invoices/{rid}", json={
            "vendor": "Acme", "invoice_number": "U1", "invoice_date": "2026-06-10",
            "total": 20, "line_items": [{"name": "Gin", "total": 20}, "oops"]})
        self.assertNotEqual(r.status_code, 500)

    def test_editing_a_line_out_clears_its_stale_last_price(self):
        rid = self._create([{"name": "Gin", "unit_cost": 20, "qty": 1, "total": 20}])
        with flask_app.app_context():
            p = get_db().execute("SELECT last_purchase_price FROM vendor_items "
                                 "WHERE lower(vendor_item_name)='gin'").fetchone()
            self.assertEqual(p["last_purchase_price"], 20.0)
        # edit the Gin line OUT (replace with a different SKU)
        self.client.put(f"/api/invoices/{rid}", json={
            "vendor": "Acme", "invoice_number": "U1", "invoice_date": "2026-06-10",
            "total": 5, "line_items": [{"name": "Lime", "unit_cost": 5, "qty": 1, "total": 5}]})
        with flask_app.app_context():
            p = get_db().execute("SELECT last_purchase_price FROM vendor_items "
                                 "WHERE lower(vendor_item_name)='gin'").fetchone()
        self.assertIsNone(p["last_purchase_price"])          # no remaining line -> NULL, not stale 20


class UploadsScoping(Base):
    def test_foreign_store_image_not_served(self):
        with flask_app.app_context():
            get_db().execute("INSERT INTO invoices(location_id,vendor,invoice_date,image_path) "
                             "VALUES(2,'V','2026-06-01','secret.jpg')")
            get_db().commit()
        r = self.client.get("/uploads/secret.jpg", headers={"X-Location-Id": "1"})
        self.assertEqual(r.status_code, 404)

    def test_unknown_image_404(self):
        self.assertEqual(self.client.get("/uploads/nope.jpg").status_code, 404)


class CsvAllPrefixes(Base):
    def test_all_formula_prefixes_neutralized(self):
        with flask_app.app_context():
            d = get_db()
            for i, vendor in enumerate(("=A1", "+1", "-2", "@SUM", " =B2")):
                iid = d.execute("INSERT INTO invoices(location_id,vendor,invoice_date,total) "
                                "VALUES(1,?,?,10)", (vendor, f"2026-06-0{i+1}")).lastrowid
                d.execute("INSERT INTO invoice_items(invoice_id,name,total) VALUES(?,'x',10)", (iid,))
            d.commit()
        text = self.client.get("/api/export/purchases.csv?start=2026-06-01&end=2026-06-30").get_data(as_text=True)
        for v in ("=A1", "+1", "-2", "@SUM"):
            self.assertIn("'" + v, text)
        self.assertIn("' =B2", text)   # leading-space formula also guarded


class ListLocationsError(Base):
    def test_request_error_soft(self):
        import requests as rq
        with flask_app.app_context():
            db.set_setting("square_token", "tok")
            with mock.patch.object(square_client.requests, "get",
                                   side_effect=rq.RequestException("boom")):
                out = square_client.list_locations()
        self.assertEqual(out["locations"], [])
        self.assertIsNotNone(out["error"])

    def test_no_token_soft(self):
        with flask_app.app_context():
            out = square_client.list_locations()
        self.assertEqual(out["locations"], [])
        self.assertIsNotNone(out["error"])


class InvoiceAiParsing(unittest.TestCase):
    def test_extract_json_bare_prose_and_garbage(self):
        self.assertEqual(invoice_ai._extract_json('{"a": 1}'), {"a": 1})
        self.assertEqual(invoice_ai._extract_json('Here you go:\n{"a": 2}\nThanks'), {"a": 2})
        self.assertIsNone(invoice_ai._extract_json("no json here"))
        self.assertIsNone(invoice_ai._extract_json(""))

    def test_normalize_preserves_negatives_and_clamps_category(self):
        out = invoice_ai._normalize({
            "vendor": "V", "line_items": [
                {"name": "Keg deposit refund", "total": -30, "unit_cost": -30, "category": "Bogus"}]})
        line = out["line_items"][0]
        self.assertEqual(line["total"], -30.0)               # negative preserved
        self.assertEqual(line["category"], "Uncategorized")  # unknown category clamped


class InvoiceParseEndpoint(Base):
    def test_missing_bad_ext_and_empty(self):
        self.assertEqual(self.client.post("/api/invoices/parse").status_code, 400)
        self.assertEqual(self.client.post("/api/invoices/parse", data={
            "image": (io.BytesIO(b"x"), "note.txt")},
            content_type="multipart/form-data").status_code, 400)
        self.assertEqual(self.client.post("/api/invoices/parse", data={
            "image": (io.BytesIO(b""), "p.jpg")},
            content_type="multipart/form-data").status_code, 400)

    def test_parse_failure_is_422(self):
        if not invoice_ai.HAVE_PIL:
            self.skipTest("PIL not installed")
        buf = io.BytesIO()
        invoice_ai.Image.new("RGB", (2, 2)).save(buf, "PNG")   # a real image -> passes the bytes check
        png = buf.getvalue()
        with mock.patch.object(app_module, "parse_invoice",
                               side_effect=app_module.InvoiceError("nope")):
            r = self.client.post("/api/invoices/parse", data={
                "image": (io.BytesIO(png), "p.png")},
                content_type="multipart/form-data")
        self.assertEqual(r.status_code, 422)
        self.assertIn("error", r.get_json())

    def test_non_image_bytes_rejected(self):
        r = self.client.post("/api/invoices/parse", data={
            "image": (io.BytesIO(b"not an image"), "p.png")},
            content_type="multipart/form-data")
        # 400 when PIL is present (bytes rejected); 422 if PIL absent (parse fails)
        self.assertIn(r.status_code, (400, 422))


class ListEndpointScoping(Base):
    def test_collections_exclude_foreign_store(self):
        with flask_app.app_context():
            d = get_db()
            d.execute("INSERT INTO inventory_items(location_id,name) VALUES(2,'NYCprod')")
            d.execute("INSERT INTO recipes(location_id,name) VALUES(2,'NYCrecipe')")
            d.execute("INSERT INTO counts(location_id,note,value) VALUES(2,'NYCcount',0)")
            d.execute("INSERT INTO vendor_items(location_id,vendor_item_name,status) "
                      "VALUES(2,'NYCsku','reviewed')")
            d.commit()
        h = {"X-Location-Id": "1"}
        prods = self.client.get("/api/products", headers=h).get_json()
        recs = self.client.get("/api/recipes", headers=h).get_json()
        counts = self.client.get("/api/counts", headers=h).get_json()
        vis = self.client.get("/api/vendor-items", headers=h).get_json()
        self.assertFalse(any(p["name"] == "NYCprod" for p in prods))
        self.assertFalse(any(r["name"] == "NYCrecipe" for r in recs))
        self.assertFalse(any(c.get("note") == "NYCcount" for c in counts))
        self.assertFalse(any(v["vendor_item_name"] == "NYCsku" for v in vis))


# ============================================================================
# Fourth audit-fix regression tests (2026-06-12, 4th re-audit)
# ============================================================================

class NumericColumnCoercion(Base):
    def test_string_in_numeric_column_does_not_corrupt_or_500(self):
        pid = self.client.post("/api/products", json={"name": "X", "unit_cost": 1,
                                                     "par_level": 5}).get_json()["id"]
        # a string par_level must coerce to NULL/number, not store "abc"
        self.client.put(f"/api/products/{pid}", json={"par_level": "abc"})
        with flask_app.app_context():
            v = get_db().execute("SELECT par_level FROM inventory_items WHERE id=?", (pid,)).fetchone()
        self.assertNotEqual(v["par_level"], "abc")
        # and the read path that does arithmetic must not 500
        self.assertEqual(self.client.get("/api/inventory/order-list").status_code, 200)
        self.assertEqual(self.client.get("/api/inventory/order-guide").status_code, 200)


class LaborBreaks(unittest.TestCase):
    def _iso(self, base, minutes):
        return (base + dt.timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def test_unpaid_break_subtracted_paid_break_kept(self):
        start = dt.datetime(2026, 6, 1, 18, 0, 0, tzinfo=dt.timezone.utc)
        shift = {
            "start_at": self._iso(start, 0), "end_at": self._iso(start, 240),   # 4h
            "wage": {"hourly_rate": {"amount": 2000}},                          # $20/hr
            "breaks": [
                {"start_at": self._iso(start, 60), "end_at": self._iso(start, 90), "is_paid": False},   # 30m unpaid
                {"start_at": self._iso(start, 120), "end_at": self._iso(start, 135), "is_paid": True},  # 15m paid
            ],
        }
        cost, hours, _ = square_client._shift_cost(shift, 0.0)
        self.assertAlmostEqual(hours, 3.5, places=2)         # 4h - 0.5h unpaid (paid break kept)
        self.assertAlmostEqual(cost, 70.0, places=2)         # 3.5h * $20


class DailySalesBucketing(Base):
    def test_orders_bucket_by_business_day_across_5am(self):
        page = {"orders": [
            {"closed_at": "2026-06-12T07:00:00Z",   # 03:00 ET -> 06-11 (before 5am)
             "net_amounts": {"total_money": {"amount": 1180},
                             "tax_money": {"amount": 100}, "tip_money": {"amount": 80}}},
            {"closed_at": "2026-06-12T10:00:00Z",   # 06:00 ET -> 06-12
             "net_amounts": {"total_money": {"amount": 1000}}},
        ], "cursor": None}

        class FakeResp:
            def raise_for_status(self): pass
            def json(self): return page

        with flask_app.app_context():
            db.set_setting("square_token", "tok")
            with mock.patch.object(square_client.requests, "post", return_value=FakeResp()):
                res = square_client.get_daily_sales(dt.date(2026, 6, 11), dt.date(2026, 6, 12))
        self.assertEqual(res.get("2026-06-11"), 10.0)        # 1180-100-80 cents
        self.assertEqual(res.get("2026-06-12"), 10.0)


class BusinessDayAttribution(Base):
    def test_order_business_day(self):
        with flask_app.app_context():
            self.assertEqual(square_client._business_day("2026-06-12T07:00:00Z"), "2026-06-11")
            self.assertEqual(square_client._business_day("2026-06-12T10:00:00Z"), "2026-06-12")
            self.assertIsNone(square_client._business_day("garbage"))


class UnitFactorParity(unittest.TestCase):
    """The JS conversion FACTORS must match units.py, not just the alias sets."""

    def _js_factors(self, varname):
        import re
        path = os.path.join(os.path.dirname(__file__), "..", "static", "js", "app.js")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        m = re.search(r"const " + varname + r"\s*=\s*\{(.*?)\};", src, re.DOTALL)
        self.assertIsNotNone(m, f"{varname} not found in app.js")   # guard: don't AttributeError on None
        out = {}
        for k1, k2, val in re.findall(r'(?:"([^"]+)"|([A-Za-z][\w ]*?))\s*:\s*([\d.]+)', m.group(1)):
            out[(k1 or k2).strip()] = float(val)
        return out

    def test_volume_weight_count_factors_match(self):
        for js, py in (("_UNIT_VOL", units._VOLUME), ("_UNIT_WT", units._WEIGHT),
                       ("_UNIT_CT", units._COUNT)):
            got = self._js_factors(js)
            self.assertEqual(set(got), set(py))
            for k in py:
                self.assertAlmostEqual(got[k], py[k], places=4, msg=f"{js}[{k}]")


class UsageCogsIntervalPerStore(Base):
    def test_interval_sales_uses_only_active_store(self):
        # Use a PAST interval so daily_sales_cached serves the seeded cache and
        # never tries to refresh today/yesterday. Interval = [02-04, 02-15] (12 days).
        with flask_app.app_context():
            d = get_db()
            db.set_setting("square_token", "tok")
            for day, val in (("2026-02-04", 1000.0), ("2026-02-15", 800.0)):   # opening / closing
                d.execute("INSERT INTO counts(location_id,taken_at,value) VALUES(1,?,?)",
                          (f"{day} 12:00:00", val))
            iid = d.execute("INSERT INTO invoices(location_id,vendor,invoice_date,total) "
                            "VALUES(1,'V','2026-02-10',700)").lastrowid
            d.execute("INSERT INTO invoice_items(invoice_id,name,total) VALUES(?,'x',700)", (iid,))
            for n in range(4, 16):   # 02-04..02-15 = 12 days, DC $100/day, NYC $999/day (ignored)
                day = f"2026-02-{n:02d}"
                d.execute("INSERT INTO daily_sales(square_location_id,date,net_sales,fetched_at) "
                          "VALUES('LNKNR2A7MBB4K',?,100.0,'2020-01-01')", (day,))
                d.execute("INSERT INTO daily_sales(square_location_id,date,net_sales,fetched_at) "
                          "VALUES('LS1WRASW8V02R',?,999.0,'2020-01-01')", (day,))
            d.commit()
            with mock.patch.object(square_client, "is_configured", return_value=True), \
                    mock.patch.object(square_client, "get_sales",
                                      return_value={"sales": 5000.0, "orders": 1, "error": None}), \
                    mock.patch.object(square_client, "get_labor", return_value=dict(_LABOR0)):
                s = cogs.summary(dt.date(2026, 2, 5), dt.date(2026, 2, 14))
        self.assertEqual(s["cogs_method"], "usage")
        self.assertEqual(s["cogs_sales_basis"], "interval")
        self.assertEqual(s["cogs_sales"], 1100.0)            # DC: 11 days (opening excl) * $100, NOT NYC's $999


# ============================================================================
# Fifth audit-fix regression tests (2026-06-12, 5th re-audit)
# ============================================================================

class ImagePathTraversal(Base):
    def test_traversal_image_path_cannot_delete_outside_uploads(self):
        # Post an invoice with a traversal image_path; it must be stored as a bare
        # basename so delete can't os.remove outside UPLOAD_DIR.
        rid = self.client.post("/api/invoices", json={
            "vendor": "V", "invoice_number": "", "invoice_date": "2026-06-01", "total": 1,
            "image_path": "../../data/ledger.db", "line_items": []}).get_json()["id"]
        with flask_app.app_context():
            stored = get_db().execute("SELECT image_path FROM invoices WHERE id=?", (rid,)).fetchone()
        self.assertNotIn("/", stored["image_path"] or "")
        self.assertNotIn("..", stored["image_path"] or "")
        # delete must succeed and (critically) not touch the real DB file
        self.assertEqual(self.client.delete(f"/api/invoices/{rid}").status_code, 200)
        self.assertTrue(os.path.exists(self.db_path))


class WindowBounds(Base):
    def test_window_applies_day_start_and_tz_to_utc(self):
        with flask_app.app_context():
            db.set_setting("tz", "America/New_York")
            db.set_setting("day_start_hour", "5")
            # 2026-03-07 5am EST = 10:00 UTC; 2026-03-09 5am EDT (after spring-forward)
            # = 09:00 UTC — the exclusive upper bound is end+1 day at day_start.
            s, e = square_client._window(dt.date(2026, 3, 7), dt.date(2026, 3, 8))
        self.assertEqual(s, "2026-03-07T10:00:00Z")
        self.assertEqual(e, "2026-03-09T09:00:00Z")   # DST: EDT, so 09:00 not 10:00


class GetLaborWindow(Base):
    def test_get_labor_posts_business_day_window(self):
        captured = {}

        class FakeResp:
            def raise_for_status(self): pass
            def json(self): return {"shifts": [], "cursor": None}

        with flask_app.app_context():
            db.set_setting("square_token", "tok")
            db.set_setting("tz", "America/New_York")
            db.set_setting("day_start_hour", "5")
            with mock.patch.object(square_client.requests, "post",
                                   side_effect=lambda url, **kw: (captured.update(kw.get("json", {})), FakeResp())[1]):
                square_client.get_labor(dt.date(2026, 6, 10), dt.date(2026, 6, 10))
        start = captured["query"]["filter"]["start"]
        self.assertEqual(start["start_at"], "2026-06-10T09:00:00Z")   # 5am EDT = 09:00 UTC
        self.assertEqual(start["end_at"], "2026-06-11T09:00:00Z")     # next day 5am EDT


class ConfigTokenHidden(Base):
    def test_config_never_returns_raw_token(self):
        with flask_app.app_context():
            db.set_setting("square_token", "super-secret-token")
        j = self.client.get("/api/config").get_json()
        self.assertIn("has_square_token", j)
        self.assertTrue(j["has_square_token"])
        self.assertNotIn("square_token", j)
        self.assertNotIn("super-secret-token", str(j))


class ImporterBreadth(Base):
    def _imp(self):
        import import_marginedge
        return import_marginedge.Importer(get_db(), 1)

    def test_import_products_upserts_cost(self):
        with flask_app.app_context():           # import_products takes a list of DICT rows
            self._imp().import_products([{"Name": "Tito's", "Category": "Liquor",
                                          "Latest Price": "$24.50"}])
            get_db().commit()
            row = get_db().execute("SELECT unit_cost FROM inventory_items WHERE name=\"Tito's\"").fetchone()
        self.assertEqual(row["unit_cost"], 24.5)

    def test_import_vendor_items_sets_last_price(self):
        with flask_app.app_context():
            self._imp().import_vendor_items([{"Vendor Item Name": "Gin 750", "Vendor": "Acme",
                                              "Last Purch $": "$18.75", "Item Code": "G750"}])
            get_db().commit()
            row = get_db().execute(
                "SELECT last_purchase_price FROM vendor_items WHERE vendor_item_name='Gin 750'").fetchone()
        self.assertEqual(row["last_purchase_price"], 18.75)

    def test_split_category_attributes_to_dominant(self):
        with flask_app.app_context():
            cid = self._imp().category_id("Liquor (80%), Beer (20%)")
            name = get_db().execute("SELECT name FROM categories WHERE id=?", (cid,)).fetchone()["name"]
        self.assertEqual(name, "Liquor")          # dominant share wins

    def test_missing_total_column_errors_clearly(self):
        path = os.path.join(self.tmpdir, "bad.csv")
        with open(path, "w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerows([["Invoice Date", "Invoice #", "Vendor"],   # no "Total"
                                      ["06/01/2026", "X", "Acme"]])
        with flask_app.app_context():
            with self.assertRaises(KeyError):
                self._imp().import_invoices(path)


# ============================================================================
# Sixth audit-fix regression tests (2026-06-12, 6th re-audit)
# ============================================================================

class VendorSpendReports(Base):
    def _inv(self, vendor, total, loc=1, date="2026-06-05"):
        with flask_app.app_context():
            d = get_db()
            iid = d.execute("INSERT INTO invoices(location_id,vendor,invoice_date,total) "
                            "VALUES(?,?,?,?)", (loc, vendor, date, total)).lastrowid
            d.execute("INSERT INTO invoice_items(invoice_id,name,total) VALUES(?,'x',?)", (iid, total))
            d.commit()

    def test_summary_sums_per_store_only(self):
        self._inv("Acme", 100.0, loc=1)
        self._inv("Acme", 999.0, loc=2)          # other store, must be excluded
        out = self.client.get("/api/vendors/summary", headers={"X-Location-Id": "1"}).get_json()
        self.assertEqual(out["total_purchased"], 100.0)

    def test_vendor_spend_is_case_insensitive(self):
        with flask_app.app_context():
            get_db().execute("INSERT INTO vendors(location_id,name) VALUES(1,'Acme')")
            get_db().commit()
        self._inv("Acme", 100.0)
        self._inv("acme", 50.0)                  # different case, same vendor
        vendors = self.client.get("/api/vendors", headers={"X-Location-Id": "1"}).get_json()
        acme = [v for v in vendors if v["name"].lower() == "acme"][0]
        self.assertEqual(acme["spend"], 150.0)


class SalesReportShape(Base):
    def test_weekly_and_ptd_totals(self):
        cache = {"2026-02-16": 100.0, "2026-02-17": 200.0, "2026-02-18": 50.0}   # Mon/Tue/Wed
        with flask_app.app_context(), \
                mock.patch.object(square_client, "daily_sales_cached", return_value=cache):
            rep = reports.sales_report(today=dt.date(2026, 2, 18))   # Wed of that week
        self.assertEqual(rep["totals"]["this_week"], 350.0)          # Mon+Tue+Wed
        self.assertEqual(rep["period_to_date"], 350.0)               # month-to-date


class ControllablePLSplitMix(Base):
    def test_split_mix_income_sums_to_sales(self):
        with flask_app.app_context():
            get_db().execute("INSERT INTO sales_mix(location_id,period_start,period_end,category_type,pct) "
                             "VALUES(1,'2026-06-01','2026-06-30','Wine',60)")
            get_db().execute("INSERT INTO sales_mix(location_id,period_start,period_end,category_type,pct) "
                             "VALUES(1,'2026-06-01','2026-06-30','Beer',40)")
            get_db().commit()
            with mock.patch.object(square_client, "get_sales",
                                   return_value={"sales": 1000.0, "orders": 1, "error": None}), \
                    mock.patch.object(square_client, "get_labor", return_value=dict(_LABOR0)), \
                    mock.patch.object(square_client, "is_configured", return_value=True):
                pl = reports.controllable_pl(dt.date(2026, 6, 1), dt.date(2026, 6, 30))
        inc = {i["category_type"]: i["amt"] for i in pl["income"]}
        self.assertEqual(inc["Wine"], 600.0)
        self.assertEqual(inc["Beer"], 400.0)
        self.assertEqual(round(sum(inc.values()), 2), 1000.0)        # income sums to sales


class GetSalesWindow(Base):
    def test_get_sales_posts_business_day_window(self):
        captured = {}

        class FakeResp:
            def raise_for_status(self): pass
            def json(self): return {"orders": [], "cursor": None}

        with flask_app.app_context():
            db.set_setting("square_token", "tok")
            db.set_setting("tz", "America/New_York")
            db.set_setting("day_start_hour", "5")
            with mock.patch.object(square_client.requests, "post",
                                   side_effect=lambda url, **kw: (captured.update(kw.get("json", {})), FakeResp())[1]):
                square_client.get_sales(dt.date(2026, 6, 10), dt.date(2026, 6, 10))
        closed = captured["query"]["filter"]["date_time_filter"]["closed_at"]
        self.assertEqual(closed["start_at"], "2026-06-10T09:00:00Z")   # 5am EDT
        self.assertEqual(closed["end_at"], "2026-06-11T09:00:00Z")


class CountTimestampBusinessDay(Base):
    def test_count_dated_on_business_day(self):
        self.client.post("/api/counts", json={"note": "n", "lines": []})
        with flask_app.app_context():
            taken = get_db().execute("SELECT taken_at FROM counts ORDER BY id DESC LIMIT 1").fetchone()["taken_at"]
            today = square_client.business_today().isoformat()
        self.assertTrue(taken.startswith(today))


# ============================================================================
# Seventh audit-fix regression tests (2026-06-12, 7th re-audit)
# ============================================================================

class ImporterTenantScoping(Base):
    def test_import_does_not_wipe_other_store(self):
        import import_marginedge
        with flask_app.app_context():
            d = get_db()
            d.execute("INSERT INTO invoices(location_id,vendor,invoice_date,total) "
                      "VALUES(2,'NYCvendor','2026-06-01',999)")
            d.execute("INSERT INTO vendor_items(location_id,vendor_item_name,status) "
                      "VALUES(2,'NYCsku','reviewed')")
            d.commit()
            imp = import_marginedge.Importer(d, 1)
            path = os.path.join(self.tmpdir, "cat.csv")
            with open(path, "w", newline="", encoding="utf-8") as fh:
                csv.writer(fh).writerows([["Invoice Date", "Invoice #", "Vendor", "Total", "Liquor"],
                                          ["06/05/2026", "I1", "DCvendor", "$50", "50"]])
            imp.import_invoices(path)
            imp.import_vendor_items([{"Vendor Item Name": "DCsku", "Vendor": "DCvendor",
                                      "Last Purch $": "$5"}])
            d.commit()
            inv2 = d.execute("SELECT COUNT(*) c FROM invoices WHERE location_id=2").fetchone()["c"]
            vi2 = d.execute("SELECT COUNT(*) c FROM vendor_items WHERE location_id=2").fetchone()["c"]
        self.assertEqual(inv2, 1)        # store 2's invoice survived a store-1 import
        self.assertEqual(vi2, 1)


class DupInvoiceLocationScoping(Base):
    def test_duplicate_guard_is_per_store(self):
        payload = {"vendor": "Acme", "invoice_number": "INV-1",
                   "invoice_date": "2026-06-01", "total": 100, "line_items": []}
        self.assertEqual(self.client.post("/api/invoices", json=payload,
                                          headers={"X-Location-Id": "1"}).status_code, 200)
        # same vendor+number in the OTHER store is NOT a duplicate
        self.assertEqual(self.client.post("/api/invoices", json=payload,
                                          headers={"X-Location-Id": "2"}).status_code, 200)
        # a repeat in store 1 IS
        self.assertEqual(self.client.post("/api/invoices", json=payload,
                                          headers={"X-Location-Id": "1"}).status_code, 409)


class SalesReportColumns(Base):
    def test_ytd_last_week_last_year(self):
        cache = {
            "2026-01-05": 500.0,                       # earlier in the year (YTD)
            "2026-02-09": 70.0, "2026-02-10": 30.0,    # last week (Mon/Tue)
            "2025-02-17": 12.0,                        # ~52 weeks back (last-year column)
            "2026-02-16": 100.0, "2026-02-18": 50.0,   # this week (Mon/Wed)
        }
        with flask_app.app_context(), \
                mock.patch.object(square_client, "daily_sales_cached", return_value=cache):
            rep = reports.sales_report(today=dt.date(2026, 2, 18))
        self.assertEqual(rep["totals"]["last_week"], 100.0)       # 70 + 30
        self.assertEqual(rep["totals"]["last_year"], 12.0)
        # YTD = every cached 2026 day <= today: 500 + 70 + 30 + 100 + 50
        self.assertEqual(rep["year_to_date"], 750.0)


class PrimeDollarConsistency(Base):
    def test_prime_dollars_reconcile_with_prime_pct(self):
        """In usage+interval mode, prime$ / range-sales must equal prime% — the
        natural P&L sanity check. prime_cogs is scaled by the SALES ratio
        (range/interval), not calendar days, so the two derived figures agree."""
        with flask_app.app_context():
            d = get_db()
            for day, val in (("2026-05-30", 1000.0), ("2026-07-02", 800.0)):
                d.execute("INSERT INTO counts(location_id,taken_at,value) VALUES(1,?,?)",
                          (f"{day} 12:00:00", val))
            iid = d.execute("INSERT INTO invoices(location_id,vendor,invoice_date,total) "
                            "VALUES(1,'V','2026-06-15',700)").lastrowid
            d.execute("INSERT INTO invoice_items(invoice_id,name,total) VALUES(?,'x',700)", (iid,))
            d.commit()
            with mock.patch.object(square_client, "is_configured", return_value=True), \
                    mock.patch.object(square_client, "get_sales",
                                      return_value={"sales": 1000.0, "orders": 1, "error": None}), \
                    mock.patch.object(square_client, "get_labor", return_value=dict(_LABOR0)), \
                    mock.patch.object(square_client, "daily_sales_cached",
                                      side_effect=lambda b, e: {(b + dt.timedelta(days=i)).isoformat(): 100.0
                                                                for i in range((e - b).days + 1)}):
                s = cogs.summary(dt.date(2026, 6, 1), dt.date(2026, 6, 30))
        # usage COGS = 1000 + 700 - 800 = 900; interval sales = 33d*100 = 3300;
        # cogs_pct = 900/3300 = 27.3%; prime_cogs = 900 * 1000/3300 = 272.73.
        self.assertEqual(s["prime"], 272.73)
        self.assertEqual(s["cogs_sales_basis"], "interval")
        # the invariant: prime$ back-checks to prime% against the range sales.
        self.assertAlmostEqual(s["prime"] / s["sales"] * 100, s["prime_pct"], places=1)


# ============================================================================
# Eighth audit-fix regression tests (2026-06-12, 8th re-audit)
# ============================================================================

class CogsTaxBasis(Base):
    def test_tax_inclusive_lines_deflated_to_pretax(self):
        with flask_app.app_context():
            d = get_db()
            wine = d.execute("SELECT id FROM categories WHERE name='Wine'").fetchone()["id"]
            # subtotal 100, tax 8, total 108; the single line prints tax-INCLUSIVE (108)
            iid = d.execute("INSERT INTO invoices(location_id,vendor,invoice_date,subtotal,tax,total) "
                            "VALUES(1,'V','2026-06-05',100,8,108)").lastrowid
            d.execute("INSERT INTO invoice_items(invoice_id,name,total,category_id) VALUES(?,?,?,?)",
                      (iid, "a", 108.0, wine))
            d.commit()
            p = cogs.purchases(dt.date(2026, 6, 1), dt.date(2026, 6, 30))
            cr = reports.category_report(dt.date(2026, 6, 1), dt.date(2026, 6, 30))
        self.assertEqual(p["total"], 100.0)              # deflated 108 * 100/108 = 100 (pre-tax)
        self.assertEqual(cr["grand_total"], 100.0)

    def test_tax_exclusive_lines_unchanged(self):
        with flask_app.app_context():
            d = get_db()
            iid = d.execute("INSERT INTO invoices(location_id,vendor,invoice_date,subtotal,tax,total) "
                            "VALUES(1,'V','2026-06-05',100,8,108)").lastrowid
            d.execute("INSERT INTO invoice_items(invoice_id,name,total) VALUES(?,'a',100.0)", (iid,))  # = subtotal
            d.commit()
            p = cogs.purchases(dt.date(2026, 6, 1), dt.date(2026, 6, 30))
        self.assertEqual(p["total"], 100.0)              # already pre-tax, untouched


class ReportEndpointsHttp(Base):
    def test_report_routes_respond(self):
        for path in ("/api/reports/category", "/api/reports/controllable-pl",
                     "/api/reports/sales", "/api/reports/price-movers",
                     "/api/reports/category?start=2026-06-01&end=2026-06-30"):
            self.assertEqual(self.client.get(path).status_code, 200)

    def test_category_report_filters(self):
        with flask_app.app_context():
            d = get_db()
            for v, n in (("Acme", "100"), ("Beta", "50")):
                iid = d.execute("INSERT INTO invoices(location_id,vendor,invoice_date,total) "
                                "VALUES(1,?,?,?)", (v, "2026-06-05", float(n))).lastrowid
                d.execute("INSERT INTO invoice_items(invoice_id,name,total) VALUES(?,'x',?)", (iid, float(n)))
            d.commit()
        cr = self.client.get("/api/reports/category?start=2026-06-01&end=2026-06-30&vendor=Acme").get_json()
        self.assertEqual(cr["grand_total"], 100.0)       # only Acme's invoice


class AuthMutatingEndpoints(AuthGateCoverage):
    def test_tokenless_mutations_rejected(self):
        for method, path in (("post", "/api/invoices"), ("delete", "/api/invoices/1"),
                             ("post", "/api/settings"), ("put", "/api/products/1")):
            r = getattr(self.client, method)(path, json={})
            self.assertEqual(r.status_code, 401, f"{method} {path}")


class PriceTenantIsolation(Base):
    def _line(self, loc, vendor, name, price, date):
        with flask_app.app_context():
            d = get_db()
            iid = d.execute("INSERT INTO invoices(location_id,vendor,invoice_date,total) "
                            "VALUES(?,?,?,?)", (loc, vendor, date, price)).lastrowid
            d.execute("INSERT INTO invoice_items(invoice_id,name,unit_cost,qty,total) "
                      "VALUES(?,?,?,?,?)", (iid, name, price, 1, price))
            d.commit()

    def test_price_movers_excludes_other_store(self):
        self._line(1, "Acme", "Gin", 20.0, "2026-05-15")   # store-1 prior
        self._line(1, "Acme", "Gin", 24.0, "2026-06-10")   # store-1 move
        self._line(2, "Acme", "Gin", 99.0, "2026-06-11")   # store-2 noise, must not appear
        with flask_app.app_context():
            res = reports.price_movers(dt.date(2026, 6, 1), dt.date(2026, 6, 30))
        gin = [m for m in res["movers"] if m["name"] == "Gin"]
        self.assertEqual(len(gin), 1)
        self.assertEqual(gin[0]["new_price"], 24.0)        # store-1's, not store-2's 99


class PriceMoversInWindowChange(Base):
    def test_change_entirely_within_window_is_caught(self):
        with flask_app.app_context():
            d = get_db()
            for price, date in ((20.0, "2026-06-05"), (26.0, "2026-06-20")):   # both in-window, no prior
                iid = d.execute("INSERT INTO invoices(location_id,vendor,invoice_date,total) "
                                "VALUES(1,'Acme',?,?)", (date, price)).lastrowid
                d.execute("INSERT INTO invoice_items(invoice_id,name,unit_cost,qty,total) "
                          "VALUES(?,'Gin',?,1,?)", (iid, price, price))
            d.commit()
            res = reports.price_movers(dt.date(2026, 6, 1), dt.date(2026, 6, 30))
        gin = [m for m in res["movers"] if m["name"] == "Gin"][0]
        self.assertEqual((gin["old_price"], gin["new_price"]), (20.0, 26.0))   # in-window fallback


class SquareLocationUnique(Base):
    def test_cannot_assign_a_square_id_to_two_stores(self):
        # store 2 already seeded with LS1WRASW8V02R; assigning it to store 1 -> 400
        r = self.client.post("/api/settings", json={"square_location_id": "LS1WRASW8V02R"},
                             headers={"X-Location-Id": "1"})
        self.assertEqual(r.status_code, 400)


# ============================================================================
# Ninth audit-fix regression tests (2026-06-12, 9th re-audit)
# ============================================================================

class LocationHeaderRobustness(Base):
    def test_oversized_header_falls_back_not_500(self):
        # > signed-64-bit: int() accepts it but SQLite would OverflowError on bind.
        # The resolver must swallow it and fall back to the default store, not 500.
        r = self.client.get("/api/dashboard", headers={"X-Location-Id": str(2 ** 63)})
        self.assertEqual(r.status_code, 200)
        r2 = self.client.get("/api/dashboard", headers={"X-Location-Id": "-" + str(2 ** 63 + 1)})
        self.assertEqual(r2.status_code, 200)

    def test_oversized_id_in_body_is_clean_not_500(self):
        # an out-of-range id in a JSON body must drop to NULL/clean error, never 500
        r = self.client.put("/api/active-location", json={"location_id": 10 ** 30})
        self.assertNotEqual(r.status_code, 500)
        r2 = self.client.post("/api/products", json={"name": "X", "category_id": 10 ** 30})
        self.assertNotEqual(r2.status_code, 500)


class LocationHeaderAuthGated(AuthGateCoverage):
    def test_authed_header_scopes_tokenless_does_not(self):
        token = self.client.post("/api/login", json={"password": "secret"}).get_json()["token"]
        # authed + X-Location-Id:2 is honored
        r = self.client.get("/api/invoices", headers={"Authorization": "Bearer " + token,
                                                       "X-Location-Id": "2"})
        self.assertEqual(r.status_code, 200)
        # tokenless request with the same header never binds an override — it's 401'd
        r2 = self.client.get("/api/invoices", headers={"X-Location-Id": "2"})
        self.assertEqual(r2.status_code, 401)


class CountsDateBoundary(Base):
    """The sargable rewrite (taken_at < next-day / >= target) must keep the same
    same-day-inclusive semantics the date() wrap had."""
    def test_same_day_count_is_found_on_both_sides(self):
        with flask_app.app_context():
            d = get_db()
            d.execute("INSERT INTO counts(location_id,taken_at,value) VALUES(1,'2026-06-15 23:30:00',500)")
            d.commit()
            before = cogs._inventory_value_near(dt.date(2026, 6, 15), prefer_before=True)
            after = cogs._inventory_value_near(dt.date(2026, 6, 15), prefer_before=False)
        self.assertEqual(before["value"], 500.0)   # late-evening count still counts for that day
        self.assertEqual(after["value"], 500.0)


class CategoryReportReconciles(Base):
    def test_grand_total_matches_controllable_pl(self):
        with flask_app.app_context():
            d = get_db()
            wine = d.execute("SELECT id FROM categories WHERE name='Wine'").fetchone()["id"]
            beer = d.execute("SELECT id FROM categories WHERE name='Liquor'").fetchone()["id"]
            # a tax-inclusive invoice with two categories whose lines sum to the total
            iid = d.execute("INSERT INTO invoices(location_id,vendor,invoice_date,subtotal,tax,total) "
                            "VALUES(1,'V','2026-06-05',100,7,107)").lastrowid
            for cat, amt in ((wine, 53.33), (beer, 53.67)):
                d.execute("INSERT INTO invoice_items(invoice_id,name,total,category_id) VALUES(?,?,?,?)",
                          (iid, "x", amt, cat))
            d.commit()
            cr = reports.category_report(dt.date(2026, 6, 1), dt.date(2026, 6, 30))
            pl = reports.controllable_pl(dt.date(2026, 6, 1), dt.date(2026, 6, 30))
        self.assertEqual(cr["grand_total"], pl["total_cogs"])   # both deflate to the same pre-tax cents


class ActiveLocationArchivedDefault(Base):
    def test_archived_persisted_default_falls_through(self):
        with flask_app.app_context():
            d = get_db()
            db.set_setting("active_location_id", "1")
            d.execute("UPDATE locations SET archived=1 WHERE id=1")
            d.commit()
            # persisted default points at an archived store -> fall through to MIN active
            self.assertEqual(db.active_location_id(), 2)


class ImporterClearNoDangling(Base):
    def test_clear_location_leaves_no_dangling_invoice_item_refs(self):
        import import_marginedge
        with flask_app.app_context():
            d = get_db()
            inv_item = d.execute("INSERT INTO inventory_items(location_id,name) VALUES(2,'Gin')").lastrowid
            vi = d.execute("INSERT INTO vendor_items(location_id,vendor_item_name) VALUES(2,'Gin case')").lastrowid
            # an invoice line in store 1 that (cross-tenant) references store-2 items
            iid = d.execute("INSERT INTO invoices(location_id,vendor,invoice_date,total) "
                            "VALUES(1,'V','2026-06-01',10)").lastrowid
            d.execute("INSERT INTO invoice_items(invoice_id,name,total,inventory_item_id,vendor_item_id) "
                      "VALUES(?,'x',10,?,?)", (iid, inv_item, vi))
            d.commit()
            import_marginedge.Importer(d, 2).clear_location()
            d.commit()
            row = d.execute("SELECT inventory_item_id, vendor_item_id FROM invoice_items "
                            "WHERE invoice_id=?", (iid,)).fetchone()
        self.assertIsNone(row["inventory_item_id"])   # nulled before the parent delete
        self.assertIsNone(row["vendor_item_id"])


class SquareErroredAggregators(Base):
    def test_summary_and_pl_surface_square_errors_without_fake_zero(self):
        soft_sales = {"sales": 0, "orders": 0, "error": "Square timed out"}
        soft_labor = {"labor": 0, "hours": 0, "error": "Square timed out"}
        with flask_app.app_context():
            d = get_db()
            iid = d.execute("INSERT INTO invoices(location_id,vendor,invoice_date,total) "
                            "VALUES(1,'V','2026-06-05',300)").lastrowid
            d.execute("INSERT INTO invoice_items(invoice_id,name,total) VALUES(?,'x',300)", (iid,))
            d.commit()
            with mock.patch.object(square_client, "is_configured", return_value=True), \
                    mock.patch.object(square_client, "get_sales", return_value=soft_sales), \
                    mock.patch.object(square_client, "get_labor", return_value=soft_labor):
                s = cogs.summary(dt.date(2026, 6, 1), dt.date(2026, 6, 30))
                pl = reports.controllable_pl(dt.date(2026, 6, 1), dt.date(2026, 6, 30))
        self.assertEqual(s["sales_error"], "Square timed out")
        self.assertEqual(s["labor_error"], "Square timed out")
        self.assertIsNone(s["cogs_pct"])    # no dividing real COGS by a spurious 0 sales
        self.assertIsNone(s["labor_pct"])
        self.assertEqual(pl["sales_error"], "Square timed out")
        # P&L profit headlines must NOT read as a real -$300 loss under a sales outage
        self.assertIsNone(pl["gross_profit"])
        self.assertIsNone(pl["controllable_profit"])


class InvoiceEditPriceDownAndRemoval(Base):
    def _post(self, num, date, items):
        return self.client.post("/api/invoices", json={
            "vendor": "Acme", "invoice_number": num, "invoice_date": date,
            "total": sum(i["total"] for i in items), "line_items": items}).get_json()["id"]

    def _gin(self, name="Gin", price=0.0):
        return [{"name": name, "unit_cost": price, "qty": 1, "total": price}]

    def _price(self):
        r = get_db().execute("SELECT last_purchase_price FROM vendor_items "
                             "WHERE lower(vendor_item_name)='gin'").fetchone()
        return r["last_purchase_price"] if r else None

    def test_lowering_latest_price_propagates_down(self):
        self._post("D1", "2026-06-01", self._gin(price=20.0))
        rid = self._post("D2", "2026-06-10", self._gin(price=26.0))
        with flask_app.app_context():
            self.assertEqual(self._price(), 26.0)
        # correct the latest invoice DOWN (AI overstated the cost)
        self.client.put(f"/api/invoices/{rid}", json={
            "vendor": "Acme", "invoice_number": "D2", "invoice_date": "2026-06-10",
            "total": 14, "line_items": self._gin(price=14.0)})
        with flask_app.app_context():
            self.assertEqual(self._price(), 14.0)

    def test_removing_latest_sku_reverts_to_older_delivery(self):
        self._post("R1", "2026-06-01", self._gin(price=20.0))
        rid = self._post("R2", "2026-06-10", self._gin(price=26.0))
        # edit the newest invoice to drop the Gin line entirely; an older 20.0 remains
        self.client.put(f"/api/invoices/{rid}", json={
            "vendor": "Acme", "invoice_number": "R2", "invoice_date": "2026-06-10",
            "total": 5, "line_items": self._gin(name="Tonic", price=5.0)})
        with flask_app.app_context():
            self.assertEqual(self._price(), 20.0)   # reverts to the surviving older delivery


# ============================================================================
# Tenth audit-fix regression tests (2026-06-12, 10th re-audit)
# ============================================================================

class UsageCogsCoincidentDay(Base):
    def test_counts_on_range_endpoints_still_align_the_denominator(self):
        """When the bracketing counts land exactly on start/end, COGS still
        excludes the opening-count day's purchases, so the sales denominator must
        drop that day too — the interval basis must run, not fall back to range."""
        with flask_app.app_context():
            d = get_db()
            for day, val in (("2026-06-01", 1000.0), ("2026-06-30", 800.0)):
                d.execute("INSERT INTO counts(location_id,taken_at,value) VALUES(1,?,?)",
                          (f"{day} 12:00:00", val))
            iid = d.execute("INSERT INTO invoices(location_id,vendor,invoice_date,total) "
                            "VALUES(1,'V','2026-06-15',500)").lastrowid
            d.execute("INSERT INTO invoice_items(invoice_id,name,total) VALUES(?,'x',500)", (iid,))
            d.commit()
            with mock.patch.object(square_client, "is_configured", return_value=True), \
                    mock.patch.object(square_client, "get_sales",
                                      return_value={"sales": 3000.0, "orders": 1, "error": None}), \
                    mock.patch.object(square_client, "get_labor", return_value=dict(_LABOR0)), \
                    mock.patch.object(square_client, "daily_sales_cached",
                                      side_effect=lambda b, e: {(b + dt.timedelta(days=i)).isoformat(): 100.0
                                                                for i in range((e - b).days + 1)}):
                s = cogs.summary(dt.date(2026, 6, 1), dt.date(2026, 6, 30))
        self.assertEqual(s["cogs_sales_basis"], "interval")   # not skipped to "range"
        self.assertEqual(s["cogs_sales"], 2900.0)             # 29 days x 100 (06-01 dropped)
        self.assertEqual(s["cogs"], 700.0)                    # 1000 + 500 - 800


class AuthNonAsciiHeader(AuthGateCoverage):
    def test_non_ascii_bearer_is_clean_401_not_500(self):
        r = self.client.get("/api/invoices",
                            headers={"Authorization": "Bearer \udcff".encode("latin-1", "replace")
                                     .decode("latin-1")})
        self.assertEqual(r.status_code, 401)


class BackupStartup(Base):
    def test_backup_creates_snapshot(self):
        with flask_app.app_context():
            snap = db.backup()
        self.assertTrue(snap and os.path.exists(snap))

    def test_start_backups_respects_disable_env(self):
        bdir = os.path.join(os.path.dirname(db.DB_PATH), "backups")
        before = set(glob.glob(os.path.join(bdir, "ledger-*.db")))
        app_module._start_backups()                 # LEDGER_DISABLE_BACKUPS=1 in the test env
        after = set(glob.glob(os.path.join(bdir, "ledger-*.db")))
        self.assertEqual(before, after)             # no snapshot taken, no thread spawned


class SquareLocationUniqueIndex(Base):
    def test_duplicate_square_id_blocked_at_db_level(self):
        with flask_app.app_context():
            d = get_db()
            with self.assertRaises(sqlite3.IntegrityError):
                d.execute("INSERT INTO locations(name, square_location_id) VALUES('X', ?)", (_DC_SQID,))
                d.commit()

    def test_blank_square_ids_do_not_collide(self):
        with flask_app.app_context():
            d = get_db()
            d.execute("INSERT INTO locations(name, square_location_id) VALUES('A','')")
            d.execute("INSERT INTO locations(name, square_location_id) VALUES('B','')")
            d.execute("INSERT INTO locations(name, square_location_id) VALUES('C', NULL)")
            d.commit()   # partial index ignores blanks/NULLs — no collision


class CategoryValidation(Base):
    def _wine_id(self):
        return get_db().execute("SELECT id FROM categories WHERE name='Wine'").fetchone()["id"]

    def test_create_rejects_unknown_type(self):
        r = self.client.post("/api/categories", json={"name": "Weird", "category_type": "Bogus"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("type", r.get_json()["error"].lower())

    def test_update_malformed_type_is_precise_not_unique_collision(self):
        with flask_app.app_context():
            cid = self._wine_id()
        r = self.client.put(f"/api/categories/{cid}", json={"category_type": ["x"]})
        self.assertEqual(r.status_code, 400)
        self.assertNotIn("already exists", r.get_json()["error"])   # not mislabeled


class MalformedJsonBody(Base):
    def test_corrupt_json_is_400_not_silent_blank_row(self):
        r = self.client.post("/api/invoices", data="{not json",
                             content_type="application/json")
        self.assertEqual(r.status_code, 400)


class SalesMixNumericGuard(Base):
    def test_non_numeric_percent_rejected(self):
        r = self.client.put("/api/sales-mix?start=2026-06-01&end=2026-06-30",
                            json={"mix": {"Food": ["oops"]}})
        self.assertEqual(r.status_code, 400)


class SalesMixTenantIsolation(Base):
    def test_store1_mix_not_visible_to_store2(self):
        q = "?start=2026-06-01&end=2026-06-30"
        self.client.put("/api/sales-mix" + q, json={"mix": {"Food": 100}},
                        headers={"X-Location-Id": "1"})
        m1 = self.client.get("/api/sales-mix" + q, headers={"X-Location-Id": "1"}).get_json()["mix"]
        m2 = self.client.get("/api/sales-mix" + q, headers={"X-Location-Id": "2"}).get_json()["mix"]
        self.assertEqual(m1["Food"], 100)
        self.assertEqual(m2["Food"], 0)        # store 2's P&L never sees store 1's mix


class InvoiceAiNonFinite(Base):
    def test_num_rejects_inf_and_nan(self):
        self.assertIsNone(invoice_ai._num(float("inf")))
        self.assertIsNone(invoice_ai._num(float("nan")))

    def test_normalize_strips_non_finite_totals(self):
        out = invoice_ai._normalize({"total": float("inf"),
                                     "line_items": [{"name": "x", "total": float("nan"),
                                                     "unit_cost": float("inf"), "category": "Wine"}]})
        self.assertIsNone(out["total"])
        self.assertIsNone(out["line_items"][0]["total"])
        self.assertIsNone(out["line_items"][0]["unit_cost"])


class DailySalesStaleZeroProtect(Base):
    def test_successful_fetch_omitting_yesterday_keeps_cached_value(self):
        with flask_app.app_context():
            db.set_setting("square_token", "tok")
            today = square_client.business_today()
            yesterday = (today - dt.timedelta(days=1)).isoformat()
            d = get_db()
            d.execute("INSERT INTO daily_sales(square_location_id,date,net_sales,fetched_at) "
                      "VALUES(?,?,?, '2020-01-01')", (_DC_SQID, yesterday, 500.0))
            d.commit()
            # a SUCCESSFUL fetch that returns only today (yesterday omitted)
            with mock.patch.object(square_client, "get_daily_sales",
                                   return_value={today.isoformat(): 40.0}):
                res = square_client.daily_sales_cached(today - dt.timedelta(days=1), today)
        self.assertEqual(res[yesterday], 500.0)        # real cached value not zeroed


class SquareMalformed200(Base):
    def _resp(self, payload):
        class FakeResp:
            def raise_for_status(self): pass
            def json(self): return payload
        return FakeResp()

    def test_null_order_is_treated_as_zero(self):
        with flask_app.app_context():
            db.set_setting("square_token", "tok")
            with mock.patch.object(square_client.requests, "post",
                                   return_value=self._resp({"orders": [None], "cursor": None})):
                info = square_client.get_sales(dt.date(2026, 6, 1), dt.date(2026, 6, 2))
        self.assertEqual(info["sales"], 0.0)
        self.assertIsNone(info["error"])               # null order is benign, not an error

    def test_wrong_shape_fails_soft_with_error(self):
        with flask_app.app_context():
            db.set_setting("square_token", "tok")
            bad = {"orders": [{"net_amounts": ["not", "a", "dict"]}], "cursor": None}
            with mock.patch.object(square_client.requests, "post", return_value=self._resp(bad)):
                info = square_client.get_sales(dt.date(2026, 6, 1), dt.date(2026, 6, 2))
        self.assertEqual(info["sales"], 0.0)
        self.assertIsNotNone(info["error"])            # malformed 200 -> soft error, not 500


class SquareHttpErrorMessage(Base):
    def test_err_extracts_square_error_detail(self):
        import requests as rq

        class FakeResp:
            status_code = 401
            def json(self): return {"errors": [{"detail": "Bad token", "code": "UNAUTHORIZED"}]}
        e = rq.exceptions.HTTPError("401 Client Error")
        e.response = FakeResp()
        with flask_app.app_context():
            db.set_setting("square_token", "tok")
            with mock.patch.object(square_client.requests, "post", side_effect=e):
                info = square_client.get_sales(dt.date(2026, 6, 1), dt.date(2026, 6, 2))
        self.assertEqual(info["error"], "Bad token")   # the response-body branch of _err


# ============================================================================
# Eleventh audit-fix regression tests (2026-06-12, 11th re-audit)
# ============================================================================

class NameRequiredOnUpdate(Base):
    def _product(self):
        return self.client.post("/api/products", json={"name": "Gin"}).get_json()["id"]

    def test_null_or_list_name_is_400_not_500(self):
        with flask_app.app_context():
            d = get_db()
            iid = d.execute("INSERT INTO inventory_items(location_id,name) VALUES(1,'X')").lastrowid
            vid = d.execute("INSERT INTO vendors(location_id,name) VALUES(1,'V')").lastrowid
            d.commit()
        for path, val in ((f"/api/inventory/{iid}", None), (f"/api/inventory/{iid}", ["x"]),
                          (f"/api/vendors/{vid}", None), (f"/api/products/{iid}", ["x"])):
            r = self.client.put(path, json={"name": val})
            self.assertEqual(r.status_code, 400, f"{path} name={val!r}")


class LoginNonStringPassword(AuthGateCoverage):
    def test_non_string_password_401_and_counts_toward_throttle(self):
        app_module._LOGIN_FAILS.clear()
        r = self.client.post("/api/login", json={"password": 123})
        self.assertEqual(r.status_code, 401)              # not a 500
        self.assertEqual(len(app_module._LOGIN_FAILS), 1)  # the bad guess was throttled
        app_module._LOGIN_FAILS.clear()


class OpenModeLoopbackGuard(Base):
    def test_non_loopback_client_blocked_in_open_mode(self):
        # Base runs open (APP_PASSWORD=""); a non-loopback client must be refused.
        r = self.client.get("/api/health", environ_overrides={"REMOTE_ADDR": "8.8.8.8"})
        self.assertEqual(r.status_code, 403)

    def test_loopback_client_allowed_in_open_mode(self):
        r = self.client.get("/api/health", environ_overrides={"REMOTE_ADDR": "127.0.0.1"})
        self.assertEqual(r.status_code, 200)


class OpenModeLoopbackWithAuth(AuthGateCoverage):
    def test_remote_client_allowed_when_auth_enabled(self):
        # With APP_PASSWORD set, the loopback guard is a no-op (auth governs access).
        token = self.client.post("/api/login", json={"password": "secret"}).get_json()["token"]
        r = self.client.get("/api/invoices", headers={"Authorization": "Bearer " + token},
                            environ_overrides={"REMOTE_ADDR": "8.8.8.8"})
        self.assertEqual(r.status_code, 200)


class CategoryDeleteMissing(Base):
    def test_nonexistent_category_delete_is_404(self):
        self.assertEqual(self.client.delete("/api/categories/999999").status_code, 404)


class FkChildIndexes(Base):
    def test_set_null_child_columns_are_indexed(self):
        with flask_app.app_context():
            idx = {r["name"] for r in get_db().execute("PRAGMA index_list(count_lines)")}
            idx |= {r["name"] for r in get_db().execute("PRAGMA index_list(recipe_items)")}
        self.assertIn("idx_countlines_item", idx)
        self.assertIn("idx_recipeitems_product", idx)


class SquareMultiPageError(Base):
    def _resp(self, payload):
        class FakeResp:
            def __init__(self, d): self._d = d
            def raise_for_status(self): pass
            def json(self): return self._d
        return FakeResp(payload)

    def test_page2_error_discards_page1_sales(self):
        import requests as rq
        page1 = {"orders": [{"net_amounts": {"total_money": {"amount": 5000}}}], "cursor": "c"}
        with flask_app.app_context():
            db.set_setting("square_token", "tok")
            with mock.patch.object(square_client.requests, "post",
                                   side_effect=[self._resp(page1), rq.RequestException("boom")]):
                info = square_client.get_sales(dt.date(2026, 6, 1), dt.date(2026, 6, 2))
        self.assertEqual(info["sales"], 0.0)        # page-1 $50 discarded, not banked
        self.assertIsNotNone(info["error"])

    def test_page2_error_returns_none_for_daily(self):
        import requests as rq
        page1 = {"orders": [{"closed_at": "2026-06-01T20:00:00Z",
                             "net_amounts": {"total_money": {"amount": 5000}}}], "cursor": "c"}
        with flask_app.app_context():
            db.set_setting("square_token", "tok")
            with mock.patch.object(square_client.requests, "post",
                                   side_effect=[self._resp(page1), rq.RequestException("boom")]):
                out = square_client.get_daily_sales(dt.date(2026, 6, 1), dt.date(2026, 6, 2))
        self.assertIsNone(out)                       # partial page never banked into the cache


class RecipeMutationIsolation(Base):
    def _product(self, name, loc):
        with flask_app.app_context():
            d = get_db()
            pid = d.execute("INSERT INTO inventory_items(location_id,name,unit_cost) VALUES(?,?,5)",
                            (loc, name)).lastrowid
            d.commit()
            return pid

    def test_foreign_recipe_put_and_delete_are_404_and_unchanged(self):
        p = self._product("NycGin", 2)
        rid = self.client.post("/api/recipes", headers={"X-Location-Id": "2"}, json={
            "name": "NYC", "menu_price": 9, "yield_qty": 1,
            "items": [{"product_id": p, "qty": 1}]}).get_json()["id"]
        # store 1 must not be able to edit or delete store 2's recipe
        put = self.client.put(f"/api/recipes/{rid}", headers={"X-Location-Id": "1"},
                              json={"name": "HACK", "menu_price": 1, "yield_qty": 1})
        dele = self.client.delete(f"/api/recipes/{rid}", headers={"X-Location-Id": "1"})
        self.assertEqual(put.status_code, 404)
        self.assertEqual(dele.status_code, 404)
        with flask_app.app_context():
            row = get_db().execute("SELECT name, menu_price FROM recipes WHERE id=?", (rid,)).fetchone()
        self.assertEqual(row["name"], "NYC")          # untouched by the foreign store
        self.assertEqual(row["menu_price"], 9)


class ParseInvoiceEndToEnd(Base):
    def _fake_anthropic(self, payload_json, fail_output_config=False):
        import types
        mod = types.ModuleType("anthropic")
        mod.APIError = type("APIError", (Exception,), {})

        class Block:
            type = "text"
            def __init__(self, t): self.text = t

        class Resp:
            def __init__(self, blocks): self.content = blocks

        class Messages:
            def create(self, **kw):
                if fail_output_config and "output_config" in kw:
                    raise TypeError("no output_config")   # exercise the plain-text fallback
                return Resp([Block(payload_json)])

        class Client:
            def __init__(self, **kw): self.messages = Messages()

        mod.Anthropic = Client
        return mod

    def test_structured_response_normalizes_into_invoice_dict(self):
        import sys
        payload = ('{"vendor":"Acme","invoice_date":"2026-06-01","invoice_number":"7",'
                   '"subtotal":100,"tax":8,"total":108,'
                   '"line_items":[{"name":"Gin","qty":1,"unit":"case","unit_cost":100,'
                   '"total":100,"category":"Liquor"}]}')
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k"}), \
                mock.patch.dict(sys.modules, {"anthropic": self._fake_anthropic(payload)}), \
                flask_app.app_context():
            out = invoice_ai.parse_invoice(b"img", "image/png")
        self.assertEqual(out["vendor"], "Acme")
        self.assertEqual(out["total"], 108.0)
        self.assertEqual(out["line_items"][0]["name"], "Gin")
        self.assertEqual(out["line_items"][0]["category"], "Liquor")

    def test_plain_text_json_fallback_path(self):
        import sys
        payload = ('{"vendor":"Beta","invoice_date":"","invoice_number":"",'
                   '"subtotal":null,"tax":null,"total":50,"line_items":[]}')
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k"}), \
                mock.patch.dict(sys.modules,
                                {"anthropic": self._fake_anthropic(payload, fail_output_config=True)}), \
                flask_app.app_context():
            out = invoice_ai.parse_invoice(b"img", "image/png")
        self.assertEqual(out["vendor"], "Beta")
        self.assertEqual(out["total"], 50.0)


# ============================================================================
# Twelfth audit-fix regression tests (2026-06-12, 12th re-audit)
# ============================================================================

class InventoryCreateNameRequired(Base):
    def test_blank_name_rejected(self):
        for payload in ({}, {"name": ""}, {"name": ["x"]}):
            r = self.client.post("/api/inventory", json=payload)
            self.assertEqual(r.status_code, 400, payload)
        # a valid name still works
        self.assertEqual(self.client.post("/api/inventory", json={"name": "Gin"}).status_code, 200)


class InvoiceListDateValidation(Base):
    def test_garbage_start_date_is_400(self):
        self.assertEqual(self.client.get("/api/invoices?start=notadate").status_code, 400)
        self.assertEqual(self.client.get("/api/invoices?end=2026-13-99").status_code, 400)
        self.assertEqual(self.client.get("/api/invoices?start=2026-06-01").status_code, 200)


class ListLocationsMalformed(Base):
    def test_non_object_body_fails_soft(self):
        class FakeResp:
            def raise_for_status(self): pass
            def json(self): return ["not", "an", "object"]   # 200 but wrong shape
        with flask_app.app_context():
            db.set_setting("square_token", "tok")
            with mock.patch.object(square_client.requests, "get", return_value=FakeResp()):
                out = square_client.list_locations()
        self.assertEqual(out["locations"], [])      # soft envelope, not a 500
        self.assertIsNotNone(out["error"])


class OpenModeProxyGuard(Base):
    def test_forwarded_for_header_refused_in_open_mode(self):
        r = self.client.get("/api/health", headers={"X-Forwarded-For": "8.8.8.8"},
                            environ_overrides={"REMOTE_ADDR": "127.0.0.1"})
        self.assertEqual(r.status_code, 403)        # loopback peer but proxied -> refuse


class SettingsSquareTokenNoClobber(Base):
    def test_blank_token_does_not_wipe_existing(self):
        with flask_app.app_context():
            db.set_setting("square_token", "live-tok")
        self.client.post("/api/settings", json={"square_token": "", "target_cogs_pct": 28})
        with flask_app.app_context():
            self.assertEqual(db.get_setting("square_token"), "live-tok")   # preserved
            self.assertEqual(db.get_setting("target_cogs_pct"), "28")      # other settings still saved

    def test_nonblank_token_persists(self):
        self.client.post("/api/settings", json={"square_token": "new-tok"})
        with flask_app.app_context():
            self.assertEqual(db.get_setting("square_token"), "new-tok")


class SquareEnvBaseUrl(Base):
    def _capture_url(self):
        captured = {}

        class FakeResp:
            def raise_for_status(self): pass
            def json(self): return {"orders": [], "cursor": None}

        def fake_post(url, **kw):
            captured["url"] = url
            return FakeResp()
        return captured, fake_post

    def test_sandbox_vs_production_base(self):
        for env, base in (("sandbox", square_client.SANDBOX_BASE),
                          ("production", square_client.PROD_BASE)):
            cap, fake_post = self._capture_url()
            with flask_app.app_context():
                db.set_setting("square_token", "tok")
                db.set_setting("square_env", env)
                with mock.patch.object(square_client.requests, "post", side_effect=fake_post):
                    square_client.get_sales(dt.date(2026, 6, 1), dt.date(2026, 6, 2))
            self.assertTrue(cap["url"].startswith(base), f"{env}: {cap['url']}")


class SameDateLastPriceTieBreak(Base):
    def _post(self, num, price):
        return self.client.post("/api/invoices", json={
            "vendor": "Acme", "invoice_number": num, "invoice_date": "2026-06-10",
            "total": price, "line_items": [{"name": "Gin", "unit_cost": price, "qty": 1, "total": price}]})

    def test_last_posted_same_date_price_wins(self):
        self._post("S1", 20.0)
        self._post("S2", 23.0)        # second delivery, same date — last writer wins on the tie
        with flask_app.app_context():
            p = get_db().execute("SELECT last_purchase_price FROM vendor_items "
                                 "WHERE lower(vendor_item_name)='gin'").fetchone()
        self.assertEqual(p["last_purchase_price"], 23.0)


class RedundantIndexDropped(Base):
    def test_date_only_invoice_index_is_gone(self):
        with flask_app.app_context():
            idx = {r["name"] for r in get_db().execute("PRAGMA index_list(invoices)")}
        self.assertNotIn("idx_invoices_date", idx)         # dropped; composite covers it
        self.assertIn("idx_invoices_loc_date", idx)


class RecipeUnconvertedOnlyGenuineMismatch(Base):
    def _sized(self, name, cost, sq, su):
        with flask_app.app_context():
            d = get_db()
            pid = d.execute("INSERT INTO inventory_items(location_id,name,unit_cost,size_qty,size_unit) "
                            "VALUES(1,?,?,?,?)", (name, cost, sq, su)).lastrowid
            d.commit()
            return pid

    def test_each_line_not_flagged_mismatch_is(self):
        lime = self._sized("Lime", 0.25, None, None)        # no size -> raw qty correct
        gin = self._sized("Gin", 20.0, 750, "ml")           # sized; 'g' can't convert to ml
        r = self.client.post("/api/recipes", json={
            "name": "Mix", "menu_price": 10, "yield_qty": 1,
            "items": [{"product_id": lime, "qty": 2, "unit": "each"},
                      {"product_id": gin, "qty": 2, "unit": "g"}]}).get_json()
        self.assertEqual(r["unconverted_lines"], 1)         # only the gin g->ml mismatch, not the lime


# ============================================================================
# Thirteenth audit-fix regression tests (2026-06-12, 13th re-audit)
# ============================================================================

class UniqueSqidMigrationGuard(Base):
    def test_duplicate_sqid_does_not_brick_migration(self):
        with flask_app.app_context():
            d = get_db()
            d.execute("DROP INDEX IF EXISTS uq_locations_sqid")
            d.execute("INSERT INTO locations(name, square_location_id) VALUES('A','DUP')")
            d.execute("INSERT INTO locations(name, square_location_id) VALUES('B','DUP')")
            d.commit()
            # re-running the migration over a pre-existing duplicate must NOT raise
            db._apply_post_indexes(d)
            d.commit()
            # the functional indexes still got created despite the unique-index skip
            idx = {r["name"] for r in d.execute("PRAGMA index_list(invoices)")}
        self.assertIn("idx_invoices_loc_date", idx)


class UsageCogsZeroCountGuard(Base):
    def test_zero_closing_count_falls_back_to_purchases(self):
        with flask_app.app_context():
            d = get_db()
            d.execute("INSERT INTO counts(location_id,taken_at,value) VALUES(1,'2026-06-01 12:00:00',1000)")
            d.execute("INSERT INTO counts(location_id,taken_at,value) VALUES(1,'2026-06-30 12:00:00',0)")  # forgotten/empty
            iid = d.execute("INSERT INTO invoices(location_id,vendor,invoice_date,total) "
                            "VALUES(1,'V','2026-06-15',500)").lastrowid
            d.execute("INSERT INTO invoice_items(invoice_id,name,total) VALUES(?,'x',500)", (iid,))
            d.commit()
            with mock.patch.object(square_client, "is_configured", return_value=True), \
                    mock.patch.object(square_client, "get_sales",
                                      return_value={"sales": 2000.0, "orders": 1, "error": None}), \
                    mock.patch.object(square_client, "get_labor", return_value=dict(_LABOR0)):
                s = cogs.summary(dt.date(2026, 6, 1), dt.date(2026, 6, 30))
        self.assertEqual(s["cogs_method"], "purchases")   # a $0 bracket is not trusted
        self.assertEqual(s["cogs"], 500.0)                 # purchases basis, not 1000+500-0=1500


class PretaxNoSubtotal(Base):
    def test_tax_inclusive_without_subtotal_still_deflates(self):
        with flask_app.app_context():
            d = get_db()
            # tax + total recorded, NO subtotal (common AI/manual case); line tax-inclusive
            iid = d.execute("INSERT INTO invoices(location_id,vendor,invoice_date,subtotal,tax,total) "
                            "VALUES(1,'V','2026-06-05',NULL,8,108)").lastrowid
            d.execute("INSERT INTO invoice_items(invoice_id,name,total) VALUES(?,'a',108)", (iid,))
            d.commit()
            p = cogs.purchases(dt.date(2026, 6, 1), dt.date(2026, 6, 30))
            cr = reports.category_report(dt.date(2026, 6, 1), dt.date(2026, 6, 30))
        self.assertEqual(p["total"], 100.0)        # base = total - tax = 100; tax stripped from COGS
        self.assertEqual(cr["grand_total"], 100.0)


class CategoryUpdateMissing(Base):
    def test_nonexistent_category_update_is_404(self):
        r = self.client.put("/api/categories/999999", json={"name": "X", "category_type": "Food"})
        self.assertEqual(r.status_code, 404)


class CogsIncludesAllStatuses(Base):
    def test_non_closed_invoice_still_counts_in_cogs(self):
        with flask_app.app_context():
            d = get_db()
            iid = d.execute("INSERT INTO invoices(location_id,vendor,invoice_date,total,status) "
                            "VALUES(1,'V','2026-06-05',200,'processing')").lastrowid
            d.execute("INSERT INTO invoice_items(invoice_id,name,total) VALUES(?,'x',200)", (iid,))
            d.commit()
            p = cogs.purchases(dt.date(2026, 6, 1), dt.date(2026, 6, 30))
            pl = reports.controllable_pl(dt.date(2026, 6, 1), dt.date(2026, 6, 30))
        # pin all-statuses behavior so a future per-view status filter can't silently diverge
        self.assertEqual(p["total"], 200.0)
        self.assertEqual(pl["total_cogs"], 200.0)


class UsageCogsRealCacheToday(Base):
    """End-to-end: summary() drives the REAL daily_sales_cached refetch/merge for an
    interval that includes today, exercising the today/yesterday stale refetch."""
    def test_interval_basis_through_real_cache_for_today_interval(self):
        with flask_app.app_context():
            d = get_db()
            today = square_client.business_today()
            b = today - dt.timedelta(days=5)
            for day, val in ((b, 1000.0), (today, 700.0)):
                d.execute("INSERT INTO counts(location_id,taken_at,value) VALUES(1,?,?)",
                          (f"{day.isoformat()} 12:00:00", val))
            iid = d.execute("INSERT INTO invoices(location_id,vendor,invoice_date,total) "
                            "VALUES(1,'V',?,500)", ((b + dt.timedelta(days=2)).isoformat(),)).lastrowid
            d.execute("INSERT INTO invoice_items(invoice_id,name,total) VALUES(?,'x',500)", (iid,))
            # seed the cache for every interval day EXCEPT today + yesterday (the stale window)
            for n in range(2, 6):
                day = (today - dt.timedelta(days=n)).isoformat()
                d.execute("INSERT INTO daily_sales(square_location_id,date,net_sales,fetched_at) "
                          "VALUES(?,?,?, '2020-01-01')", (_DC_SQID, day, 100.0))
            d.commit()
            db.set_setting("square_token", "tok")
            fresh = {today.isoformat(): 100.0, (today - dt.timedelta(days=1)).isoformat(): 100.0}
            with mock.patch.object(square_client, "is_configured", return_value=True), \
                    mock.patch.object(square_client, "get_sales",
                                      return_value={"sales": 3000.0, "orders": 1, "error": None}), \
                    mock.patch.object(square_client, "get_labor", return_value=dict(_LABOR0)), \
                    mock.patch.object(square_client, "get_daily_sales", return_value=fresh):
                s = cogs.summary(b, today)
        # the cache+fetch layer covered every interval day, so the interval basis is trusted
        self.assertEqual(s["cogs_sales_basis"], "interval")
        self.assertEqual(s["cogs_method"], "usage")


# ============================================================================
# Fourteenth audit-fix regression tests (2026-06-12, 14th re-audit)
# ============================================================================

class CategoryReportArchivedSpend(Base):
    def test_archived_category_spend_still_counted_and_reconciles(self):
        with flask_app.app_context():
            d = get_db()
            cid = d.execute("INSERT INTO categories(name, category_type, sort_order) "
                            "VALUES('Seasonal','Liquor',5)").lastrowid
            iid = d.execute("INSERT INTO invoices(location_id,vendor,invoice_date,total) "
                            "VALUES(1,'V','2026-06-05',500)").lastrowid
            d.execute("INSERT INTO invoice_items(invoice_id,name,total,category_id) VALUES(?,?,?,?)",
                      (iid, "x", 500.0, cid))
            d.execute("UPDATE categories SET archived=1 WHERE id=?", (cid,))   # soft-delete the category
            d.commit()
            cr = reports.category_report(dt.date(2026, 6, 1), dt.date(2026, 6, 30))
            pl = reports.controllable_pl(dt.date(2026, 6, 1), dt.date(2026, 6, 30))
        self.assertEqual(cr["grand_total"], 500.0)          # archived spend NOT dropped
        self.assertEqual(cr["grand_total"], pl["total_cogs"])  # still ties to the P&L
        # the archived category's $500 folds into the Uncategorized column
        self.assertEqual(cr["column_totals"][0], 500.0)


class CategorySummaryCsvPretax(Base):
    def test_tax_inclusive_export_deflates_to_pretax(self):
        with flask_app.app_context():
            d = get_db()
            wine = d.execute("SELECT id FROM categories WHERE name='Wine'").fetchone()["id"]
            iid = d.execute("INSERT INTO invoices(location_id,vendor,invoice_date,subtotal,tax,total) "
                            "VALUES(1,'V','2026-06-05',100,8,108)").lastrowid
            d.execute("INSERT INTO invoice_items(invoice_id,name,total,category_id) VALUES(?,?,?,?)",
                      (iid, "a", 108.0, wine))   # tax-inclusive line
            d.commit()
        body = self.client.get("/api/export/category-summary.csv"
                               "?start=2026-06-01&end=2026-06-30").get_data(as_text=True)
        rows = [r.split(",") for r in body.strip().splitlines()]
        total = [r for r in rows if len(r) >= 2 and r[1] == "TOTAL"][0]
        self.assertEqual(total[2], "100.0")        # deflated, ties to Category Report (not 108)


class InvoiceDateValidation(Base):
    def test_blank_or_garbage_date_rejected(self):
        for bad in ("", "not-a-date", "2026-13-40"):
            r = self.client.post("/api/invoices", json={
                "vendor": "V", "invoice_date": bad, "total": 10, "line_items": []})
            self.assertEqual(r.status_code, 400, bad)
        ok = self.client.post("/api/invoices", json={
            "vendor": "V", "invoice_date": "2026-06-05", "total": 10, "line_items": []})
        self.assertEqual(ok.status_code, 200)

    def test_update_rejects_bad_date(self):
        iid = self.client.post("/api/invoices", json={
            "vendor": "V", "invoice_date": "2026-06-05", "total": 10, "line_items": []}).get_json()["id"]
        r = self.client.put(f"/api/invoices/{iid}", json={
            "vendor": "V", "invoice_date": "", "total": 10, "line_items": []})
        self.assertEqual(r.status_code, 400)


class OpenModeForwardingHeaders(Base):
    def test_cf_connecting_ip_refused_in_open_mode(self):
        for hdr in ("CF-Connecting-IP", "X-Real-IP", "True-Client-IP"):
            r = self.client.get("/api/health", headers={hdr: "8.8.8.8"},
                                environ_overrides={"REMOTE_ADDR": "127.0.0.1"})
            self.assertEqual(r.status_code, 403, hdr)


class VendorSummaryBasisPinned(Base):
    def test_total_purchased_is_tax_inclusive_grand_total(self):
        with flask_app.app_context():
            d = get_db()
            iid = d.execute("INSERT INTO invoices(location_id,vendor,invoice_date,subtotal,tax,total) "
                            "VALUES(1,'V','2026-06-05',100,8,108)").lastrowid
            d.execute("INSERT INTO invoice_items(invoice_id,name,total) VALUES(?,'a',108)", (iid,))
            d.commit()
        out = self.client.get("/api/vendors/summary", headers={"X-Location-Id": "1"}).get_json()
        # "total purchased" is what you PAID the vendor (A/P, tax-included) — deliberately
        # the gross invoice total, NOT the pre-tax COGS basis. Pin it so it can't drift.
        self.assertEqual(out["total_purchased"], 108.0)


class CategoryReportFilters(Base):
    def _inv(self, num, status, total):
        with flask_app.app_context():
            d = get_db()
            iid = d.execute("INSERT INTO invoices(location_id,vendor,invoice_date,invoice_number,status,total) "
                            "VALUES(1,'Acme',?,?,?,?)", ("2026-06-05", num, status, total)).lastrowid
            d.execute("INSERT INTO invoice_items(invoice_id,name,total) VALUES(?,'x',?)", (iid, total))
            d.commit()

    def test_status_and_search_filters(self):
        self._inv("CLO-1", "closed", 100)
        self._inv("PRO-9", "processing", 40)
        q = "/api/reports/category?start=2026-06-01&end=2026-06-30"
        self.assertEqual(self.client.get(q).get_json()["grand_total"], 140.0)               # both
        self.assertEqual(self.client.get(q + "&status=processing").get_json()["grand_total"], 40.0)
        self.assertEqual(self.client.get(q + "&q=CLO").get_json()["grand_total"], 100.0)   # search = ?q=


class SettingsGlobalTenancy(Base):
    def test_numeric_settings_are_global_across_stores(self):
        # pin the intended design: target_cogs_pct etc. are GLOBAL (single owner),
        # not per-store — set under store 1, visible from store 2.
        self.client.post("/api/settings", json={"target_cogs_pct": 31},
                         headers={"X-Location-Id": "1"})
        c2 = self.client.get("/api/config", headers={"X-Location-Id": "2"}).get_json()
        self.assertEqual(str(c2["target_cogs_pct"]), "31")


# ============================================================================
# Fifteenth audit-fix regression tests (2026-06-12, 15th re-audit)
# ============================================================================

class CategoryReportPerLineRounding(Base):
    def test_tax_inclusive_multiline_reconciles_to_pl_to_the_penny(self):
        with flask_app.app_context():
            d = get_db()
            wine = d.execute("SELECT id FROM categories WHERE name='Wine'").fetchone()["id"]
            # three tax-inclusive lines summing to 108; factor 100/108. Per-GROUP
            # rounding gives $100.00, per-LINE gives $99.99 — the report must match
            # the P&L, which rounds per line.
            iid = d.execute("INSERT INTO invoices(location_id,vendor,invoice_date,subtotal,tax,total) "
                            "VALUES(1,'V','2026-06-05',100,8,108)").lastrowid
            for _ in range(3):
                d.execute("INSERT INTO invoice_items(invoice_id,name,total,category_id) "
                          "VALUES(?,'x',36.0,?)", (iid, wine))
            d.commit()
            cr = reports.category_report(dt.date(2026, 6, 1), dt.date(2026, 6, 30))
            pl = reports.controllable_pl(dt.date(2026, 6, 1), dt.date(2026, 6, 30))
        self.assertEqual(cr["grand_total"], pl["total_cogs"])   # same per-line granularity
        self.assertEqual(cr["grand_total"], 99.99)              # 3 * round(36*100/108) = 9999c


class RecipeNameRequired(Base):
    def test_blank_recipe_name_rejected(self):
        r = self.client.post("/api/recipes", json={"menu_price": 5, "yield_qty": 1, "items": []})
        self.assertEqual(r.status_code, 400)
        rid = self.client.post("/api/recipes", json={"name": "Real", "menu_price": 5,
                                                     "yield_qty": 1, "items": []}).get_json()["id"]
        upd = self.client.put(f"/api/recipes/{rid}", json={"name": "", "menu_price": 5,
                                                           "yield_qty": 1, "items": []})
        self.assertEqual(upd.status_code, 400)


class ImporterRelinksRecipes(Base):
    def test_reimport_preserves_recipe_product_link(self):
        import import_marginedge
        with flask_app.app_context():
            d = get_db()
            pid = d.execute("INSERT INTO inventory_items(location_id,name,unit_cost) "
                            "VALUES(1,'Gin',20)").lastrowid
            rid = d.execute("INSERT INTO recipes(location_id,name,menu_price,yield_qty) "
                            "VALUES(1,'Martini',12,1)").lastrowid
            riid = d.execute("INSERT INTO recipe_items(recipe_id,product_id,qty,unit) "
                             "VALUES(?,?,1,'each')", (rid, pid)).lastrowid
            d.commit()
            imp = import_marginedge.Importer(d, 1)
            imp.clear_location()                       # wipes inventory_items -> SET NULL on the link
            imp.import_products([{"Name": "Gin", "Latest Price": "22"}])
            imp.relink_recipes()
            d.commit()
            row = d.execute("SELECT product_id FROM recipe_items WHERE id=?", (riid,)).fetchone()
            newgin = d.execute("SELECT id FROM inventory_items WHERE location_id IS 1 "
                               "AND name='Gin'").fetchone()["id"]
        self.assertEqual(row["product_id"], newgin)    # re-linked by name, not left NULL


class NetSalesRefundedOrderFrame(Base):
    def test_net_amounts_frame_wins_over_order_level_totals(self):
        # a partially-refunded order carries BOTH net_amounts AND order-level total_*;
        # the net_amounts frame must win and total_money must be ignored (never mixed).
        order = {"net_amounts": {"total_money": {"amount": 1180},
                                 "tax_money": {"amount": 100},
                                 "tip_money": {"amount": 80}},
                 "total_money": {"amount": 9999}}
        self.assertEqual(square_client._net_sales_cents(order), 1000)   # 1180-100-80, not 9999


class ShiftCostClamp(Base):
    def test_negative_duration_shift_clamps_to_zero(self):
        bad = {"start_at": "2026-06-01T20:00:00Z", "end_at": "2026-06-01T18:00:00Z"}  # end before start
        self.assertEqual(square_client._shift_cost(bad, 15.0), (0.0, 0.0, 0.0))

    def test_breaks_longer_than_shift_clamp_to_zero(self):
        s = {"start_at": "2026-06-01T18:00:00Z", "end_at": "2026-06-01T19:00:00Z",
             "wage": {"hourly_rate": {"amount": 1500}},
             "breaks": [{"start_at": "2026-06-01T18:00:00Z", "end_at": "2026-06-01T21:00:00Z"}]}
        cost, hours, _ = square_client._shift_cost(s, 0.0)
        self.assertEqual((cost, hours), (0.0, 0.0))   # 3h unpaid break > 1h shift -> floored


class ActiveLocationPutValidation(AuthGateCoverage):
    def test_archived_and_unknown_ids_rejected(self):
        token = self.client.post("/api/login", json={"password": "secret"}).get_json()["token"]
        hdr = {"Authorization": "Bearer " + token}
        with flask_app.app_context():
            d = get_db()
            arch = d.execute("INSERT INTO locations(name, archived) VALUES('Closed',1)").lastrowid
            d.commit()
        self.assertEqual(self.client.put("/api/active-location", json={"location_id": arch},
                                         headers=hdr).status_code, 400)
        self.assertEqual(self.client.put("/api/active-location", json={"location_id": 99999},
                                         headers=hdr).status_code, 400)


if __name__ == "__main__":
    unittest.main()

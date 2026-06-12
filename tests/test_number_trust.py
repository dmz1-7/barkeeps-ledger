"""Tests for the number-trust fix batch.

Covers the four fixes:
  1. Net sales basis excludes tax / tips / service charges.
  2. The daily-sales cache is never overwritten with zeros on a failed fetch.
  4. Duplicate invoices are caught (and can be overridden).
  5. By-id endpoints are scoped to the active location.

Pure stdlib unittest — no pytest needed:

    .venv/bin/python -m unittest discover -s tests
"""
import datetime as dt
import os
import tempfile
import unittest

# Point at a throwaway DB and disable the auth gate BEFORE importing the app, so
# we never touch the real data/ledger.db and the test client isn't challenged.
os.environ["LEDGER_DB"] = tempfile.mktemp(suffix=".db")
os.environ["APP_PASSWORD"] = ""

import db                       # noqa: E402
import square_client            # noqa: E402
from db import get_db           # noqa: E402
import app as app_module        # noqa: E402

flask_app = app_module.app


class Base(unittest.TestCase):
    def setUp(self):
        os.environ["APP_PASSWORD"] = ""
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db.DB_PATH = self.db_path                 # get_db() reads this at call time
        with flask_app.app_context():
            db.init_db()                          # creates schema + seeds DC(1)/NYC(2)
        self.client = flask_app.test_client()

    def tearDown(self):
        try:
            os.unlink(self.db_path)
        except OSError:
            pass


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
        db.set_setting("square_token", "x")
        db.set_setting("square_location_id", "LOC1")

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


if __name__ == "__main__":
    unittest.main()

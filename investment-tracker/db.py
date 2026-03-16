import sqlite3
import os
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), "instance", "investments.db")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS transactions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker          TEXT    NOT NULL,
            name            TEXT    NOT NULL DEFAULT '',
            asset_type      TEXT    NOT NULL DEFAULT 'Share',
            transaction_type TEXT   NOT NULL,
            quantity        REAL    NOT NULL,
            price_per_unit  REAL    NOT NULL,
            currency        TEXT    NOT NULL DEFAULT 'CZK',
            date            DATE    NOT NULL,
            notes           TEXT    DEFAULT '',
            created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS price_cache (
            ticker       TEXT PRIMARY KEY,
            price        REAL,
            currency     TEXT,
            last_updated DATETIME
        );
    """)
    conn.commit()
    conn.close()


# ── Transactions ──────────────────────────────────────────────────────────────

def add_transaction(ticker, name, asset_type, transaction_type,
                    quantity, price_per_unit, currency, date, notes=""):
    conn = get_db()
    conn.execute(
        """INSERT INTO transactions
           (ticker, name, asset_type, transaction_type, quantity,
            price_per_unit, currency, date, notes)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (ticker.upper(), name, asset_type, transaction_type.upper(),
         quantity, price_per_unit, currency.upper(), date, notes)
    )
    conn.commit()
    conn.close()


def get_all_transactions(filters=None):
    conn = get_db()
    sql = "SELECT * FROM transactions WHERE 1=1"
    params = []
    if filters:
        if filters.get("ticker"):
            sql += " AND ticker = ?"
            params.append(filters["ticker"].upper())
        if filters.get("transaction_type"):
            sql += " AND transaction_type = ?"
            params.append(filters["transaction_type"].upper())
        if filters.get("asset_type"):
            sql += " AND asset_type = ?"
            params.append(filters["asset_type"])
        if filters.get("date_from"):
            sql += " AND date >= ?"
            params.append(filters["date_from"])
        if filters.get("date_to"):
            sql += " AND date <= ?"
            params.append(filters["date_to"])
    sql += " ORDER BY date DESC, id DESC"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return rows


def get_transaction(tx_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM transactions WHERE id=?", (tx_id,)).fetchone()
    conn.close()
    return row


def update_transaction(tx_id, ticker, name, asset_type, transaction_type,
                       quantity, price_per_unit, currency, date, notes=""):
    conn = get_db()
    conn.execute(
        """UPDATE transactions SET
           ticker=?, name=?, asset_type=?, transaction_type=?,
           quantity=?, price_per_unit=?, currency=?, date=?, notes=?
           WHERE id=?""",
        (ticker.upper(), name, asset_type, transaction_type.upper(),
         quantity, price_per_unit, currency.upper(), date, notes, tx_id)
    )
    conn.commit()
    conn.close()


def delete_transaction(tx_id):
    conn = get_db()
    conn.execute("DELETE FROM transactions WHERE id=?", (tx_id,))
    conn.commit()
    conn.close()


# ── Portfolio aggregation ─────────────────────────────────────────────────────

def get_open_lots():
    """Return open buy lots using FIFO matching against sells.
    Returns list of dicts with: ticker, name, asset_type, currency,
    date, quantity, price_per_unit, cost_basis"""
    conn = get_db()

    # Group buys per ticker chronologically
    buys = conn.execute(
        """SELECT id, ticker, name, asset_type, currency, date, quantity, price_per_unit
           FROM transactions WHERE transaction_type='BUY'
           ORDER BY ticker, date ASC, id ASC"""
    ).fetchall()

    sells = conn.execute(
        """SELECT ticker, SUM(quantity) as total_sold
           FROM transactions WHERE transaction_type='SELL'
           GROUP BY ticker"""
    ).fetchall()
    conn.close()

    sold_map = {r["ticker"]: r["total_sold"] for r in sells}

    # FIFO: consume oldest lots first
    open_lots = []
    buys_by_ticker = {}
    for b in buys:
        buys_by_ticker.setdefault(b["ticker"], []).append(dict(b))

    for ticker, lots in buys_by_ticker.items():
        remaining_sold = sold_map.get(ticker, 0.0)
        for lot in lots:
            qty = lot["quantity"]
            if remaining_sold >= qty:
                remaining_sold -= qty
                continue
            open_qty = qty - remaining_sold
            remaining_sold = 0.0
            open_lots.append({
                **lot,
                "quantity": open_qty,
                "cost_basis": open_qty * lot["price_per_unit"],
            })

    return open_lots


def get_dividends():
    conn = get_db()
    rows = conn.execute(
        """SELECT * FROM transactions WHERE transaction_type='DIVIDEND'
           ORDER BY date DESC"""
    ).fetchall()
    conn.close()
    return rows


def get_realized_pnl():
    """Calculate realized P&L per ticker using FIFO."""
    conn = get_db()
    all_tx = conn.execute(
        """SELECT * FROM transactions WHERE transaction_type IN ('BUY','SELL')
           ORDER BY ticker, date ASC, id ASC"""
    ).fetchall()
    conn.close()

    results = {}
    buys_by_ticker = {}
    for tx in all_tx:
        t = dict(tx)
        ticker = t["ticker"]
        if t["transaction_type"] == "BUY":
            buys_by_ticker.setdefault(ticker, []).append({"qty": t["quantity"], "price": t["price_per_unit"]})
        else:  # SELL
            sell_qty = t["quantity"]
            sell_proceeds = sell_qty * t["price_per_unit"]
            cost = 0.0
            lots = buys_by_ticker.get(ticker, [])
            remaining = sell_qty
            for lot in lots:
                if lot["qty"] == 0:
                    continue
                take = min(lot["qty"], remaining)
                cost += take * lot["price"]
                lot["qty"] -= take
                remaining -= take
                if remaining == 0:
                    break
            pnl = sell_proceeds - cost
            results.setdefault(ticker, 0.0)
            results[ticker] += pnl

    return results

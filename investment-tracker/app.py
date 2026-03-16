from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from datetime import date

import db as database
from tax import enrich_lots_with_tax, get_tax_status
from prices import get_price, refresh_prices

app = Flask(__name__)
app.secret_key = "dev-secret-change-in-production"

ASSET_TYPES = ["ETF", "Share", "Bond", "Other"]
CURRENCIES = ["CZK", "EUR", "USD", "GBP", "CHF"]
TX_TYPES = ["BUY", "SELL", "DIVIDEND"]


@app.before_request
def ensure_db():
    database.init_db()


# ── Portfolio overview ────────────────────────────────────────────────────────

@app.route("/")
def portfolio():
    lots = database.get_open_lots()
    realized_pnl = database.get_realized_pnl()

    # Attach current prices and tax status
    tickers = list({lot["ticker"] for lot in lots})
    price_data = {t: get_price(t) for t in tickers}

    total_invested = 0.0
    total_current = 0.0
    holdings = {}

    for lot in lots:
        ticker = lot["ticker"]
        cost = lot["cost_basis"]
        p = price_data.get(ticker)
        current_price = p["price"] if p else None
        current_value = (lot["quantity"] * current_price) if current_price else None

        tax = get_tax_status(lot["date"])

        if ticker not in holdings:
            holdings[ticker] = {
                "ticker": ticker,
                "name": lot["name"],
                "asset_type": lot["asset_type"],
                "currency": lot["currency"],
                "quantity": 0.0,
                "cost_basis": 0.0,
                "current_value": 0.0 if current_value is not None else None,
                "current_price": current_price,
                "lots": [],
                # For overview we show the earliest lot's tax status
                "earliest_tax": tax,
            }

        h = holdings[ticker]
        h["quantity"] += lot["quantity"]
        h["cost_basis"] += cost
        if current_value is not None and h["current_value"] is not None:
            h["current_value"] += current_value
        elif current_value is None:
            h["current_value"] = None

        h["lots"].append({**lot, **tax})

        total_invested += cost
        if current_value is not None:
            total_current += current_value

    for h in holdings.values():
        if h["current_value"] is not None:
            h["unrealized_pnl"] = h["current_value"] - h["cost_basis"]
            h["pnl_pct"] = (h["unrealized_pnl"] / h["cost_basis"] * 100) if h["cost_basis"] else 0
        else:
            h["unrealized_pnl"] = None
            h["pnl_pct"] = None
        h["avg_cost"] = h["cost_basis"] / h["quantity"] if h["quantity"] else 0

    total_unrealized = total_current - total_invested if total_current else None

    dividends = database.get_dividends()
    total_dividends = sum(d["quantity"] * d["price_per_unit"] for d in dividends)

    total_realized = sum(realized_pnl.values())

    # Asset allocation for chart
    alloc = {}
    for h in holdings.values():
        at = h["asset_type"]
        alloc[at] = alloc.get(at, 0) + h["cost_basis"]

    return render_template(
        "portfolio.html",
        holdings=list(holdings.values()),
        total_invested=total_invested,
        total_current=total_current,
        total_unrealized=total_unrealized,
        total_dividends=total_dividends,
        total_realized=total_realized,
        alloc=alloc,
    )


# ── Transactions ──────────────────────────────────────────────────────────────

@app.route("/transactions")
def transactions():
    filters = {
        "ticker": request.args.get("ticker", "").strip() or None,
        "transaction_type": request.args.get("type", "").strip() or None,
        "asset_type": request.args.get("asset_type", "").strip() or None,
        "date_from": request.args.get("date_from", "").strip() or None,
        "date_to": request.args.get("date_to", "").strip() or None,
    }
    rows = database.get_all_transactions(filters)
    return render_template(
        "transactions.html",
        transactions=rows,
        filters=filters,
        asset_types=ASSET_TYPES,
        tx_types=TX_TYPES,
    )


@app.route("/transactions/add", methods=["GET", "POST"])
def add_transaction():
    if request.method == "POST":
        try:
            database.add_transaction(
                ticker=request.form["ticker"].strip(),
                name=request.form.get("name", "").strip(),
                asset_type=request.form["asset_type"],
                transaction_type=request.form["transaction_type"],
                quantity=float(request.form["quantity"]),
                price_per_unit=float(request.form["price_per_unit"]),
                currency=request.form["currency"],
                date=request.form["date"],
                notes=request.form.get("notes", "").strip(),
            )
            flash("Transaction added.", "success")
            return redirect(url_for("transactions"))
        except (ValueError, KeyError) as e:
            flash(f"Error: {e}", "danger")

    return render_template(
        "transaction_form.html",
        tx=None,
        action=url_for("add_transaction"),
        asset_types=ASSET_TYPES,
        currencies=CURRENCIES,
        tx_types=TX_TYPES,
        today=date.today().isoformat(),
    )


@app.route("/transactions/<int:tx_id>/edit", methods=["GET", "POST"])
def edit_transaction(tx_id):
    tx = database.get_transaction(tx_id)
    if tx is None:
        flash("Transaction not found.", "danger")
        return redirect(url_for("transactions"))

    if request.method == "POST":
        try:
            database.update_transaction(
                tx_id=tx_id,
                ticker=request.form["ticker"].strip(),
                name=request.form.get("name", "").strip(),
                asset_type=request.form["asset_type"],
                transaction_type=request.form["transaction_type"],
                quantity=float(request.form["quantity"]),
                price_per_unit=float(request.form["price_per_unit"]),
                currency=request.form["currency"],
                date=request.form["date"],
                notes=request.form.get("notes", "").strip(),
            )
            flash("Transaction updated.", "success")
            return redirect(url_for("transactions"))
        except (ValueError, KeyError) as e:
            flash(f"Error: {e}", "danger")

    return render_template(
        "transaction_form.html",
        tx=tx,
        action=url_for("edit_transaction", tx_id=tx_id),
        asset_types=ASSET_TYPES,
        currencies=CURRENCIES,
        tx_types=TX_TYPES,
        today=date.today().isoformat(),
    )


@app.route("/transactions/<int:tx_id>/delete", methods=["POST"])
def delete_transaction(tx_id):
    database.delete_transaction(tx_id)
    flash("Transaction deleted.", "info")
    return redirect(url_for("transactions"))


# ── Tax time test ─────────────────────────────────────────────────────────────

@app.route("/tax")
def tax():
    lots = database.get_open_lots()
    enrich_lots_with_tax(lots)
    lots.sort(key=lambda l: l["tax_free_date"])

    tax_free = [l for l in lots if l["is_tax_free"]]
    not_yet = [l for l in lots if not l["is_tax_free"]]

    return render_template("tax.html", lots=lots, tax_free=tax_free, not_yet=not_yet)


# ── Dividends ─────────────────────────────────────────────────────────────────

@app.route("/dividends")
def dividends():
    rows = database.get_dividends()
    # Group by year
    by_year = {}
    for r in rows:
        year = r["date"][:4]
        total = r["quantity"] * r["price_per_unit"]
        by_year.setdefault(year, {"total": 0.0, "rows": []})
        by_year[year]["total"] += total
        by_year[year]["rows"].append(r)

    return render_template("dividends.html", by_year=by_year, all_rows=rows)


# ── API ───────────────────────────────────────────────────────────────────────

@app.route("/api/prices/refresh", methods=["POST"])
def api_refresh_prices():
    lots = database.get_open_lots()
    tickers = list({lot["ticker"] for lot in lots})
    results = refresh_prices(tickers)
    return jsonify({t: v for t, v in results.items() if v})


if __name__ == "__main__":
    database.init_db()
    app.run(debug=True, port=5000)

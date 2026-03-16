import json
import os
import shutil
import tempfile
from datetime import date
from io import BytesIO

# Fix: curl_cffi (used by yfinance) fails when the certifi CA bundle path
# contains non-ASCII characters (e.g. Windows username "Uživatel").
# Copy cacert.pem to the system temp directory (uses 8.3 short path = ASCII-safe).
try:
    import certifi as _certifi
    _dst = os.path.join(tempfile.gettempdir(), "cacert.pem")
    if not os.path.exists(_dst):
        shutil.copy2(_certifi.where(), _dst)
    os.environ.setdefault("CURL_CA_BUNDLE", _dst)
    os.environ.setdefault("REQUESTS_CA_BUNDLE", _dst)
except Exception:
    pass

from flask import (Flask, render_template, request, redirect,
                   url_for, flash, jsonify, session, send_file, g)
from fpdf import FPDF

import db as database
from tax import enrich_lots_with_tax, get_tax_status
from prices import get_price, refresh_prices
from metadata import get_metadata, upsert_metadata
import forex
from xtb_import import parse_xtb_csv

app = Flask(__name__)
app.secret_key = "dev-secret-change-in-production"

ASSET_TYPES = ["ETF", "Share", "Bond", "Other"]
CURRENCIES = ["CZK", "EUR", "USD", "GBP", "CHF"]
TX_TYPES = ["BUY", "SELL", "DIVIDEND"]
DISPLAY_CURRENCIES = ["CZK", "EUR", "USD"]

CURRENCY_SYMBOLS = {"CZK": "Kč", "EUR": "€", "USD": "$", "GBP": "£", "CHF": "Fr"}


@app.before_request
def ensure_db():
    database.init_db()
    database.migrate_db()


@app.context_processor
def inject_globals():
    """Inject display currency into every template."""
    dc = session.get("display_currency", "CZK")
    return {
        "display_currency": dc,
        "display_currencies": DISPLAY_CURRENCIES,
        "currency_symbol": CURRENCY_SYMBOLS.get(dc, dc),
    }


# ── Portfolio overview ────────────────────────────────────────────────────────

@app.route("/")
def portfolio():
    display_currency = session.get("display_currency", "CZK")
    lots = database.get_open_lots()
    realized_pnl = database.get_realized_pnl()

    # Attach current prices, tax status, and metadata
    tickers = list({lot["ticker"] for lot in lots})
    price_data = {t: get_price(t) for t in tickers}
    meta_data = {t: get_metadata(t) for t in tickers}

    total_invested = 0.0
    total_current = 0.0
    holdings = {}

    for lot in lots:
        ticker = lot["ticker"]
        orig_currency = lot["currency"]
        cost = lot["cost_basis"]
        cost_display = forex.convert(cost, orig_currency, display_currency)

        p = price_data.get(ticker)
        price_currency = p["currency"] if p else orig_currency
        current_price = p["price"] if p else None
        current_value = (lot["quantity"] * current_price) if current_price else None
        current_value_display = forex.convert(current_value, price_currency, display_currency) if current_value is not None else None

        tax = get_tax_status(lot["date"], hold_years=lot.get("hold_years", 3))
        meta = meta_data.get(ticker, {"sector": "", "geography": ""})

        if ticker not in holdings:
            holdings[ticker] = {
                "ticker": ticker,
                "name": lot["name"],
                "asset_type": lot["asset_type"],
                "currency": orig_currency,
                "sector": meta.get("sector", ""),
                "geography": meta.get("geography", ""),
                "quantity": 0.0,
                "cost_basis": 0.0,
                "cost_basis_orig": 0.0,
                "current_value": 0.0 if current_value_display is not None else None,
                "current_price": current_price,
                "lots": [],
                "earliest_tax": tax,
            }

        h = holdings[ticker]
        h["quantity"] += lot["quantity"]
        h["cost_basis"] += cost_display
        h["cost_basis_orig"] += cost
        if current_value_display is not None and h["current_value"] is not None:
            h["current_value"] += current_value_display
        elif current_value_display is None:
            h["current_value"] = None

        h["lots"].append({**dict(lot), **tax})

        total_invested += cost_display
        if current_value_display is not None:
            total_current += current_value_display

    for h in holdings.values():
        if h["current_value"] is not None:
            h["unrealized_pnl"] = h["current_value"] - h["cost_basis"]
            h["pnl_pct"] = (h["unrealized_pnl"] / h["cost_basis"] * 100) if h["cost_basis"] else 0
        else:
            h["unrealized_pnl"] = None
            h["pnl_pct"] = None
        h["avg_cost"] = h["cost_basis_orig"] / h["quantity"] if h["quantity"] else 0

    total_unrealized = total_current - total_invested if total_current else None

    dividends = database.get_dividends()
    total_dividends = sum(
        forex.convert(d["quantity"] * d["price_per_unit"], d["currency"], display_currency)
        for d in dividends
    )

    total_realized = sum(realized_pnl.values())

    # Build three allocation dicts (by type, sector, geography)
    alloc = {}
    alloc_sector = {}
    alloc_geo = {}
    for h in holdings.values():
        cb = h["cost_basis"]
        at = h["asset_type"] or "Other"
        sec = h["sector"] or "Unknown"
        geo = h["geography"] or "Unknown"
        alloc[at] = alloc.get(at, 0) + cb
        alloc_sector[sec] = alloc_sector.get(sec, 0) + cb
        alloc_geo[geo] = alloc_geo.get(geo, 0) + cb

    # Check for any holdings needing review
    needs_review_count = sum(
        1 for lot in lots if lot.get("needs_review")
    )

    return render_template(
        "portfolio.html",
        holdings=list(holdings.values()),
        total_invested=total_invested,
        total_current=total_current,
        total_unrealized=total_unrealized,
        total_dividends=total_dividends,
        total_realized=total_realized,
        alloc=alloc,
        alloc_sector=alloc_sector,
        alloc_geo=alloc_geo,
        needs_review_count=needs_review_count,
    )


# ── Currency switcher ─────────────────────────────────────────────────────────

@app.route("/set-currency", methods=["POST"])
def set_currency():
    c = request.form.get("currency", "CZK")
    if c in DISPLAY_CURRENCIES:
        session["display_currency"] = c
    return redirect(request.referrer or url_for("portfolio"))


# ── Transactions ──────────────────────────────────────────────────────────────

@app.route("/transactions")
def transactions():
    filters = {
        "ticker": request.args.get("ticker", "").strip() or None,
        "transaction_type": request.args.get("type", "").strip() or None,
        "asset_type": request.args.get("asset_type", "").strip() or None,
        "date_from": request.args.get("date_from", "").strip() or None,
        "date_to": request.args.get("date_to", "").strip() or None,
        "needs_review": request.args.get("needs_review", "").strip() or None,
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
        ticker = request.form["ticker"].strip().upper()
        sector = request.form.get("sector", "").strip()
        geography = request.form.get("geography", "").strip()
        try:
            hold_years = max(1, min(99, int(request.form.get("hold_years", 3) or 3)))
        except ValueError:
            hold_years = 3

        try:
            database.add_transaction(
                ticker=ticker,
                name=request.form.get("name", "").strip(),
                asset_type=request.form["asset_type"],
                transaction_type=request.form["transaction_type"],
                quantity=float(request.form["quantity"]),
                price_per_unit=float(request.form["price_per_unit"]),
                currency=request.form["currency"],
                date=request.form["date"],
                notes=request.form.get("notes", "").strip(),
                hold_years=hold_years,
            )
            upsert_metadata(ticker, sector, geography)
            flash("Transaction added.", "success")
            return redirect(url_for("transactions"))
        except (ValueError, KeyError) as e:
            flash(f"Error: {e}", "danger")

    ticker = request.args.get("ticker", "").upper()
    metadata = get_metadata(ticker) if ticker else None
    return render_template(
        "transaction_form.html",
        tx=None,
        action=url_for("add_transaction"),
        asset_types=ASSET_TYPES,
        currencies=CURRENCIES,
        tx_types=TX_TYPES,
        today=date.today().isoformat(),
        metadata=metadata,
    )


@app.route("/transactions/<int:tx_id>/edit", methods=["GET", "POST"])
def edit_transaction(tx_id):
    tx = database.get_transaction(tx_id)
    if tx is None:
        flash("Transaction not found.", "danger")
        return redirect(url_for("transactions"))

    if request.method == "POST":
        ticker = request.form["ticker"].strip().upper()
        sector = request.form.get("sector", "").strip()
        geography = request.form.get("geography", "").strip()
        try:
            hold_years = max(1, min(99, int(request.form.get("hold_years", 3) or 3)))
        except ValueError:
            hold_years = 3

        try:
            database.update_transaction(
                tx_id=tx_id,
                ticker=ticker,
                name=request.form.get("name", "").strip(),
                asset_type=request.form["asset_type"],
                transaction_type=request.form["transaction_type"],
                quantity=float(request.form["quantity"]),
                price_per_unit=float(request.form["price_per_unit"]),
                currency=request.form["currency"],
                date=request.form["date"],
                notes=request.form.get("notes", "").strip(),
                hold_years=hold_years,
                needs_review=0,  # editing clears the review flag
            )
            upsert_metadata(ticker, sector, geography)
            flash("Transaction updated.", "success")
            return redirect(url_for("transactions"))
        except (ValueError, KeyError) as e:
            flash(f"Error: {e}", "danger")

    metadata = get_metadata(tx["ticker"])
    # Override with form values if the metadata was manually set
    return render_template(
        "transaction_form.html",
        tx=tx,
        action=url_for("edit_transaction", tx_id=tx_id),
        asset_types=ASSET_TYPES,
        currencies=CURRENCIES,
        tx_types=TX_TYPES,
        today=date.today().isoformat(),
        metadata=metadata,
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

    # Years that have SELL transactions (for Příloha 2 picker)
    sell_years = database.get_sell_years()
    if not sell_years:
        sell_years = [date.today().year]

    return render_template(
        "tax.html",
        lots=lots,
        tax_free=tax_free,
        not_yet=not_yet,
        sell_years=sell_years,
    )


# ── Dividends ─────────────────────────────────────────────────────────────────

@app.route("/dividends")
def dividends():
    rows = database.get_dividends()
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


# ── Příloha 2 PDF export ──────────────────────────────────────────────────────

@app.route("/export/priloha2")
def export_priloha2():
    year = request.args.get("year", date.today().year, type=int)
    rows = database.get_realized_pnl_detail(year)
    pdf_bytes = _generate_priloha2_pdf(rows, year)
    return send_file(
        BytesIO(pdf_bytes),
        mimetype="application/pdf",
        download_name=f"priloha2_{year}.pdf",
        as_attachment=True,
    )


def _generate_priloha2_pdf(rows: list, year: int) -> bytes:
    """Generate Příloha 2 PDF for Czech tax bureau."""
    pdf = FPDF(orientation="L", format="A4")
    pdf.set_margins(10, 10, 10)
    pdf.add_page()

    # Title
    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 8, f"Příloha č. 2 – Přehled prodejů cenných papírů ({year})", ln=True, align="C")
    pdf.set_font("Helvetica", "", 8)
    pdf.cell(0, 5, "§ 10 zákona č. 586/1992 Sb. (Zákon o daních z příjmů) – Ostatní příjmy", ln=True, align="C")
    pdf.ln(3)

    # Column widths (landscape A4 = ~277mm usable)
    cols = [
        ("Ticker", 20),
        ("Název", 55),
        ("Datum nákupu", 28),
        ("Datum prodeje", 28),
        ("Množství", 20),
        ("Nákupní cena", 30),
        ("Prodejní cena", 30),
        ("Zisk / Ztráta", 30),
    ]

    # Header row
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_fill_color(220, 230, 241)
    for label, w in cols:
        pdf.cell(w, 7, label, border=1, fill=True, align="C")
    pdf.ln()

    # Data rows
    pdf.set_font("Helvetica", "", 8)
    total_gain = 0.0
    total_buy = 0.0
    total_sell = 0.0

    fill = False
    pdf.set_fill_color(245, 248, 252)
    for r in rows:
        gain = r["gain_loss"]
        total_gain += gain
        total_buy += r["buy_total"]
        total_sell += r["sell_total"]
        gain_str = f"{gain:+.2f}"

        values = [
            r["ticker"],
            r["name"][:28] if r["name"] else "",
            r["buy_date"],
            r["sell_date"],
            f"{r['quantity']:.4f}",
            f"{r['buy_total']:.2f}",
            f"{r['sell_total']:.2f}",
            gain_str,
        ]
        for (_, w), val in zip(cols, values):
            pdf.cell(w, 6, val, border=1, fill=fill)
        pdf.ln()
        fill = not fill

    # Totals row
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_fill_color(220, 230, 241)
    totals = ["", "CELKEM", "", "", "",
              f"{total_buy:.2f}", f"{total_sell:.2f}", f"{total_gain:+.2f}"]
    for (_, w), val in zip(cols, totals):
        pdf.cell(w, 7, val, border=1, fill=True, align="R" if val else "L")
    pdf.ln()

    if not rows:
        pdf.ln(4)
        pdf.set_font("Helvetica", "I", 9)
        pdf.cell(0, 6, f"Žádné realizované prodeje v roce {year}.", ln=True, align="C")

    pdf.ln(4)
    pdf.set_font("Helvetica", "", 7)
    pdf.cell(0, 4,
             "Vygenerováno aplikací Investment Tracker. Zkontrolujte údaje před podáním daňového přiznání.",
             ln=True, align="C")

    return bytes(pdf.output())


# ── XTB Import ────────────────────────────────────────────────────────────────

@app.route("/import", methods=["GET", "POST"])
def import_xtb():
    if request.method == "GET":
        return render_template("import.html")

    file = request.files.get("file")
    action = request.form.get("action", "preview")

    if action == "preview":
        if not file or not file.filename:
            flash("Please select a CSV file.", "danger")
            return render_template("import.html")
        try:
            importable, skipped = parse_xtb_csv(file.stream)
        except Exception as e:
            flash(f"Could not parse file: {e}", "danger")
            return render_template("import.html")

        session["import_data"] = json.dumps(importable)
        return render_template(
            "import.html",
            importable=importable,
            skipped=skipped,
            preview=True,
        )

    elif action == "confirm":
        rows = json.loads(session.pop("import_data", "[]"))
        if not rows:
            flash("Nothing to import. Please upload the file again.", "warning")
            return redirect(url_for("import_xtb"))
        for row in rows:
            database.add_transaction(**row)
        flash(f"Successfully imported {len(rows)} transaction(s). "
              "Please review flagged transactions and update quantities/prices.",
              "success")
        return redirect(url_for("transactions") + "?needs_review=1")

    return redirect(url_for("import_xtb"))


if __name__ == "__main__":
    database.init_db()
    database.migrate_db()
    app.run(debug=True, port=5000)

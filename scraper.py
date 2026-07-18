#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
paasi — reconstruction des contributions de l'indice MSCI EM Asia
(proxy de composition : iShares MSCI EM Asia UCITS ETF, réplication physique,
 IE00B5L8K969, produit 253723 sur ishares.com — PAASI étant synthétique,
 Amundi ne publie que le panier de substitution, inutilisable).

Pipeline :
 1. Télécharge la composition complète (CSV) depuis ishares.com
    (lien .ajax découvert dynamiquement dans le HTML de la page produit).
 2. Garde le top N actions par poids.
 3. Mappe chaque ligne vers un ticker Yahoo (bourse locale).
 4. Récupère ~3 mois de clôtures Yahoo par ticker + taux EUR/devise.
 5. Calcule la perf 30 jours EN EUR et la contribution = poids x perf.
 6. Écrit contributions.json (consommé par index.html).

Sortie : contributions.json
{
  "generated": "YYYY-MM-DDTHH:MM:SSZ",
  "holdings_date": "YYYY-MM-DD",
  "window_days": 30,
  "source": "iShares MSCI EM Asia UCITS ETF (proxy)",
  "covered_weight": 83.2,
  "stocks": [
    {"name": "...", "yahoo": "2330.TW", "isin": "...", "currency": "TWD",
     "country": "...", "sector": "...", "weight": 10.5,
     "perf_local": 0.061, "perf_eur": 0.055, "contrib": 0.577,
     "status": "ok"|"no_ticker"|"no_history"},
    ...
  ]
}
Les poids sont en % ; contribution en points de % d'indice.
"""

import json
import re
import sys
import time
import datetime as dt

try:
    from curl_cffi import requests as creq
    HAVE_CFFI = True
except ImportError:
    import requests as creq  # repli
    HAVE_CFFI = False

# ----------------------------------------------------------------------
# Paramètres
# ----------------------------------------------------------------------
TOP_N = 60           # nb de lignes gardées (couvre l'essentiel du poids)
WINDOW_DAYS = 30     # fenêtre de contribution
OUT_FILE = "contributions.json"

ISHARES_PRODUCT_PAGE = (
    "https://www.ishares.com/uk/individual/en/products/253723/"
    "ishares-msci-em-asia-ucits-etf"
    "?switchLocale=y&siteEntryPassthrough=true"
)
ISHARES_BASE = "https://www.ishares.com"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# ----------------------------------------------------------------------
# Mapping bourse locale -> suffixe Yahoo
# ----------------------------------------------------------------------
def yahoo_ticker(local_ticker, ccy, exchange, name):
    """Retourne le ticker Yahoo ou None si non mappable."""
    t = (local_ticker or "").strip().upper()
    ccy = (ccy or "").strip().upper()
    ex = (exchange or "").upper()

    if not t:
        return None

    # Overrides manuels (à compléter au fil de l'eau si status no_ticker)
    OVERRIDES = {
        # "TICKER_LOCAL": "TICKER.YAHOO",
    }
    if t in OVERRIDES:
        return OVERRIDES[t]

    if ccy == "TWD":
        # TWSE -> .TW ; OTC/Taipei Exchange -> .TWO
        suf = ".TWO" if ("TPEX" in ex or "OTC" in ex or "TAIPEI EXCH" in ex) else ".TW"
        return t + suf
    if ccy == "KRW":
        suf = ".KQ" if "KOSDAQ" in ex else ".KS"
        return t.zfill(6) + suf
    if ccy == "HKD":
        return t.zfill(4) + ".HK"
    if ccy == "CNY" or ccy == "CNH":
        if t.startswith("6"):
            return t + ".SS"
        return t + ".SZ"
    if ccy == "INR":
        return t + ".NS"
    if ccy == "IDR":
        return t + ".JK"
    if ccy == "THB":
        return t.replace("/F", "").replace("-R", "") + ".BK"
    if ccy == "MYR":
        return t + ".KL"
    if ccy == "PHP":
        return t + ".PS"
    if ccy == "SGD":
        return t + ".SI"
    if ccy == "USD":
        # ADR cotés US (rare dans ce fonds mais possible)
        return t
    return None


# ----------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------
def http_get(url, **kw):
    kw.setdefault("timeout", 30)
    if HAVE_CFFI:
        kw.setdefault("impersonate", "chrome")
    else:
        kw.setdefault("headers", {"User-Agent": UA})
    return creq.get(url, **kw)


# ----------------------------------------------------------------------
# 1. Composition iShares
# ----------------------------------------------------------------------
def fetch_holdings_csv():
    r = http_get(ISHARES_PRODUCT_PAGE)
    r.raise_for_status()
    html = r.text
    # Lien du type: /uk/.../253723/.../1506575576011.ajax?fileType=csv&...&dataType=fund
    m = re.search(
        r'(/uk/[^"\']*253723[^"\']*\.ajax\?fileType=csv[^"\']*dataType=fund)',
        html)
    if not m:
        m = re.search(r'([^"\']*\.ajax\?fileType=csv[^"\']*dataType=fund)', html)
    if not m:
        raise RuntimeError("Lien CSV holdings introuvable dans la page iShares")
    csv_url = ISHARES_BASE + m.group(1).replace("&amp;", "&")
    print(f"[ishares] CSV: {csv_url}")
    r2 = http_get(csv_url)
    r2.raise_for_status()
    return r2.text


def parse_holdings(csv_text):
    """CSV iShares : préambule variable, puis en-tête Ticker,Name,...
    Retourne (holdings_date, [dict par ligne])."""
    lines = csv_text.splitlines()
    hdate = None
    header_idx = None
    for i, ln in enumerate(lines):
        if hdate is None:
            m = re.search(r'as of[,"\s]+([0-9]{1,2}-\w{3}-[0-9]{4})', ln)
            if m:
                try:
                    hdate = dt.datetime.strptime(m.group(1), "%d-%b-%Y").date().isoformat()
                except ValueError:
                    pass
        if ln.startswith("Ticker,") or ln.startswith('"Ticker"'):
            header_idx = i
            break
    if header_idx is None:
        raise RuntimeError("En-tête CSV iShares introuvable")

    import csv as csvmod
    reader = csvmod.reader(lines[header_idx:])
    header = next(reader)
    idx = {h.strip(): k for k, h in enumerate(header)}

    def col(row, *names):
        for n in names:
            if n in idx and idx[n] < len(row):
                return row[idx[n]].strip()
        return ""

    out = []
    for row in reader:
        if len(row) < 3:
            continue
        asset = col(row, "Asset Class")
        if asset and asset.upper() != "EQUITY":
            continue
        try:
            w = float(col(row, "Weight (%)").replace(",", ""))
        except ValueError:
            continue
        out.append({
            "ticker_local": col(row, "Ticker"),
            "name": col(row, "Name"),
            "sector": col(row, "Sector"),
            "isin": col(row, "ISIN"),
            "country": col(row, "Location"),
            "exchange": col(row, "Exchange"),
            "currency": col(row, "Market Currency", "Currency"),
            "weight": w,
        })
    out.sort(key=lambda x: -x["weight"])
    return hdate, out


# ----------------------------------------------------------------------
# 2. Historique Yahoo (cours + FX)
# ----------------------------------------------------------------------
def yahoo_history(symbol, range_="3mo"):
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
           f"?range={range_}&interval=1d")
    r = http_get(url)
    if r.status_code != 200:
        return None
    try:
        res = r.json()["chart"]["result"][0]
        ts = res["timestamp"]
        closes = res["indicators"]["quote"][0]["close"]
        return [(dt.date.fromtimestamp(t), c) for t, c in zip(ts, closes)
                if c is not None]
    except (KeyError, TypeError, IndexError):
        return None


def perf_over_window(series, window_days):
    """Perf entre la clôture la plus récente et la clôture la plus proche
    de (dernière date - window_days). Retourne None si données insuffisantes."""
    if not series or len(series) < 5:
        return None
    series = sorted(series)
    last_date, last_close = series[-1]
    target = last_date - dt.timedelta(days=window_days)
    ref = min(series, key=lambda p: abs((p[0] - target).days))
    if abs((ref[0] - target).days) > 7:
        return None
    if ref[1] in (None, 0):
        return None
    return last_close / ref[1] - 1.0


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------
def main():
    csv_text = fetch_holdings_csv()
    hdate, holdings = parse_holdings(csv_text)
    top = holdings[:TOP_N]
    covered = sum(h["weight"] for h in top)
    print(f"[compo] {len(holdings)} lignes actions, top {TOP_N} = "
          f"{covered:.1f}% du fonds, date {hdate}")

    # FX : une seule série par devise
    fx_cache = {}

    def fx_perf(ccy):
        """Perf de la devise locale contre EUR sur la fenêtre."""
        if ccy in ("EUR", "", None):
            return 0.0
        if ccy not in fx_cache:
            # EURTWD=X = TWD par EUR ; si TWD s'apprécie, EURTWD baisse
            s = yahoo_history(f"EUR{ccy}=X")
            p = perf_over_window(s, WINDOW_DAYS) if s else None
            fx_cache[ccy] = None if p is None else (1.0 / (1.0 + p) - 1.0)
            time.sleep(0.4)
        return fx_cache[ccy]

    stocks = []
    for h in top:
        y = yahoo_ticker(h["ticker_local"], h["currency"], h["exchange"], h["name"])
        entry = {
            "name": h["name"], "yahoo": y, "isin": h["isin"],
            "currency": h["currency"], "country": h["country"],
            "sector": h["sector"], "weight": round(h["weight"], 4),
            "perf_local": None, "perf_eur": None, "contrib": None,
            "status": "no_ticker",
        }
        if y:
            series = yahoo_history(y)
            p_loc = perf_over_window(series, WINDOW_DAYS) if series else None
            if p_loc is None:
                entry["status"] = "no_history"
            else:
                fxp = fx_perf(h["currency"])
                p_eur = p_loc if fxp is None else (1 + p_loc) * (1 + fxp) - 1
                entry.update({
                    "perf_local": round(p_loc, 5),
                    "perf_eur": round(p_eur, 5),
                    "contrib": round(h["weight"] * p_eur, 4),
                    "status": "ok",
                })
            time.sleep(0.4)
        else:
            print(f"[map] no_ticker: {h['ticker_local']} {h['name']} "
                  f"({h['currency']}, {h['exchange']})")
        stocks.append(entry)

    ok = [s for s in stocks if s["status"] == "ok"]
    print(f"[calc] {len(ok)}/{len(stocks)} lignes ok")

    data = {
        "generated": dt.datetime.now(dt.timezone.utc)
                       .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "holdings_date": hdate,
        "window_days": WINDOW_DAYS,
        "source": "iShares MSCI EM Asia UCITS ETF (proxy de composition)",
        "covered_weight": round(covered, 2),
        "stocks": stocks,
    }
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print(f"[out] {OUT_FILE} écrit")

    if len(ok) < 20:
        print("ERREUR: moins de 20 lignes exploitables", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

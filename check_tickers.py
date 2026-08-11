#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
paasi / check_tickers.py — validation de la liste de tickers candidats.

Ne calcule aucune attribution. Se contente de repondre, pour chaque ticker :
  - Yahoo renvoie-t-il une serie exploitable ?
  - dans quelle devise, sur quelle place ?
  - la cotation est-elle liquide ou en escalier ?

Sortie : check_tickers.json  (+ tableau lisible dans le log Actions)
"""

import json
import statistics
import sys
import time
import datetime as dt

try:
    from curl_cffi import requests as creq
    HAVE_CFFI = True
except ImportError:
    import requests as creq
    HAVE_CFFI = False

RANGE = "6mo"
OUT_FILE = "check_tickers.json"

# Liste candidate : plusieurs tickers par agregat, on garde le meilleur ensuite.
# ("agregat", "role", "ticker Yahoo")
CANDIDATES = [
    # --- references a expliquer ---
    ("reference", "PAASI (ma ligne)",            "PAASI.PA"),
    ("reference", "WPEA (ma ligne)",             "WPEA.PA"),

    # --- agregats PAASI retenus au 1er tour ---
    ("tw", "iShares MSCI Taiwan (Amsterdam)",    "ITWN.AS"),
    ("kr", "Amundi MSCI Korea (Paris)",          "KRW.PA"),
    ("cn", "iShares MSCI China (Xetra)",         "ICGA.DE"),
    ("in", "Amundi PEA Inde (Paris)",            "PINR.PA"),

    # --- Inde : chercher un MSCI India physique en euros ---
    ("in", "iShares MSCI India (Xetra)",         "IIND.DE"),
    ("in", "iShares MSCI India (Londres)",       "NDIA.L"),
    ("in", "Amundi MSCI India (Xetra)",          "18MF.DE"),

    # --- Asie : agregat ASEAN a trouver ---
    ("asean", "Xtrackers MSCI Indonesia",        "XMID.DE"),
    ("asean", "HSBC MSCI Indonesia",             "HIDR.L"),
    ("asean", "Amundi MSCI Malaysia",            "LYMY.DE"),
    ("asean", "iShares MSCI Thailand",           "ITKY.L"),

    # --- agregats MSCI World retenus au 1er tour ---
    ("w_tech",   "Xtrackers World Info Tech",    "XDWT.DE"),
    ("w_fin",    "Xtrackers World Financials",   "XDWF.DE"),
    ("w_sante",  "Xtrackers World Health Care",  "XDWH.DE"),
    ("w_indus",  "Xtrackers World Industrials",  "XDWI.DE"),
    ("w_conso",  "Xtrackers World Cons Discr",   "XDWC.DE"),
    ("w_comm",   "Xtrackers World Comm Services", "XDWS.DE"),

    # --- World : secteurs manquants, famille Xtrackers ---
    ("w_energie", "Xtrackers World Energy",      "XDW0.DE"),
    ("w_materiaux", "Xtrackers World Materials", "XDWM.DE"),
    ("w_conso_base", "Xtrackers World Staples",  "XDWY.DE"),
    ("w_utilities", "Xtrackers World Utilities", "XDWU.DE"),
    ("w_immo", "Xtrackers World Real Estate",    "XDER.DE"),

    # --- World : repli famille SPDR si les Xtrackers manquent ---
    ("w_energie", "SPDR World Energy",           "WNRG.DE"),
    ("w_materiaux", "SPDR World Materials",      "WMAT.DE"),
    ("w_conso_base", "SPDR World Staples",       "WCOS.DE"),
    ("w_utilities", "SPDR World Utilities",      "WUTI.DE"),
    ("w_immo", "SPDR World Real Estate",         "WORD.DE"),

    # --- change (controle) ---
    ("fx", "EUR/USD", "EURUSD=X"),
]


def http_get(url, **kw):
    kw.setdefault("timeout", 30)
    if HAVE_CFFI:
        kw.setdefault("impersonate", "chrome")
    else:
        kw.setdefault("headers", {"User-Agent": "Mozilla/5.0"})
    return creq.get(url, **kw)


def probe(symbol):
    """Interroge Yahoo et renvoie un diagnostic pour un ticker."""
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
           f"?range={RANGE}&interval=1d")
    out = {"status": "ko", "detail": ""}
    try:
        r = http_get(url)
    except Exception as exc:
        out["detail"] = f"exception: {exc.__class__.__name__}"
        return out
    if r.status_code != 200:
        out["status"] = "http_error"
        out["detail"] = f"HTTP {r.status_code}"
        return out
    try:
        res = r.json()["chart"]["result"][0]
    except Exception:
        out["status"] = "no_data"
        out["detail"] = "reponse illisible ou ticker inconnu"
        return out

    meta = res.get("meta", {})
    stamps = res.get("timestamp") or []
    quote = (res.get("indicators", {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []
    vols = quote.get("volume") or []

    pairs = [(t, c, (vols[i] if i < len(vols) else None))
             for i, (t, c) in enumerate(zip(stamps, closes)) if c is not None]
    if len(pairs) < 20:
        out["status"] = "no_history"
        out["detail"] = f"{len(pairs)} cloture(s) exploitable(s)"
        out["currency"] = meta.get("currency")
        return out

    days = [dt.datetime.utcfromtimestamp(t).date() for t, _, _ in pairs]
    vals = [c for _, c, _ in pairs]
    volumes = [v for _, _, v in pairs if v]

    gaps = [(days[i] - days[i - 1]).days for i in range(1, len(days))]
    flats = sum(1 for i in range(1, len(vals)) if vals[i] == vals[i - 1])

    out.update({
        "status": "ok",
        "nom": meta.get("longName") or meta.get("shortName") or "",
        "devise": meta.get("currency"),
        "place": meta.get("fullExchangeName") or meta.get("exchangeName"),
        "type": meta.get("instrumentType"),
        "n_cloture": len(pairs),
        "debut": days[0].isoformat(),
        "fin": days[-1].isoformat(),
        "dernier": round(vals[-1], 4),
        "perf_6m_pct": round((vals[-1] / vals[0] - 1) * 100, 2),
        "trou_max_j": max(gaps) if gaps else 0,
        "jours_plats_pct": round(flats / (len(vals) - 1) * 100, 1),
        "volume_median": int(statistics.median(volumes)) if volumes else 0,
    })
    return out


def main():
    resultats = []
    for agregat, role, symbole in CANDIDATES:
        d = probe(symbole)
        d.update({"agregat": agregat, "role": role, "ticker": symbole})
        resultats.append(d)
        time.sleep(0.5)

    payload = {
        "generated": dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "range": RANGE,
        "curl_cffi": HAVE_CFFI,
        "tickers": resultats,
    }
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)

    entete = (f"{'agregat':10s} {'ticker':12s} {'etat':10s} {'dev':4s} "
              f"{'n':>4s} {'trou':>4s} {'plat%':>6s} {'perf6m':>7s}  place")
    print(entete)
    print("-" * len(entete))
    for d in resultats:
        if d["status"] == "ok":
            print(f"{d['agregat']:10s} {d['ticker']:12s} {'ok':10s} "
                  f"{str(d['devise']):4s} {d['n_cloture']:4d} "
                  f"{d['trou_max_j']:4d} {d['jours_plats_pct']:6.1f} "
                  f"{d['perf_6m_pct']:7.2f}  {d['place']}")
        else:
            print(f"{d['agregat']:10s} {d['ticker']:12s} {d['status']:10s} "
                  f"{'':4s} {'':>4s} {'':>4s} {'':>6s} {'':>7s}  {d['detail']}")

    ok = sum(1 for d in resultats if d["status"] == "ok")
    print(f"\n{ok}/{len(resultats)} tickers exploitables -> {OUT_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

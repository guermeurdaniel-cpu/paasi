#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
paasi / attribution.py — pourquoi WPEA et PAASI montent ou baissent.

Le script ne calcule aucune fenetre : il livre ~3 mois de cours bruts alignes
sur un calendrier commun. La page choisit sa periode et recalcule tout.

Un agregat est defini par une combinaison lineaire d'instruments :

    ("libelle", poids, [(ticker, coefficient, ticker_de_change), ...])

Les coefficients somment toujours a 1. Une liste vide = agregat fige (poids
connu, evolution inconnue), qui tombe dans le residu.

Cela permet deux choses qu'un simple ticker ne permettait pas :
  - convertir un titre asiatique en euros (2330.TW via EURTWD=X) ;
  - reconstruire un "reste de pays" en retranchant les megacaps de l'ETF
    pays, dont les indices sont plafonnes 20/35 et sous-ponderent
    massivement TSMC, Samsung et SK Hynix.

POIDS : saisis a la main, aucun acces iShares (leurs pages bloquent les
        serveurs GitHub). Releves des 07 et 12/08/2026.
        A refaire vers debut novembre 2026.

Sortie : attribution.json
"""

import json
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
GARDE_JOURS = 68
OUT_FILE = "attribution.json"
POIDS_DATE = "2026-08-12"

TWD, KRW = "EURTWD=X", "EURKRW=X"

# ----------------------------------------------------------------------
# Poids des megacaps DANS L'ETF pays (releve justETF du 12/08/2026).
# Ce sont eux qui comptent, pas les plafonds theoriques : entre deux
# reequilibrages le poids derive sous le plafond.
# ----------------------------------------------------------------------
ETF_TSMC = 0.2925                    # TSMC dans iShares MSCI Taiwan
ETF_SS, ETF_SK = 0.3165, 0.1835      # Samsung (ord+pref) et SK Hynix dans Amundi Korea

kTW = 1.0 / (1.0 - ETF_TSMC)                 # 1.4134
kKR = 1.0 / (1.0 - ETF_SS - ETF_SK)          # 2.0000

# Poids relatifs dans la memoire coreenne : Samsung 8.62, SK Hynix 5.57
MEM_SS = (7.64 + 0.98) / (7.64 + 0.98 + 5.57)
MEM_SK = 1.0 - MEM_SS

PAASI = {
    "cle": "paasi",
    "nom": "PAASI — MSCI Emerging Asia",
    "ref_ticker": "PAASI.PA",
    "controles": ["CEBL.DE"],
    "agregats": [
        # TSMC seul : premier moteur de l'indice a lui tout seul
        ("TSMC — fonderie", 18.33,
         [("2330.TW", 1.0, TWD)]),

        # Chaine IA taiwanaise = ETF Taiwan moins TSMC (Taiwan 32.90 - 18.33)
        ("Chaine IA taiwanaise", 14.57,
         [("ITWN.AS", kTW, None), ("2330.TW", -ETF_TSMC * kTW, TWD)]),

        # Memoire coreenne : Samsung (ord + pref) et SK Hynix
        ("Memoire coreenne", 14.19,
         [("005930.KS", MEM_SS, KRW), ("000660.KS", MEM_SK, KRW)]),

        # Coree hors memoire = ETF Coree moins les deux megacaps (22.74 - 14.19)
        ("Coree hors memoire", 8.55,
         [("KRW.PA", kKR, None),
          ("005930.KS", -ETF_SS * kKR, KRW),
          ("000660.KS", -ETF_SK * kKR, KRW)]),

        # Chine offshore 21.82 + ligne actions A 4.46 : ICGA couvre les deux
        ("Chine — plateformes et banques", 26.28,
         [("ICGA.DE", 1.0, None)]),

        ("Inde — banques et consommation", 14.20,
         [("PINR.PA", 1.0, None)]),

        # Thailande 1.22 + Malaisie 1.14 + divers 0.97 + liquidites 0.54
        ("Reste (ASEAN, divers)", 3.87, []),
    ],
}

WORLD = {
    "cle": "world",
    "nom": "WPEA — MSCI World",
    "ref_ticker": "WPEA.PA",
    "controles": ["EUNL.DE"],
    "agregats": [
        ("Technologie",                  29.75, [("XDWT.DE", 1.0, None)]),
        ("Finance",                      16.44, [("XDWF.DE", 1.0, None)]),
        ("Industrie",                    11.42, [("XDWI.DE", 1.0, None)]),
        ("Sante",                         9.00, [("XDWH.DE", 1.0, None)]),
        ("Consommation discretionnaire",  8.97, [("XDWC.DE", 1.0, None)]),
        ("Telecoms et medias",            7.97, [("XDWS.DE", 1.0, None)]),
        ("Consommation de base",          4.94, [("XDWY.DE", 1.0, None)]),
        ("Energie",                       3.76, [("XDW0.DE", 1.0, None)]),
        ("Materiaux",                     3.32, [("XDWM.DE", 1.0, None)]),
        ("Services publics",              2.39, [("XDWU.DE", 1.0, None)]),
        ("Autres (immobilier, divers)",   2.04, []),
    ],
}

FONDS = [WORLD, PAASI]


# ----------------------------------------------------------------------
def http_get(url, **kw):
    kw.setdefault("timeout", 20)
    if HAVE_CFFI:
        kw.setdefault("impersonate", "chrome")
    else:
        kw.setdefault("headers", {"User-Agent": "Mozilla/5.0"})
    return creq.get(url, **kw)


_cache = {}


def yahoo(symbol):
    """Cours ajustes des dividendes : {date iso: cours}."""
    if symbol in _cache:
        return _cache[symbol]
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
           f"?range={RANGE}&interval=1d&events=div%2Csplit")
    print(f"  [yahoo] {symbol}", flush=True)
    serie = {}
    try:
        r = http_get(url)
        if r.status_code != 200:
            print(f"    [!] HTTP {r.status_code}", flush=True)
            _cache[symbol] = serie
            return serie
        res = r.json()["chart"]["result"][0]
        stamps = res.get("timestamp") or []
        ind = res.get("indicators", {})
        adj = ind["adjclose"][0].get("adjclose") if ind.get("adjclose") else None
        vals = adj if adj and any(v is not None for v in adj) \
            else (ind.get("quote") or [{}])[0].get("close") or []
        for t, v in zip(stamps, vals):
            if v is not None:
                serie[dt.datetime.utcfromtimestamp(t).date().isoformat()] = float(v)
    except Exception as exc:
        print(f"    [!] {exc.__class__.__name__}", flush=True)
    _cache[symbol] = serie
    time.sleep(0.4)
    return serie


def aligner(serie, calendrier):
    """Projette sur le calendrier commun en reportant la derniere cloture."""
    out, dernier = [], None
    for d in calendrier:
        if d in serie:
            dernier = serie[d]
        out.append(dernier)
    prem = next((v for v in out if v is not None), None)
    if prem is None:
        return None
    return [prem if v is None else v for v in out]


def cours_en_euros(ticker, fx, calendrier):
    """Cours alignes, convertis en euros si un ticker de change est donne."""
    c = aligner(yahoo(ticker), calendrier)
    if c is None:
        return None
    if fx:
        t = aligner(yahoo(fx), calendrier)
        if t is None:
            return None
        c = [p / x for p, x in zip(c, t)]      # fx = unites locales par euro
    return c


def synthetique(termes, calendrier):
    """Serie de cours reconstituee a partir d'une combinaison de rendements."""
    parts = []
    for ticker, coef, fx in termes:
        c = cours_en_euros(ticker, fx, calendrier)
        if c is None:
            return None, f"{ticker} indisponible"
        parts.append((coef, c))
    n = len(calendrier)
    cours, niveau = [100.0], 100.0
    for i in range(1, n):
        r = sum(coef * (c[i] / c[i - 1] - 1) for coef, c in parts)
        niveau *= (1 + r)
        cours.append(niveau)
    return cours, None


# ----------------------------------------------------------------------
def traiter(fonds):
    print(f"\n=== {fonds['nom']} ===", flush=True)

    ref = yahoo(fonds["ref_ticker"])
    if len(ref) < 30:
        raise RuntimeError(f"Serie {fonds['ref_ticker']} trop courte")
    calendrier = sorted(ref)[-GARDE_JOURS:]
    prix_ref = [round(ref[d], 4) for d in calendrier]
    n = len(calendrier)

    resultats, somme, poids_fige = [], 0.0, 0.0

    for libelle, poids, termes in fonds["agregats"]:
        item = {"libelle": libelle, "poids": poids,
                "instruments": [t[0] for t in termes]}

        if not termes:
            poids_fige += poids
            item.update({"suivi": False, "cours": None,
                         "perf_pct": None, "contrib": None})
            resultats.append(item)
            continue

        cours, err = synthetique(termes, calendrier)
        if cours is None:
            poids_fige += poids
            item.update({"suivi": False, "cours": None, "perf_pct": None,
                         "contrib": None, "erreur": err})
            print(f"    [!] {libelle} : {err}", flush=True)
            resultats.append(item)
            continue

        perf = (cours[-1] / cours[0] - 1) * 100.0
        contrib = poids * perf / 100.0
        somme += contrib
        item.update({"suivi": True, "cours": [round(v, 5) for v in cours],
                     "perf_pct": round(perf, 2), "contrib": round(contrib, 3)})
        resultats.append(item)

    controles = {}
    for sym in fonds.get("controles", []):
        s = aligner(yahoo(sym), calendrier)
        if s:
            controles[sym] = round((s[-1] / s[0] - 1) * 100.0, 2)

    perf_ref = (prix_ref[-1] / prix_ref[0] - 1) * 100.0
    residu = perf_ref - somme
    print(f"  {calendrier[0]} -> {calendrier[-1]} ({n} cotations)", flush=True)
    print(f"  {fonds['ref_ticker']} {perf_ref:+.2f} % | somme {somme:+.2f} pt "
          f"| residu {residu:+.2f} pt | poids fige {poids_fige:.2f} %", flush=True)
    for r in resultats:
        if r["suivi"]:
            print(f"    {r['libelle'][:32]:34s} {r['poids']:5.2f} % "
                  f"perf {r['perf_pct']:+7.2f} % contrib {r['contrib']:+6.2f} pt",
                  flush=True)
        else:
            print(f"    {r['libelle'][:32]:34s} {r['poids']:5.2f} %  (fige)",
                  flush=True)
    for k, v in controles.items():
        print(f"    [controle] {k} {v:+.2f} %", flush=True)
    print(f"  [verif] somme des poids "
          f"{sum(r['poids'] for r in resultats):.2f} %", flush=True)

    return {
        "cle": fonds["cle"],
        "nom": fonds["nom"],
        "ref_ticker": fonds["ref_ticker"],
        "poids_date": POIDS_DATE,
        "dates": calendrier,
        "prix_ref": prix_ref,
        "perf_ref_pct": round(perf_ref, 2),
        "somme_contrib": round(somme, 3),
        "residu": round(residu, 3),
        "poids_non_suivi": round(poids_fige, 2),
        "controles": controles,
        "agregats": resultats,
    }


def main():
    print(f"coefficients : Taiwan reste = {kTW:.4f} ETF "
          f"{-ETF_TSMC * kTW:+.4f} TSMC | Coree reste = {kKR:.4f} ETF "
          f"{-ETF_SS * kKR:+.4f} Samsung {-ETF_SK * kKR:+.4f} SKHynix",
          flush=True)
    print(f"memoire coreenne : {MEM_SS:.3f} Samsung + {MEM_SK:.3f} SKHynix",
          flush=True)

    fonds = []
    for f in FONDS:
        try:
            fonds.append(traiter(f))
        except Exception as exc:
            print(f"[ECHEC] {f['cle']} : {exc}", flush=True)

    if not fonds:
        print("Aucun fonds traite, fichier non ecrit", flush=True)
        return 1

    with open(OUT_FILE, "w", encoding="utf-8") as fh:
        json.dump({
            "generated": dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "poids_date": POIDS_DATE,
            "fonds": fonds,
        }, fh, ensure_ascii=False, indent=1)
    print(f"\n-> {OUT_FILE} ({len(fonds)} fonds)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

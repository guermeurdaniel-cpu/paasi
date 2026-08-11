#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
paasi / attribution.py — pourquoi PAASI et WPEA montent ou baissent.

  contribution d'un agregat = poids dans l'indice x performance de l'agregat
  residu = performance reelle de l'ETF - somme des contributions

POIDS : saisis a la main (les indices ne sont rebalances que trimestriellement).
        Releves sur les pages produit iShares le 07/08/2026.
        A remettre a jour vers debut novembre 2026.
PERFS : un ETF cote en EUR par agregat, via Yahoo. Aucune conversion de change.

Aucun acces iShares a l'execution : uniquement des appels Yahoo.

Le JSON porte, pour chaque agregat :
  serie  : contribution cumulee, en points de pourcentage de l'indice
  ratio  : cours de l'agregat rapporte a son cours du premier jour (1.0 au debut)
et, pour chaque fonds :
  prix_ref : cours de cloture reels de l'ETF de reference, en euros

La page reconstitue la valeur d'un agregat par
  valeur = poids / 100 x prix_ref[0] x ratio
de sorte que la somme des agregats vaut le prix de l'indice.

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

WINDOW_DAYS = 30
RANGE = "3mo"
OUT_FILE = "attribution.json"
POIDS_DATE = "2026-08-07"

# ----------------------------------------------------------------------
# (libelle, ticker EUR ou None, poids en %)
# ticker None = poids connu, performance non suivie : fige a ratio 1.0
# ----------------------------------------------------------------------
PAASI = {
    "cle": "paasi",
    "nom": "PAASI — MSCI Emerging Asia",
    "ref_ticker": "PAASI.PA",
    "controles": ["CEBL.DE"],
    "agregats": [
        ("Taiwan — fonderie et semi-conducteurs", "ITWN.AS", 32.90),
        ("Coree — memoire et electronique",       "KRW.PA",  22.74),
        # Chine 21.82 + 4.46 de l'ETF Chine A domicilie en Irlande
        ("Chine — plateformes et banques",        "ICGA.DE", 26.28),
        ("Inde — banques et consommation",        "PINR.PA", 14.20),
        # Thailande 1.22 + Malaisie 1.14 + divers 0.97 + liquidites 0.54
        ("Autres (ASEAN, divers)",                None,       3.87),
    ],
}

WORLD = {
    "cle": "world",
    "nom": "WPEA — MSCI World",
    "ref_ticker": "WPEA.PA",
    "controles": ["EUNL.DE"],
    "agregats": [
        ("Technologie",                  "XDWT.DE", 29.75),
        ("Finance",                      "XDWF.DE", 16.44),
        ("Industrie",                    "XDWI.DE", 11.42),
        ("Sante",                        "XDWH.DE",  9.00),
        ("Consommation discretionnaire", "XDWC.DE",  8.97),
        ("Telecoms et medias",           "XDWS.DE",  7.97),
        ("Consommation de base",         "XDWY.DE",  4.94),
        ("Energie",                      "XDW0.DE",  3.76),
        ("Materiaux",                    "XDWM.DE",  3.32),
        ("Services publics",             "XDWU.DE",  2.39),
        ("Autres (immobilier, divers)",  None,       2.04),
    ],
}

FONDS = [PAASI, WORLD]


def http_get(url, **kw):
    kw.setdefault("timeout", 20)
    if HAVE_CFFI:
        kw.setdefault("impersonate", "chrome")
    else:
        kw.setdefault("headers", {"User-Agent": "Mozilla/5.0"})
    return creq.get(url, **kw)


def yahoo_serie(symbol):
    """Retourne {date iso: cloture}, {} si indisponible."""
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
           f"?range={RANGE}&interval=1d")
    print(f"  [yahoo] {symbol}", flush=True)
    try:
        r = http_get(url)
        if r.status_code != 200:
            print(f"    [!] HTTP {r.status_code}", flush=True)
            return {}
        res = r.json()["chart"]["result"][0]
    except Exception as exc:
        print(f"    [!] {exc.__class__.__name__}", flush=True)
        return {}
    stamps = res.get("timestamp") or []
    closes = (res.get("indicators", {}).get("quote") or [{}])[0].get("close") or []
    return {dt.datetime.utcfromtimestamp(t).date().isoformat(): float(c)
            for t, c in zip(stamps, closes) if c is not None}


def aligner(serie, calendrier):
    """Projette une serie sur le calendrier de reference en reportant
    la derniere cloture connue (places boursieres non synchrones)."""
    out, dernier = [], None
    for d in calendrier:
        if d in serie:
            dernier = serie[d]
        out.append(dernier)
    return out


def traiter(fonds):
    print(f"\n=== {fonds['nom']} ===", flush=True)

    ref = yahoo_serie(fonds["ref_ticker"])
    if len(ref) < WINDOW_DAYS // 2:
        raise RuntimeError(f"Serie de reference {fonds['ref_ticker']} vide")
    calendrier = sorted(ref)[-(WINDOW_DAYS + 1):]
    prix_ref = [round(ref[d], 4) for d in calendrier]
    perf_ref = (prix_ref[-1] / prix_ref[0] - 1) * 100.0
    n = len(calendrier)

    resultats, somme, poids_muet = [], 0.0, 0.0

    for libelle, ticker, poids in fonds["agregats"]:
        item = {"libelle": libelle, "ticker": ticker, "poids": poids}

        serie = aligner(yahoo_serie(ticker), calendrier) if ticker else None
        if ticker:
            time.sleep(0.4)

        if not serie or serie[0] is None or serie[-1] is None:
            # poids connu, evolution inconnue : on fige l'agregat
            poids_muet += poids
            item.update({
                "suivi": False,
                "perf_pct": None,
                "contrib": None,
                "serie": [0.0] * n,
                "ratio": [1.0] * n,
            })
            if ticker:
                item["erreur"] = "serie incomplete"
            resultats.append(item)
            continue

        base = serie[0]
        ratio = [round(v / base, 6) if v is not None else None for v in serie]
        # report des trous eventuels
        dernier = 1.0
        for i in range(n):
            if ratio[i] is None:
                ratio[i] = dernier
            else:
                dernier = ratio[i]

        perf = (ratio[-1] - 1.0) * 100.0
        contrib = poids * perf / 100.0
        somme += contrib
        item.update({
            "suivi": True,
            "perf_pct": round(perf, 2),
            "contrib": round(contrib, 3),
            # contribution cumulee, en POINTS de pourcentage de l'indice
            "serie": [round(poids * (r - 1.0), 4) for r in ratio],
            "ratio": ratio,
        })
        resultats.append(item)

    controles = {}
    for sym in fonds.get("controles", []):
        time.sleep(0.4)
        s = aligner(yahoo_serie(sym), calendrier)
        if s[0] and s[-1]:
            controles[sym] = round((s[-1] / s[0] - 1) * 100.0, 2)

    residu = perf_ref - somme
    print(f"  perf {fonds['ref_ticker']} {perf_ref:+.2f} % "
          f"| somme {somme:+.2f} pt | residu {residu:+.2f} pt "
          f"| poids fige {poids_muet:.2f} %", flush=True)
    print(f"  prix {prix_ref[0]:.4f} -> {prix_ref[-1]:.4f} EUR", flush=True)
    for r in resultats:
        if not r["suivi"]:
            print(f"    {r['libelle'][:34]:36s} poids {r['poids']:5.2f} %"
                  f"   (fige)", flush=True)
        else:
            print(f"    {r['libelle'][:34]:36s} poids {r['poids']:5.2f} %"
                  f"  perf {r['perf_pct']:+7.2f} %"
                  f"  contrib {r['contrib']:+6.2f} pt", flush=True)
    for k, v in controles.items():
        print(f"    [controle] {k} {v:+.2f} %", flush=True)

    # controle interne : la somme des series doit finir sur somme_contrib
    fin = sum(r["serie"][-1] for r in resultats)
    print(f"  [verif] somme des series en fin de fenetre {fin:+.3f} pt "
          f"(attendu {somme:+.3f})", flush=True)

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
        "poids_non_suivi": round(poids_muet, 2),
        "controles": controles,
        "agregats": resultats,
    }


def main():
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
            "window_days": WINDOW_DAYS,
            "poids_date": POIDS_DATE,
            "fonds": fonds,
        }, fh, ensure_ascii=False, indent=1)
    print(f"\n-> {OUT_FILE} ({len(fonds)} fonds)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

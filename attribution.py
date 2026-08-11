#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
paasi / attribution.py — pourquoi PAASI et WPEA montent ou baissent.

Ce script ne fait plus aucun calcul de fenetre : il livre les COURS BRUTS
alignes sur un calendrier commun, sur environ trois mois. La page choisit
sa fenetre (1, 2 ou 3 mois) et recalcule tout a partir de la date de depart
retenue. C'est ce qui permet au curseur de periode de fonctionner sans
relancer le workflow.

POIDS : saisis a la main (les indices ne sont rebalances que
        trimestriellement). Releves sur les pages produit iShares le
        07/08/2026. A remettre a jour vers debut novembre 2026.
COURS : un ETF cote en EUR par agregat, via Yahoo. Aucune conversion de
        change. Aucun acces iShares.

JSON produit, par fonds :
  dates     : calendrier commun (jours de cotation de l'ETF de reference)
  prix_ref  : cloture reelle de l'ETF de reference, en euros
  agregats  : [{libelle, ticker, poids, suivi, cours: [...]}]

et, a titre de controle seulement, les chiffres calcules sur la fenetre
complete (perf_ref_pct, somme_contrib, residu).

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

RANGE = "6mo"          # on demande large, on ne garde que la fin
GARDE_JOURS = 68       # ~3 mois de cotations
OUT_FILE = "attribution.json"
POIDS_DATE = "2026-08-07"

# ----------------------------------------------------------------------
# (libelle, ticker EUR ou None, poids en %)
# ticker None = poids connu, evolution non suivie : cours = null
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
    """Projette une serie sur le calendrier de reference en reportant la
    derniere cloture connue (places boursieres non synchrones). Les dates
    anterieures a la premiere cotation connue restent a None."""
    out, dernier = [], None
    for d in calendrier:
        if d in serie:
            dernier = serie[d]
        out.append(dernier)
    return out


def combler_debut(cours):
    """Remplit d'eventuels None de tete par la premiere valeur connue."""
    prem = None
    for v in cours:
        if v is not None:
            prem = v
            break
    if prem is None:
        return None
    return [prem if v is None else v for v in cours]


def traiter(fonds):
    print(f"\n=== {fonds['nom']} ===", flush=True)

    ref = yahoo_serie(fonds["ref_ticker"])
    if len(ref) < 30:
        raise RuntimeError(f"Serie de reference {fonds['ref_ticker']} trop courte")
    calendrier = sorted(ref)[-GARDE_JOURS:]
    prix_ref = [round(ref[d], 4) for d in calendrier]
    n = len(calendrier)

    resultats, somme, poids_muet = [], 0.0, 0.0

    for libelle, ticker, poids in fonds["agregats"]:
        item = {"libelle": libelle, "ticker": ticker, "poids": poids}

        cours = None
        if ticker:
            time.sleep(0.4)
            cours = combler_debut(aligner(yahoo_serie(ticker), calendrier))

        if not cours:
            poids_muet += poids
            item.update({"suivi": False, "cours": None,
                         "perf_pct": None, "contrib": None})
            if ticker:
                item["erreur"] = "serie indisponible"
            resultats.append(item)
            continue

        perf = (cours[-1] / cours[0] - 1) * 100.0
        contrib = poids * perf / 100.0
        somme += contrib
        item.update({
            "suivi": True,
            "cours": [round(v, 4) for v in cours],
            "perf_pct": round(perf, 2),
            "contrib": round(contrib, 3),
        })
        resultats.append(item)

    controles = {}
    for sym in fonds.get("controles", []):
        time.sleep(0.4)
        s = combler_debut(aligner(yahoo_serie(sym), calendrier))
        if s:
            controles[sym] = round((s[-1] / s[0] - 1) * 100.0, 2)

    perf_ref = (prix_ref[-1] / prix_ref[0] - 1) * 100.0
    residu = perf_ref - somme
    print(f"  fenetre complete : {calendrier[0]} -> {calendrier[-1]} "
          f"({n} cotations)", flush=True)
    print(f"  prix {prix_ref[0]:.4f} -> {prix_ref[-1]:.4f} EUR "
          f"({perf_ref:+.2f} %)", flush=True)
    print(f"  somme {somme:+.2f} pt | residu {residu:+.2f} pt "
          f"| poids fige {poids_muet:.2f} %", flush=True)
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

    total_poids = sum(r["poids"] for r in resultats)
    print(f"  [verif] somme des poids {total_poids:.2f} % "
          f"| longueur des series : " +
          ", ".join(str(len(r["cours"])) if r["cours"] else "0"
                    for r in resultats), flush=True)

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
            "poids_date": POIDS_DATE,
            "fonds": fonds,
        }, fh, ensure_ascii=False, indent=1)
    print(f"\n-> {OUT_FILE} ({len(fonds)} fonds)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

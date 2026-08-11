#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
paasi / attribution.py — pourquoi PAASI et WPEA montent ou baissent.

Principe :
  contribution d'un agregat = poids dans l'indice x performance de l'agregat
  residu = performance reelle de l'ETF - somme des contributions

Les POIDS viennent de la composition complete d'un ETF physique de reference
publiee par iShares (CSV), agregee par pays (Asie) ou par secteur (Monde).
Aucune troncature : on additionne toutes les lignes.

Les PERFORMANCES viennent d'un ETF cote en euros par agregat (Yahoo).
Tous les instruments retenus cotent en EUR : aucune conversion de change.

Sortie : attribution.json (consomme par index.html)
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
    import requests as creq
    HAVE_CFFI = False

WINDOW_DAYS = 30
RANGE = "3mo"
OUT_FILE = "attribution.json"
ISHARES_BASE = "https://www.ishares.com"

# ----------------------------------------------------------------------
# Definition des deux fonds a expliquer
# ----------------------------------------------------------------------
# regle : (libelle, ticker EUR ou None, [valeurs de la colonne du CSV])
# None = agregat sans instrument : son poids est connu, sa perf non ;
#        sa contribution tombe dans le residu.

PAASI = {
    "cle": "paasi",
    "nom": "PAASI — MSCI Emerging Asia",
    "ref_ticker": "PAASI.PA",
    "produit": "253723",
    "page": (ISHARES_BASE + "/uk/individual/en/products/253723/"
             "ishares-msci-em-asia-ucits-etf"
             "?switchLocale=y&siteEntryPassthrough=true"),
    "colonne": "country",
    "agregats": [
        ("Taiwan — fonderie et semi-conducteurs", "ITWN.AS", ["taiwan"]),
        ("Coree — memoire et electronique", "KRW.PA", ["korea"]),
        ("Chine — plateformes et banques", "ICGA.DE",
         ["china", "hong kong", "ireland"]),
        ("Inde — banques et consommation", "PINR.PA", ["india"]),
        ("Autres (ASEAN, divers)", None, []),
    ],
}

WORLD = {
    "cle": "world",
    "nom": "WPEA — MSCI World",
    "ref_ticker": "WPEA.PA",
    "produit": "251882",
    "page": (ISHARES_BASE + "/uk/individual/en/products/251882/"
             "ishares-msci-world-ucits-etf-acc-fund"
             "?switchLocale=y&siteEntryPassthrough=true"),
    "colonne": "sector",
    "agregats": [
        ("Technologie", "XDWT.DE", ["information technology"]),
        ("Finance", "XDWF.DE", ["financials"]),
        ("Sante", "XDWH.DE", ["health care"]),
        ("Industrie", "XDWI.DE", ["industrials"]),
        ("Consommation discretionnaire", "XDWC.DE", ["consumer discretionary"]),
        ("Consommation de base", "XDWY.DE", ["consumer staples"]),
        ("Telecoms et medias", "XDWS.DE", ["communication"]),
        ("Energie", "XDW0.DE", ["energy"]),
        ("Materiaux", "XDWM.DE", ["materials"]),
        ("Services publics", "XDWU.DE", ["utilities"]),
        ("Autres (immobilier, divers)", None, []),
    ],
}

FONDS = [PAASI, WORLD]


# ----------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------
def http_get(url, **kw):
    kw.setdefault("timeout", 45)
    if HAVE_CFFI:
        kw.setdefault("impersonate", "chrome")
    else:
        kw.setdefault("headers", {"User-Agent": "Mozilla/5.0"})
    return creq.get(url, **kw)


# ----------------------------------------------------------------------
# 1. Composition iShares -> poids par agregat
# ----------------------------------------------------------------------
def fetch_holdings_csv(page_url, produit):
    r = http_get(page_url)
    r.raise_for_status()
    html = r.text
    motif = (r'(/[^"\']*' + produit +
             r'[^"\']*\.ajax\?fileType=csv[^"\']*dataType=fund)')
    m = re.search(motif, html)
    if not m:
        m = re.search(r'([^"\']*\.ajax\?fileType=csv[^"\']*dataType=fund)', html)
    if not m:
        raise RuntimeError(f"Lien CSV introuvable (produit {produit})")
    url = ISHARES_BASE + m.group(1).replace("&amp;", "&")
    print(f"[ishares {produit}] {url}")
    r2 = http_get(url)
    r2.raise_for_status()
    return r2.text


def parse_holdings(csv_text):
    """Retourne (date de composition, [lignes actions])."""
    lines = csv_text.splitlines()
    hdate = None
    header_idx = None
    for i, ln in enumerate(lines):
        if hdate is None:
            m = re.search(r'as of[,"\s]+([0-9]{1,2}-\w{3}-[0-9]{4})', ln)
            if m:
                try:
                    hdate = dt.datetime.strptime(
                        m.group(1), "%d-%b-%Y").date().isoformat()
                except ValueError:
                    pass
        if ln.startswith("Ticker,") or ln.startswith('"Ticker"'):
            header_idx = i
            break
    if header_idx is None:
        raise RuntimeError("En-tete CSV iShares introuvable")

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
            "name": col(row, "Name"),
            "sector": col(row, "Sector"),
            "country": col(row, "Location"),
            "weight": w,
        })
    return hdate, out


def poids_par_agregat(lignes, colonne, agregats):
    """Additionne les poids de TOUTES les lignes, sans troncature."""
    regles = []
    for i, (libelle, ticker, cles) in enumerate(agregats):
        regles.append((i, [c.lower() for c in cles]))

    poids = [0.0] * len(agregats)
    reste = len(agregats) - 1          # dernier agregat = fourre-tout
    for ln in lignes:
        val = (ln.get(colonne) or "").strip().lower()
        # cas particulier : ETF detenu dans le fonds (Chine A, domicilie Irlande)
        if colonne == "country" and "etf" in ln["name"].lower():
            val = "china"
        cible = reste
        for i, cles in regles:
            if any(val.startswith(c) for c in cles):
                cible = i
                break
        poids[cible] += ln["weight"]

    total = sum(poids)
    if total <= 0:
        raise RuntimeError("Poids total nul")
    return [p / total * 100.0 for p in poids], total


# ----------------------------------------------------------------------
# 2. Series Yahoo (toutes en EUR)
# ----------------------------------------------------------------------
def yahoo_serie(symbol):
    """Retourne {date iso: cloture} ou {} si indisponible."""
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
           f"?range={RANGE}&interval=1d")
    try:
        r = http_get(url)
        if r.status_code != 200:
            print(f"  [!] {symbol} HTTP {r.status_code}")
            return {}
        res = r.json()["chart"]["result"][0]
    except Exception as exc:
        print(f"  [!] {symbol} {exc.__class__.__name__}")
        return {}
    stamps = res.get("timestamp") or []
    closes = (res.get("indicators", {}).get("quote") or [{}])[0].get("close") or []
    serie = {}
    for t, c in zip(stamps, closes):
        if c is None:
            continue
        serie[dt.datetime.utcfromtimestamp(t).date().isoformat()] = float(c)
    return serie


def aligner(serie, calendrier):
    """Projette une serie sur le calendrier de reference, en reportant
    la derniere cloture connue (places boursieres non synchrones)."""
    out = []
    dernier = None
    for d in calendrier:
        if d in serie:
            dernier = serie[d]
        out.append(dernier)
    return out


# ----------------------------------------------------------------------
# 3. Traitement d'un fonds
# ----------------------------------------------------------------------
def traiter(fonds):
    print(f"\n=== {fonds['nom']} ===")
    hdate, lignes = parse_holdings(
        fetch_holdings_csv(fonds["page"], fonds["produit"]))
    poids, couverture = poids_par_agregat(
        lignes, fonds["colonne"], fonds["agregats"])
    print(f"  {len(lignes)} lignes actions, {couverture:.1f} % de poids agrege"
          f" (composition du {hdate})")

    ref = yahoo_serie(fonds["ref_ticker"])
    if len(ref) < WINDOW_DAYS // 2:
        raise RuntimeError(f"Serie de reference {fonds['ref_ticker']} vide")
    calendrier = sorted(ref)[-(WINDOW_DAYS + 1):]
    base_ref = ref[calendrier[0]]
    perf_ref = (ref[calendrier[-1]] / base_ref - 1) * 100.0

    resultats = []
    somme = 0.0
    poids_sans_instrument = 0.0

    for (libelle, ticker, _), p in zip(fonds["agregats"], poids):
        item = {"libelle": libelle, "ticker": ticker, "poids": round(p, 2)}
        if not ticker:
            poids_sans_instrument += p
            item.update({"perf_pct": None, "contrib": None, "serie": []})
            resultats.append(item)
            continue

        time.sleep(0.4)
        serie = aligner(yahoo_serie(ticker), calendrier)
        if serie[0] is None or serie[-1] is None:
            poids_sans_instrument += p
            item.update({"perf_pct": None, "contrib": None, "serie": [],
                         "erreur": "serie incomplete"})
            resultats.append(item)
            continue

        base = serie[0]
        perf = (serie[-1] / base - 1) * 100.0
        contrib = p * perf / 100.0
        somme += contrib
        item.update({
            "perf_pct": round(perf, 2),
            "contrib": round(contrib, 3),
            # historique de la contribution cumulee, en points d'indice
            "serie": [None if v is None else round(p * (v / base - 1) / 100.0, 4)
                      for v in serie],
        })
        resultats.append(item)

    residu = perf_ref - somme
    print(f"  perf {fonds['ref_ticker']} {perf_ref:+.2f} %  "
          f"| somme contributions {somme:+.2f} pt  | residu {residu:+.2f} pt")
    for r in resultats:
        if r["contrib"] is None:
            print(f"    {r['libelle'][:34]:36s} poids {r['poids']:5.1f} %"
                  f"   (pas d'instrument)")
        else:
            print(f"    {r['libelle'][:34]:36s} poids {r['poids']:5.1f} %"
                  f"  perf {r['perf_pct']:+7.2f} %  contrib {r['contrib']:+6.2f} pt")

    return {
        "cle": fonds["cle"],
        "nom": fonds["nom"],
        "ref_ticker": fonds["ref_ticker"],
        "holdings_date": hdate,
        "n_lignes": len(lignes),
        "dates": calendrier,
        "perf_ref_pct": round(perf_ref, 2),
        "somme_contrib": round(somme, 3),
        "residu": round(residu, 3),
        "poids_sans_instrument": round(poids_sans_instrument, 2),
        "agregats": resultats,
    }


def main():
    fonds = []
    for f in FONDS:
        try:
            fonds.append(traiter(f))
        except Exception as exc:
            print(f"[ECHEC] {f['cle']} : {exc}")

    if not fonds:
        print("Aucun fonds traite, fichier non ecrit")
        return 1

    payload = {
        "generated": dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window_days": WINDOW_DAYS,
        "fonds": fonds,
    }
    with open(OUT_FILE, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
    print(f"\n-> {OUT_FILE} ({len(fonds)} fonds)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
paasi / check_categories.py — repérage avant refonte de la page PAASI.

Ne touche pas a Yahoo. Fait deux choses seulement :

1) verifie que le fichier de composition iShares remonte toujours
   (c'est ce telechargement qui avait bloque attribution.py) ;
2) applique le decoupage en categories a la composition COMPLETE et
   affiche, pour chacune, son poids exact et ses principales lignes avec
   la couverture cumulee.

C'est ce tableau qui dira combien de titres il faut reellement recuperer
par categorie pour couvrir l'essentiel, et ce qui restera dans le solde.

Sortie : categories.json
"""

import csv as csvmod
import json
import re
import sys
import datetime as dt

try:
    from curl_cffi import requests as creq
    HAVE_CFFI = True
except ImportError:
    import requests as creq
    HAVE_CFFI = False

OUT_FILE = "categories.json"
ISHARES_BASE = "https://www.ishares.com"
ISHARES_PRODUCT_PAGE = (
    "https://www.ishares.com/uk/individual/en/products/253723/"
    "ishares-msci-em-asia-ucits-etf"
    "?switchLocale=y&siteEntryPassthrough=true"
)
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

TSMC = {"2330"}
MEMOIRE = {"005930", "005935", "000660"}
TECH_CN = {"Information Technology", "Communication", "Communication Services",
           "Consumer Discretionary"}


def http_get(url, **kw):
    kw.setdefault("timeout", 30)
    if HAVE_CFFI:
        kw.setdefault("impersonate", "chrome")
    else:
        kw.setdefault("headers", {"User-Agent": UA})
    print(f"  [http] {url[:95]}", flush=True)
    return creq.get(url, **kw)


def fetch_holdings_csv():
    r = http_get(ISHARES_PRODUCT_PAGE)
    r.raise_for_status()
    html = r.text
    print(f"  page recue : {len(html)} caracteres", flush=True)
    m = re.search(
        r'(/uk/[^"\']*253723[^"\']*\.ajax\?fileType=csv[^"\']*dataType=fund)',
        html)
    if not m:
        m = re.search(r'([^"\']*\.ajax\?fileType=csv[^"\']*dataType=fund)', html)
    if not m:
        raise RuntimeError("Lien CSV introuvable dans la page iShares")
    url = ISHARES_BASE + m.group(1).replace("&amp;", "&")
    r2 = http_get(url)
    r2.raise_for_status()
    print(f"  CSV recu : {len(r2.text)} caracteres", flush=True)
    return r2.text


def parse_holdings(csv_text):
    lines = csv_text.splitlines()
    hdate, header_idx = None, None
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
        raise RuntimeError("En-tete CSV introuvable")

    reader = csvmod.reader(lines[header_idx:])
    header = next(reader)
    print(f"  colonnes : {', '.join(h.strip() for h in header)}", flush=True)
    idx = {h.strip(): k for k, h in enumerate(header)}

    def col(row, *noms):
        for n in noms:
            if n in idx and idx[n] < len(row):
                return row[idx[n]].strip()
        return ""

    out = []
    for row in reader:
        if len(row) < 3:
            continue
        if (col(row, "Asset Class") or "EQUITY").upper() != "EQUITY":
            continue
        try:
            w = float(col(row, "Weight (%)").replace(",", ""))
        except ValueError:
            continue
        out.append({
            "ticker": col(row, "Ticker"),
            "nom": col(row, "Name"),
            "secteur": col(row, "Sector"),
            "isin": col(row, "ISIN"),
            "pays": col(row, "Location"),
            "place": col(row, "Exchange"),
            "devise": col(row, "Market Currency", "Currency"),
            "poids": w,
        })
    out.sort(key=lambda x: -x["poids"])
    return hdate, out


def categorie(l):
    """Regle d'affectation d'une ligne a une categorie."""
    pays, sect, nom = l["pays"], l["secteur"], l["nom"].upper()

    if "ETF" in nom:
        return "Actions A chinoises (ETF)"
    if l["ticker"] in TSMC:
        return "TSMC"
    if l["ticker"] in MEMOIRE:
        return "Memoire coreenne"
    if pays.startswith("Taiwan"):
        return "Chaine IA taiwanaise"
    if pays.startswith("Korea"):
        return "Coree hors memoire"
    if pays.startswith("India"):
        return "Inde"
    if pays.startswith("China") or pays.startswith("Hong Kong"):
        return ("Plateformes et tech chinoises" if sect in TECH_CN
                else "Finance chinoise" if sect == "Financials"
                else "Chine autres secteurs")
    return "Reste (ASEAN, divers)"


ORDRE = ["TSMC", "Memoire coreenne", "Chaine IA taiwanaise",
         "Plateformes et tech chinoises", "Finance chinoise",
         "Chine autres secteurs", "Actions A chinoises (ETF)",
         "Coree hors memoire", "Inde", "Reste (ASEAN, divers)"]


def main():
    try:
        hdate, lignes = parse_holdings(fetch_holdings_csv())
    except Exception as exc:
        print(f"\n[ECHEC] recuperation iShares : {exc}", flush=True)
        return 1

    total = sum(l["poids"] for l in lignes)
    print(f"\n{len(lignes)} lignes actions, poids total {total:.2f} % "
          f"(composition du {hdate})\n", flush=True)

    groupes = {c: [] for c in ORDRE}
    for l in lignes:
        groupes.setdefault(categorie(l), []).append(l)

    resume = []
    for cat in ORDRE:
        g = sorted(groupes.get(cat, []), key=lambda x: -x["poids"])
        if not g:
            continue
        p = sum(x["poids"] for x in g)
        print(f"=== {cat} — {p:.2f} % de l'indice, {len(g)} lignes ===")
        cum = 0.0
        for i, x in enumerate(g[:12]):
            cum += x["poids"]
            print(f"   {x['poids']:5.2f} %  cumul {cum / p * 100:5.1f} %  "
                  f"{x['ticker']:8s} {x['nom'][:34]:36s} {x['devise']} "
                  f"{x['place'][:14]}")
        if len(g) > 12:
            print(f"   ... {len(g) - 12} autres lignes, "
                  f"{p - cum:.2f} % de l'indice")
        print()
        resume.append({
            "categorie": cat, "poids": round(p, 2), "n_lignes": len(g),
            "lignes": [{"ticker": x["ticker"], "nom": x["nom"],
                        "isin": x["isin"], "devise": x["devise"],
                        "place": x["place"], "poids": x["poids"]}
                       for x in g[:12]],
        })

    print("--- couverture selon le nombre de titres pris par categorie ---")
    for k in (2, 3, 5, 8, 12):
        c = 0.0
        for cat in ORDRE:
            g = sorted(groupes.get(cat, []), key=lambda x: -x["poids"])
            if cat == "Inde":          # l'Inde passe par son ETF, poids entier
                c += sum(x["poids"] for x in g)
            else:
                c += sum(x["poids"] for x in g[:k])
        print(f"   {k:2d} titres par categorie -> {c:.1f} % couverts, "
              f"solde deduit {total - c:.1f} %")

    with open(OUT_FILE, "w", encoding="utf-8") as fh:
        json.dump({
            "generated": dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "holdings_date": hdate,
            "n_lignes": len(lignes),
            "poids_total": round(total, 2),
            "categories": resume,
        }, fh, ensure_ascii=False, indent=1)
    print(f"\n-> {OUT_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

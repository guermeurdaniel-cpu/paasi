#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
paasi / check_paasi.py — validation de la reconstruction de PAASI.

Deux etapes, dans un seul passage :

1) DIAGNOSTIC des series jamais testees (3 actions asiatiques, 2 changes) :
   Yahoo repond-il, dans quelle devise, avec quelle liquidite.

2) ABLATION : on reconstruit l'indice de quatre facons et on compare a la
   realite. Chaque variante ajoute une correction, pour voir ce que chacune
   rapporte reellement :

     A  poids de fin  + ETF pays bruts      (ce que fait la version actuelle)
     B  poids de debut + ETF pays bruts
     C  poids de fin  + Taiwan et Coree decapes
     D  poids de debut + decapage           (architecture proposee)

   Cibles : CEBL.DE (ETF physique du meme indice) et PAASI.PA (la ligne
   reellement detenue). L'ecart residuel de D dit si l'architecture tient.

Rien n'est ecrit dans le depot en dehors de check_paasi.json.
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
OUT_FILE = "check_paasi.json"
FENETRES = [("30 jours", 31), ("3 mois", 67)]

# ----------------------------------------------------------------------
# Poids releves a la main le 12/08/2026
# ----------------------------------------------------------------------
# Compartiments de l'indice (fonds iShares MSCI EM Asia), en % de l'indice.
# La ligne "ISH MSCI CHINA A ETF" (4.46, classee Irlande) est rattachee a la
# Chine : c'est ce rattachement qui fait concorder Tencent a 0,2 pt pres.
POIDS_FIN = {
    "tw":     32.90,
    "kr":     22.74,
    "cn":     21.82 + 4.46,
    "in":     14.20,
    "autres":  1.22 + 1.14 + 0.97 + 0.54,
}

# Poids des megacaps DANS L'INDICE, en % de l'indice entier
IDX_TSMC = 18.33
IDX_SS   = 7.64 + 0.98      # ordinaire + preference
IDX_SK   = 5.57

# Poids des memes titres DANS L'ETF pays, en % de l'ETF
ETF_TSMC = 29.25
ETF_SS   = 28.54 + 3.11
ETF_SK   = 18.35

TICKERS = {
    "ref":   "PAASI.PA",
    "temoin": "CEBL.DE",
    "tw":    "ITWN.AS",
    "kr":    "KRW.PA",
    "cn":    "ICGA.DE",
    "in":    "PINR.PA",
    "tsmc":  "2330.TW",
    "ss":    "005930.KS",
    "sk":    "000660.KS",
}
FX = {"twd": "EURTWD=X", "krw": "EURKRW=X",
      "usd_twd": "TWD=X", "usd_krw": "KRW=X", "eurusd": "EURUSD=X"}


# ----------------------------------------------------------------------
# Reseau
# ----------------------------------------------------------------------
def http_get(url, **kw):
    kw.setdefault("timeout", 20)
    if HAVE_CFFI:
        kw.setdefault("impersonate", "chrome")
    else:
        kw.setdefault("headers", {"User-Agent": "Mozilla/5.0"})
    return creq.get(url, **kw)


def yahoo(symbol):
    """Renvoie (serie ajustee {date: cours}, diagnostic)."""
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
           f"?range={RANGE}&interval=1d&events=div%2Csplit")
    diag = {"ticker": symbol, "status": "ko", "detail": ""}
    print(f"  [yahoo] {symbol}", flush=True)
    try:
        r = http_get(url)
    except Exception as exc:
        diag["detail"] = exc.__class__.__name__
        return {}, diag
    if r.status_code != 200:
        diag.update(status="http_error", detail=f"HTTP {r.status_code}")
        return {}, diag
    try:
        res = r.json()["chart"]["result"][0]
    except Exception:
        diag.update(status="no_data", detail="ticker inconnu")
        return {}, diag

    meta = res.get("meta", {})
    stamps = res.get("timestamp") or []
    ind = res.get("indicators", {})
    quote = (ind.get("quote") or [{}])[0]
    closes = quote.get("close") or []
    vols = quote.get("volume") or []

    adj = None
    if ind.get("adjclose"):
        adj = ind["adjclose"][0].get("adjclose")
    source = "adjclose" if adj and any(v is not None for v in adj) else "close"
    valeurs = adj if source == "adjclose" else closes

    serie, jours, vals, volumes = {}, [], [], []
    for i, t in enumerate(stamps):
        v = valeurs[i] if i < len(valeurs) else None
        if v is None:
            continue
        d = dt.datetime.utcfromtimestamp(t).date()
        serie[d.isoformat()] = float(v)
        jours.append(d)
        vals.append(float(v))
        if i < len(vols) and vols[i]:
            volumes.append(vols[i])

    if len(serie) < 25:
        diag.update(status="no_history", detail=f"{len(serie)} clotures")
        return serie, diag

    ecarts = [(jours[i] - jours[i - 1]).days for i in range(1, len(jours))]
    plats = sum(1 for i in range(1, len(vals)) if vals[i] == vals[i - 1])
    diag.update({
        "status": "ok",
        "nom": meta.get("longName") or meta.get("shortName") or "",
        "devise": meta.get("currency"),
        "place": meta.get("fullExchangeName") or meta.get("exchangeName"),
        "source_cours": source,
        "n": len(serie),
        "debut": jours[0].isoformat(),
        "fin": jours[-1].isoformat(),
        "perf_6m_pct": round((vals[-1] / vals[0] - 1) * 100, 2),
        "trou_max_j": max(ecarts) if ecarts else 0,
        "jours_plats_pct": round(plats / (len(vals) - 1) * 100, 1),
        "volume_median": int(statistics.median(volumes)) if volumes else 0,
    })
    return serie, diag


def aligner(serie, calendrier):
    out, dernier = [], None
    for d in calendrier:
        if d in serie:
            dernier = serie[d]
        out.append(dernier)
    prem = next((v for v in out if v is not None), None)
    return [prem if v is None else v for v in out] if prem else None


# ----------------------------------------------------------------------
# Outils de calcul
# ----------------------------------------------------------------------
def rendements(cours):
    """Serie de rendements simples, 0 le premier jour."""
    return [0.0] + [cours[i] / cours[i - 1] - 1 for i in range(1, len(cours))]


def en_euros(local, taux_eur_local):
    """Cours local -> cours en euros. taux = unites locales par euro."""
    return [p / t for p, t in zip(local, taux_eur_local)]


def combiner(series_rend, coefs):
    """Combinaison lineaire de rendements quotidiens, puis composition."""
    n = len(series_rend[0])
    ratio, cum = [1.0], 1.0
    for i in range(1, n):
        r = sum(c * s[i] for c, s in zip(coefs, series_rend))
        cum *= (1 + r)
        ratio.append(cum)
    return ratio


def perf(ratio, i0):
    return (ratio[-1] / ratio[i0] - 1) * 100


def poids_debut(poids_fin, ratios, i0):
    """Poids en debut de fenetre = poids courant / progression, normalise."""
    brut = {k: poids_fin[k] / (ratios[k][-1] / ratios[k][i0])
            for k in poids_fin}
    tot = sum(brut.values())
    return {k: v / tot * 100 for k, v in brut.items()}


# ----------------------------------------------------------------------
def main():
    # --- 1. recuperation ---
    series, diags = {}, []
    for cle, sym in TICKERS.items():
        s, d = yahoo(sym)
        d["role"] = cle
        series[cle] = s
        diags.append(d)
        time.sleep(0.4)

    fx = {}
    for cle, sym in FX.items():
        s, d = yahoo(sym)
        d["role"] = "fx:" + cle
        fx[cle] = s
        diags.append(d)
        time.sleep(0.4)

    # --- diagnostic lisible ---
    entete = (f"{'role':8s} {'ticker':11s} {'etat':10s} {'dev':4s} {'src':9s} "
              f"{'n':>4s} {'trou':>4s} {'plat%':>6s} {'perf6m':>8s}  place")
    print("\n" + entete)
    print("-" * len(entete))
    for d in diags:
        if d["status"] == "ok":
            print(f"{d['role']:8s} {d['ticker']:11s} {'ok':10s} "
                  f"{str(d['devise']):4s} {d['source_cours']:9s} {d['n']:4d} "
                  f"{d['trou_max_j']:4d} {d['jours_plats_pct']:6.1f} "
                  f"{d['perf_6m_pct']:8.2f}  {d.get('place','')}")
        else:
            print(f"{d['role']:8s} {d['ticker']:11s} {d['status']:10s} "
                  f"{'':4s} {'':9s} {'':>4s} {'':>4s} {'':>6s} {'':>8s}  "
                  f"{d['detail']}")

    if not series.get("ref"):
        print("\n[STOP] serie de reference PAASI.PA indisponible")
        return 1

    # --- 2. calendrier commun ---
    calendrier = sorted(series["ref"])[-140:]
    cours = {}
    for cle in TICKERS:
        a = aligner(series[cle], calendrier)
        if a is None:
            print(f"\n[STOP] serie {cle} ({TICKERS[cle]}) inexploitable")
            return 1
        cours[cle] = a

    # --- change : EURTWD direct, sinon reconstruit via le dollar ---
    def taux(direct, usd_local):
        a = aligner(fx.get(direct, {}), calendrier)
        if a:
            return a, direct
        u = aligner(fx.get(usd_local, {}), calendrier)
        e = aligner(fx.get("eurusd", {}), calendrier)
        if u and e:
            return [x * y for x, y in zip(u, e)], f"{usd_local} x EURUSD=X"
        return None, None

    t_twd, src_twd = taux("twd", "usd_twd")
    t_krw, src_krw = taux("krw", "usd_krw")
    if not t_twd or not t_krw:
        print("\n[STOP] taux de change indisponibles")
        return 1
    print(f"\nchange : TWD via {src_twd} | KRW via {src_krw}")

    # --- 3. coefficients de decapage ---
    wt_tsmc = IDX_TSMC / POIDS_FIN["tw"]
    we_tsmc = ETF_TSMC / 100
    k_tw = (1 - wt_tsmc) / (1 - we_tsmc)
    c_tsmc = wt_tsmc - we_tsmc * k_tw

    wt_ss, wt_sk = IDX_SS / POIDS_FIN["kr"], IDX_SK / POIDS_FIN["kr"]
    we_ss, we_sk = ETF_SS / 100, ETF_SK / 100
    k_kr = (1 - wt_ss - wt_sk) / (1 - we_ss - we_sk)
    c_ss = wt_ss - we_ss * k_kr
    c_sk = wt_sk - we_sk * k_kr

    print(f"Taiwan = {k_tw:.4f} ETF + {c_tsmc:.4f} TSMC"
          f"   (somme {k_tw + c_tsmc:.4f})")
    print(f"Coree  = {k_kr:.4f} ETF + {c_ss:.4f} Samsung + {c_sk:.4f} SKHynix"
          f"   (somme {k_kr + c_ss + c_sk:.4f})")

    # --- 4. ratios par compartiment, bruts et decapes ---
    r_tsmc = rendements(en_euros(cours["tsmc"], t_twd))
    r_ss = rendements(en_euros(cours["ss"], t_krw))
    r_sk = rendements(en_euros(cours["sk"], t_krw))

    brut = {}
    for cle in ("tw", "kr", "cn", "in"):
        brut[cle] = combiner([rendements(cours[cle])], [1.0])
    brut["autres"] = [1.0] * len(calendrier)

    decape = dict(brut)
    decape["tw"] = combiner([rendements(cours["tw"]), r_tsmc], [k_tw, c_tsmc])
    decape["kr"] = combiner([rendements(cours["kr"]), r_ss, r_sk],
                            [k_kr, c_ss, c_sk])

    # --- 5. ablation ---
    resultats = []
    for nom, longueur in FENETRES:
        i0 = max(0, len(calendrier) - longueur)
        cible_temoin = perf(cours["temoin"], i0)
        cible_ref = perf(cours["ref"], i0)

        bloc = {"fenetre": nom, "du": calendrier[i0], "au": calendrier[-1],
                "temoin_CEBL": round(cible_temoin, 2),
                "ref_PAASI": round(cible_ref, 2), "variantes": {}}

        print(f"\n=== {nom} : du {calendrier[i0]} au {calendrier[-1]} ===")
        print(f"  CEBL.DE {cible_temoin:+7.2f} %   PAASI.PA {cible_ref:+7.2f} %")
        print(f"  {'variante':34s} {'somme':>8s} {'vs CEBL':>9s} {'vs PAASI':>9s}")

        for nomv, ratios, debut in (
                ("A  poids fin  + ETF bruts",   brut,    False),
                ("B  poids debut + ETF bruts",  brut,    True),
                ("C  poids fin  + decapage",    decape,  False),
                ("D  poids debut + decapage",   decape,  True)):
            p = poids_debut(POIDS_FIN, ratios, i0) if debut else POIDS_FIN
            somme = sum(p[k] * (ratios[k][-1] / ratios[k][i0] - 1)
                        for k in POIDS_FIN)
            print(f"  {nomv:34s} {somme:+8.2f} {somme - cible_temoin:+9.2f} "
                  f"{somme - cible_ref:+9.2f}")
            bloc["variantes"][nomv.split()[0]] = {
                "libelle": nomv, "somme": round(somme, 3),
                "ecart_temoin": round(somme - cible_temoin, 3),
                "ecart_ref": round(somme - cible_ref, 3),
                "poids": {k: round(v, 2) for k, v in p.items()},
            }

        # detail par compartiment, variante D
        p = poids_debut(POIDS_FIN, decape, i0)
        print(f"\n  detail variante D")
        for k in POIDS_FIN:
            perf_k = (decape[k][-1] / decape[k][i0] - 1) * 100
            brut_k = (brut[k][-1] / brut[k][i0] - 1) * 100
            print(f"    {k:7s} poids {p[k]:5.2f} %  perf decapee {perf_k:+7.2f} %"
                  f"  (ETF brut {brut_k:+7.2f} %)"
                  f"  contrib {p[k] * perf_k / 100:+6.2f} pt")
        for nom_t, r in (("TSMC", r_tsmc), ("Samsung", r_ss), ("SKHynix", r_sk)):
            c = 1.0
            for x in r[i0 + 1:]:
                c *= (1 + x)
            print(f"    {nom_t:9s} en euros sur la fenetre : {(c - 1) * 100:+7.2f} %")

        resultats.append(bloc)

    with open(OUT_FILE, "w", encoding="utf-8") as fh:
        json.dump({
            "generated": dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "coefficients": {
                "taiwan": {"etf": round(k_tw, 4), "tsmc": round(c_tsmc, 4)},
                "coree": {"etf": round(k_kr, 4), "samsung": round(c_ss, 4),
                          "skhynix": round(c_sk, 4)},
            },
            "diagnostics": diags,
            "ablation": resultats,
        }, fh, ensure_ascii=False, indent=1)
    print(f"\n-> {OUT_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

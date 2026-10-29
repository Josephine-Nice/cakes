#!/usr/bin/env python3
"""
Josephine (Nice) - message de production des cakes pour Paula.

Lun-ven a 17h15, le script :
  1. recupere les ventes Square (parts de cakes, sur place + a emporter),
  2. estime le stock restant (journal de production + ventes, premier entre premier sorti, DLC 4 jours),
  3. prevoit la demande des jours a couvrir (memes jours de semaine passes, +10 %),
  4. calcule les cakes a produire (max 6 par jour, en avancant la production si besoin),
  5. envoie le message a Paula par SMS ou WhatsApp (Twilio).

Usage :
  python production_cakes.py --dry-run                      # API Square, n'envoie rien
  python production_cakes.py                                # API Square + envoi + journal
  python production_cakes.py --csv export.csv --date 2026-10-30 --dry-run   # test sur un export

Cron (serveur en heure de Paris) :  15 17 * * 1-5  python /chemin/production_cakes.py
"""
import argparse
import csv
import io
import json
import os
import re
import sys
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# ----------------------------------------------------------------- reglages
TZ = ZoneInfo("Europe/Paris")
PARTS_PAR_CAKE = 11
CAKES_MAX_PAR_JOUR = 6       # capacite de Paula
CAKES_CIBLE_DEFAUT = 5       # charge visee par jour (marge sous le plafond)
CAKES_CIBLE_PAR_JOUR = {4: 4}   # vendredi : 4 seulement, pour pre-fabriquer le stock du week-end/lundi
DLC_JOURS = 4               # un cake produit le jour J se vend de J+1 a J+4
DLC_PAR_SAVEUR = {"Audrey (rose framboise)": 3}   # exceptions : Audrey se vend de J+1 a J+3


def dlc(saveur):
    return DLC_PAR_SAVEUR.get(saveur, DLC_JOURS)
SEMAINES_HISTORIQUE = 4      # nb de semaines regardees pour la prevision
MARGE = 1.10                 # +10 % de securite
SEUIL_ARRONDI = 0.4          # un cake de plus des que la fraction depasse 40 %
JOURS_OUVERTS = {0, 1, 2, 3, 4, 5}   # cafe : lundi -> samedi
JOURS_PAULA = {0, 1, 2, 3, 4}        # Paula : lundi -> vendredi (prise de poste 17h30)
HORIZON_PRODUCTIONS = 6      # nb de productions simulees a l'avance
JOURNAL = Path(os.environ.get("JOURNAL_PRODUCTION", Path(__file__).with_name("journal_production.json")))

# Libelle affiche dans le message -> mot-cle du nom de l'article Square
SAVEURS = {
    "Mel (marbré chocolat)": "Mel",
    "Celia (citron)": "Celia",
    "Elisa (carrot cake)": "Elisa",
    "Val (matcha chocolat blanc)": "Val",
    "Audrey (rose framboise)": "Audrey",
}
JOURS_FR = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
SQUARE_URL = "https://connect.squareup.com/v2"


# ------------------------------------------------------------- identification
def saveur_de(article: str):
    """'Cake de Celia - Citron ae' -> 'Celia (citron)'. None si ce n'est pas une part de cake."""
    if not re.match(r"\s*cake\b", article, re.I):
        return None
    for libelle, mot in SAVEURS.items():
        if re.search(rf"\bcake d[e']\s?{mot}\b", article, re.I):
            return libelle
    return None


# ---------------------------------------------------------------- historique
def ventes_depuis_csv(chemin):
    """{date: {saveur: parts}} depuis un export Square (CSV UTF-16 ou UTF-8)."""
    brut = open(chemin, "rb").read()
    texte = brut.decode("utf-16") if brut[:2] in (b"\xff\xfe", b"\xfe\xff") else brut.decode("utf-8-sig")
    delim = "\t" if "\t" in texte.splitlines()[0] else ","
    ventes = defaultdict(lambda: defaultdict(float))
    for ligne in csv.DictReader(io.StringIO(texte), delimiter=delim):
        s = saveur_de(ligne["Article"])
        if s:
            ventes[date.fromisoformat(ligne["Date"])][s] += float(ligne["Qté"].replace(",", "."))
    return ventes


def ventes_depuis_square(debut: date, fin: date):
    """{date: {saveur: parts}} via l'API Square (commandes terminees)."""
    token = os.environ["SQUARE_ACCESS_TOKEN"]
    lieu = os.environ["SQUARE_LOCATION_ID"]
    en_tetes = {"Authorization": f"Bearer {token}", "Square-Version": "2024-10-17"}
    t0 = datetime.combine(debut, time.min, TZ).astimezone(ZoneInfo("UTC"))
    t1 = datetime.combine(fin, time.min, TZ).astimezone(ZoneInfo("UTC"))
    corps = {
        "location_ids": [lieu],
        "query": {
            "filter": {
                "state_filter": {"states": ["COMPLETED"]},
                "date_time_filter": {"closed_at": {
                    "start_at": t0.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "end_at": t1.strftime("%Y-%m-%dT%H:%M:%SZ")}},
            },
            "sort": {"sort_field": "CLOSED_AT", "sort_order": "ASC"},
        },
        "limit": 500,
    }
    ventes = defaultdict(lambda: defaultdict(float))
    while True:
        r = requests.post(f"{SQUARE_URL}/orders/search", json=corps, headers=en_tetes, timeout=30)
        r.raise_for_status()
        data = r.json()
        for cmd in data.get("orders", []):
            jour = datetime.fromisoformat(cmd["closed_at"].replace("Z", "+00:00")).astimezone(TZ).date()
            for li in cmd.get("line_items", []):
                s = saveur_de(li.get("name", ""))
                if s:
                    ventes[jour][s] += float(li.get("quantity", 1))
        if not data.get("cursor"):
            return ventes
        corps["cursor"] = data["cursor"]


# ------------------------------------------------------------------ calendrier
def prochain(d: date, jours: set) -> date:
    d += timedelta(days=1)
    while d.weekday() not in jours:
        d += timedelta(days=1)
    return d


def jours_couverts(p: date):
    """Jours d'ouverture que la production du jour `p` doit couvrir :
    de la prochaine ouverture jusqu'a la veille de la 1re ouverture suivant la production suivante.
    Ex. production du vendredi -> samedi + lundi ; du lundi -> mardi seulement."""
    debut = prochain(p, JOURS_OUVERTS)
    fin = prochain(prochain(p, JOURS_PAULA), JOURS_OUVERTS)
    jours, j = [], debut
    while j < fin:
        if j.weekday() in JOURS_OUVERTS:
            jours.append(j)
        j += timedelta(days=1)
    return jours


# ----------------------------------------------------------------- prevision
def prevoir(ventes, cible: date):
    """Parts attendues par saveur pour `cible` (memes jours de semaine passes, +marge)."""
    passes = sorted(d for d in ventes if d < cible and d.weekday() == cible.weekday())[-SEMAINES_HISTORIQUE:]
    if not passes:
        sys.exit(f"Pas d'historique de ventes pour un {JOURS_FR[cible.weekday()]}.")
    poids = range(1, len(passes) + 1)   # les semaines recentes comptent plus
    attendu = {s: sum(w * ventes[d].get(s, 0) for w, d in zip(poids, passes)) / sum(poids) * MARGE
               for s in SAVEURS}
    return attendu, len(passes)


# --------------------------------------------------------------------- stock
def consommer(lots, jour: date, demande: float, dlc_jours: int = DLC_JOURS) -> float:
    """Vend `demande` parts le `jour`, des lots les plus anciens aux plus recents.
    Un lot [date_prod, parts] est vendable de prod+1 a prod+DLC. Retourne les parts non servies."""
    lots[:] = [l for l in lots if l[0] + timedelta(days=dlc_jours) >= jour]   # purge des perimes
    restant = demande
    for l in sorted(lots):
        if l[0] >= jour or restant <= 1e-9:
            continue
        pris = min(l[1], restant)
        l[1] -= pris
        restant -= pris
    lots[:] = [l for l in lots if l[1] > 1e-9]
    return restant


def lire_journal():
    if JOURNAL.exists():
        return json.loads(JOURNAL.read_text())
    return []


def ecrire_journal(jour: date, cakes):
    entrees = [e for e in lire_journal() if e["date"] != jour.isoformat()]
    entrees.append({"date": jour.isoformat(), "cakes": cakes})
    JOURNAL.write_text(json.dumps(sorted(entrees, key=lambda e: e["date"]), indent=2, ensure_ascii=False))


MOTS_CLES = {   # ce que Nathalie peut ecrire dans la colonne "Saveur" (sans accents ni majuscules)
    "Mel (marbré chocolat)": ["mel", "marbre"],
    "Celia (citron)": ["celia", "citron"],
    "Elisa (carrot cake)": ["elisa", "carrot", "carotte"],
    "Val (matcha chocolat blanc)": ["val", "matcha"],
    "Audrey (rose framboise)": ["audrey", "rose", "framboise"],
}


def _sans_accents(t: str) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", t.lower()) if unicodedata.category(c) != "Mn")


def _saveur_souple(texte: str):
    t = _sans_accents(texte)
    return next((lib for lib, mots in MOTS_CLES.items() if any(m in t for m in mots)), None)


def _date_souple(texte: str):
    m = re.fullmatch(r"\s*(\d{1,2})[/.-](\d{1,2})\s*", texte)   # "28/10" : annee en cours
    if m:
        try:
            return date(datetime.now(TZ).year, int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y"):
        try:
            return datetime.strptime(texte.strip(), fmt).date()
        except ValueError:
            pass
    return None


def lire_comptages(source=None):
    """Comptages physiques saisis par Nathalie (Google Sheet publie en CSV, ou fichier local de test).
    Colonnes : Date | Saveur | Parts restantes | Produit le (facultatif).
    Retourne {saveur: [(date_comptage, parts, date_production), ...]}. Facultatif : {} si aucune source."""
    source = source or os.environ.get("STOCK_SHEET_URL")
    if not source:
        return {}
    try:
        if source.startswith("http"):
            r = requests.get(source, timeout=30)
            r.raise_for_status()
            texte = r.content.decode("utf-8-sig")
        else:
            texte = Path(source).read_text(encoding="utf-8-sig")
    except (requests.RequestException, OSError) as err:
        print(f"[attention] feuille de stock illisible ({err}) : stock théorique utilisé", file=sys.stderr)
        return {}
    delim = ";" if texte.splitlines() and texte.splitlines()[0].count(";") > texte.splitlines()[0].count(",") else ","
    res = defaultdict(list)
    for ligne in csv.DictReader(io.StringIO(texte), delimiter=delim):
        l = {(k or "").strip().lower(): (v or "").strip() for k, v in ligne.items()}
        d = _date_souple(l.get("date", ""))
        saveur = _saveur_souple(l.get("saveur", ""))
        try:
            parts = float(l.get("parts restantes", "").replace(",", "."))
        except ValueError:
            parts = None
        if not (d and saveur) or parts is None:
            if any(l.values()):
                print(f"[attention] ligne de stock ignorée : {dict(ligne)}", file=sys.stderr)
            continue
        prod = _date_souple(l.get("produit le", "")) or (d - timedelta(days=1))   # par defaut : la veille
        res[saveur].append((d, parts, min(prod, d)))
    return dict(res)


def dernier_comptage(comptages, saveur, aujourdhui: date):
    liste = [c for c in comptages.get(saveur, []) if c[0] <= aujourdhui]
    if not liste:
        return None
    meilleur = max(c[0] for c in liste)
    return [c for c in liste if c[0] == meilleur][-1]   # a date egale, la derniere ligne saisie gagne


def stock_estime(ventes, aujourdhui: date, comptages=None):
    """Stock estime par saveur : repart du dernier comptage physique s'il existe, sinon du journal de production ;
    rejoue ensuite productions et ventes reelles jusqu'a aujourd'hui."""
    comptages = comptages or {}
    journal = [e for e in lire_journal() if date.fromisoformat(e["date"]) < aujourdhui]   # la production du jour meme est vendable des demain
    lots = {s: [] for s in SAVEURS}
    for s in SAVEURS:
        cpt = dernier_comptage(comptages, s, aujourdhui)
        debut = cpt[0] if cpt else None
        if cpt and cpt[1] > 0:
            lots[s].append([cpt[2], cpt[1]])
        prods = []
        for e in journal:
            d, n = date.fromisoformat(e["date"]), e["cakes"].get(s, 0)
            if n and (debut is None or d >= debut):   # comptage fait en fermant, avant la production du soir
                prods.append(d)
                lots[s].append([d, n * PARTS_PAR_CAKE])
        depart = debut or (min(date.fromisoformat(e["date"]) for e in journal) if journal else None)
        if depart is None:
            continue
        j = depart + timedelta(days=1)
        while j <= aujourdhui:
            consommer(lots[s], j, ventes.get(j, {}).get(s, 0), dlc(s))
            j += timedelta(days=1)
    return lots


# ---------------------------------------------------------------------- plan
def arrondi_cakes(parts: float) -> int:
    entiers, frac = divmod(parts / PARTS_PAR_CAKE, 1)
    return int(entiers) + (1 if frac >= SEUIL_ARRONDI else 0)


def planifier(ventes, stock, aujourdhui: date):
    """Liste de productions successives [{'jour', 'couvre', 'items', 'manque'}], la 1re = aujourd'hui."""
    lots = {s: [l[:] for l in v] for s, v in stock.items()}
    jours_prod, p = [], aujourdhui
    for _ in range(HORIZON_PRODUCTIONS):
        jours_prod.append(p)
        p = prochain(p, JOURS_PAULA)

    plan = []
    for p in jours_prod:
        couvre = jours_couverts(p)
        demande = {j: prevoir(ventes, j)[0] for j in couvre}
        # 1) ce qui manque malgre le stock
        test = {s: [l[:] for l in lots[s]] for s in SAVEURS}
        manque = {s: sum(consommer(test[s], j, demande[j][s], dlc(s)) for j in couvre) for s in SAVEURS}
        cakes = {s: arrondi_cakes(manque[s]) for s in SAVEURS}
        # 2) on ajoute la production au stock, puis on vend les jours couverts
        for s in SAVEURS:
            if cakes[s]:
                lots[s].append([p, cakes[s] * PARTS_PAR_CAKE])
            for j in couvre:
                consommer(lots[s], j, demande[j][s], dlc(s))
        items = [(s, couvre[-1]) for s in SAVEURS for _ in range(cakes[s])]   # (saveur, dernier jour a couvrir)
        plan.append({"jour": p, "couvre": couvre, "items": items, "manque": manque, "coupes": 0})

    # 3) lissage de la charge, en remontant le temps. Les cakes en trop sont avances aux jours
    #    precedents tant que la DLC le permet ; le vendredi vise CAKES_CIBLE_PAR_JOUR (< plafond) pour
    #    garder de la marge, le plafond de 6 n'etant qu'une limite dure.
    def avancer(i, item):
        """Deplace un cake du plan i vers la production precedente si sa DLC le permet
        ET si cette production a encore de la place (sinon le cake reste au jour d'origine)."""
        if (i > 0 and len(plan[i - 1]["items"]) < CAKES_MAX_PAR_JOUR
                and item[1] <= plan[i - 1]["jour"] + timedelta(days=dlc(item[0]))):
            plan[i]["items"].remove(item)
            plan[i - 1]["items"].append(item)
            return True
        return False

    for i in range(len(plan) - 1, -1, -1):
        cur = plan[i]
        cible = CAKES_CIBLE_PAR_JOUR.get(cur["jour"].weekday(), CAKES_CIBLE_DEFAUT)
        # a) objectif souple : on avance ce qui depasse la cible
        for item in sorted(cur["items"], key=lambda it: cur["manque"][it[0]]):
            if len(cur["items"]) <= cible:
                break
            avancer(i, item)
        # b) limite dure : au-dela de 6, on avance quoi qu'il arrive, sinon on coupe
        while len(cur["items"]) > CAKES_MAX_PAR_JOUR:
            item = min(cur["items"], key=lambda it: cur["manque"][it[0]])
            if not avancer(i, item):
                cur["items"].remove(item)
                cur["coupes"] += 1
    return plan


def comptage(items):
    c = {s: 0 for s in SAVEURS}
    for s, _ in items:
        c[s] += 1
    return c


def libelle_jours(jours):
    return " et ".join(f"{JOURS_FR[j.weekday()]} {j:%d/%m}" for j in jours)


def message(prod, nb_semaines):
    c = comptage(prod["items"])
    total = sum(c.values())
    tete = f"Bonjour Paula, cakes à produire ce soir (pour {libelle_jours(prod['couvre'])}) :"
    if total == 0:
        return tete + "\nRien à produire, le stock suffit."
    lignes = [tete] + [f"- {n} {s}" for s, n in c.items() if n]
    lignes.append(f"Total : {total} cakes")
    if prod["coupes"]:
        lignes.append(f"(plafond de {CAKES_MAX_PAR_JOUR} cakes atteint : {prod['coupes']} cake(s) non produit(s))")
    if nb_semaines < 3:
        lignes.append("(estimation provisoire, peu d'historique)")
    return "\n".join(lignes)


# -------------------------------------------------------------------- envoi
def envoyer(texte):
    sid, auth = os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"]
    destinataire = os.environ["PAULA_NUMERO"]            # ex. +33612345678
    if os.environ.get("CANAL", "sms").lower() == "whatsapp":
        expediteur = "whatsapp:" + os.environ["TWILIO_WHATSAPP_FROM"]
        destinataire = "whatsapp:" + destinataire
    else:
        expediteur = os.environ["TWILIO_SMS_FROM"]
    r = requests.post(
        f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
        auth=(sid, auth),
        data={"From": expediteur, "To": destinataire, "Body": texte},
        timeout=30,
    )
    if not r.ok:
        try:
            err = r.json()
            detail = f"code {err.get('code')} : {err.get('message')}"
        except ValueError:
            detail = r.text
        sys.exit(f"Twilio a refusé l'envoi (HTTP {r.status_code}) - {detail}")
    print("Message envoyé :", r.json().get("sid"))


# ----------------------------------------------------------------- page HTML
def generer_html(ventes, plan, stock, aujourdhui: date, chemin, comptages=None):
    """Ecrit une page HTML : plan de production, stock estime et detail des calculs."""
    from html import escape as e
    court = {s: s.split(" ")[0] for s in SAVEURS}
    jfr = lambda d: f"{JOURS_FR[d.weekday()][:3]}. {d:%d/%m}"

    # 1) plan de production
    lignes = []
    for pr in plan[:6]:
        c = comptage(pr["items"])
        total = sum(c.values())
        cellules = "".join(f"<td class='n{' z' if not c[s] else ''}'>{c[s] or '·'}</td>" for s in SAVEURS)
        barre = f"<div class='bar'><i style='width:{min(total, CAKES_MAX_PAR_JOUR) / CAKES_MAX_PAR_JOUR * 100:.0f}%'></i></div>"
        note = f"<span class='warn'> {pr['coupes']} cake(s) non produit(s) : plafond</span>" if pr["coupes"] else ""
        lignes.append(f"<tr><td><b>{e(JOURS_FR[pr['jour'].weekday()].capitalize())} {pr['jour']:%d/%m}</b></td>"
                      f"<td class='sm'>{e(' + '.join(jfr(j) for j in pr['couvre']))}</td>{cellules}"
                      f"<td class='n'><b>{total}</b>{barre}{note}</td></tr>")
    entetes = "".join(f"<th>{e(court[s])}</th>" for s in SAVEURS)
    plan_html = (f"<table><tr><th>Production (soir)</th><th>Pour les ventes de</th>{entetes}"
                 f"<th>Total / {CAKES_MAX_PAR_JOUR}</th></tr>{''.join(lignes)}</table>")

    # 2) stock estime
    st = [f"<td class='n'>{sum(l[1] for l in stock[s]):.0f}</td>" for s in SAVEURS]
    def age(s):
        c = dernier_comptage(comptages or {}, s, aujourdhui)
        return f"<td class='n sm'>{c[0]:%d/%m} ({(aujourdhui - c[0]).days} j)</td>" if c else "<td class='n sm'>jamais</td>"
    stock_html = (f"<table><tr><th>Parts en stock estimé ce soir</th>{entetes}</tr>"
                  f"<tr><td>au {aujourdhui:%d/%m}</td>{''.join(st)}</tr>"
                  f"<tr><td class='sm'>dernier comptage physique</td>{''.join(age(s) for s in SAVEURS)}</tr></table>")

    # 3) detail des calculs, jour par jour
    blocs = []
    vus = set()
    for pr in plan[:5]:
        for j in pr["couvre"]:
            if j in vus:
                continue
            vus.add(j)
            passes = sorted(d for d in ventes if d < j and d.weekday() == j.weekday())[-SEMAINES_HISTORIQUE:]
            if not passes:
                continue
            attendu, _ = prevoir(ventes, j)
            poids = list(range(1, len(passes) + 1))
            th = "".join(f"<th>{jfr(d)}</th>" for d in passes)
            rows = []
            for s in SAVEURS:
                hist = [ventes[d].get(s, 0) for d in passes]
                moy = sum(w * h for w, h in zip(poids, hist)) / sum(poids)
                cells = "".join(f"<td class='n'>{h:.0f}</td>" for h in hist)
                rows.append(f"<tr><td>{e(s)}</td>{cells}<td class='n'>{moy:.1f}</td>"
                            f"<td class='n'><b>{attendu[s]:.1f}</b></td>"
                            f"<td class='n'>{attendu[s] / PARTS_PAR_CAKE:.2f}</td></tr>")
            blocs.append(f"<h3>Ventes attendues {e(JOURS_FR[j.weekday()])} {j:%d/%m}</h3>"
                         f"<table><tr><th>Saveur</th>{th}<th>Moyenne pondérée</th>"
                         f"<th>× {MARGE:.2f} = parts attendues</th><th>= cakes</th></tr>{''.join(rows)}</table>")

    # 0) bandeau pour Paula : ce qu'il faut produire ce soir
    p0 = plan[0]
    c0 = comptage(p0["items"])
    total0 = sum(c0.values())
    titre0 = f"{JOURS_FR[p0['jour'].weekday()].capitalize()} {p0['jour']:%d/%m}"
    pour0 = e(libelle_jours(p0["couvre"]))
    if total0:
        tuiles = "".join(
            f"<div class='tuile'><div class='gros'>{c0[s]}</div><div class='nomc'>{e(court[s])}</div>"
            f"<div class='sm'>{e(s[len(court[s]):].strip(' ()'))}</div></div>"
            for s in SAVEURS if c0[s])
        corps0 = (f"<div class='tuiles'>{tuiles}</div>"
                  f"<p class='tot'>Total : <b>{total0} cake{'s' if total0 > 1 else ''}</b> "
                  f"<span class='sm'>({total0 * PARTS_PAR_CAKE} parts)</span></p>")
    else:
        corps0 = "<p class='tot'><b>Rien à produire ce soir</b> : le stock suffit.</p>"
    if p0["coupes"]:
        corps0 += (f"<p class='warn'>Plafond de {CAKES_MAX_PAR_JOUR} cakes atteint : "
                   f"{p0['coupes']} cake(s) non produit(s).</p>")
    bandeau = (f"<section class='paula'><div class='sm'>À produire ce soir · {e(titre0)}</div>"
               f"<h1>Bonjour Paula 👋</h1><p class='pour'>Cakes pour les ventes de {pour0}</p>{corps0}</section>")

    # 0 bis) bilan jour par jour : produit / vendu (theorie) / stock en fin de journee
    lots = {s: [l[:] for l in stock[s]] for s in SAVEURS}
    prod_par_jour = {pr["jour"]: comptage(pr["items"]) for pr in plan}
    jours, d = [], aujourdhui
    while d <= plan[min(5, len(plan) - 1)]["jour"]:
        if d.weekday() in JOURS_OUVERTS:
            jours.append(d)
        d += timedelta(days=1)
    bilan = {s: [] for s in SAVEURS}          # saveur -> [(produit_cakes, vendu_parts, stock_parts, manque_parts)]
    for j in jours:
        for s in SAVEURS:
            if j == aujourdhui:               # le stock estime est deja apres les ventes reelles du jour
                vendu, manque = ventes.get(j, {}).get(s, 0), 0
            else:
                vendu = prevoir(ventes, j)[0][s]
                manque = consommer(lots[s], j, vendu, dlc(s))
            n = prod_par_jour.get(j, {}).get(s, 0)
            if n:
                lots[s].append([j, n * PARTS_PAR_CAKE])      # produit le soir : vendable des le lendemain
            lots[s][:] = [l for l in lots[s] if l[0] + timedelta(days=dlc(s)) >= j + timedelta(days=1)]
            bilan[s].append((n, vendu, sum(l[1] for l in lots[s]), manque))
    tetes = "".join(f"<th>{jfr(j)}{'<br><span class=sm>aujourd’hui</span>' if j == aujourdhui else ''}</th>" for j in jours)

    def fr(v):   # parts -> cakes, une decimale, virgule francaise ("1,5"), sans ",0"
        t = f"{v / PARTS_PAR_CAKE:.1f}".replace(".", ",")
        return t[:-2] if t.endswith(",0") else t

    def cel(v, gras=False, zero=False, cakes=False):
        txt = (fr(v) if cakes else f"{v:.0f}") if (v >= 0.05 or zero) else "·"
        txt = f"<b>{txt}</b>" if gras and v else txt
        return f"<td class='n{' z' if not v and not zero else ''}'>{txt}</td>"

    def cel_stock(v, manque):
        alerte = f"<div class='rupt'>manque {fr(manque)}</div>" if manque >= 0.5 else ""
        return f"<td class='n'>{fr(v) if v >= 0.05 else '0'}{alerte}</td>"

    def bloc(nom, donnees):
        l1 = "".join(cel(x[0], gras=True) for x in donnees)
        l2 = "".join(cel(x[1], cakes=True) for x in donnees)
        l3 = "".join(cel_stock(x[2], x[3]) for x in donnees)
        return (f"<tbody><tr><th rowspan='3' class='fl'>{e(nom)}</th><td class='lb'>Produit (cakes)</td>{l1}</tr>"
                f"<tr><td class='lb'>Vendu (cakes)</td>{l2}</tr>"
                f"<tr class='fin'><td class='lb'>Stock fin de journée (cakes)</td>{l3}</tr></tbody>")
    tot = [tuple(sum(bilan[s][i][k] for s in SAVEURS) for k in range(4)) for i in range(len(jours))]
    bilan_html = (f"<table class='bilan'><tr><th></th><th></th>{tetes}</tr>{bloc('Total', tot)}"
                  + "".join(bloc(court[s], bilan[s]) for s in SAVEURS) + "</table>")

    page = f"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>Cakes à produire · Joséphine</title>
<style>
:root{{--bg:#fbf7ee;--fg:#2a2320;--mut:#7a6f66;--line:#e5dccb;--acc:#b8412f;--card:#fff}}
@media (prefers-color-scheme:dark){{:root{{--bg:#1f1b18;--fg:#f0e9de;--mut:#a89d92;--line:#3a332d;--acc:#e8826f;--card:#2a2522}}}}
body{{font:15px/1.45 -apple-system,system-ui,sans-serif;background:var(--bg);color:var(--fg);margin:0;padding:24px 16px}}
main{{max-width:980px;margin:auto}} h1{{font-size:24px;margin:0 0 4px}} h2{{font-size:18px;margin:32px 0 8px}} h3{{font-size:15px;margin:22px 0 6px}}
p.mut{{color:var(--mut);margin:4px 0 12px}} .sm{{font-size:13px;color:var(--mut)}}
table{{border-collapse:collapse;width:100%;background:var(--card);border:1px solid var(--line);border-radius:8px;overflow:hidden;font-size:14px}}
th,td{{padding:7px 10px;border-bottom:1px solid var(--line);text-align:left}} th{{font-size:12px;color:var(--mut);font-weight:600}}
td.n,th:not(:first-child){{text-align:right}} td.z{{color:var(--mut)}}
.bar{{height:5px;background:var(--line);border-radius:3px;margin-top:4px}} .bar i{{display:block;height:100%;background:var(--acc);border-radius:3px}}
.warn{{color:var(--acc);font-size:12px}} .wrap{{overflow-x:auto}}
.paula{{background:var(--card);border:2px solid var(--acc);border-radius:16px;padding:20px 20px 16px;margin-bottom:8px}}
.paula h1{{font-size:28px;margin:2px 0 2px}} .pour{{font-size:17px;margin:0 0 16px}}
.tuiles{{display:flex;flex-wrap:wrap;gap:12px}}
.tuile{{flex:1 1 130px;max-width:200px;background:var(--bg);border:1px solid var(--line);border-radius:14px;padding:12px 8px;text-align:center}}
.gros{{font-size:54px;font-weight:700;line-height:1;color:var(--acc)}} .nomc{{font-size:20px;font-weight:600;margin-top:4px}}
.tot{{font-size:20px;margin:16px 0 0}} .maj{{margin:10px 0 0}}
.bilan td.lb{{font-size:12px;color:var(--mut);text-align:left;white-space:nowrap}} .bilan th.fl{{vertical-align:middle;font-size:14px;color:var(--fg);text-align:left}}
.bilan tbody{{border-top:2px solid var(--line)}} .bilan tr.fin td{{background:rgba(184,65,47,.06)}} .bilan .rupt{{color:var(--acc);font-weight:600;font-size:11px}}
h2.det{{margin-top:36px;padding-top:12px;border-top:1px solid var(--line);font-size:20px}}
</style></head><body><main>
{bandeau}
<p class="sm maj">Mis à jour le {datetime.now(TZ):%d/%m/%Y à %H:%M} à partir des ventes Square.</p>
<h2 class="det">Détail : plan de la semaine, stock et calculs</h2>
<p class="mut">{PARTS_PAR_CAKE} parts par cake, DLC {DLC_JOURS} jours (Audrey : {dlc('Audrey (rose framboise)')} jours), {CAKES_MAX_PAR_JOUR} cakes maximum par jour.</p>
<h2>Plan de la semaine</h2><div class="wrap">{plan_html}</div>
<p class="sm">Les jours à venir dépendent de ce qui sera réellement produit et vendu : seuls les premiers jours sont fiables.</p>
<h2>Bilan jour par jour</h2>
<p class="mut">Ce qui est produit le soir (vendable dès le lendemain), ce qui sera vendu en théorie dans la journée (moyenne pondérée + marge), et le stock qui reste en fin de journée, après la production du soir. Tout est en cakes (1 cake = {PARTS_PAR_CAKE} parts, donc 0,5 = un demi-cake). « manque N » (en rouge) signale que la demande prévue ce jour-là dépasse le stock de N cakes.</p>
<div class="wrap">{bilan_html}</div>
<h2>Stock estimé</h2><div class="wrap">{stock_html}</div>
<h2>Détail des calculs</h2>
<p class="mut">Pour chaque jour : les ventes réelles des mêmes jours de semaine passés (les plus récents comptent plus), la moyenne pondérée, puis la marge de {int((MARGE - 1) * 100)} %.</p>
<div class="wrap">{''.join(blocs)}</div>
</main></body></html>"""
    Path(chemin).write_text(page, encoding="utf-8")


# ----------------------------------------------------------------- reglages .env
def charger_env(chemin=None):
    """Lit le fichier .env a cote du script (NOM=valeur par ligne). Tolere les espaces et les guillemets,
    et n'affiche jamais les valeurs. Les variables deja definies dans l'environnement restent prioritaires."""
    chemin = Path(chemin) if chemin else Path(__file__).with_name(".env")
    if not chemin.exists():
        return
    for ligne in chemin.read_text(encoding="utf-8-sig").splitlines():
        ligne = ligne.strip()
        if not ligne or ligne.startswith("#") or "=" not in ligne:
            continue
        cle, valeur = ligne.split("=", 1)
        valeur = valeur.strip().strip("'\"")
        if cle.strip() and valeur:
            os.environ.setdefault(cle.strip(), valeur)


# --------------------------------------------------------------------- main
def main():
    charger_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", help="export Square au lieu de l'API (test)")
    ap.add_argument("--date", help="jour de production AAAA-MM-JJ (test), par defaut aujourd'hui")
    ap.add_argument("--dry-run", action="store_true", help="affiche sans envoyer ni ecrire le journal")
    ap.add_argument("--journal-seul", action="store_true", help="ecrit le journal sans envoyer (test)")
    ap.add_argument("--stock-csv", help="fichier ou lien CSV des comptages physiques (sinon STOCK_SHEET_URL)")
    ap.add_argument("--html", nargs="?", const="plan_production.html",
                    help="cree une page HTML (plan + calculs), sans rien envoyer ni ecrire dans le journal")
    ap.add_argument("--journal-apres", metavar="HH:MM",
                    help="avec --html : enregistre la production du jour dans le journal si l'heure (Paris) est >= HH:MM")
    a = ap.parse_args()

    aujourdhui = date.fromisoformat(a.date) if a.date else datetime.now(TZ).date()
    if aujourdhui.weekday() not in JOURS_PAULA:
        sys.exit("Paula ne travaille pas ce jour-la : rien a envoyer.")

    if a.csv:
        ventes = ventes_depuis_csv(a.csv)
    else:
        ventes = ventes_depuis_square(aujourdhui - timedelta(weeks=SEMAINES_HISTORIQUE + 1), aujourdhui + timedelta(days=1))

    comptages = lire_comptages(a.stock_csv)
    stock = stock_estime(ventes, aujourdhui, comptages)
    plan = planifier(ventes, stock, aujourdhui)
    nb_sem = prevoir(ventes, plan[0]["couvre"][0])[1]
    texte = message(plan[0], nb_sem)
    if a.html:
        Path(a.html).parent.mkdir(parents=True, exist_ok=True)
        generer_html(ventes, plan, stock, aujourdhui, a.html, comptages)
        print(f"Page créée : {a.html}")
        if a.journal_apres:
            h, m = map(int, a.journal_apres.split(":"))
            maintenant = datetime.now(TZ)
            if (maintenant.hour, maintenant.minute) >= (h, m):
                ecrire_journal(aujourdhui, {s: n for s, n in comptage(plan[0]["items"]).items() if n})
                print("Production du jour enregistrée dans le journal.")
            else:
                print(f"Journal non écrit (avant {a.journal_apres}).")
        return
    print(texte)

    print("\n[détail] stock estimé (parts) :",
          {s: round(sum(l[1] for l in v), 1) for s, v in stock.items() if v} or "aucun")
    for pr in plan[:4]:
        print(f"[détail] {JOURS_FR[pr['jour'].weekday()]} {pr['jour']:%d/%m} -> couvre {libelle_jours(pr['couvre'])} :",
              {s: n for s, n in comptage(pr["items"]).items() if n}, f"({len(pr['items'])} cakes)")

    if a.dry_run:
        return
    ecrire_journal(aujourdhui, {s: n for s, n in comptage(plan[0]["items"]).items() if n})
    if not a.journal_seul:
        envoyer(texte)


if __name__ == "__main__":
    main()

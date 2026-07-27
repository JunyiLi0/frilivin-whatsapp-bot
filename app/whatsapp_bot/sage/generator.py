"""Sage 50 import generator — vendored, adapted from the operator's script.

Kept deliberately close to the original: it has been validated against real
Sage imports, and rewriting it would risk changing output nobody would notice
until an invoice is wrong. Two changes only:

  - the Tkinter interface is gone (the bot never opens a window, and Tkinter is
    not installed in the slim image);
  - ``fabriquer`` accepts pre-built client/article indexes, so the worker can
    cache them instead of re-parsing 4 MB of exports on every spreadsheet.

Everything is standard library: the .xlsx files are read with zipfile + xml.
"""

import difflib
import re
import unicodedata
import zipfile
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

# ============================================================
#  CONFIGURATION  (à ajuster selon ton dossier Sage)
# ============================================================
SEUIL_CLIENT = 0.60
# Nombre minimal de mots au dénominateur du score de recouvrement client.
# Voir score_client() : garde-fou contre les fiches Sage à un seul mot.
MIN_MOTS_RECOUVREMENT = 2

# Formes juridiques et mots de liaison : présents dans un nom d'entreprise sur
# deux, ils ne distinguent rien. Les compter dans le recouvrement rattachait
# « ENTREPRISE ... SARL » à « KUBERA SARL » sur ce seul mot commun.
MOTS_VIDES = {
    "SARL", "SAS", "SASU", "EURL", "SA", "SNC", "SCI", "SCOP", "SELARL", "GIE",
    "ETS", "ETABLISSEMENTS", "STE", "SOCIETE", "CIE", "COMPAGNIE", "GROUPE",
    "LTD", "LLC", "INC", "GMBH", "BV", "SPA", "SRL", "NV", "PLC", "SPRL", "BVBA", "AG", "OY", "AB",
    "ET", "DE", "DU", "DES", "LA", "LE", "LES", "AU", "AUX",
}
GARDER_COULEUR_DANS_CODE = False
TYPE_PIECE        = "Facture"
VALIDEE           = "Non"            # brouillons à relire dans Sage ; passe à "Oui" si besoin
MODE_PAIEMENT     = "Autre"
FORME_JURIDIQUE   = "Aucune"
TARIF_CLIENT      = "Aucun"
STATUT_DEVIS      = "Non défini"
CADRE_FACTURATION = "B1 - Dépôt d'une facture de bien"
PAYS_LIVR_CODE, PAYS_LIVR_NOM = "FRA", "France"   # marchandise expédiée de France (EXW)
TAUX_LOCAL_DEFAUT = 20.0                            # % si l'article n'indique pas de taux

REGIME = {  # utilisé seulement en repli si le client n'est pas trouvé
    "FRANCE": "Local", "CEE": "CEE", "HORSCEE": "Hors CEE", "OUTREMER": "Hors CEE"}

# Catégorie TVA (col 82) déduite du Mode gestion TVA de la fiche client
CAT_PAR_MODE = {
    "LOCAL":              "S = Taux de TVA standard",
    "CEE":                "K = Exonération pour cause de livraison intracommunautaire",
    "HORS CEE":           "G = Exonération de TVA pour Export hors UE",
    "SUSPENSION DE TAXE": "O = Hors du périmètre d'application de la TVA",
}
# Motif d'exonération (col 83) déduit du Mode gestion TVA
MOTIF_PAR_MODE = {
    "CEE":                "Approvisionnement intracommunautaire",
    "HORS CEE":           "Exportation en dehors de l'Union Européenne",
    "SUSPENSION DE TAXE": "Non soumis à la TVA",
}
# Articles de mention (col 49) + leur description légale (col 55)
MENTION = {
    "CEE":      ("CEE",      "EXONERATION TVA. ART 262 TER I DU CODE GENERAL DES IMPOTS"),
    "OUTREMER": ("OUTREMER", "EXONERATION TVA. ART . 294-2 ALINEA 1. ET 262 I DU CODE. GENERAL DES IMPOTS"),
    "HORSCEE":  ("HORS CEE", "EXONERATION TVA ART 262 I DU CODE GENERAL DES IMPOTS EORI : FR821936754 INCO TERM : EXW"),
}
PAYS_DOM = {"MARTINIQUE", "LA REUNION", "REUNION", "GUADELOUPE", "GUYANE", "MAYOTTE",
    "MTQ", "REU", "GLP", "GUF", "MYT", "RE", "GP", "GF", "YT",
    "SAINT PIERRE ET MIQUELON", "NOUVELLE CALEDONIE", "POLYNESIE FRANCAISE"}
PAYS_UE = {"ALLEMAGNE", "AUTRICHE", "BELGIQUE", "BULGARIE", "CHYPRE", "CROATIE", "DANEMARK", "ESPAGNE",
    "ESTONIE", "FINLANDE", "GRECE", "HONGRIE", "IRLANDE", "ITALIE", "LETTONIE", "LITUANIE", "LUXEMBOURG",
    "MALTE", "PAYS-BAS", "POLOGNE", "PORTUGAL", "TCHEQUIE", "REPUBLIQUE TCHEQUE", "ROUMANIE",
    "SLOVAQUIE", "SLOVENIE", "SUEDE",
    # codes ISO2 fréquents en source
    "DE", "AT", "BE", "BG", "CY", "HR", "DK", "ES", "EE", "FI", "GR", "HU", "IE", "IT",
    "LV", "LT", "LU", "MT", "NL", "PL", "PT", "CZ", "RO", "SK", "SI", "SE"}
CP_OUTREMER = {"971", "972", "973", "974", "975", "976", "977", "978", "984", "986", "987", "988"}

# Table pays -> (ISO3, nom en clair). Sert à remplir Code pays (col 17) + normaliser le nom (col 18).
# Les alias couvrent le nom Sage (en clair) ET les codes ISO2 vus en source (FR, RE, ...).
_PAYS_TABLE = [
    (["FRANCE", "FR", "FRA"],                              "FRA", "France"),
    (["ITALIE", "IT", "ITA", "ITALY"],                     "ITA", "Italie"),
    (["BELGIQUE", "BE", "BEL"],                            "BEL", "Belgique"),
    (["PAYS-BAS", "PAYS BAS", "NL", "NLD", "NETHERLANDS"], "NLD", "Pays-Bas"),
    (["ALLEMAGNE", "DE", "DEU", "GERMANY"],                "DEU", "Allemagne"),
    (["ESPAGNE", "ES", "ESP", "SPAIN"],                    "ESP", "Espagne"),
    (["PORTUGAL", "PT", "PRT"],                            "PRT", "Portugal"),
    (["LUXEMBOURG", "LU", "LUX"],                          "LUX", "Luxembourg"),
    (["IRLANDE", "IE", "IRL"],                             "IRL", "Irlande"),
    (["AUTRICHE", "AT", "AUT"],                            "AUT", "Autriche"),
    (["POLOGNE", "PL", "POL"],                             "POL", "Pologne"),
    (["SUEDE", "SE", "SWE"],                               "SWE", "Suède"),
    (["DANEMARK", "DK", "DNK"],                            "DNK", "Danemark"),
    (["FINLANDE", "FI", "FIN"],                            "FIN", "Finlande"),
    (["GRECE", "GR", "GRC"],                               "GRC", "Grèce"),
    (["TCHEQUIE", "CZ", "CZE", "REPUBLIQUE TCHEQUE"],      "CZE", "Tchéquie"),
    (["ROUMANIE", "RO", "ROU"],                            "ROU", "Roumanie"),
    (["HONGRIE", "HU", "HUN"],                             "HUN", "Hongrie"),
    (["BULGARIE", "BG", "BGR"],                            "BGR", "Bulgarie"),
    (["CROATIE", "HR", "HRV"],                             "HRV", "Croatie"),
    (["SLOVAQUIE", "SK", "SVK"],                           "SVK", "Slovaquie"),
    (["SLOVENIE", "SI", "SVN"],                            "SVN", "Slovénie"),
    (["LITUANIE", "LT", "LTU"],                            "LTU", "Lituanie"),
    (["LETTONIE", "LV", "LVA"],                            "LVA", "Lettonie"),
    (["ESTONIE", "EE", "EST"],                             "EST", "Estonie"),
    (["CHYPRE", "CY", "CYP"],                              "CYP", "Chypre"),
    (["MALTE", "MT", "MLT"],                               "MLT", "Malte"),
    # DOM-TOM (code pays propre, cf. Martinique -> MTQ dans la fiche de réf)
    (["MARTINIQUE", "MTQ", "MQ"],                          "MTQ", "Martinique"),
    (["REUNION", "LA REUNION", "REU", "RE"],               "REU", "Réunion"),
    (["GUADELOUPE", "GLP", "GP"],                          "GLP", "Guadeloupe"),
    (["GUYANE", "GUF", "GF", "GUYANE FRANCAISE"],          "GUF", "Guyane"),
    (["MAYOTTE", "MYT", "YT"],                             "MYT", "Mayotte"),
    # Export courants
    (["TURQUIE", "TR", "TUR", "TURKEY"],                   "TUR", "Turquie"),
    (["SUISSE", "CH", "CHE", "SWITZERLAND"],               "CHE", "Suisse"),
    (["ROYAUME-UNI", "ROYAUME UNI", "GB", "GBR", "UK"],    "GBR", "Royaume-Uni"),
    (["MAROC", "MA", "MAR"],                               "MAR", "Maroc"),
    (["ALGERIE", "DZ", "DZA"],                             "DZA", "Algérie"),
    (["TUNISIE", "TN", "TUN"],                             "TUN", "Tunisie"),
    (["ETATS-UNIS", "US", "USA", "UNITED STATES"],         "USA", "États-Unis"),
    (["CANADA", "CA", "CAN"],                              "CAN", "Canada"),
    (["CHINE", "CN", "CHN", "CHINA"],                      "CHN", "Chine"),
]

# ============================================================
#  COLONNES DE DESTINATION  (les 92 colonnes de l'export Sage)
# ============================================================
COL = {
    "type_ligne": 1, "type_piece": 2, "num_piece": 3, "date": 4,
    "code_client": 9, "societe": 10, "forme_juridique": 11,
    "adresse1": 12, "adresse2": 13, "adresse3": 14,
    "cp": 15, "ville": 16, "code_pays": 17, "pays": 18, "mode_tva": 19,
    "tarif_client": 20, "nii": 21, "observations": 23, "mode_paiement": 25, "date_echeance": 26,
    "date_livraison_p": 27, "validee": 28, "transmise": 29, "soldee": 30, "comptabilisee": 31,
    "type_remise_p": 37, "taux_remise_p": 38, "mt_remise_p": 39, "taux_escompte": 40,
    "statut_devis": 41, "ref_commande": 42,
    "port_sans_tva": 44, "port_soumis_tva": 45, "taux_tva_port": 46, "mt_total_ttc": 48,
    "article": 49, "qte": 50, "pu_ht": 51, "pu_ttc": 52, "tx_tva": 53,
    "description": 55, "niveau_st": 56, "taux_tpf": 57, "depot": 58,
    "pds_brut": 59, "pds_net": 60, "qte_colis": 61, "nbre_colis": 62,
    "date_livraison_l": 63, "type_remise_l": 64, "taux_remise_l": 65,
    "mt_remise_ht": 66, "mt_remise_ttc": 67, "pa_ht": 68, "pamp": 69,
    "type_comm": 73, "taux_comm": 74, "mt_comm": 75, "eco_part": 77, "qte_livree": 78,
    "no_ligne": 80, "cat_tva": 82, "motif": 83,
    "code_pays_livr": 90, "pays_livr": 91, "cadre_fact": 92,
}
NB_COLONNES = 92

# Valeurs de base (gabarit) : tout ce qui n'est PAS dans la commande / fiches reste tel quel.
# -> reproduit exactement les "0", "0.00", "0.000", "1" de la fiche de référence.
BASE_E = {
    "type_piece": TYPE_PIECE, "forme_juridique": FORME_JURIDIQUE, "tarif_client": TARIF_CLIENT,
    "mode_paiement": MODE_PAIEMENT, "statut_devis": STATUT_DEVIS,
    "type_remise_p": "0", "taux_remise_p": "0.00", "mt_remise_p": "0.00", "taux_escompte": "0.00",
    "port_sans_tva": "0.00", "port_soumis_tva": "0.00", "taux_tva_port": "0.00",
    "code_pays_livr": PAYS_LIVR_CODE, "pays_livr": PAYS_LIVR_NOM, "cadre_fact": CADRE_FACTURATION,
    # mt_total_ttc / num_piece laissés vides : Sage les calcule
}
BASE_L = {
    "niveau_st": "0", "taux_tpf": "0.00",
    "pds_brut": "0.000", "pds_net": "0.000", "qte_colis": "0.000", "nbre_colis": "0.000",
    "type_remise_l": "1", "taux_remise_l": "0.00", "mt_remise_ht": "0.00", "mt_remise_ttc": "0.00",
    "pa_ht": "0.00", "pamp": "0.00",
    "type_comm": "0", "taux_comm": "0.00", "mt_comm": "0.00", "eco_part": "0.00", "qte_livree": "0.000",
}

# ============================================================
#  OUTILS
# ============================================================
def strip_accents(s):
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")

def norm_nom(s):
    if s is None: return ""
    s = strip_accents(str(s)).upper()
    s = re.sub(r"[^A-Z0-9 &]", " ", s)
    return re.sub(r"\s+", " ", s).strip()

def mots(s): return [m for m in norm_nom(s).split(" ") if len(m) >= 2 and m not in MOTS_VIDES]

def score_client(a, b):
    na, nb = norm_nom(a), norm_nom(b)
    if not na or not nb: return 0.0
    seq = difflib.SequenceMatcher(None, na, nb).ratio()
    ma, mb = set(mots(a)), set(mots(b))
    # Le dénominateur est plancherné à MIN_MOTS_RECOUVREMENT : sans ça, une fiche
    # Sage tenant en UN mot obtient 1.0 dès qu'un seul mot est commun, et toute
    # commande contenant ce mot lui est rattachée (« ...QUI N EXISTE PAS SARL »
    # -> fiche « PAS »). Les correspondances légitimes sur nom court passent par
    # seq / debut, pas par ce recouvrement.
    rec = len(ma & mb) / max(MIN_MOTS_RECOUVREMENT, min(len(ma), len(mb))) if ma and mb else 0.0
    debut = 0.85 if (na.startswith(nb) or nb.startswith(na)) else 0.0
    return max(seq, rec, debut)

def clean_article_ref(raw, garder=GARDER_COULEUR_DANS_CODE):
    if raw is None: return ""
    s = re.sub(r"[^A-Za-z0-9 \-]", " ", str(raw).replace("#", " "))
    s = re.sub(r"\s+", " ", s).strip().upper()
    s = s.split(" ")[0] if s else ""
    base = re.sub(r"-\d{1,3}$", "", s)
    return s if garder else base

def base_ref(raw): return clean_article_ref(raw, garder=False)

# table pays : alias normalisé -> (iso3, nom)
_PAYS_LOOKUP = {}
for _aliases, _iso3, _nom in _PAYS_TABLE:
    for _a in _aliases:
        _PAYS_LOOKUP[norm_nom(_a)] = (_iso3, _nom)

def lookup_pays(valeur):
    """Renvoie (code ISO3, nom en clair). (None, valeur d'origine) si inconnu."""
    if not valeur: return (None, "")
    iso3, nom = _PAYS_LOOKUP.get(norm_nom(valeur), (None, None))
    return (iso3, nom if nom else str(valeur).strip())

def regime_client(pays, cp):
    p = norm_nom(pays)
    cp3 = re.sub(r"\D", "", str(cp or ""))[:3]
    if p in ("FRANCE", "FR", ""):
        return "OUTREMER" if cp3 in CP_OUTREMER else "FRANCE"
    if p in PAYS_DOM or cp3 in CP_OUTREMER:
        return "OUTREMER"
    return "CEE" if p in PAYS_UE else "HORSCEE"

def est_dom(pays, cp):
    cp3 = re.sub(r"\D", "", str(cp or ""))[:3]
    return norm_nom(pays) in PAYS_DOM or cp3 in CP_OUTREMER

def mode_par_defaut(pays, cp):
    if est_dom(pays, cp): return "Hors CEE"
    return REGIME[regime_client(pays, cp)]

def infos_regime(mode_tva, pays, cp):
    """Renvoie (mode_tva, catégorie col82, motif col83, mention ou None, exonéré?)."""
    m = norm_nom(mode_tva)
    cat   = CAT_PAR_MODE.get(m, "")
    motif = MOTIF_PAR_MODE.get(m, "")
    exonere = m not in ("LOCAL", "")
    if not exonere:
        mention = None
    elif m == "CEE":
        mention = MENTION["CEE"]
    else:                                   # Hors CEE / Suspension de taxe
        mention = MENTION["OUTREMER"] if est_dom(pays, cp) else MENTION["HORSCEE"]
    return mode_tva, cat, motif, mention, exonere

def to_date_fr(v):
    if v is None or v == "": return ""
    s = str(v)
    m = re.search(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})", s)
    if m: return f"{int(m.group(1)):02d}/{int(m.group(2)):02d}/{m.group(3)}"
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m: return f"{m.group(3)}/{m.group(2)}/{m.group(1)}"
    return s.split(" ")[0]

def _to_float(v):
    if v is None or v == "": return None
    try: return float(str(v).replace(",", "."))
    except (ValueError, TypeError): return None

def qte3(v):
    """Quantités / poids : 3 décimales (ex. '8.000'). '0.000' si vide/invalide."""
    f = _to_float(v)
    return f"{f:.3f}" if f is not None else "0.000"

def prix2(v):
    """Prix / taux : 2 décimales (ex. '11.50'). '0.00' si vide/invalide."""
    f = _to_float(v)
    return f"{f:.2f}" if f is not None else "0.00"

# ============================================================
#  LECTURE DES FICHIERS  (xlsx en pur Python, sans openpyxl)
# ============================================================
_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

def _col_to_idx(letters):
    n = 0
    for ch in letters: n = n * 26 + (ord(ch) - 64)
    return n - 1

def read_xlsx(path):
    with zipfile.ZipFile(path) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in root.findall(_NS + "si"):
                shared.append("".join(t.text or "" for t in si.iter(_NS + "t")))
        sheet = next((n for n in z.namelist()
                      if re.match(r"xl/worksheets/sheet\d+\.xml$", n)), None)
        root = ET.fromstring(z.read(sheet))
        rows = []
        for row in root.iter(_NS + "row"):
            cells, maxc = {}, -1
            for c in row.findall(_NS + "c"):
                ci = _col_to_idx(re.match(r"[A-Z]+", c.get("r")).group())
                t = c.get("t")
                v = c.find(_NS + "v")
                isn = c.find(_NS + "is")
                if t == "s" and v is not None:
                    val = shared[int(v.text)]
                elif t == "inlineStr" and isn is not None:
                    val = "".join(x.text or "" for x in isn.iter(_NS + "t"))
                else:
                    val = v.text if v is not None else ""
                cells[ci] = val
                maxc = max(maxc, ci)
            rows.append([cells.get(i, "") for i in range(maxc + 1)])
        w = max((len(r) for r in rows), default=0)
        return [r + [""] * (w - len(r)) for r in rows]

def lire_table(path):
    path = Path(path)
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        rows = read_xlsx(path)
        head = [str(h).strip() if h is not None else "" for h in rows[0]]
        data = [{head[i]: (r[i] if i < len(r) else None) for i in range(len(head))} for r in rows[1:]]
        return head, data
    raw = None
    for enc in ("utf-8-sig", "utf-8", "cp1252"):
        try: raw = path.read_text(encoding=enc); break
        except UnicodeDecodeError: continue
    if raw is None: raw = path.read_text(encoding="cp1252", errors="replace")
    first = raw.split("\n", 1)[0]
    sep = "\t" if first.count("\t") >= first.count(";") else ";"
    lines = [l for l in raw.replace("\r", "").split("\n") if l != ""]
    head = [h.strip() for h in lines[0].split(sep)]
    data = [{head[i]: (f[i] if i < len(f) else "") for i in range(len(head))}
            for f in (l.split(sep) for l in lines[1:])]
    return head, data

def col_match(entetes, *cibles):
    norm = {norm_nom(h): h for h in entetes}
    for c in cibles:
        nc = norm_nom(c)
        if nc in norm: return norm[nc]
        for k, v in norm.items():
            if nc and nc in k: return v
    return None

# ============================================================
#  TRAITEMENT
# ============================================================
def build_clients_index(path):
    head, data = lire_table(path)
    c = {"code": col_match(head, "Code"), "nom": col_match(head, "Nom", "Société"),
         "adr1": col_match(head, "Adresse 1"), "adr2": col_match(head, "Adresse 2"),
         "adr3": col_match(head, "Adresse 3"),
         "cp": col_match(head, "Code Postal", "CP"), "ville": col_match(head, "Ville"),
         "pays": col_match(head, "Pays"), "nii": col_match(head, "N° TVA intracom", "NII"),
         "mode_tva": col_match(head, "Mode TVA")}
    out = []
    for r in data:
        if not str(r.get(c["code"], "") or "").strip(): continue
        out.append({k: str(r.get(c[k], "") or "").strip() for k in c})
    return out

def build_articles_index(path):
    head, data = lire_table(path)
    cc = col_match(head, "Code")
    cd = col_match(head, "Désignation longue", "Désignation courte", "Designation")
    ct = col_match(head, "Taux TVA", "Taux de TVA", "TVA")
    by_full, by_base = {}, {}      # clé -> (code exact, description, taux)
    for r in data:
        cb = str(r.get(cc, "") or "")
        if not cb.strip(): continue
        exact = cb                                  # code VERBATIM (un espace en fin fait parfois partie du code Sage)
        desc  = str(r.get(cd, "") or "").strip()
        try:    taux = float(str(r.get(ct, "") or "").replace(",", ".") or TAUX_LOCAL_DEFAUT)
        except Exception: taux = TAUX_LOCAL_DEFAUT
        full  = clean_article_ref(cb, garder=True)  # ex. "CFA50413-1"
        base  = base_ref(cb)                         # ex. "CFA50413"
        if full and full not in by_full: by_full[full] = (exact, desc, taux)
        if base and base not in by_base: by_base[base] = (exact, desc, taux)
    return by_full, by_base

def lire_commandes(path):
    head, rows = lire_table(path)
    g = lambda *c: col_match(head, *c)
    cCmd, cCli, cAdr, cVille, cCp, cPays, cDate, cRem = (
        g("N° commande", "Numéro commande"), g("Client"), g("Adresse"),
        g("Ville"), g("Code postal"), g("Pays"), g("Date création"), g("Remarque"))
    cRef, cQte, cCol, cPie, cPu, cCat = (
        g("N° de produits", "Numéro de produits"), g("Qté"), g("Colisage"),
        g("Nombre de pièces (Quantité*unités de colisage)", "Nombre de pièces"),
        g("Prix unitaire"), g("Catégorie"))
    commandes, cur = [], None
    for r in rows:
        nom = str(r.get(cCli, "") or "").strip()
        if nom:                                   # une ligne avec un client = nouvelle commande
            cur = {"nom": nom, "adr": str(r.get(cAdr, "") or "").strip(),
                   "ville": str(r.get(cVille, "") or "").strip(), "cp": str(r.get(cCp, "") or "").strip(),
                   "pays": str(r.get(cPays, "") or "").strip(), "date": to_date_fr(r.get(cDate)),
                   "cmd": str(r.get(cCmd, "") or "").strip(),
                   "remarque": str(r.get(cRem, "") or "").strip(), "lignes": []}
            commandes.append(cur)
        ref = str(r.get(cRef, "") or "").strip()
        if cur and ref:
            pieces = r.get(cPie)
            if pieces in (None, ""):
                try: pieces = float(r.get(cQte) or 0) * float(r.get(cCol) or 0)
                except Exception: pieces = ""
            cat = str(r.get(cCat, "") or "").strip()
            cur["lignes"].append({"ref": ref, "pieces": pieces, "pu": r.get(cPu), "cat": cat})
    return commandes

def match_client(cli, clients, seuil=SEUIL_CLIENT):
    best, bs = None, 0.0
    num_cmd = re.findall(r"\d+", cli["adr"] or "")
    for cl in clients:
        s = score_client(cli["nom"], cl["nom"])
        if cli["cp"] and cl["cp"] and re.sub(r"\D", "", cli["cp"]) == re.sub(r"\D", "", cl["cp"]): s += 0.20
        if cli["ville"] and norm_nom(cli["ville"]) == norm_nom(cl["ville"]): s += 0.15
        if num_cmd and cl["adr1"] and num_cmd[0] in re.findall(r"\d+", cl["adr1"]): s += 0.10
        if s > bs: best, bs = cl, s
    return (best, round(bs, 3)) if bs >= seuil else (None, round(bs, 3))

def _desc_categorie(cat):
    """Première étiquette FR d'une catégorie source ('CHEMISE M. C. / SHIRT S. S.' -> 'CHEMISE M. C.')."""
    if not cat: return ""
    return cat.split("/")[0].strip().upper()

def libelle_article_introuvable(ref_raw, code, cat):
    """Description d'un article absent de la fiche Sage.

    Le champ « N° de produits » contient souvent le code SUIVI du libellé article
    (ex. '503712 PANT', '446568 SHORT', '380128 TSHIRT LOT3'). On reprend ce
    libellé tel quel : tous les jetons ASCII qui suivent le code, en ignorant les
    mots de couleur non latins (ex. '军绿'). À défaut de libellé exploitable, on
    retombe sur la catégorie source ('JOGGING', 'ENSEMBLE / SET'...).
    """
    raw = re.sub(r"\s+", " ", str(ref_raw or "").replace("#", " ")).strip()
    apres_code = raw.split(" ")[1:]                       # tout ce qui suit le code
    labels = [t for t in apres_code if re.fullmatch(r"[A-Za-z0-9\-]+", t)]
    label = " ".join(labels).upper().strip() or _desc_categorie(cat)
    return f"{code} {label}".strip()

# ============================================================
#  NOUVEAU FORMAT "TaylormanCommande" (1 bloc en-tête + tableau produits, multi-fichiers)
# ============================================================
def _raw_rows(path):
    """Lignes brutes (liste de listes), sans interpréter d'en-tête."""
    path = Path(path)
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        return read_xlsx(path)
    raw = None
    for enc in ("utf-8-sig", "utf-8", "cp1252"):
        try: raw = path.read_text(encoding=enc); break
        except UnicodeDecodeError: continue
    if raw is None: raw = path.read_text(encoding="cp1252", errors="replace")
    lignes = raw.replace("\r", "").split("\n")
    sep = "\t" if (lignes and lignes[0].count("\t") >= lignes[0].count(";")) else ";"
    return [l.split(sep) for l in lignes]

def detect_format(path):
    """'taylorman' si le fichier débute par le titre 'TaylormanCommande', sinon 'base'."""
    try:
        for r in _raw_rows(path)[:6]:
            for c in r:
                if norm_nom(c).startswith("TAYLORMANCOMMANDE"):
                    return "taylorman"
    except Exception:
        pass
    return "base"

def _idx_bloc(entetes, *cibles):
    """Index de colonne (dans l'en-tête produits Taylorman) par nom, sinon None."""
    norm = {norm_nom(str(h)): i for i, h in enumerate(entetes)}
    for c in cibles:
        nc = norm_nom(c)
        if nc in norm: return norm[nc]
        for k, i in norm.items():
            if nc and nc in k: return i
    return None

def _parse_adresse(blob):
    """Extrait (cp, ville, pays) au mieux d'une adresse en texte libre. Vide si absent.
    Ex. 'Fiesta  658040124 6 rue monsigny  75002 paris FRANCE' -> ('75002','paris','France')."""
    s = re.sub(r"\s+", " ", str(blob or "")).strip()
    if not s: return ("", "", "")
    cp = ville = pays = ""
    m = re.search(r"\b(\d{5})\b", s)                       # code postal FR sur 5 chiffres
    if m:
        cp = m.group(1)
        toks = s[m.end():].strip().split(" ")
        if toks and toks[-1] and lookup_pays(toks[-1])[0]:
            pays = lookup_pays(toks[-1])[1]; toks = toks[:-1]
        ville = " ".join(t for t in toks if t).strip()
    else:
        toks = s.split(" ")
        if toks and lookup_pays(toks[-1])[0]:
            pays = lookup_pays(toks[-1])[1]
    return (cp, ville, pays)

def lire_commandes_taylorman(paths):
    """Lit un ou plusieurs fichiers 'TaylormanCommande' -> liste de commandes
    (même structure que lire_commandes). Un fichier peut contenir plusieurs blocs."""
    if isinstance(paths, (str, Path)): paths = [paths]
    commandes = []
    for p in paths:
        cur, colmap = None, None
        for r in _raw_rows(p):
            cells = [("" if c is None else str(c)) for c in r]
            c0 = cells[0].strip() if cells else ""
            n0 = norm_nom(c0)
            val1 = cells[1].strip() if len(cells) > 1 else ""
            if n0.startswith("TAYLORMANCOMMANDE") or n0.startswith("MONTANT"):
                if n0.startswith("TAYLORMANCOMMANDE"): cur, colmap = None, None
                continue
            if n0 == "NUMERO":
                cur = {"nom": "", "adr": "", "ville": "", "cp": "", "pays": "",
                       "date": "", "cmd": val1, "remarque": "", "lignes": []}
                commandes.append(cur); colmap = None
                continue
            if cur is not None and n0 == "CLIENT":
                cur["nom"] = val1; continue
            if cur is not None and n0 == "ADRESSE":
                cur["adr"] = val1
                cur["cp"], cur["ville"], cur["pays"] = _parse_adresse(val1)
                continue
            if "REFERENCE" in " ".join(norm_nom(x) for x in cells):   # en-tête du tableau produits
                colmap = {"ref": _idx_bloc(cells, "Référence"),
                          "prix": _idx_bloc(cells, "Prix", "Prix unitaire"),
                          "qte": _idx_bloc(cells, "Quantité"), "col": _idx_bloc(cells, "Colisage"),
                          "pieces": _idx_bloc(cells, "Nombre de pièces (Quantité*unités de colisage)", "Nombre de pièces"),
                          "cat": _idx_bloc(cells, "Catégorie")}
                continue
            if cur is not None and colmap and colmap["ref"] is not None and c0.isdigit():  # ligne produit
                def gg(i): return cells[i].strip() if (i is not None and i < len(cells)) else ""
                ref = gg(colmap["ref"])
                if not ref: continue
                pieces = gg(colmap["pieces"])
                if not pieces:
                    try: pieces = float(gg(colmap["qte"]) or 0) * float(gg(colmap["col"]) or 0)
                    except Exception: pieces = ""
                cur["lignes"].append({"ref": ref, "pieces": pieces,
                                      "pu": gg(colmap["prix"]), "cat": gg(colmap["cat"])})
    return commandes

def charger_commandes(cmd_path, fmt="auto"):
    """Charge les commandes sans générer. Renvoie (commandes, fmt_effectif, paths)."""
    paths = list(cmd_path) if isinstance(cmd_path, (list, tuple)) else [cmd_path]
    if fmt == "auto":
        fmt = detect_format(paths[0])
    if fmt == "taylorman":
        return lire_commandes_taylorman(paths), fmt, paths
    return lire_commandes(paths[0]), fmt, paths

def fabriquer(cmd_path, clients_path, articles_path, sortie,
              seuil=SEUIL_CLIENT, garder_couleur=GARDER_COULEUR_DANS_CODE, validee=VALIDEE,
              date_piece=None, fmt="auto", commandes=None,
              clients=None, articles=None):
    """`clients` / `articles` : index déjà construits (voir build_*_index).
    Passés par le worker pour éviter de relire les exports à chaque fichier."""
    today = date_piece or date.today().strftime("%d/%m/%Y")
    if clients is None:
        clients = build_clients_index(clients_path)
    by_full, by_base = articles if articles is not None else build_articles_index(articles_path)
    paths = list(cmd_path) if isinstance(cmd_path, (list, tuple)) else [cmd_path]
    if commandes is None:
        commandes, fmt, paths = charger_commandes(cmd_path, fmt)
    elif fmt == "auto":
        fmt = detect_format(paths[0])

    def st(row, key, val):
        row[COL[key] - 1] = "" if val is None else str(val)

    def make_row(base):
        r = [""] * NB_COLONNES
        for k, v in base.items():
            r[COL[k] - 1] = v
        return r

    all_rows, rapport = [], []
    clients_manquants, articles_manquants, pays_inconnus = [], [], set()

    for cmd in commandes:
        fiche, score = match_client(cmd, clients, seuil)
        pays_src = fiche["pays"] if fiche and fiche.get("pays") else cmd["pays"]
        cp   = fiche["cp"] if fiche and fiche.get("cp") else cmd["cp"]
        mode = (fiche["mode_tva"] if fiche and fiche.get("mode_tva") else mode_par_defaut(pays_src, cp))
        mode_tva, cat, motif, mention, exonere = infos_regime(mode, pays_src, cp)
        iso3, nom_pays = lookup_pays(pays_src)
        if pays_src and iso3 is None:
            pays_inconnus.add(pays_src)

        # ---- En-tête E ----
        E = make_row(BASE_E)
        st(E, "type_ligne", "E")
        st(E, "date", today); st(E, "date_echeance", today); st(E, "date_livraison_p", today)
        st(E, "validee", validee)
        st(E, "mode_tva", mode_tva); st(E, "cat_tva", cat); st(E, "motif", motif)
        st(E, "observations", cmd.get("remarque"))
        st(E, "code_pays", iso3); st(E, "pays", nom_pays)
        if fiche:
            st(E, "code_client", fiche["code"]); st(E, "societe", fiche["nom"])
            st(E, "adresse1", fiche["adr1"]); st(E, "adresse2", fiche["adr2"]); st(E, "adresse3", fiche["adr3"])
            st(E, "cp", fiche["cp"]); st(E, "ville", fiche["ville"]); st(E, "nii", fiche["nii"])
        else:
            # pas de code client : on garde au moins le nom + l'adresse source pour identification
            st(E, "societe", cmd["nom"]); st(E, "adresse1", cmd["adr"])
            st(E, "cp", cmd["cp"]); st(E, "ville", cmd["ville"])
            clients_manquants.append(f"{cmd['nom']} (cmd {cmd['cmd'] or '?'}, score {score})")
        all_rows.append(E)

        # ---- Lignes L ----
        n, rep_art = 0, []
        for lg in cmd["lignes"]:
            n += 1
            full = clean_article_ref(lg["ref"], garder=True); base = base_ref(lg["ref"])
            if full in by_full:                       # le -N fait partie du code (ex. CFA50413-1)
                code, desc, taux_art = by_full[full]; trouve = True
            elif base in by_base:                     # code base, couleur ignorée
                code, desc, taux_art = by_base[base]; trouve = True
            else:                                     # introuvable : code nettoyé + desc depuis la catégorie source
                code = clean_article_ref(lg["ref"], garder_couleur)
                desc = libelle_article_introuvable(lg["ref"], code, lg.get("cat"))
                taux_art = TAUX_LOCAL_DEFAUT; trouve = False
                articles_manquants.append(f"{lg['ref'].strip()}  ->  {code}")
            rep_art.append((lg["ref"], code, desc, trouve))
            taux = 0.0 if exonere else taux_art       # exonéré -> 0 % ; local -> taux de l'article
            pu = _to_float(lg["pu"])
            pu_ttc = round(pu * (1 + taux / 100.0), 2) if pu is not None else None

            L = make_row(BASE_L)
            st(L, "type_ligne", "L"); st(L, "article", code)
            st(L, "qte", qte3(lg["pieces"]))
            st(L, "pu_ht", prix2(lg["pu"])); st(L, "pu_ttc", prix2(pu_ttc)); st(L, "tx_tva", prix2(taux))
            st(L, "description", desc); st(L, "date_livraison_l", today)
            st(L, "no_ligne", str(n))
            all_rows.append(L)

        # ---- Ligne de mention légale (exonération) ----
        if mention:
            n += 1; cm, dm = mention
            M = make_row(BASE_L)
            st(M, "type_ligne", "L"); st(M, "article", cm); st(M, "description", dm)
            st(M, "qte", "0.000"); st(M, "pu_ht", "0.00"); st(M, "pu_ttc", "0.00"); st(M, "tx_tva", "0.00")
            st(M, "date_livraison_l", today); st(M, "no_ligne", str(n))
            all_rows.append(M)

        # ---- Rapport par facture ----
        rapport.append(f"Commande {cmd['cmd'] or '?'} — {cmd['nom']!r}")
        rapport.append("  -> " + (f"CLIENT TROUVÉ {fiche['code']} ({fiche['nom']})" if fiche
                       else f"CLIENT NON TROUVÉ (meilleur score {score}) — Code client laissé vide")
                       + f" | mode TVA={mode_tva or 'Local'} | TVA lignes={'0 %' if exonere else 'taux article'}"
                       + f" | mention={mention[0] if mention else '—'}")
        for ref, code, desc, trouve in rep_art:
            flag = "" if trouve else "   [ARTICLE NON TROUVÉ dans la fiche -> code nettoyé + desc catégorie]"
            rapport.append(f"     {ref.strip()}  ->  {code}{flag}")
        rapport.append("")

    Path(sortie).write_text("\r\n".join(";".join(r) for r in all_rows) + "\r\n",
                            encoding="cp1252", newline="")

    # ---- Synthèse "à vérifier" ----
    rapport.append("=" * 64)
    rapport.append(f"Format : {fmt}" + (f"  |  {len(paths)} fichier(s) ingéré(s)" if len(paths) > 1 else ""))
    rapport.append(f"{len(commandes)} facture(s), {len(all_rows)} ligne(s) écrites dans : {sortie}")
    rapport.append(f"Date appliquée (pièce/échéance/livraison) : {today}")
    rapport.append("Champs laissés vides volontairement (calcul auto Sage) : N° pièce, Mt total TTC, totaux TVA.")
    if clients_manquants:
        rapport.append("")
        rapport.append(f"⚠ CLIENTS À CRÉER / VÉRIFIER dans Sage ({len(clients_manquants)}) :")
        for c in clients_manquants: rapport.append(f"   - {c}")
    if articles_manquants:
        rapport.append("")
        rapport.append(f"⚠ ARTICLES NON TROUVÉS dans la fiche ({len(articles_manquants)}) :")
        for a in articles_manquants: rapport.append(f"   - {a}")
    if pays_inconnus:
        rapport.append("")
        rapport.append(f"⚠ PAYS sans code ISO connu (Code pays laissé vide) : {', '.join(sorted(pays_inconnus))}")
    if not (clients_manquants or articles_manquants or pays_inconnus):
        rapport.append("Aucun champ bloquant : tous les clients, articles et pays ont été résolus.")
    return "\n".join(rapport)

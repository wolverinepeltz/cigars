"""
The deal alert email, shared by every store.

    build_email(store_name, items) -> (subject, text, html)
    send_email(store_name, items)  -> True if the mail went out

Layout:

    discount band (80%+, 70-79%, ... 30-39%), then "On clearance"
      brand (A to Z)
        cigar (A to Z), with its % range
          one row per vitola, cheapest first, two lines at most:
            [tag] vitola size, pack count, rating
            price, comparison price, % off, price per cigar

A cigar sits in the band of its best vitola, so its sizes stay together.
Each row is a link to the product page. Prices over HIGH_PRICE are red.

Items are the dicts the stores already produce (see core.py). Optional keys
fill in more detail when a store has them; anything missing is skipped:

    brand        maker, when the store gives it separately
    size         size/variant label (e.g. a Shopify variant: "Toro 6x50")
    pack_count   cigars in the pack, when the store gives it
    ref          price it's discounted from; ref_label "was" or "MSRP"
    rating, reviews
    change       why it's emailed: "new", "back", "price dropped from $X",
                 "discount up from X%", "discount down from X%"
    listed_deal  e.g. "on clearance", for deals with no % off

This module needs only the standard library, so scripts with their own
dependencies (Cigarplace) can use it without installing requests.
"""

import html
import os
import re
import smtplib
import unicodedata
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

SENDER_EMAIL = "peltz.chris@gmail.com"
SENDER_PASSWORD = os.environ.get("GMAIL_PASSWORD", "")  # set in GitHub Secrets
TO_EMAIL = "peltz.chris@gmail.com"

HIGH_PRICE = 125     # prices above this show in red
TOP_BAND = 80        # 80%+ is the highest band

# --------------------------------------------------------------------------
# Reading brand, cigar and vitola out of a product name
# --------------------------------------------------------------------------

# Makers whose names are more than one word, or that a name might start
# with ahead of a longer line name. Longest match wins; any other name's
# first word is taken as the brand.
BRANDS = sorted({
    "A.J. Fernandez", "AJ Fernandez", "Aganorsa Leaf", "Aging Room",
    "Alec Bradley", "Alec & Bradley", "Arturo Fuente", "Ashton", "Asylum",
    "Avo", "Bolivar", "Brick House", "CAO", "Camacho", "Casa Carrillo",
    "Casa Fernandez", "Cohiba", "Crowned Heads", "Cuesta Rey", "Davidoff",
    "Diamond Crown", "Don Pepin Garcia", "Drew Estate", "Dunbarton",
    "E.P. Carrillo", "El Rey del Mundo", "Espinosa", "Fratello", "Foundation",
    "Gilberto Oliva", "Gurkha", "H. Upmann", "H Upmann", "Hoyo de Monterrey",
    "Illusione", "Jaime Garcia", "Joya de Nicaragua", "JC Newman",
    "J.C. Newman", "La Aroma de Cuba", "La Aurora", "La Flor Dominicana",
    "La Gloria Cubana", "Leaf by Oscar", "Liga Privada", "Los Statos",
    "Macanudo", "My Father", "Montecristo", "Nub", "Oliva", "Oscar Valladares",
    "Padron", "Partagas", "Perdomo", "Plasencia", "Punch", "Rocky Patel",
    "Rojas", "RoMa Craft", "Romeo y Julieta", "Room 101", "Sabor de Esteli",
    "San Cristobal", "Southern Draw", "Tabak Especial", "Tatiana", "Tatuaje",
    "Undercrown", "West Tampa", "601", "Stalk Leaf by Oscar", "Purofino", "Purofino by Perdomo",
    "David P. Ehrlich", "Flores y Rodriguez", "Nomad", "DBL", "Island Jim",
}, key=len, reverse=True)

# Vitola (shape) names, longest first so "Gran Toro" beats "Toro".
VITOLAS = sorted({
    "robusto", "short robusto", "double robusto", "robusto extra",
    "robusto grande", "grand robusto", "petit robusto", "petite robusto",
    "toro", "gran toro", "toro grande", "toro gordo", "toro especial",
    "toro extra", "double toro", "super toro", "torito", "torpedo",
    "churchill", "short churchill", "double corona", "corona", "corona gorda",
    "corona grande", "corona extra", "petit corona", "petite corona",
    "half corona", "gran corona", "lonsdale", "lancero", "petit lancero",
    "panatela", "panetela", "belicoso", "petit belicoso", "petite belicoso",
    "gordo", "gordito", "gigante", "magnum", "perfecto", "figurado",
    "pyramid", "piramide", "presidente", "rothschild", "rothchildes",
    "salomon", "cigarillo", "cigarillos", "puritos", "pitas", "canonazo",
    "cañonazo", "trabuco", "epicure", "sublime", "prince philip", "no. 1",
    "no. 2", "no. 3", "no. 4", "no. 5", "no. 47", "no. 9",
}, key=len, reverse=True)
VITOLA_RE = re.compile(r"(?<![\w.])(" + "|".join(re.escape(v) for v in VITOLAS)
                       + r")s?(?![\w])", re.I)

FRACTIONS = {"½": ".5", "¼": ".25", "¾": ".75", "⅛": ".125", "⅜": ".375",
             "⅝": ".625", "⅞": ".875", " 1/2": ".5", " 1/4": ".25",
             " 3/4": ".75"}
# Length x ring gauge: 5x52, 5.7x54, 7.0"x50, 5½ x 54, 6" X 44, 7×50, 7 1/2″ x 54
SIZE_RE = re.compile(
    r"(\d{1,2}(?:\.\d+)?(?:\s?[½¼¾⅛⅜⅝⅞]| [123]/[248])?)\s*[\"″”']?\s*[x×X]\s*(\d{2})(?!\d)")
# "5 tins of 6" = 30 cigars
MULTI_PACK_RE = re.compile(
    r"\b(\d{1,2})\s*(?:tins|packs|boxes|sleeves)\s*of\s*(\d{1,3})(?:\s*cigars)?\b", re.I)
PACK_RES = [
    re.compile(r"\b(?:box|pack|bundle|tin|bag|carton|cabinet|case|jar|crate|"
               r"tray|humidor|sampler|sleeve|display)(?:e?s)?\s*(?:of\s*)?(\d{1,3})\b", re.I),
    re.compile(r"\b(\d{1,3})\s*-?\s*(?:pack|pk|ct|count|cigars?)"
               r"(?:\s*(?:box|pack|bundle|tin))?\b", re.I),
]
SINGLE_RE = re.compile(r"\bsingles?\b", re.I)
# Store sales copy tacked onto names.
PROMO_RES = [
    re.compile(r"\s*\+\s*\d+%\s*off.*$", re.I),
    re.compile(r"\s*\+?\s*free shipping.*$", re.I),
    re.compile(r"\s*(\.\.\.|…)\s*$"),
    re.compile(r"\s*\b(special|deal)\s*$", re.I),
]


def _clean(text):
    return re.sub(r"\s+", " ", html.unescape(text or "")).strip(" -|,:")


def _size(text):
    """('5.5x54', text without it), or (None, text)."""
    m = SIZE_RE.search(text)
    if not m:
        return None, text
    length = m.group(1)
    for frac, dec in FRACTIONS.items():
        length = length.replace(frac, dec)
    length = re.sub(r"\s+", "", length)
    if length.endswith(".0"):
        length = length[:-2]
    try:
        length = f"{float(length):g}"
    except ValueError:
        pass
    return f"{length}x{m.group(2)}", (text[:m.start()] + " " + text[m.end():])


def _pack(text):
    """(count, text without the pack wording), or (None, text)."""
    m = MULTI_PACK_RE.search(text)
    if m:
        return int(m.group(1)) * int(m.group(2)), text[:m.start()] + " " + text[m.end():]
    for rx in PACK_RES:
        m = rx.search(text)
        if m and 1 <= int(m.group(1)) <= 300:
            return int(m.group(1)), text[:m.start()] + " " + text[m.end():]
    if SINGLE_RE.search(text):
        return 1, SINGLE_RE.sub(" ", text)
    return None, text


def _strip_brackets(text):
    """
    Drop what's left of a size/pack bracket once those are read out, e.g.
    '( / )' or '(Size Varies / )', and a bracket cut off by the store's
    name length limit, e.g. '... Belicoso (A'.
    """
    text = re.sub(r"\([^()]*/[^()]*\)", " ", text)
    text = re.sub(r"\(\s*[,\s]*\)", " ", text)
    text = re.sub(r"\s*\([^()]*$", "", text)
    return re.sub(r"\s+", " ", text)


def _plain(text):
    """Lowercase without accents, for matching 'Estelí' to 'Esteli'."""
    return "".join(c for c in unicodedata.normalize("NFKD", text)
                   if not unicodedata.combining(c)).lower()


def parse_name(name, brand=None, size=None):
    """
    Splits a product name into brand, cigar line and vitola:

        "La Aurora ADN Dominicano Toro (5.7x54 / Box 20)"
        -> brand "La Aurora", line "ADN Dominicano", vitola "Toro",
           dims "5.7x54", pack 20

    brand  maker from the store, when it has one
    size   variant label from the store (Shopify), read the same way
    Anything it can't place stays in the line name, so nothing is lost.
    """
    text = _clean(name)
    for rx in PROMO_RES:
        text = rx.sub("", text)

    dims, text = _size(text)
    pack, text = _pack(text)
    extra = ""
    if size:
        s_dims, s_rest = _size(_clean(size))
        s_pack, s_rest = _pack(s_rest)
        dims, pack = dims or s_dims, pack or s_pack
        extra = _clean(_strip_brackets(s_rest))
    text = _clean(_strip_brackets(text))

    # Samplers and head-to-head packs: "Slam Pack: A vs B"
    if " vs " in text.lower() or re.match(r"^[^:]{3,30}:\s", text):
        return {"brand": "Samplers and bundles", "line": text,
                "vitola": extra, "dims": dims, "pack": pack}

    # "Partagas | 1845 Toro"
    if " | " in text and not brand:
        brand, text = (p.strip() for p in text.split(" | ", 1))

    if brand:
        brand = _clean(brand)
        if _plain(text).startswith(_plain(brand) + " "):
            text = text[len(brand):].strip()
    else:
        low = _plain(text)
        brand = next((b for b in BRANDS if low.startswith(_plain(b) + " ")), None)
        if brand:
            brand, text = text[:len(brand)], text[len(brand):].strip()
        else:
            parts = text.split(" ", 1)
            brand, text = parts[0], (parts[1] if len(parts) > 1 else "")
    text = text.strip(" -|")

    line, vitola = text, ""
    if " - " in text:                                   # "Flor de Oliva - Churchill"
        line, vitola = (p.strip() for p in text.split(" - ", 1))
    else:
        m = next((m for m in VITOLA_RE.finditer(text) if m.start() > 0), None)
        if m:
            line, vitola = text[:m.start()].strip(" -,"), text[m.start():].strip()
    if extra and extra.lower() not in vitola.lower():
        vitola = f"{vitola} {extra}".strip()
    return {"brand": brand or "Other", "line": line or brand or "Other",
            "vitola": vitola, "dims": dims, "pack": pack}


# --------------------------------------------------------------------------
# Item details
# --------------------------------------------------------------------------

def pct(item):
    """Whole-number % off for display, or None for no discount."""
    p = item.get("discount_pct") or 0
    return int(p + 0.5) if p > 0 else None


def comparison(item):
    """("MSRP", 221.0, exact) for the price this deal is measured from."""
    if item.get("ref"):
        return item.get("ref_label") or "was", item["ref"], True
    p = item.get("discount_pct") or 0
    if 0 < p < 100:  # site gives only "save X%": work the list price out
        return "list", item["price"] / (1 - p / 100), False
    return None, None, False


def tags(item):
    """[(text, kind)] for the change tags at the start of a row."""
    change = item.get("change") or ""
    out = []
    if change == "new":
        return [("NEW", "new")]
    if "back" in change:
        out.append(("back", "back"))
    m = re.search(r"price dropped from \$([\d,.]+)", change)
    if m:
        old = float(m.group(1).replace(",", ""))
        out.append((f"↓ ${old - item['price']:.2f}", "drop"))
    m = re.search(r"discount (up|down) from ([\d.]+)%", change)
    if m:
        arrow = "↑" if m.group(1) == "up" else "↓"
        out.append((f"{arrow} from {int(float(m.group(2)) + 0.5)}%",
                    "up" if m.group(1) == "up" else "down"))
    return out


def change_kind(change):
    """Main reason, for counting: new > back > price drop > discount."""
    change = change or "new"
    if change == "new":
        return "new"
    if change.startswith("back"):
        return "back"
    if "price dropped" in change:
        return "price drop"
    if "discount down" in change:
        return "discount down"
    return "discount up"


def summarize(items):
    """e.g. '2 new, 1 back, 3 price drops' (each deal counted once)."""
    plural = {"new": "new", "back": "back", "price drop": "price drops",
              "discount up": "discounts up", "discount down": "discounts down"}
    counts = {}
    for i in items:
        k = change_kind(i.get("change"))
        counts[k] = counts.get(k, 0) + 1
    return ", ".join(f"{counts[k]} {k if counts[k] == 1 else plural[k]}"
                     for k in plural if k in counts)


def pct_range(values):
    values = sorted(v for v in values if v)
    if not values:
        return ""
    lo, hi = values[0], values[-1]
    return f"{lo}% off" if lo == hi else f"{lo}-{hi}% off"


# --------------------------------------------------------------------------
# Grouping
# --------------------------------------------------------------------------

def band_of(p):
    """Band floor (80, 70, ... 30, 20...), or None for clearance with no %."""
    if not p:
        return None
    return min(p // 10 * 10, TOP_BAND)


def band_label(b):
    if b is None:
        return "On clearance"
    return f"{b}%+ off" if b == TOP_BAND else f"{b}-{b + 9}% off"


def group(items):
    """[(band, [(brand, [(line, range, [rows])])])], best band first."""
    cigars = {}
    for it in items:
        parts = parse_name(it["name"], it.get("brand"), it.get("size"))
        row = dict(it, _p=parts, _pct=pct(it))
        if it.get("pack_count"):
            row["_p"]["pack"] = it["pack_count"]
        key = (parts["brand"].lower(), parts["line"].lower())
        cigars.setdefault(key, {"brand": parts["brand"], "line": parts["line"],
                                "rows": []})["rows"].append(row)

    bands = {}
    for c in cigars.values():
        best = max((r["_pct"] or 0) for r in c["rows"])
        c["rows"].sort(key=lambda r: (r["price"], r["_p"]["vitola"].lower()))
        c["range"] = pct_range(r["_pct"] for r in c["rows"])
        bands.setdefault(band_of(best), {}).setdefault(c["brand"], []).append(c)

    order = sorted((b for b in bands if b is not None), reverse=True)
    if None in bands:
        order.append(None)
    out = []
    for b in order:
        brands = []
        for brand in sorted(bands[b], key=str.lower):
            lines = sorted(bands[b][brand], key=lambda c: c["line"].lower())
            brands.append((brand, lines))
        out.append((b, brands))
    return out


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def subject(store_name, items):
    n = len(items)
    rng = pct_range(pct(i) for i in items)
    detail = ", ".join(x for x in (summarize(items), rng) if x)
    return f"{store_name}: {n} deal{'s' if n != 1 else ''} ({detail})"


def _money(v):
    return f"${v:,.2f}"


def _whole(v):
    return f"${v:,.0f}"


def row_parts(r):
    """The two lines of a row, as pieces both renderers can use."""
    p = r["_p"]
    what = " ".join(x for x in (p["vitola"], p["dims"]) if x)
    if p["pack"] and p["pack"] > 1:
        what = f"{what}, {p['pack']}ct" if what else f"{p['pack']}ct"
    elif p["pack"] == 1:
        what = f"{what}, single" if what else "single"
    if not what:
        what = p["line"]
    rating = (f"★ {r['rating']:.1f}" if r.get("rating") else "")
    label, ref, exact = comparison(r)
    if label == "list":
        compare = f"about {_whole(ref)} list"
    elif label:
        compare = f"{label} {_whole(ref)}"
    else:
        compare = ""
    off = f"{r['_pct']}% off" if r["_pct"] else (r.get("listed_deal") or "")
    each = (f"{_money(r['price'] / p['pack'])} ea"
            if p["pack"] and p["pack"] > 1 else "")
    return {"tags": tags(r), "what": what, "rating": rating,
            "price": _money(r["price"]), "high": r["price"] > HIGH_PRICE,
            "compare": compare, "compare_label": label, "compare_value": ref,
            "off": off, "each": each, "url": r["url"]}


def render_text(store_name, items):
    out = []
    for b, brands in group(items):
        out += ["", f"==== {band_label(b)} ===="]
        for brand, lines in brands:
            out += ["", brand.upper()]
            for c in lines:
                out.append(f"  {c['line']}" + (f"  ({c['range']})" if c["range"] else ""))
                for r in c["rows"]:
                    x = row_parts(r)
                    tag = " ".join(f"[{t}]" for t, _ in x["tags"])
                    first = " ".join(v for v in (tag, x["what"], x["rating"]) if v)
                    second = ", ".join(v for v in (x["price"], x["compare"],
                                                   x["off"], x["each"]) if v)
                    out += [f"    - {first}", f"      {second}", f"      {x['url']}"]
    return "\n".join(out) + "\n"


# Colors (light backgrounds; mail apps that force dark mode adjust them).
BAND_STYLE = {
    TOP_BAND: ("#173404", "#EAF3DE"), 70: ("#173404", "#EAF3DE"),
    60: ("#27500A", "#EAF3DE"), 50: ("#27500A", "#EAF3DE"),
    40: ("#C0DD97", "#173404"), 30: ("#EAF3DE", "#27500A"),
    20: ("#F1EFE8", "#444441"), 10: ("#F1EFE8", "#444441"),
    None: ("#F1EFE8", "#444441"),
}
TAG_STYLE = {"new": ("#EAF3DE", "#27500A"), "back": ("#E6F1FB", "#0C447C"),
             "drop": ("#FCEBEB", "#791F1F"), "up": ("#EAF3DE", "#27500A"),
             "down": ("#F1EFE8", "#444441")}
MUTED = "#5F5E5A"
GREEN = "#3B6D11"
RED = "#A32D2D"
STAR = "#BA7517"


def _e(text):
    return html.escape(str(text), quote=True)


def render_html(store_name, items):
    h = ['<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,'
         'sans-serif;max-width:560px;margin:0 auto;color:#2C2C2A;font-size:14px;'
         'line-height:1.5">']
    for b, brands in group(items):
        bg, fg = BAND_STYLE.get(b, BAND_STYLE[None])
        h.append(f'<div style="background:{bg};color:{fg};padding:6px 12px;'
                 f'border-radius:8px;font-size:15px;font-weight:bold;'
                 f'margin:20px 0 8px">{_e(band_label(b))}</div>')
        for brand, lines in brands:
            h.append(f'<div style="font-size:16px;font-weight:bold;margin:14px 0 2px;'
                     f'padding-bottom:3px;border-bottom:1.5px solid #B4B2A9">'
                     f'{_e(brand)}</div>')
            for c in lines:
                rng = (f'<span style="font-weight:normal;color:{MUTED};font-size:12px;'
                       f'margin-left:6px">{_e(c["range"])}</span>' if c["range"] else "")
                h.append(f'<div style="font-size:15px;font-weight:bold;'
                         f'margin:10px 0 2px">{_e(c["line"])}{rng}</div>')
                for n, r in enumerate(c["rows"]):
                    h.append(_row_html(row_parts(r), first=(n == 0)))
    h.append("</div>")
    return "".join(h)


def _row_html(x, first):
    border = "" if first else "border-top:0.5px solid #D3D1C7;"
    tags_html = "".join(
        f'<span style="background:{TAG_STYLE[k][0]};color:{TAG_STYLE[k][1]};'
        f'font-size:11px;padding:1px 6px;border-radius:4px;margin-right:6px">'
        f'{_e(t)}</span>' for t, k in x["tags"])
    rating = (f'<span style="color:{STAR};font-size:12px;margin-left:6px">'
              f'{_e(x["rating"])}</span>' if x["rating"] else "")
    price_color = RED if x["high"] else "#2C2C2A"
    parts = [f'<span style="font-weight:bold;color:{price_color}">{_e(x["price"])}</span>']
    if x["compare_label"]:
        word = "about" if x["compare_label"] == "list" else x["compare_label"]
        tail = " list" if x["compare_label"] == "list" else ""
        parts.append(f'<span style="color:{MUTED};font-size:12px">{_e(word)} '
                     f'<s>{_e(_whole(x["compare_value"]))}</s>{tail}</span>')
    if x["off"]:
        parts.append(f'<span style="font-weight:bold;color:{GREEN}">{_e(x["off"])}</span>')
    if x["each"]:
        parts.append(f'<span style="color:{MUTED};font-size:12px">· {_e(x["each"])}</span>')
    return (f'<a href="{_e(x["url"])}" style="display:block;padding:7px 0;{border}'
            f'text-decoration:none;color:#2C2C2A;font-size:13px">'
            f'{tags_html}{_e(x["what"])}{rating}<br>{" ".join(parts)}</a>')


def build_email(store_name, items):
    return (subject(store_name, items), render_text(store_name, items),
            render_html(store_name, items))


def send_email(store_name, items):
    """Returns True if the mail went out."""
    if not SENDER_PASSWORD:
        print("ERROR: GMAIL_PASSWORD environment variable not set.")
        print("  No email sent. History not updated, so these will retry next run.")
        return False
    subj, text, body = build_email(store_name, items)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subj
    msg["From"] = SENDER_EMAIL
    msg["To"] = TO_EMAIL
    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(body, "html", "utf-8"))
    print(f"Sending email to {TO_EMAIL} ...")
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(SENDER_EMAIL, SENDER_PASSWORD)
            server.sendmail(SENDER_EMAIL, TO_EMAIL, msg.as_string())
        print(f"Email sent to {TO_EMAIL}")
        return True
    except Exception as e:
        print(f"Email failed: {type(e).__name__}: {e}")
        return False

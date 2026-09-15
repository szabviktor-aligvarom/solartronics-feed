#!/usr/bin/env python3
"""
solartronics.de -> napi ar- es keszletfeed (JSON + CSV)

Adatforras: sitemap.xml + kategoria listaoldalak (termek URL-ek gyujtese),
majd termekoldalankent schema.org microdata (Product / Offer / PriceSpecification).
Nincs publikus JSON API a boltban, ezert microdata-alapu kinyeres tortenik.

Csendes hiba elleni vedelem: ha a sanity-check elbukik, a script HIBAVAL leall
es NEM irja felul a meglevo (jo) feedet.
"""
import csv
import gzip
import html
import io
import json
import os
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

BASE = "https://www.solartronics.de"
SITEMAP_INDEX = f"{BASE}/sitemap.xml"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

OUT_DIR = os.path.dirname(os.path.abspath(__file__))
JSON_PATH = os.path.join(OUT_DIR, "feed.json")
CSV_PATH = os.path.join(OUT_DIR, "feed.csv")
STATE_PATH = os.path.join(OUT_DIR, "state.json")

CURRENCY = "EUR"
VAT_RATE = 0.19  # nemet AFA, a szamitott netto arhoz

# --- sanity-check kuszobok (6. pont) ---
MIN_PRODUCTS = 350          # fix minimum termekszam
MAX_MISSING_PRICE_PCT = 20  # max ennyi %-nal lehet hianyzo ar
MAX_SHRINK_PCT = 30         # elozo futashoz kepest max ennyi %-ot eshet a katalogus

WORKERS = 4
REQUEST_PAUSE = 0.25  # kis szunet kerések kozott, hogy a bolt ne lassitson le minket


# ---------------------------------------------------------------- HTTP
def fetch(url, tries=4, binary=False):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept-Language": "de-DE,de;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Connection": "close",
            })
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
            time.sleep(REQUEST_PAUSE)
            return raw if binary else raw.decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            last = e
            # a bolt lassit, ha sok kerés megy egyszerre -> egyre hosszabb varakozas
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"fetch failed: {url}: {last}")


# ---------------------------------------------------------------- helpers
def txt(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def one(pat, h, g=1, flags=0):
    m = re.search(pat, h, flags)
    return m.group(g).strip() if m else ""


def money(s):
    """'1.234,56 EUR' -> 1234.56 (float) vagy None"""
    s = txt(s).replace("*", "").replace("\u20ac", "").replace("EUR", "")
    s = re.sub(r"[^0-9.,]", "", s)
    if not s:
        return None
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        return round(float(s), 2)
    except ValueError:
        return None


# ---------------------------------------------------------------- URL gyujtes
def product_urls_from_sitemap():
    idx = fetch(SITEMAP_INDEX)
    maps = re.findall(r"<loc>([^<]+)</loc>", idx)
    urls, lastmod = set(), {}
    for m in maps:
        raw = fetch(m, binary=True)
        if m.endswith(".gz"):
            raw = gzip.decompress(raw)
        xml = raw.decode("utf-8", "replace")
        for blk in re.findall(r"<url>(.*?)</url>", xml, re.S):
            loc = one(r"<loc>([^<]+)</loc>", blk)
            if not loc:
                continue
            lm = one(r"<lastmod>([^<]+)</lastmod>", blk)
            urls.add(loc)
            if lm:
                lastmod[loc] = lm
    return urls, lastmod


def product_urls_from_categories(cat_urls):
    """A sitemapbol kimaradt termekek (nincs szep URL-juk) igy is elojonnek."""
    found = set()
    for c in cat_urls:
        page = 1
        while page <= 30:
            try:
                h = fetch(f"{c}?p={page}&n=100")
            except Exception as e:  # noqa: BLE001
                print(f"  [warn] kategoria hiba {c} p{page}: {e}", flush=True)
                break
            links = re.findall(
                r'<a href="(' + re.escape(BASE) + r'/[^"#?]+)"[^>]*class="product--(?:title|image)"', h)
            links += re.findall(
                r'class="product--(?:title|image)"[^>]*href="(' + re.escape(BASE) + r'/[^"#?]+)"', h)
            new = set(links)
            if not new or new <= found:
                found |= new
                break
            found |= new
            if len(new) < 100:
                break
            page += 1
    return found


def is_product_candidate(u):
    if not u.startswith(BASE):
        return False
    path = u[len(BASE):]
    if path in ("", "/"):
        return False
    if path.endswith("/"):          # kategoria listaoldal
        return False
    bad = ("/blog", "/jobs", "/account", "/note", "/compare", "/checkout",
           "/register", "/address", "/widgets", "/ticket", "/tracking",
           "/kontaktformular", "/agb", "/impressum", "/datenschutz", "/faq",
           "/lieferung-versand", "/bedarfsrechner", "/bedienungsanleitung",
           "/grosshandel", "/batterieruecknahme", "/newsletter", "/sitemap",
           "/en/", "/widerruf", "/zahlung")
    return not any(b in path for b in bad)


def collect_urls():
    sm_urls, lastmod = product_urls_from_sitemap()
    cats = sorted({u for u in sm_urls if u.startswith(BASE) and u.endswith("/")
                   and "/blog" not in u and "/jobs" not in u})
    print(f"sitemap URL: {len(sm_urls)} | kategoria: {len(cats)}", flush=True)

    cat_urls = product_urls_from_categories(cats)
    print(f"kategoriakbol jott termeklink: {len(cat_urls)}", flush=True)

    allu = {u for u in (sm_urls | cat_urls) if is_product_candidate(u)}
    # /detail/index/sArticle/123/sCategory/45 -> kanonikus alak
    norm = {re.sub(r"/sCategory/\d+$", "", u) for u in allu}
    print(f"termek URL osszesen: {len(norm)}", flush=True)
    return sorted(norm), lastmod


# ---------------------------------------------------------------- parse
def parse(url, h, lastmod_map):
    d = {"url": url}

    d["cikkszam"] = txt(one(r'itemprop="sku"[^>]*>(.*?)</span>', h, 1, re.S))
    d["ean"] = (txt(one(r'itemprop="gtin13"[^>]*>(.*?)</span>', h, 1, re.S))
                or one(r'itemprop="gtin13" content="([^"]+)"', h))
    d["termek_id"] = one(r'itemprop="productID" content="([^"]+)"', h)
    d["nev"] = (txt(one(r'<h1[^>]*class="product--title"[^>]*>(.*?)</h1>', h, 1, re.S))
                or txt(one(r'<meta property="og:title" content="([^"]+)"', h)))
    d["marka"] = one(r'<meta property="product:brand" content="([^"]+)"', h)

    # --- ar: ket szerkezet letezik a boltban ---
    #  A) price-vat (brutto) + price-zerotax (netto); schema.org price = NETTO
    #  B) csak content--default + "inkl. MwSt."; schema.org price = BRUTTO
    schema_price = money(one(r'<meta itemprop="price" content="([^"]+)"', h))
    vat_block = one(r'class="price--content price-vat content--default"\s*>(.*?)</span>', h, 1, re.S)
    has_zerotax = "price-zerotax" in h

    if vat_block and has_zerotax:
        brutto = money(vat_block)
        netto = schema_price
        ar_tipus = "brutto+netto (oldalrol)"
    else:
        plain = one(r'class="price--content content--default"\s*>(.*?)</span>', h, 1, re.S)
        brutto = money(plain) or schema_price
        netto = round(brutto / (1 + VAT_RATE), 2) if brutto else None
        ar_tipus = f"brutto (netto szamitott, {int(VAT_RATE * 100)}% MwSt)"

    d["ar"] = brutto
    d["netto_ar"] = netto
    d["ar_tipus"] = ar_tipus
    # "ab X EUR" = a legkisebb valtozat ara, nem fix ar
    d["tol_ar"] = bool(re.search(r'class="price--content[^"]*"\s*>\s*(?:<meta[^>]*>\s*)?ab\s', h))

    # Variansos termek: a kiirt ar csak az alapvaltozatra igaz, a tobbi valtozat
    # (pl. hosszabb kabel) dragabb. Fontos jelzes az arazashoz.
    d["variansos"] = bool(re.search(r'<select[^>]*name="group\[\d+\]"', h))
    # kiszereles / alapmennyiseg, pl. "10 Meter"
    unit = one(r'class="price--label label--purchase-unit"[^>]*>\s*Inhalt:\s*</span>(.*?)</div>', h, 1, re.S)
    d["kiszereles"] = txt(unit)
    # ha variansos vagy 'ab' aras, az ar nem fix
    d["ar_fix"] = not (d["tol_ar"] or d["variansos"])

    # --- listaar / athuzott ar (Shopware: pseudoprice) ---
    pseudo = money(one(r'class="price--line-through"[^>]*>(.*?)</span>', h, 1, re.S)) \
        or money(one(r'itemprop="highPrice" content="([^"]+)"', h)) \
        or money(one(r'class="price--pseudopreis"[^>]*>(.*?)</span>', h, 1, re.S))
    d["listaar"] = pseudo
    if pseudo and brutto and pseudo > brutto:
        d["akcios"] = True
        d["kedvezmeny_szazalek"] = round((pseudo - brutto) / pseudo * 100, 1)
    else:
        d["akcios"] = False
        d["kedvezmeny_szazalek"] = None

    # --- keszlet ---
    avail = one(r'itemprop="availability" href="https://schema\.org/([A-Za-z]+)"', h)
    cands = re.findall(r'<span class="delivery--text([^"]*)">(.*?)</span>', h, re.S)
    delivery = ""
    for cls, body in cands:
        if "shipping-free" in cls:      # ez szallitasi reklam, nem keszlet
            continue
        delivery = txt(body)
        break
    if not delivery and cands:
        delivery = txt(cands[0][1])

    has_cart = bool(re.search(r'<form name="sAddToBasket"', h))
    statuses = set(re.findall(r"delivery--status-((?!icon|shipping-free)[a-z-]+)", h))
    hard_out = bool(re.search(r"ausverkauft|nicht mehr verf\u00fcgbar|nicht lieferbar", h, re.I))

    if "available" in statuses:
        keszlet = "raktaron"
    elif "not-available" in statuses:
        keszlet = "nincs raktaron (beszerzes alatt)"
    elif "more-is-coming" in statuses:
        keszlet = "utanpotlas uton"
    elif avail == "InStock":
        keszlet = "raktaron"
    elif avail == "OutOfStock":
        keszlet = "elfogyott"
    else:
        keszlet = "ismeretlen"

    d["keszleten_van"] = (keszlet == "raktaron")
    d["keszlet_statusz"] = keszlet
    d["szallitasi_info"] = delivery
    # a bolt akkor is engedi a rendelest, ha nincs raktaron
    d["rendelheto"] = bool(has_cart and avail != "OutOfStock" and not hard_out)

    # --- suly ---
    w = one(r'itemprop="weight" content="([^"]+)"', h)
    d["suly_kg"] = money(w) if w else None

    # --- fokep ---
    d["fokep_url"] = one(r'<meta property="og:image" content="([^"]+)"', h)

    d["utolso_modositas"] = lastmod_map.get(url, "")
    return d


def job(args):
    url, lastmod_map = args
    try:
        return parse(url, fetch(url), lastmod_map)
    except Exception as e:  # noqa: BLE001
        return {"url": url, "cikkszam": "", "_hiba": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------- dedup (3. pont)
def dedup(rows):
    """Cikkszam szerint egyedive tesz. Keszleten levo valtozat nyer."""
    by_sku = {}
    for r in rows:
        by_sku.setdefault(r["cikkszam"], []).append(r)

    out = []
    for sku, group in by_sku.items():
        if len(group) == 1:
            g = dict(group[0])
            g["duplikalt_cikkszam"] = False
            g["duplikalt_db"] = 1
            out.append(g)
            continue
        # keszleten levo elonyben, aztan a rendelheto, aztan a dragabb (teljesebb adat)
        group.sort(key=lambda r: (
            0 if r.get("keszleten_van") else 1,
            0 if r.get("rendelheto") else 1,
            -(r.get("ar") or 0),
        ))
        winner = dict(group[0])
        winner["duplikalt_cikkszam"] = True
        winner["duplikalt_db"] = len(group)
        winner["duplikalt_url_k"] = [g["url"] for g in group[1:]]
        out.append(winner)
    return out


# ---------------------------------------------------------------- sanity (6. pont)
def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:  # noqa: BLE001
            return {}
    return {}


def sanity_check(rows, state):
    errors = []
    n = len(rows)

    if n < MIN_PRODUCTS:
        errors.append(f"termekszam {n} < minimum {MIN_PRODUCTS}")

    no_price = sum(1 for r in rows if not r.get("ar"))
    pct = (no_price / n * 100) if n else 100
    if pct > MAX_MISSING_PRICE_PCT:
        errors.append(f"ar nelkuli termek {pct:.1f}% > {MAX_MISSING_PRICE_PCT}%")

    prev = state.get("product_count")
    if prev:
        drop = (prev - n) / prev * 100
        if drop > MAX_SHRINK_PCT:
            errors.append(f"katalogus zuhanas {drop:.1f}% (elozo {prev} -> most {n}) > {MAX_SHRINK_PCT}%")

    fails = sum(1 for r in rows if r.get("_hiba"))
    print(f"sanity: db={n} ar_nelkul={no_price} ({pct:.1f}%) elozo={prev} letoltesi_hiba={fails}", flush=True)
    return errors


# ---------------------------------------------------------------- output
CSV_FIELDS = [
    "cikkszam", "nev", "ar", "listaar", "akcios", "kedvezmeny_szazalek",
    "keszleten_van", "keszlet_statusz", "rendelheto", "suly_kg",
    "url", "fokep_url", "utolso_modositas",
    "netto_ar", "ar_tipus", "ar_fix", "tol_ar", "variansos", "kiszereles", "ean", "marka",
    "duplikalt_cikkszam", "duplikalt_db",
]


def write_outputs(rows, generated):
    rows.sort(key=lambda r: (r.get("marka") or "", r.get("nev") or ""))
    in_stock = sum(1 for r in rows if r.get("keszleten_van"))

    meta = {
        "forras": BASE,
        "generalva": generated,
        "penznem": CURRENCY,
        "ar_tipus": "brutto kiskereskedelmi ar (inkl. MwSt), nemet AFA 19%",
        "termek_db": len(rows),
        "keszleten_db": in_stock,
        "keszlet_tipus": "statusz (van/nincs), nem darabszam - a bolt nem ad ki mennyiseget",
        "megjegyzes": ("A bolt akkor is engedi a rendelest, ha a termek nincs raktaron, "
                       "ezert a 'rendelheto' szinte mindig true. A valos keszletinfo: "
                       "'keszleten_van' es 'keszlet_statusz'."),
        "azonosito_kulcs": "cikkszam",
    }

    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "products": rows}, f, ensure_ascii=False, indent=1)

    with open(CSV_PATH, "w", newline="", encoding="utf-8-sig") as f:
        f.write(f"# forras={BASE} generalva={generated} penznem={CURRENCY} "
                f"ar=brutto_kiskereskedelmi_inkl_MwSt termek_db={len(rows)} keszleten={in_stock}\n")
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, delimiter=";", extrasaction="ignore")
        w.writeheader()
        for r in rows:
            row = dict(r)
            for k, v in list(row.items()):
                if isinstance(v, bool):
                    row[k] = "igen" if v else "nem"
                elif v is None:
                    row[k] = ""
            w.writerow(row)

    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump({"product_count": len(rows), "in_stock": in_stock,
                   "generated": generated}, f, indent=1)
    return meta


# ---------------------------------------------------------------- main
def main():
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"=== solartronics feed | {generated}", flush=True)

    urls, lastmod = collect_urls()
    if len(urls) < MIN_PRODUCTS:
        print(f"HIBA: mar az URL-gyujtes is keves termeket adott ({len(urls)} < {MIN_PRODUCTS}). "
              f"A meglevo feed valtozatlan marad.", file=sys.stderr)
        sys.exit(1)

    rows = []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for i, r in enumerate(ex.map(job, [(u, lastmod) for u in urls]), 1):
            rows.append(r)
            if i % 100 == 0:
                print(f"  {i}/{len(urls)}", flush=True)

    ok = [r for r in rows if not r.get("_hiba") and r.get("cikkszam")]
    print(f"letoltve: {len(rows)} | ervenyes cikkszammal: {len(ok)}", flush=True)

    deduped = dedup(ok)
    dups = sum(1 for r in deduped if r.get("duplikalt_cikkszam"))
    print(f"dedup utan: {len(deduped)} egyedi cikkszam (duplikalt volt: {dups})", flush=True)

    state = load_state()
    errors = sanity_check(deduped, state)
    if errors:
        print("SANITY-CHECK ELBUKOTT, a feed NEM lesz felulirva:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        sys.exit(1)

    meta = write_outputs(deduped, generated)
    print(f"OK: {meta['termek_db']} termek, keszleten {meta['keszleten_db']}", flush=True)


if __name__ == "__main__":
    main()

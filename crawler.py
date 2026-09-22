import os
import re
import json
import time
import random
import datetime
import requests
from bs4 import BeautifulSoup
from supabase import create_client

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
sb = create_client(SUPABASE_URL, SUPABASE_KEY)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.mubawab.ma/fr/",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
}

MIN_DELAY, MAX_DELAY = 3.0, 4.5   # validated-safe pacing, plus jitter
TIMEOUT = 20
MAX_RETRIES = 3

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

# City list — adding a town later means adding one entry here, nothing else changes
CITIES = [
    {
        "city": "Mohammedia",
        "table": "sale_listings",
        "base_url": "https://www.mubawab.ma/fr/ct/mohammedia/immobilier-a-vendre-all",
    },
]

TYPE_KEYWORDS = [
    ("terrain", "Terrains"),
    ("villa", "Villas"),
    ("riad", "Riads"),
    ("ferme", "Fermes"),
    ("bureau", "Bureaux"),
    ("local commercial", "Locaux commerciaux"),
    ("magasin", "Locaux commerciaux"),
    ("maison", "Maisons"),
    ("appartement", "Appartements"),
    ("studio", "Appartements"),
    ("duplexe", "Appartements"),
]


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

def fetch(url):
    for attempt in range(MAX_RETRIES):
        try:
            resp = SESSION.get(url, timeout=TIMEOUT)
            print(f"[fetch] {url} -> status {resp.status_code}, {len(resp.text)} bytes, "
                  f"server={resp.headers.get('server')}, cf-ray={resp.headers.get('cf-ray')}")
            if resp.status_code == 200:
                return resp.text
            if resp.status_code == 404:
                return None
            if resp.status_code in (403, 429, 503):
                # likely bot-detection / rate-limit response — log a snippet so we can tell
                # a Cloudflare/Akamai challenge page apart from a real block
                print(f"[fetch] {url} -> blocked-ish body snippet: {resp.text[:300]!r}")
        except requests.RequestException as e:
            print(f"[fetch] {url} -> exception {e!r}")
        time.sleep(2 * (attempt + 1))
    return None


def pace():
    time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def classify_type(title):
    t = (title or "").lower()
    for kw, label in TYPE_KEYWORDS:
        if kw in t:
            return label
    return "Immobilier divers"


# ---------------------------------------------------------------------------
# List-page parsing
# ---------------------------------------------------------------------------

def results_count(html):
    m = re.search(r"\(([\d\s]+)\s*r[ée]sultats?\)", html)
    if m:
        return int(m.group(1).replace(" ", ""))
    return None


def parse_list_page(html):
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for box in soup.select("div.listingBox"):
        if box.get("promotion-id"):
            for promo in box.select("div.promo-item"):
                row = parse_card(promo)
                if row:
                    rows.append(row)
            continue
        row = parse_card(box)
        if row:
            rows.append(row)
    return rows


def parse_card(card):
    id_input = card.select_one("input.adId")
    mubawab_id = id_input.get("value") if id_input else None
    if not mubawab_id:
        return None

    link_el = card.select_one("a.listingLink") or card.select_one("a")
    url = link_el.get("href") if link_el else None
    if url and url.startswith("/"):
        url = "https://www.mubawab.ma" + url

    title_el = card.select_one("h2.listingTit") or card.select_one("h2")
    title = title_el.get_text(strip=True) if title_el else None

    price_el = card.select_one(".priceTag")
    price_mad = clean_price(price_el.get_text()) if price_el else None

    drop_el = card.select_one(".priceDown, span.priceDown")
    price_drop_mad = clean_price(drop_el.get_text()) if drop_el else None

    surface_m2 = bedrooms = bathrooms = None
    for feat in card.select(".adDetailFeature"):
        icon = feat.select_one("i")
        cls = " ".join(icon.get("class", [])) if icon else ""
        val = feat.get_text(strip=True)
        num = extract_number(val)
        if "triangle" in cls:
            surface_m2 = num
        elif "bed" in cls:
            bedrooms = num
        elif "bath" in cls:
            bathrooms = num

    loc_el = card.select_one("h3.listingH3") or card.select_one(".listingH3")
    neighborhood_tag = loc_el.get_text(strip=True) if loc_el else None

    is_premium = bool(card.select_one(".listingPremium, .premiumTag"))

    return {
        "mubawab_id": mubawab_id,
        "url": url,
        "title": title,
        "price_mad": price_mad,
        "price_drop_mad": price_drop_mad,
        "surface_m2": surface_m2,
        "bedrooms": bedrooms,
        "bathrooms": bathrooms,
        "neighborhood_tag": neighborhood_tag,
        "is_premium": is_premium,
        "property_type": classify_type(title),
    }


def clean_price(text):
    digits = re.sub(r"[^\d]", "", text or "")
    return int(digits) if digits else None


def extract_number(text):
    m = re.search(r"[\d.,]+", text or "")
    if not m:
        return None
    return float(m.group().replace(",", "."))


# ---------------------------------------------------------------------------
# Detail-page parsing
# ---------------------------------------------------------------------------

def parse_detail_page(html):
    soup = BeautifulSoup(html, "html.parser")
    out = {}

    block = soup.select_one("div.caractBlockProp")
    attrs = {}
    amenities = []
    if block:
        for it in block.select("div.adMainFeature"):
            lab = it.select_one("p.adMainFeatureContentLabel")
            val = it.select_one("p.adMainFeatureContentValue")
            if lab and val:
                attrs[lab.get_text(strip=True)] = val.get_text(" ", strip=True)
        amenities = [p.get_text(" ", strip=True) for p in block.select("div.adFeature p")]

    out["condition"] = attrs.get("Etat") or attrs.get("État")
    out["building_age"] = attrs.get("Années")
    floor = attrs.get("Étage du bien") or attrs.get("Etage du bien")
    out["floor_num"] = parse_floor(floor)
    out["orientation"] = attrs.get("Orientation")
    out["floor_covering"] = attrs.get("Type du sol")
    out["standing"] = attrs.get("Standing")
    out["construction_status"] = attrs.get("État") if "Finalisé" in (attrs.get("État") or "") or "construction" in (attrs.get("État") or "").lower() else None
    out["delivery_date"] = attrs.get("Livraison")
    out["new_construction"] = "neuf" in " ".join(amenities).lower() or "jamais habité" in (out["condition"] or "").lower()

    out["amenities_raw"] = amenities

    ld = {}
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(s.get_text(), strict=False)
        except Exception:
            continue
        if isinstance(data, list):
            data = next((d for d in data if isinstance(d, dict)), {})
        if isinstance(data, dict):
            ld = data
            break
    seller = ld.get("seller") if isinstance(ld.get("seller"), dict) else {}
    out["description_text"] = (ld.get("description") or "")[:2000] or None

    biz = soup.select_one("span.businessName a")
    if biz:
        out["seller_type"] = "agency"
        out["seller_name"] = biz.get_text(strip=True)
    elif seller.get("name"):
        out["seller_type"] = "promoter"
        out["seller_name"] = seller.get("name")
    else:
        out["seller_type"] = "private_or_unknown"
        out["seller_name"] = None

    ref = soup.select_one("div.refBox span")
    out["agency_ref"] = re.sub(r"^\s*R[ée]f\s*:\s*", "", ref.get_text(strip=True)) if ref else None

    out["photo_count"] = len(soup.select("div.sliderPhotos img")) or None
    out["has_catalogue_pdf"] = bool(soup.select_one("a[href*='Catalogue'], a:-soup-contains('Catalogue PDF')"))

    # Coordinates from the embedded map div
    map_div = soup.select_one("div#mapOpen, div[lat][lon]")
    if map_div and map_div.get("lat") and map_div.get("lon"):
        try:
            out["latitude"] = float(map_div["lat"])
            out["longitude"] = float(map_div["lon"])
        except ValueError:
            out["latitude"] = out["longitude"] = None
    else:
        out["latitude"] = out["longitude"] = None

    return out


def parse_floor(text):
    if not text:
        return None
    m = re.search(r"(\d+)", text)
    if m:
        return int(m.group(1))
    if "rez" in text.lower():
        return 0
    return None


AMENITY_MAP = {
    "ascenseur": "am_ascenseur", "chambre de rangement": "am_chambre_rangement",
    "chauffage central": "am_chauffage_central", "cheminée": "am_cheminee",
    "climatisation": "am_climatisation", "cuisine équipée": "am_cuisine_equipee",
    "double vitrage": "am_double_vitrage", "entre-sol": "am_entre_sol",
    "garage": "am_garage", "jardin": "am_jardin", "meublé": "am_meuble",
    "piscine": "am_piscine", "porte blindée": "am_porte_blindee",
    "salon marocain": "am_salon_marocain", "sécurité": "am_securite",
    "terrasse": "am_terrasse", "vue sur mer": "am_vue_sur_mer",
    "vue sur les montagnes": "am_vue_sur_montagnes", "concierge": "am_concierge",
    "salon européen": "am_salon_europeen", "antenne parabolique": "am_antenne_parabolique",
}


def amenity_flags(amenities_raw):
    present = {a.lower().strip() for a in amenities_raw}
    return {col: any(key in a for a in present) for key, col in AMENITY_MAP.items()}


# ---------------------------------------------------------------------------
# Crawl one city
# ---------------------------------------------------------------------------

def crawl_city(city_cfg, crawl_date, crawl_run_id):
    all_rows = []
    page = 1
    prev_ids = set()

    while True:
        url = city_cfg["base_url"] if page == 1 else f"{city_cfg['base_url']}:p:{page}"
        html = fetch(url)
        if html is None:
            print(f"[{city_cfg['city']}] page {page}: fetch returned None, stopping")
            break

        soup_debug = BeautifulSoup(html, "html.parser")
        box_count = len(soup_debug.select("div.listingBox"))
        print(f"[{city_cfg['city']}] page {page}: {box_count} div.listingBox found in HTML")

        rows = parse_list_page(html)
        if not rows:
            print(f"[{city_cfg['city']}] page {page}: parse_list_page returned 0 rows, stopping")
            break

        new_ids = {r["mubawab_id"] for r in rows} - prev_ids
        if page > 1 and len(new_ids) < len(rows) * 0.5:
            break  # reached the end / repeating content

        all_rows.extend(rows)
        prev_ids.update(r["mubawab_id"] for r in rows)

        page += 1
        pace()
        if page > 60:  # sanity ceiling
            break

    total_site = None
    if all_rows:
        first_html = fetch(city_cfg["base_url"])
        if first_html:
            total_site = results_count(first_html)

    print(f"[{city_cfg['city']}] list pages done — {len(all_rows)} listings found "
          f"(site reports ~{total_site})")

    # Detail-page enrichment — full daily refetch, feasible at this city's scale
    enriched = []
    for i, row in enumerate(all_rows, 1):
        detail = {}
        if row.get("url"):
            html = fetch(row["url"])
            pace()
            if html:
                detail = parse_detail_page(html)
        flags = amenity_flags(detail.pop("amenities_raw", []))

        record = {
            "mubawab_id": row["mubawab_id"],
            "url": row["url"],
            "crawl_date": crawl_date,
            "crawl_run_id": crawl_run_id,
            "transaction_type": "vente",
            "property_type": row["property_type"],
            "title": row["title"],
            "price_mad": row["price_mad"],
            "price_drop_mad": row["price_drop_mad"],
            "is_premium": row["is_premium"],
            "surface_m2": row["surface_m2"],
            "bedrooms": row["bedrooms"],
            "bathrooms": row["bathrooms"],
            "city": city_cfg["city"],
            "neighborhood_tag": row["neighborhood_tag"],
            **detail,
            **flags,
        }
        enriched.append(record)

        if i % 25 == 0:
            print(f"[{city_cfg['city']}] detail pages: {i}/{len(all_rows)}")

    return enriched


# ---------------------------------------------------------------------------
# Write to Supabase
# ---------------------------------------------------------------------------

def upsert_rows(table, rows):
    if not rows:
        return
    batch_size = 200
    for i in range(0, len(rows), batch_size):
        chunk = rows[i:i + batch_size]
        sb.table(table).upsert(chunk, on_conflict="mubawab_id,crawl_date").execute()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    crawl_date = datetime.date.today().isoformat()
    crawl_run_id = f"run_{datetime.datetime.utcnow().strftime('%Y%m%dT%H%M%S')}"

    for city_cfg in CITIES:
        rows = crawl_city(city_cfg, crawl_date, crawl_run_id)
        upsert_rows(city_cfg["table"], rows)
        print(f"[{city_cfg['city']}] wrote {len(rows)} rows for {crawl_date}")


if __name__ == "__main__":
    main()

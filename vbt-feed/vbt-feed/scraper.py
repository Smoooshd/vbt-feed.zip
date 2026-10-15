#!/usr/bin/env python3
"""
Toyota of Vero Beach -> Meta (Facebook) vehicle catalog feed.

1. Reads the dealer's DealerOn sitemap, which lists every new and used vehicle page (VDP) by VIN.
2. Visits each VDP and pulls price, mileage, colors, specs, photos and dealer comments.
3. Writes feed/vehicles.csv in Meta's vehicle catalog format.
   A sold unit drops off the website -> drops out of the CSV -> Meta removes it on the next fetch.
4. Logs added/removed VINs to feed/changes.log and skipped units to feed/problems.csv.

Settings are in the CONFIG block below.
"""
import csv
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ----------------------------- CONFIG -----------------------------
BASE = "https://www.verobeachtoyota.com"
SITEMAP = f"{BASE}/sitemap.aspx"

DEALER = {
    "name": "Toyota of Vero Beach",
    "phone": "772-291-0653",
    "addr1": "1075 S US Highway 1",
    "city": "Vero Beach",
    "region": "FL",
    "postal_code": "32962",
    "country": "US",
    "latitude": 27.584434509277344,
    "longitude": -80.378173828125,
}

INCLUDE_NEW = True
INCLUDE_USED = True

# First label found on the page wins. "Vero Transparent Price" is the site's
# fee-inclusive number (selling price + dealer fee + e-title fee).
PRICE_PREFERENCE = [
    "Vero Transparent Price",
    "Selling Price",
    "Sale Price",
    "Final Price",
    "Retail Price",
    "MSRP",
]

MAX_PHOTOS = 20        # Meta accepts up to 20 images per vehicle
DELAY_SEC = 1.0        # pause between page requests, keeps load on the site light
MIN_KEEP_RATIO = 0.5   # abort (keep old feed) if this run finds < 50% of last run's count
# ------------------------------------------------------------------

OUT = Path(__file__).resolve().parent / "feed"
FEED = OUT / "vehicles.csv"
STATE = OUT / "last_vins.json"
LOG = OUT / "changes.log"
PROBLEMS = OUT / "problems.csv"

session = requests.Session()
session.headers["User-Agent"] = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)


def get(url, tries=3):
    for i in range(tries):
        try:
            r = session.get(url, timeout=30)
            if r.status_code == 200:
                return r.text
            if r.status_code in (404, 410):
                return None
        except requests.RequestException:
            pass
        time.sleep(3 * (i + 1))
    return None


# ------------------------- inventory list -------------------------
VDP_PATH = re.compile(r"^/(new|used)-.+-([A-Za-z0-9]{17})$")


def list_vehicles():
    page = get(SITEMAP)
    if not page:
        sys.exit("ERROR: could not load sitemap, feed left unchanged.")
    soup = BeautifulSoup(page, "lxml")
    found = {}
    for a in soup.find_all("a", href=True):
        path = re.sub(r"^https?://[^/]+", "", a["href"].strip())
        m = VDP_PATH.match(path)
        if not m:
            continue
        cond, vin = m.group(1).lower(), m.group(2).upper()
        if (cond == "new" and not INCLUDE_NEW) or (cond == "used" and not INCLUDE_USED):
            continue
        found[vin] = {
            "vin": vin,
            "condition": cond,
            "url": BASE + path,
            "path": path,
            "link_text": a.get_text(" ", strip=True),
        }
    return found


# --------------------------- name parsing --------------------------
def take_prefix(text, slug_piece):
    """Match a URL slug piece (e.g. 'Mazda+CX+30') against the start of readable
    text (e.g. 'Mazda CX-30 Touring') ignoring punctuation. Returns (match, rest)."""
    target = re.sub(r"[^a-z0-9]", "", slug_piece.lower())
    if not target:
        return None, text
    acc = ""
    for i, ch in enumerate(text):
        if ch.isalnum():
            acc += ch.lower()
            if acc == target:
                return text[: i + 1].strip(), text[i + 1 :].strip()
            if not target.startswith(acc):
                break
    return None, text


def parse_name(v):
    """Year / make / model / trim from the sitemap link text, guided by the URL slug."""
    name = v["link_text"].split(" / ")[0].strip()
    parts = v["path"].lstrip("/").split("-")  # [cond, location, year, make, model, trim..., vin]
    year = parts[2] if len(parts) > 2 and parts[2].isdigit() else name.split()[0]
    rest = name[len(year):].strip() if name.startswith(year) else name

    make_slug = parts[3] if len(parts) > 3 else ""
    make, rest = take_prefix(rest, make_slug)
    if not make:
        make = make_slug.replace("+", " ")

    model_slug = parts[4] if len(parts) > 4 else ""
    model, after = take_prefix(rest, model_slug)
    if not model:
        model = rest.split()[0] if rest else ""
        after = rest[len(model):].strip()
    if model.lower().startswith(make.lower() + " "):   # "Toyota Crown" -> "Crown"
        model = model[len(make) + 1 :]
    return year, make, model, after, name


# ---------------------------- VDP parsing --------------------------
def money(s):
    m = re.search(r"\$?\s*([\d,]{3,})", s or "")
    return int(m.group(1).replace(",", "")) if m else None


def find_label(lines, label):
    for i, ln in enumerate(lines):
        if ln.startswith(label):
            val = ln[len(label):].strip(" :")
            if val:
                return val
            if i + 1 < len(lines):
                return lines[i + 1].strip(" :")
    return None


def json_ld(soup):
    """Pull a schema.org Vehicle/Car/Product block if the page has one."""
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(s.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for item in data if isinstance(data, list) else [data]:
            t = item.get("@type", "")
            t = " ".join(t) if isinstance(t, list) else t
            if any(k in t for k in ("Car", "Vehicle", "Product")):
                return item
    return {}


def parse_vdp(html, vin):
    soup = BeautifulSoup(html, "lxml")
    ld = json_ld(soup)

    # Photos: DealerOn serves them at /inventoryphotos/<acct>/<vin>/ip/<n>.jpg
    photos = {}
    for acct, n in re.findall(rf"/inventoryphotos/(\d+)/{vin.lower()}/ip/(\d+)\.jpg", html, re.I):
        photos.setdefault(int(n), f"{BASE}/inventoryphotos/{acct}/{vin.lower()}/ip/{n}.jpg")
    images = [photos[k] for k in sorted(photos)]
    if not images and ld.get("image"):
        img = ld["image"]
        images = img if isinstance(img, list) else [img]
    images = images[:MAX_PHOTOS]

    heading = ""
    for h in soup.find_all(["h1", "h2"]):
        txt = h.get_text(" ", strip=True)
        if re.search(r"\b(19|20)\d{2}\b", txt):
            heading = txt
            break

    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    lines = [ln.strip() for ln in soup.get_text("\n").splitlines() if ln.strip()]

    # Prices appear above "Vehicle Information"; specs below it
    price, price_label = None, None
    for label in PRICE_PREFERENCE:
        p = money(find_label(lines, label))
        if p:
            price, price_label = p, label
            break
    if not price:
        offers = ld.get("offers") or {}
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        try:
            price = int(float(offers.get("price")))
            price_label = "json-ld"
        except (TypeError, ValueError):
            pass

    try:
        spec = lines[lines.index("Vehicle Information"):]
    except ValueError:
        spec = lines

    mileage_txt = find_label(spec, "Mileage")
    mileage = money(mileage_txt) if mileage_txt and re.search(r"\d", mileage_txt) else None
    if mileage is None:
        odo = ld.get("mileageFromOdometer")
        if isinstance(odo, dict):
            try:
                mileage = int(float(odo.get("value")))
            except (TypeError, ValueError):
                pass

    comments = ""
    if "Dealer Comments" in lines:
        i = lines.index("Dealer Comments") + 1
        buf = []
        while i < len(lines) and lines[i] not in ("Read More...", "Hide Comments", "Eligible Benefits"):
            buf.append(lines[i])
            i += 1
        comments = " ".join(buf)

    return {
        "heading": heading,
        "price": price,
        "price_label": price_label,
        "mileage": mileage,
        "stock": find_label(spec, "Stock #"),
        "body": find_label(spec, "Body Style") or ld.get("bodyType"),
        "ext_color": find_label(spec, "Exterior Color") or ld.get("color"),
        "int_color": find_label(spec, "Interior Color") or ld.get("vehicleInteriorColor"),
        "engine": find_label(spec, "Engine"),
        "transmission": find_label(spec, "Transmission") or ld.get("vehicleTransmission"),
        "fuel": find_label(spec, "Fuel Type") or ld.get("fuelType"),
        "drivetrain": find_label(spec, "Drivetrain") or find_label(spec, "Drive Type")
                      or ld.get("driveWheelConfiguration"),
        "comments": comments or ld.get("description", ""),
        "images": images,
    }


# ------------------------ Meta value mapping -----------------------
def map_body(s):
    s = (s or "").lower()
    for keys, val in [
        (("sport utility", "suv"), "SUV"),
        (("crew cab", "double cab", "extended cab", "regular cab", "pickup", "truck"), "TRUCK"),
        (("minivan",), "MINIVAN"),
        (("van",), "VAN"),
        (("convertible",), "CONVERTIBLE"),
        (("coupe",), "COUPE"),
        (("hatchback",), "HATCHBACK"),
        (("wagon",), "WAGON"),
        (("sedan",), "SEDAN"),
        (("crossover",), "CROSSOVER"),
    ]:
        if any(k in s for k in keys):
            return val
    return "OTHER"


def map_trans(s):
    s = (s or "").lower()
    if "manual" in s:
        return "MANUAL"
    return "AUTOMATIC" if s else ""


def map_fuel(s, name):
    s = f"{s or ''} {name}".lower()
    if "plug" in s:
        return "PLUGIN_HYBRID"
    if "hybrid" in s:
        return "HYBRID"
    if "electric" in s or " bz" in s:
        return "ELECTRIC"
    if "diesel" in s:
        return "DIESEL"
    if "flex" in s:
        return "FLEX"
    if "gas" in s:
        return "GASOLINE"
    return "OTHER"


def map_drive(s, name):
    s = f"{s or ''} {name}".lower()
    if "4wd" in s or "4x4" in s or "four wheel" in s:
        return "4X4"
    if "awd" in s or "all wheel" in s or "quattro" in s or "4matic" in s or "xdrive" in s:
        return "AWD"
    if "fwd" in s or "front wheel" in s:
        return "FWD"
    if "rwd" in s or "rear wheel" in s:
        return "RWD"
    return ""


# ------------------------------ main ------------------------------
def main():
    OUT.mkdir(exist_ok=True)
    started = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    vehicles = list_vehicles()
    print(f"Sitemap lists {len(vehicles)} vehicles")

    last = json.loads(STATE.read_text()) if STATE.exists() else []
    if last and len(last) > 20 and len(vehicles) < MIN_KEEP_RATIO * len(last):
        sys.exit(f"ABORT: found {len(vehicles)} vehicles vs {len(last)} last run. "
                 "Site may be down or changed. Old feed kept.")

    rows, problems = [], []
    max_imgs = 0
    for n, (vin, v) in enumerate(vehicles.items(), 1):
        html = get(v["url"])
        time.sleep(DELAY_SEC)
        if not html:
            problems.append((vin, v["url"], "page did not load"))
            continue
        d = parse_vdp(html, vin)
        year, make, model, trim, name = parse_name(v)

        missing = [k for k in ("price", "images") if not d[k]]
        if v["condition"] == "used" and d["mileage"] is None:
            missing.append("mileage")
        if missing:
            problems.append((vin, v["url"], "missing " + ", ".join(missing)))
            continue

        state = "NEW" if v["condition"] == "new" else (
            "CPO" if "certified" in d["heading"].lower() else "USED")
        title = d["heading"] or name
        desc = d["comments"] or f"{title} at {DEALER['name']}, {DEALER['city']}, {DEALER['region']}."
        max_imgs = max(max_imgs, len(d["images"]))

        row = {
            "vehicle_id": vin,
            "vin": vin,
            "title": title[:150],
            "description": desc[:5000],
            "url": v["url"],
            "make": make,
            "model": model,
            "year": year,
            "trim": trim,
            "mileage.value": d["mileage"] if d["mileage"] is not None else 0,
            "mileage.unit": "MI",
            "price": f"{d['price']} USD",
            "body_style": map_body(d["body"]),
            "exterior_color": d["ext_color"] or "",
            "interior_color": d["int_color"] or "",
            "transmission": map_trans(d["transmission"]),
            "fuel_type": map_fuel(d["fuel"], title),
            "drivetrain": map_drive(d["drivetrain"], f"{title} {d['engine'] or ''}"),
            "state_of_vehicle": state,
            "dealer_name": DEALER["name"],
            "dealer_phone": DEALER["phone"],
            "address": json.dumps({k: DEALER[k] for k in
                                   ("addr1", "city", "region", "postal_code", "country")}),
            "latitude": DEALER["latitude"],
            "longitude": DEALER["longitude"],
            "custom_label_0": d["stock"] or "",
            "custom_label_1": d["price_label"],
        }
        for i, img in enumerate(d["images"]):
            row[f"image[{i}].url"] = img
        rows.append(row)
        if n % 25 == 0:
            print(f"  {n}/{len(vehicles)} pages read")

    if not rows:
        sys.exit("ABORT: no usable vehicles parsed. Page layout may have changed. Old feed kept.")

    fields = [f for f in rows[0] if not f.startswith("image[")] + \
             [f"image[{i}].url" for i in range(max_imgs)]
    tmp = FEED.with_suffix(".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    tmp.replace(FEED)

    with PROBLEMS.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["vin", "url", "issue"])
        w.writerows(problems)

    now_vins = sorted(r["vin"] for r in rows)
    added = sorted(set(now_vins) - set(last))
    removed = sorted(set(last) - set(now_vins))
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"{started}  listed={len(now_vins)}  added={len(added)}  removed={len(removed)}  "
                f"skipped={len(problems)}\n")
        for vin in added:
            f.write(f"    + {vin}\n")
        for vin in removed:
            f.write(f"    - {vin}  (sold or pulled from site)\n")
    STATE.write_text(json.dumps(now_vins))

    print(f"Feed: {len(rows)} vehicles  |  added {len(added)}  removed {len(removed)}  "
          f"skipped {len(problems)} (see feed/problems.csv)")


if __name__ == "__main__":
    main()

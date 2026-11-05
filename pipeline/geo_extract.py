"""One-off: static geography for each place -> pipeline/cache/geo.json
  * coordinates (NRCan Canadian Geographical Names)
  * 2021 census subdivision match (DGUID) from cache/census.json
  * drive time from downtown Vancouver and to the nearest airport with scheduled flights (OSRM)
  * forest cover within 60 km (NRCan 2020 Land Cover of Canada, 30 m product rendered via WMS)
These don't change day to day, so the twice-daily refresh reuses the cache.
Run after census_extract.py:  python pipeline/geo_extract.py
"""
import csv, io, json, math, os, re, time, unicodedata, urllib.parse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
UA = {"User-Agent": "bc-ab-relocation-atlas/1.0 (github.com/WestCoastRJ/bc-ab-relocation-atlas)"}
VANCOUVER = (49.2827, -123.1207)

# Rural municipal districts / counties are not towns: keep only the two big "specialized
# municipalities" that are really urban centres, shown under their town name.
URBAN_SPECIALIZED = {
    "Strathcona County": ("Sherwood Park (Strathcona County)", 53.5413, -113.2958),
    "Wood Buffalo, Regional Municipality of": ("Fort McMurray (Wood Buffalo)", 56.7267, -111.3810),
}
TYPE_CODE = {"City": "CY", "Town": "T", "District municipality": "DM", "Resort municipality": "RGM",
             "Specialized municipality": "SM", "Municipal district": "MD"}
PROV = {"British Columbia": "59", "Alberta": "48"}

# Airports with scheduled passenger service (lat, lon)
AIRPORTS = {
    "Vancouver YVR": (49.194, -123.184), "Abbotsford YXX": (49.025, -122.361), "Victoria YYJ": (48.647, -123.426),
    "Nanaimo YCD": (49.052, -123.870), "Comox YQQ": (49.711, -124.887), "Campbell River YBL": (49.951, -125.271),
    "Port Hardy YZT": (50.681, -127.367), "Powell River YPW": (49.834, -124.500), "Kelowna YLW": (49.956, -119.378),
    "Penticton YYF": (49.463, -119.602), "Kamloops YKA": (50.702, -120.444), "Castlegar YCG": (49.296, -117.632),
    "Cranbrook YXC": (49.611, -115.782), "Prince George YXS": (53.889, -122.679), "Quesnel YQZ": (53.026, -122.510),
    "Williams Lake YWL": (52.183, -122.054), "Smithers YYD": (54.825, -127.183), "Terrace YXT": (54.469, -128.576),
    "Prince Rupert YPR": (54.286, -130.445), "Fort St. John YXJ": (56.238, -120.740), "Dawson Creek YDQ": (55.742, -120.183),
    "Fort Nelson YYE": (58.836, -122.597), "Trail YZZ": (49.056, -117.609), "Calgary YYC": (51.131, -114.010),
    "Edmonton YEG": (53.310, -113.580), "Grande Prairie YQU": (55.180, -118.885), "Fort McMurray YMM": (56.653, -111.222),
    "Lethbridge YQL": (49.630, -112.800), "Medicine Hat YXH": (50.019, -110.721), "Lloydminster YLL": (53.309, -110.073),
    "Peace River YPE": (56.227, -117.447), "High Level YOJ": (58.621, -117.165),
}

# 2020 Land Cover of Canada palette (rendered WMS colours) -> forest classes
FOREST_RGB = [(0, 60, 0), (91, 116, 42), (19, 139, 4), (147, 155, 111)]
SKIP_RGB = [(0, 0, 0), (75, 111, 162)]  # no data, water
LC_BBOX = (48.0, -140.0, 60.5, -109.0)  # lat0, lon0, lat1, lon1
LC_SIZE = (2480, 1000)


def get(url, data=None, tries=5):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers=UA)
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.read()
        except Exception as e:  # network blips through proxies are common
            if i == tries - 1:
                raise
            time.sleep(2 * (i + 1))


def norm(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def km(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 12742 * math.asin(math.sqrt(h))


def geocode(name, prov):
    q = urllib.parse.quote(name)
    d = json.loads(get("https://geogratis.gc.ca/services/geoname/en/geonames.json?q=%s&province=%s" % (q, PROV[prov])))
    best = None
    for it in d.get("items", []):
        code = it["concise"]["code"]
        rank = {"CITY": 0, "TOWN": 1, "MUN1": 2, "MUN2": 2, "DIST": 3, "VILG": 3, "UNP": 4}.get(code)
        if rank is None:
            continue
        exact = norm(it["name"]) == norm(name)
        key = (0 if exact else 1, rank)
        if best is None or key < best[0]:
            best = (key, it["latitude"], it["longitude"])
    return (best[1], best[2]) if best else (None, None)


def osrm_durations(src, dests):
    """hours from src to each dest (driving), batched for the public OSRM server."""
    out = []
    for i in range(0, len(dests), 80):
        chunk = dests[i:i + 80]
        coords = ";".join("%f,%f" % (lo, la) for la, lo in [src] + chunk)
        d = json.loads(get("https://router.project-osrm.org/table/v1/driving/%s?sources=0&annotations=duration" % coords))
        out += [None if v is None else v / 3600 for v in d["durations"][0][1:]]
        time.sleep(1)
    return out


def forest_share(img, lat, lon, radius_km=60):
    from math import cos, radians
    W, H = LC_SIZE
    lat0, lon0, lat1, lon1 = LC_BBOX
    px_x = (lon1 - lon0) / W  # degrees per pixel
    px_y = (lat1 - lat0) / H
    dlat = radius_km / 111.0
    dlon = radius_km / (111.0 * cos(radians(lat)))
    forest = total = 0
    y = lat - dlat
    while y <= lat + dlat:
        x = lon - dlon
        while x <= lon + dlon:
            if km((lat, lon), (y, x)) <= radius_km:
                cx, cy = int((x - lon0) / px_x), int((lat1 - y) / px_y)
                if 0 <= cx < W and 0 <= cy < H:
                    rgb = img.getpixel((cx, cy))[:3]
                    if rgb not in SKIP_RGB:
                        total += 1
                        if rgb in FOREST_RGB:
                            forest += 1
            x += px_x * 2
        y += px_y * 2
    return round(100 * forest / total, 1) if total else None


def main():
    from PIL import Image
    census = json.load(open(os.path.join(HERE, "cache", "census.json")))["geos"]
    csds = [(g, v) for g, v in census.items() if g.startswith("2021A0005") and "name" in v]

    places = []
    for row in csv.DictReader(open(os.path.join(HERE, "places_7500plus_2021.csv"), encoding="utf-8")):
        name, prov, typ = row["name"], row["province"], row["municipal_type"]
        if typ in ("Municipal district",) or (typ == "Specialized municipality" and name not in URBAN_SPECIALIZED):
            continue  # rural counties, not towns
        pc = PROV[prov]
        tc = TYPE_CODE.get(typ)
        cands = [(g, v) for g, v in csds if g[9:11] == pc and norm(v["name"]) == norm(name.split(",")[0])]
        if len(cands) > 1 and tc:
            cands = [c for c in cands if c[1].get("csd_type") == tc] or cands
        if not cands:
            print("NO CENSUS MATCH", name, prov)
            continue
        g, v = min(cands, key=lambda c: abs((c[1].get("pop") or 0) - int(row["population_2021"])))
        label, lat, lon = name, None, None
        if name in URBAN_SPECIALIZED:
            label, lat, lon = URBAN_SPECIALIZED[name]
        else:
            label = name.replace(" (part)", "")
            lat, lon = geocode(label, prov)
            time.sleep(0.2)
        cd = v["csd_code"][:4]
        places.append({"id": ("bc-" if pc == "59" else "ab-") + norm(label).replace(" ", "-"),
                       "name": label, "province": prov, "type": typ, "dguid": g, "cd": cd,
                       "lat": lat, "lon": lon, "pop2021": int(row["population_2021"]),
                       "pop2016": int(row["population_2016"])})
        print(label, lat, lon, g)

    pts = [(p["lat"], p["lon"]) for p in places]
    van = osrm_durations(VANCOUVER, pts)
    for p, h in zip(places, van):
        p["drive_vancouver_h"] = None if h is None else round(h, 2)

    # nearest airport: pick 3 closest by straight line, then drive time
    for p in places:
        near = sorted(AIRPORTS.items(), key=lambda a: km((p["lat"], p["lon"]), a[1]))[:3]
        if km((p["lat"], p["lon"]), near[0][1]) < 15:
            p["airport"], p["airport_h"] = near[0][0], 0.3
            continue
        hs = osrm_durations((p["lat"], p["lon"]), [a for _, a in near])
        best = min((h, n) for h, (n, _) in zip(hs, near) if h is not None)
        p["airport"], p["airport_h"] = best[1], round(best[0], 2)

    lc = get("https://datacube.services.geo.ca/ows/landcover?service=WMS&version=1.3.0&request=GetMap"
             "&layers=landcover-2020&styles=&crs=EPSG:4326&bbox=%s&width=%d&height=%d&format=image/png"
             % (",".join(map(str, LC_BBOX)), LC_SIZE[0], LC_SIZE[1]))
    img = Image.open(io.BytesIO(lc)).convert("RGB")
    for p in places:
        p["forest_60km_pct"] = forest_share(img, p["lat"], p["lon"])

    json.dump({"places": places}, open(os.path.join(HERE, "cache", "geo.json"), "w"), indent=1)
    print("wrote", len(places), "places")


if __name__ == "__main__":
    main()

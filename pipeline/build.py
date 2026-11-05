"""Twice-daily refresh: builds site/data/places.json.

Static inputs (rebuilt rarely):  pipeline/cache/census.json, pipeline/cache/geo.json
Live inputs (fetched every run):
  * CMHC Rental Market Survey average rents by centre (latest October survey)
  * Statistics Canada Canadian Business Counts by census subdivision (latest release)
If a live source fails, the previous values are kept from the last places.json.
"""
import csv, io, json, math, os, re, sys, time, urllib.request, zipfile, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "site", "data", "places.json")
UA = {"User-Agent": "bc-ab-relocation-atlas/1.0 (github.com/WestCoastRJ/bc-ab-relocation-atlas)"}
TOP_PICKS = {"bc-prince-rupert": "Your own top pick"}

# How hard AI hits each broad occupation group (NOC 2021 broad categories).
# Value = share of that work we treat as hard to automate. Gradual = office work erodes over 10+ years;
# fast = most desk work automated within a few years and robotics starts reaching physical work.
AI_RESIST = {
    #     gradual fast
    "0": (0.55, 0.30),  # management
    "1": (0.20, 0.05),  # business, finance, admin
    "2": (0.40, 0.20),  # natural & applied sciences (incl. software)
    "3": (0.85, 0.75),  # health
    "4": (0.60, 0.40),  # education, law, social, government
    "5": (0.40, 0.20),  # art, culture, recreation
    "6": (0.55, 0.35),  # sales & service
    "7": (0.90, 0.75),  # trades, transport, equipment operators
    "8": (0.90, 0.75),  # natural resources, agriculture
    "9": (0.70, 0.45),  # manufacturing & utilities
}

CRITERIA = [
    # key, label, unit, better, default weight, hint, group
    ("rent", "Rent (2-bed)", "$", "low", 4, "CMHC avg 2-bedroom rent, Oct survey. Your target: under $2,000", "Cost of living"),
    ("home_value", "Home prices in town", "$", "low", 1, "Median single-detached value, 2021 census (owner estimate)", "Cost of living"),
    ("rural_value", "Rural property prices nearby", "$", "low", 3, "Median rural home value in the surrounding regional district / county, 2021 census", "Rural land"),
    ("rural_u300", "Rural homes under $300k", "%", "high", 3, "Share of rural single-detached homes nearby valued under $300k, 2021 census", "Rural land"),
    ("forest", "Forest within 60 km", "%", "high", 3, "Share of land within 60 km that is forest (NRCan land cover 2020)", "Rural land"),
    ("women_ratio", "Women per 100 men (20-49)", "", "high", 4, "2021 census, ages 20 to 49", "Social"),
    ("age_25_44", "People aged 25-44", "%", "high", 2, "Share of residents aged 25 to 44, 2021 census", "Social"),
    ("median_age", "Median age", "yrs", "low", 1, "2021 census", "Social"),
    ("fun", "Restaurants, bars & recreation", "per 1k", "high", 2, "Food, drink, arts & recreation businesses per 1,000 people (StatCan business counts)", "Social"),
    ("ai", "AI-resistant jobs", "/100", "high", 4, "How much of the local workforce does hands-on work AI can't easily replace (2021 census occupations)", "Economy"),
    ("diversity", "Economic diversity", "sectors", "high", 3, "Effective number of industries employing locals (higher = less reliant on one industry)", "Economy"),
    ("backup_jobs", "Well-paying backup jobs", "%", "high", 4, "Share of workers earning $80k+ from employment, 2021 census", "Economy"),
    ("clients", "Commercial client base", "per 1k", "high", 4, "Businesses with employees per 1,000 people (StatCan business counts)", "RJC demand"),
    ("competition", "Competing contractors", "per 1k", "low", 3, "Building + specialty trade contractor businesses per 1,000 people (lower = less competition)", "RJC demand"),
    ("growth", "Population growth 2016-21", "%", "high", 3, "Growth drives construction demand", "RJC demand"),
    ("old_housing", "Older homes (pre-1981)", "%", "high", 2, "Share of homes built before 1981: more repair & reno work", "RJC demand"),
    ("repairs", "Homes needing repairs", "%", "high", 1, "Share of homes needing minor or major repairs, 2021 census", "RJC demand"),
    ("airport", "Airport (only if >8h from Vancouver)", "h", "low", 1, "Drive to nearest airport with scheduled flights; counts as 0 when within 8h of Vancouver", "Travel"),
]

OSRM_CALIBRATION = 0.82  # public OSRM car times run ~20% slower than real highway times


def get(url, data=None, tries=5, timeout=300):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(3 * (i + 1))


def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def km(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 12742 * math.asin(math.sqrt(h))


# ---------------------------------------------------------------- live: CMHC rents
CMHC_EXTRA_CENTRES = {"duncan": (48.778, -123.708), "wood buffalo": (56.7267, -111.381)}


def fetch_cmhc():
    """{normalized centre name: {"bed1":..., "bed2":..., "year":...}} for BC + AB."""
    year = datetime.date.today().year
    for y in (year, year - 1, year - 2):
        out = {}
        for prov in ("59", "48"):
            body = ("TableId=2.1.11.2&GeographyId=%s&GeographyTypeId=2&BreakdownGeographyTypeId=3&DisplayAs=Table"
                    "&exportType=csv&ForTimePeriod.Year=%d" % (prov, y)).encode()
            raw = get("https://www03.cmhc-schl.gc.ca/hmip-pimh/en/TableMapChart/ExportTable", data=body)
            text = raw.decode("latin-1")
            for row in csv.reader(io.StringIO(text)):
                if len(row) < 11 or not row[0] or row[0].startswith((" ", "October")) or row[1] == "Studio":
                    continue

                def val(s):
                    s = s.replace(",", "").strip()
                    return float(s) if re.match(r"^\d+(\.\d+)?$", s) else None
                name = re.sub(r" (CY|T|DM|MD|SM|RDA|C|DIS|CV)$", "", row[0].strip())
                for part in name.split(" - "):
                    out[norm(part)] = {"bed1": val(row[3]), "bed2": val(row[5]), "year": y}
        if len(out) > 30 and any(v["bed2"] for v in out.values()):
            return out
    raise RuntimeError("CMHC rents not available")


# ---------------------------------------------------------------- live: business counts
def latest_business_counts_pid():
    cubes = json.loads(get("https://www150.statcan.gc.ca/t1/wds/rest/getAllCubesListLite"))
    best = None
    for c in cubes:
        t = c["cubeTitleEn"]
        if t.startswith("Canadian Business Counts, with employees, census metropolitan areas and census subdivisions"):
            if best is None or c["cubeEndDate"] > best["cubeEndDate"]:
                best = c
    return str(best["productId"]), best["cubeTitleEn"]


def fetch_business_counts(dguids):
    pid, title = latest_business_counts_pid()
    raw = get("https://www150.statcan.gc.ca/n1/tbl/csv/%s-eng.zip" % pid, timeout=900)
    want = {"1": "total", "23": "construction", "236": "building", "238": "trades", "71": "arts_rec",
            "722": "food_drink"}
    out = {}
    with zipfile.ZipFile(io.BytesIO(raw)) as zf, zf.open(pid + ".csv") as f:
        r = csv.reader(io.TextIOWrapper(f, encoding="utf-8-sig"))
        h = next(r)
        ig, isz, inc, iv, idate = h.index("DGUID"), h.index("Employment size"), \
            h.index("North American Industry Classification System (NAICS)"), h.index("VALUE"), h.index("REF_DATE")
        for row in r:
            if row[ig] not in dguids or row[isz] != "Total, with employees":
                continue
            m = re.search(r"\[([\d-]+)\]$", row[inc])
            if m and m.group(1) in want:
                out.setdefault(row[ig], {})[want[m.group(1)]] = float(row[iv] or 0)
                out[row[ig]]["ref"] = row[idate]
    return out, title


# ---------------------------------------------------------------- build
def main():
    census = json.load(open(os.path.join(HERE, "cache", "census.json")))["geos"]
    geo = json.load(open(os.path.join(HERE, "cache", "geo.json")))["places"]
    prev = {}
    if os.path.exists(OUT):
        try:
            prev = {p["id"]: p for p in json.load(open(OUT))["places"]}
        except Exception:
            prev = {}
    sources, notes = [], []

    try:
        rents = fetch_cmhc()
        ry = max(v["year"] for v in rents.values())
        sources.append("CMHC Rental Market Survey, October %d (average rent, row + apartment)" % ry)
    except Exception as e:
        rents = None
        notes.append("CMHC rents could not be refreshed this run (%s); kept previous values." % e)

    dguids = {p["dguid"] for p in geo}
    try:
        biz, biz_title = fetch_business_counts(dguids)
        sources.append("Statistics Canada, " + biz_title)
    except Exception as e:
        biz = None
        notes.append("Business counts could not be refreshed this run (%s); kept previous values." % e)

    # rural property: rural CSDs (regional district electoral areas, MDs, counties) in the same census division
    rural = {}
    for g, v in census.items():
        if v.get("csd_type") in ("RDA", "MD", "SM") and v.get("sd_median_value") and v.get("sd_households"):
            rural.setdefault(v["csd_code"][:4], []).append(v)

    centre_pts = {}
    for p in geo:
        centre_pts[norm(p["name"].split(" (")[0])] = (p["lat"], p["lon"])
    centre_pts.update(CMHC_EXTRA_CENTRES)

    places = []
    for p in geo:
        c = census.get(p["dguid"], {})
        vals, extra = {}, {}

        # rent: own centre, else nearest surveyed centre within 30 km (CMA / CA core)
        if rents is not None:
            key = norm(p["name"].split(" (")[0])
            hit, src = rents.get(key), p["name"]
            if not hit or not hit.get("bed2"):
                best = None
                for k, v in rents.items():
                    if v.get("bed2") and k in centre_pts:
                        d = km((p["lat"], p["lon"]), centre_pts[k])
                        if d < 30 and (best is None or d < best[0]):
                            best = (d, k, v)
                if best:
                    hit, src = best[2], best[1].title() + " area"
            if hit and hit.get("bed2"):
                vals["rent"] = hit["bed2"]
                extra["rent_1bed"] = hit.get("bed1")
                extra["rent_source"] = src
        elif p["id"] in prev:
            for k in ("rent",):
                if k in prev[p["id"]]["values"]:
                    vals[k] = prev[p["id"]]["values"][k]
            for k in ("rent_1bed", "rent_source"):
                if k in prev[p["id"]].get("extra", {}):
                    extra[k] = prev[p["id"]]["extra"][k]

        vals["home_value"] = c.get("sd_median_value")
        rs = rural.get(p["cd"], [])
        if rs:
            hh = sum(v["sd_households"] for v in rs)
            vals["rural_value"] = round(sum(v["sd_median_value"] * v["sd_households"] for v in rs) / hh, -3)
            vals["rural_u300"] = round(100 * sum(v.get("sd_under_300k", 0) for v in rs) / hh, 1)
            extra["rural_areas"] = len(rs)
        vals["forest"] = p.get("forest_60km_pct")

        if c.get("m2049"):
            vals["women_ratio"] = round(100 * c["w2049"] / c["m2049"], 1)
        if c.get("pop"):
            vals["age_25_44"] = round(100 * c.get("p2544", 0) / c["pop"], 1)
        vals["median_age"] = c.get("median_age")

        occ = c.get("occ") or {}
        tot = sum(occ.values())
        if tot:
            vals_g = sum(occ.get(k, 0) * w[0] for k, w in AI_RESIST.items()) / tot
            vals_f = sum(occ.get(k, 0) * w[1] for k, w in AI_RESIST.items()) / tot
            extra["ai_gradual"] = round(100 * vals_g, 1)
            extra["ai_fast"] = round(100 * vals_f, 1)
            vals["ai"] = extra["ai_gradual"]
            extra["hands_on_pct"] = round(100 * sum(occ.get(k, 0) for k in ("3", "7", "8", "9")) / tot, 1)
            extra["desk_pct"] = round(100 * sum(occ.get(k, 0) for k in ("1", "2")) / tot, 1)
        ind = c.get("ind") or {}
        it = sum(ind.values())
        if it:
            vals["diversity"] = round(1 / sum((v / it) ** 2 for v in ind.values()), 1)
            top = sorted(ind.items(), key=lambda kv: -kv[1])[:3]
            extra["top_industries"] = [[k, round(100 * v / it, 1)] for k, v in top]
        vals["backup_jobs"] = None if c.get("pct_80k_plus") is None else round(c["pct_80k_plus"], 1)
        extra["median_income"] = c.get("median_emp_income")

        b = (biz or {}).get(p["dguid"])
        if b and c.get("pop"):
            k = 1000 / p["pop2021"]
            vals["clients"] = round((b.get("total", 0) - b.get("construction", 0)) * k, 1)
            vals["competition"] = round((b.get("building", 0) + b.get("trades", 0)) * k, 2)
            vals["fun"] = round((b.get("food_drink", 0) + b.get("arts_rec", 0)) * k, 2)
            extra["businesses"] = b.get("total")
            extra["contractors"] = b.get("building", 0) + b.get("trades", 0)
        elif biz is None and p["id"] in prev:
            for k in ("clients", "competition", "fun"):
                if k in prev[p["id"]]["values"]:
                    vals[k] = prev[p["id"]]["values"][k]

        if p.get("pop2016"):
            vals["growth"] = round(100 * (p["pop2021"] - p["pop2016"]) / p["pop2016"], 1)
        if c.get("households"):
            vals["old_housing"] = round(100 * c.get("hh_pre1981", 0) / c["households"], 1)
            vals["repairs"] = round(100 * (c.get("hh_major_repairs", 0) + c.get("hh_minor_repairs", 0)) / c["households"], 1)

        dv = p.get("drive_vancouver_h")
        dv = None if dv is None else round(dv * OSRM_CALIBRATION, 1)
        ah = p.get("airport_h")
        ah = None if ah is None else round(max(0.3, ah * OSRM_CALIBRATION), 1)
        vals["airport"] = None if dv is None else (0 if dv <= 8 else ah)
        extra.update({"drive_vancouver_h": dv, "airport": p.get("airport"), "airport_h": ah})

        region = "Metro Vancouver" if p["cd"] == "5915" else ""
        places.append({
            "id": p["id"], "name": p["name"], "province": p["province"], "type": p["type"], "region": region,
            "lat": p["lat"], "lon": p["lon"], "pop2021": p["pop2021"], "pop2016": p["pop2016"],
            "pick": TOP_PICKS.get(p["id"]),
            "values": {k: v for k, v in vals.items() if v is not None},
            "extra": {k: v for k, v in extra.items() if v is not None},
        })

    sources += [
        "Statistics Canada, 2021 Census of Population (age & gender, occupations & industries, employment income, "
        "dwelling condition & age, owner-estimated dwelling values)",
        "Natural Resources Canada, 2020 Land Cover of Canada (forest share within 60 km)",
        "Natural Resources Canada, Canadian Geographical Names (locations)",
        "OSRM / OpenStreetMap routing (drive times, calibrated)",
    ]
    out = {
        "updated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "criteria": [dict(key=k, label=l, unit=u, better=b, defaultWeight=w, hint=h, group=g)
                     for k, l, u, b, w, h, g in CRITERIA],
        "aiModes": {"gradual": "Gradual: office jobs erode over 10+ years",
                    "fast": "Fast takeoff: most desk work automated within a few years, robots start on physical work"},
        "sources": sources,
        "notes": notes,
        "places": places,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(out, open(OUT, "w"), indent=0, separators=(",", ":"))
    print("wrote", OUT, len(places), "places;", "; ".join(notes) or "all sources refreshed")


if __name__ == "__main__":
    main()

"""Twice-daily refresh: builds site/data/places.json and site/data/status.json.

Static inputs (rebuilt rarely):  pipeline/cache/census.json, pipeline/cache/geo.json, pipeline/cache/extra.json
  (extra.json: rural property assessments, Alberta's rural rent survey, BC census renter costs; see extra_extract.py)
Live inputs (checked every run; the publishers only release new data once or twice a year):
  * CMHC Rental Market Survey: average rent, reliability grade and vacancy rate by centre (October survey)
  * Statistics Canada Canadian Business Counts by census subdivision (June / December releases)

Every live source is validated (row count, coverage of our towns, required fields) before use.
If a source fails or fails validation, each town keeps its previous values with their original
provenance, and status.json marks the run as degraded (the workflow then fails so GitHub emails the owner).

places.json only changes when the data changes. status.json (not committed) carries
last_checked / last_successful_fetch / reference period for each source.
"""
import csv, io, json, math, os, re, statistics, sys, time, urllib.request, zipfile, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "site", "data", "places.json")
STATUS = os.path.join(ROOT, "site", "data", "status.json")
LIVE_STATUS_URL = "https://westcoastrj.github.io/bc-ab-relocation-atlas/data/status.json"
UA = {"User-Agent": "bc-ab-relocation-atlas/1.0 (github.com/WestCoastRJ/bc-ab-relocation-atlas)"}
TOP_PICKS = {"Prince George": "Your own top pick"}

# ------------------------------------------------------------------ AI scenario assumptions
# Share of each NOC 2021 broad occupation group we ASSUME is hard to automate. These are hand-set
# scenario assumptions, not a calibrated forecast: broad groups can't separate, say, truck drivers
# (autonomy-exposed) from maintenance trades. Treat the two modes as a stress test.
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

# key, label, unit, better, default weight (0-5), hint, group.
# Default weights follow the audit's split: RJC 25%, backup work 15%, rent 15%, land 15%, forest 10%,
# resilience/AI 10%, social 10% (20 points in total, 1 point = 5%).
CRITERIA = [
    ("rent", "Rent (2-bed average)", "$", "low", 3,
     "CMHC October survey average of occupied + vacant units; Alberta's own survey for 8 small Alberta towns; "
     "a census-based estimate for 4 BC towns with no survey (marked est.). Not today's asking rents.", "Rent"),
    ("vacancy", "Rental vacancy", "%", "high", 0,
     "Vacancy rate, all unit sizes (CMHC, or Alberta's survey): how easy it is to find a place as a newcomer", "Rent"),
    ("rural_house", "Typical rural house within 1 h (assessed)", "$", "low", 3,
     "2024 median assessed value of single-detached houses in the rural areas around town (StatCan, from BC "
     "Assessment / Alberta municipal rolls), weighted by how many of each area's homes you can reach within an "
     "hour by road (in BC, no ferries), adjusted to the part of each area within the hour. Assessed values trail "
     "sale prices; not listings.", "Land (unverified)"),
    ("rural_land", "Typical vacant rural lot within 1 h (assessed)", "$", "low", 0,
     "Same, for vacant land parcels. Includes tiny, remote and odd parcels, so it can look very cheap.",
     "Land (unverified)"),
    ("forest", "Forest within 60 km", "%", "high", 2,
     "Share of land within 60 km that is forest (NRCan land cover 2020). Not proof of buyable wooded land.", "Forest"),
    ("clients", "Commercial market size", "businesses", "high", 2,
     "Non-construction businesses with employees in the town (StatCan business counts)", "RJC opportunity"),
    ("competition", "Contractors per 100 client businesses", "", "low", 1,
     "Building + specialty-trade businesses with employees per 100 non-construction businesses. "
     "Misses sole operators.", "RJC opportunity"),
    ("growth", "Population growth 2016-21", "%", "high", 1, "Growth drives construction demand", "RJC opportunity"),
    ("old_housing", "Older homes (pre-1981)", "%", "high", 1,
     "Share of homes built before 1981: residential repair & reno work (RJC's secondary market)", "RJC opportunity"),
    ("repairs", "Homes needing repairs", "%", "high", 0,
     "Share of homes needing minor or major repairs, 2021 census (overlaps with older homes)", "RJC opportunity"),
    ("backup_jobs", "Well-paying backup jobs (proxy)", "%", "high", 3,
     "Share of residents with 2020 employment income of $80k+ (2021 census). Past earnings, not current vacancies.",
     "Backup work"),
    ("ai", "AI-resistant jobs (scenario)", "/100", "high", 1,
     "Hand-set scenario based on the local occupation mix; see the AI switch. Not a forecast.", "Resilience"),
    ("diversity", "Economic diversity", "sectors", "high", 1,
     "Effective number of industries employing locals. Can't see shared dependence on one port, mill or employer.",
     "Resilience"),
    ("women_ratio", "Women per 100 men (20-49)", "", "high", 1, "2021 census, all marital statuses", "Social"),
    ("age_25_44", "People aged 25-44", "%", "high", 1, "Share of residents aged 25 to 44, 2021 census", "Social"),
    ("median_age", "Median age", "yrs", "low", 0, "2021 census (overlaps with the 25-44 share)", "Social"),
    ("fun", "Restaurants, bars & recreation", "per 1k", "high", 0,
     "Food, drink, arts & recreation businesses per 1,000 people (overlaps with market size)", "Social"),
    ("home_value", "House prices in town", "$", "low", 0,
     "Median single-detached value, 2021 census owner estimate", "Other"),
]

# ------------------------------------------------------------------ drive-time calibration
# The public OSRM demo server's car profile is consistently slow on BC/AB highways. Checked against
# typical Google Maps times from downtown Vancouver (Nov 2026):
#   Kamloops 3.5 h / OSRM 4.25 = 0.82   Kelowna 3.9 / 4.81 = 0.81   Prince George 8.6 / 10.59 = 0.81
#   Nelson 7.5 / 8.98 = 0.84            Cranbrook 9.4 / 11.31 = 0.83  Calgary 9.7 / 12.52 = 0.77
#   Prince Rupert 15.5 / 20.02 = 0.77   -> median 0.81
OSRM_CALIBRATION = 0.81
FAR_FROM_VANCOUVER_H = 8.0
HEALTH_MIN = {"cmhc_centres": 25, "biz_coverage": 0.95}
LAND_BUDGET = 300000

# Rent cross-checks for the 4 BC towns no rent survey covers (shown on the town card, not scored)
RENT_NOTES = {
    "Revelstoke": "Local survey (Revelstoke Community Housing Society, 2022): 2-bed median $1,800 incl. utilities. "
                  "The few 2-beds on listing sites in 2025-26 asked about $2,500.",
    "Sechelt": "Sunshine Coast Alliance for Affordable Housing, Sept 2023 (quoted in Vital Signs 2024): 2-bed average "
               "$2,234 across the whole coast. Listing sites show only a handful of Sechelt rentals, mostly higher.",
    "Whistler": "Much of Whistler's rentals are price-restricted staff housing (Whistler Housing Authority), which pulls "
                "the census figure down. Open-market 2-beds on listing sites ask roughly $2,300 to $4,400.",
    "Kitimat": "CMHC last published Kitimat rents in 2011 (2-bed average $691). The few 2-bed listings seen (Zumper, 2024) "
               "asked about $1,800 to $2,500.",
}
# Why each of those towns has no surveyed rent (CMHC Rental Market Survey tables, checked for 2019-2025)
RENT_WHY = {
    "Revelstoke": "No rent survey covers Revelstoke: CMHC doesn't list it.",
    "Kitimat": "No rent survey covers Kitimat: CMHC hasn't listed it since 2011.",
    "Sechelt": "CMHC surveys Sechelt but suppresses its figures every year (too few rental units, or unreliable).",
    "Whistler": "CMHC lists Whistler but suppresses its figures (too few rental units, or unreliable).",
}
# 2021 census metropolitan areas in BC: their CMHC figure covers several municipalities, so they don't
# calibrate the small-town rent estimate
BC_CMA_CORES = {"vancouver", "victoria", "kelowna", "abbotsford", "mission", "nanaimo", "kamloops", "chilliwack"}


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


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


# ------------------------------------------------------------------ live: CMHC
# Towns inside a CMHC census metropolitan area use that CMA's figures (nearest-centre matching
# would put e.g. Langley in Abbotsford).
CMA_MEMBERS = {
    "vancouver": "CD:5915", "victoria": "CD:5917",
    "edmonton": ["St. Albert", "Sherwood Park (Strathcona County)", "Spruce Grove", "Stony Plain",
                 "Fort Saskatchewan", "Leduc", "Beaumont", "Morinville"],
    "calgary": ["Airdrie", "Chestermere", "Cochrane"],
    "kelowna": ["West Kelowna", "Lake Country"],
}
CMHC_EXTRA_CENTRES = {"duncan": (48.778, -123.708), "wood buffalo": (56.7267, -111.381)}
GRADE = {"a": "excellent", "b": "very good", "c": "good", "d": "poor - use with caution"}


def cmhc_table(table_id, prov, year):
    body = ("TableId=%s&GeographyId=%s&GeographyTypeId=2&BreakdownGeographyTypeId=3&DisplayAs=Table"
            "&exportType=csv&ForTimePeriod.Year=%d" % (table_id, prov, year)).encode()
    text = get("https://www03.cmhc-schl.gc.ca/hmip-pimh/en/TableMapChart/ExportTable", data=body).decode("latin-1")
    out = {}
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 11 or not row[0].strip() or row[0].startswith(("October", " ")) or row[1] == "Studio":
            continue

        def cell(i):
            s = row[i].replace(",", "").strip()
            v = float(s) if re.match(r"^\d+(\.\d+)?$", s) else None
            return v, row[i + 1].strip() or None
        name = re.sub(r" (CY|T|DM|MD|SM|RDA|C|DIS|CV)$", "", row[0].strip())
        for part in name.split(" - "):
            out[norm(part)] = {"bed1": cell(3), "bed2": cell(5), "total": cell(9)}
    return out


def fetch_cmhc():
    year = datetime.date.today().year
    for y in (year, year - 1, year - 2):
        rents, vac = {}, {}
        for prov in ("59", "48"):
            rents.update(cmhc_table("2.1.11.2", prov, y))
            vac.update(cmhc_table("2.1.1.2", prov, y))
        good = [k for k, v in rents.items() if v["bed2"][0]]
        if len(good) >= HEALTH_MIN["cmhc_centres"]:
            out = {}
            for k, v in rents.items():
                out[k] = {"bed1": v["bed1"][0], "bed2": v["bed2"][0], "grade": v["bed2"][1],
                          "vacancy": vac.get(k, {}).get("total", (None, None))[0]}
            return out, "October %d" % y
    raise RuntimeError("CMHC returned fewer than %d centres with 2-bed rents" % HEALTH_MIN["cmhc_centres"])


# ------------------------------------------------------------------ live: business counts
def latest_business_counts_pid():
    cubes = json.loads(get("https://www150.statcan.gc.ca/t1/wds/rest/getAllCubesListLite"))
    best = None
    for c in cubes:
        if c["cubeTitleEn"].startswith("Canadian Business Counts, with employees, census metropolitan areas and census subdivisions"):
            if best is None or c["cubeEndDate"] > best["cubeEndDate"]:
                best = c
    if not best:
        raise RuntimeError("business counts table not found in StatCan cube list")
    return str(best["productId"]), best["cubeTitleEn"]


def fetch_business_counts(dguids):
    pid, title = latest_business_counts_pid()
    raw = get("https://www150.statcan.gc.ca/n1/tbl/csv/%s-eng.zip" % pid, timeout=900)
    want = {"1": "total", "23": "construction", "236": "building", "238": "trades", "71": "arts_rec", "722": "food_drink"}
    out = {}
    with zipfile.ZipFile(io.BytesIO(raw)) as zf, zf.open(pid + ".csv") as f:
        r = csv.reader(io.TextIOWrapper(f, encoding="utf-8-sig"))
        h = next(r)
        ig, isz, inc, iv = h.index("DGUID"), h.index("Employment size"), \
            h.index("North American Industry Classification System (NAICS)"), h.index("VALUE")
        for row in r:
            if row[ig] not in dguids or row[isz] != "Total, with employees":
                continue
            m = re.search(r"\[([\d-]+)\]$", row[inc])
            if m and m.group(1) in want and row[iv] != "":
                out.setdefault(row[ig], {})[want[m.group(1)]] = float(row[iv])
    complete = [g for g, v in out.items() if "total" in v and "building" in v and "trades" in v]
    cov = len(complete) / max(1, len(dguids))
    if cov < HEALTH_MIN["biz_coverage"]:
        raise RuntimeError("business counts cover only %d%% of towns" % round(100 * cov))
    period = re.sub(r"^.*,\s*", "", title)  # e.g. "June 2026"
    return out, period


# ------------------------------------------------------------------ build
def load_prev():
    places, meta = {}, {}
    if os.path.exists(OUT):
        try:
            d = json.load(open(OUT))
            places = {p["id"]: p for p in d["places"]}
            meta = d.get("sourceMeta", {})
        except Exception:
            pass
    status = {}
    try:
        status = json.loads(get(LIVE_STATUS_URL, tries=2, timeout=30))
    except Exception:
        pass
    return places, meta, status


def main():
    census = json.load(open(os.path.join(HERE, "cache", "census.json")))["geos"]
    geo = json.load(open(os.path.join(HERE, "cache", "geo.json")))["places"]
    prev, prev_meta, prev_status = load_prev()
    checked = now()
    status = {"last_checked": checked, "sources": {}, "degraded": False, "problems": []}

    def src_status(key, ok, period=None, error=None):
        old = (prev_status.get("sources") or {}).get(key, {})
        s = {"last_checked": checked, "ok": ok,
             "last_successful_fetch": checked if ok else old.get("last_successful_fetch"),
             "reference_period": period or (prev_meta.get(key) or {}).get("reference_period")}
        if error:
            s["error"] = str(error)[:300]
            status["degraded"] = True
            status["problems"].append("%s: %s" % (key, s["error"]))
        status["sources"][key] = s

    try:
        rents, rent_period = fetch_cmhc()
        src_status("cmhc", True, rent_period)
    except Exception as e:
        rents, rent_period = None, None
        src_status("cmhc", False, error=e)

    dguids = {p["dguid"] for p in geo}
    try:
        biz, biz_period = fetch_business_counts(dguids)
        src_status("business_counts", True, biz_period)
    except Exception as e:
        biz, biz_period = None, None
        src_status("business_counts", False, error=e)

    # committed with the repo; without it the build would silently drop rural values and survey rents,
    # so stop instead (the live site keeps its last good data)
    xtra_doc = json.load(open(os.path.join(HERE, "cache", "extra.json")))
    xtra = xtra_doc["towns"]

    centre_pts = {norm(p["name"].split(" (")[0]): (p["lat"], p["lon"]) for p in geo}
    centre_pts.update(CMHC_EXTRA_CENTRES)

    places = []
    pending_estimates = []  # BC towns with no rent survey: estimated from the 2021 census after the loop
    for p in geo:
        pid = "csd-" + p["dguid"][-7:]
        old = prev.get(pid, {})
        c = census.get(p["dguid"], {})
        vals, extra, prov = {}, {}, {}

        def keep_old(keys, src):
            for k in keys:
                if k in old.get("values", {}):
                    vals[k] = old["values"][k]
                    prov[k] = old.get("prov", {}).get(k, src + " (previous run)")
                if k in old.get("extra", {}):
                    extra[k] = old["extra"][k]

        # --- rent: the town's own survey first (its own CMHC centre, else Alberta's survey for that town),
        # then its CMA or a CMHC centre within 30 km, then (BC) a census-based estimate after the loop.
        # Only real CMHC values are ever carried over from the previous run; the others are rebuilt from
        # extra.json every time, so their flags and updates can't get lost.
        rent_keys = ("rent", "vacancy")
        cmhc_extras = ("rent_1bed", "rent_source", "rent_grade")
        x = xtra.get(p["dguid"], {})
        ab = x.get("rent_ab")
        old_is_cmhc = str(old.get("prov", {}).get("rent", "")).startswith("CMHC")
        if rents is not None:
            key = norm(p["name"].split(" (")[0])
            hit, src = rents.get(key), p["name"]
            for cma, members in CMA_MEMBERS.items():
                inside = members == "CD:" + p["cd"] if isinstance(members, str) else p["name"] in members
                if inside and rents.get(cma, {}).get("bed2") and key != cma:
                    hit, src = rents[cma], cma.title() + " CMA"
            if not hit or not hit.get("bed2"):
                best = None
                for k, v in rents.items():
                    if v.get("bed2") and k in centre_pts:
                        d = km((p["lat"], p["lon"]), centre_pts[k])
                        if d < 30 and (best is None or d < best[0]):
                            best = (d, k, v)
                if best:
                    hit, src = best[2], best[1].title() + " area"
            if hit and hit.get("bed2") and src != p["name"] and ab:
                extra["rent_neighbour"] = [src, hit["bed2"]]  # shown on the card; the town's own survey wins
                hit = None
            if hit and hit.get("bed2"):
                vals["rent"] = hit["bed2"]
                prov["rent"] = "CMHC " + rent_period
                extra["rent_1bed"] = hit.get("bed1")
                extra["rent_source"] = src
                extra["rent_grade"] = GRADE.get((hit.get("grade") or "").lower())
                if hit.get("vacancy") is not None:
                    vals["vacancy"] = hit["vacancy"]
                    prov["vacancy"] = "CMHC " + rent_period
            elif old_is_cmhc and not ab:
                keep_old(rent_keys + cmhc_extras, "CMHC")  # centre missing from this response: keep last known value
                extra["rent_stale"] = True
        elif old_is_cmhc:
            keep_old(rent_keys + cmhc_extras, "CMHC")  # CMHC unreachable this run: keep last known value
            extra["rent_kept"] = True
        elif ab:
            keep_old(("rent_neighbour",), "CMHC")

        # --- Alberta's own rent survey for towns CMHC doesn't publish
        if "rent" not in vals and ab:
            vals["rent"] = ab["bed2"]
            prov["rent"] = "Government of Alberta rental survey, " + ab["period"]
            extra["rent_1bed"] = ab.get("bed1")
            extra["rent_small_sample"] = ab.get("small_sample")
            vals.pop("vacancy", None)
            if ab.get("vacancy") is not None:
                vals["vacancy"] = ab["vacancy"]
                prov["vacancy"] = "Government of Alberta rental survey, " + ab["period"]
        if "rent" not in vals and x.get("census_rent_2021"):
            pending_estimates.append((p["dguid"], x["census_rent_2021"]))
        if p["name"] in RENT_NOTES:
            extra["rent_note"] = RENT_NOTES[p["name"]]

        # --- rural property values within an hour's drive (assessed, unverified as land for sale)
        r = x.get("rural")
        if r:
            for k, ptype in (("rural_house", "house"), ("rural_land", "land")):
                if r.get(ptype) is not None:
                    vals[k] = r[ptype]
                    prov[k] = "StatCan CHSP %s assessments, rural areas within 1 h" % r["year"]
                    if ptype == "house" and r.get("house_est_pct"):
                        prov[k] += "; %d%% ESTIMATED from 2021 census values (no StatCan figure for %s)" % (
                            r["house_est_pct"], ", ".join(r.get("house_est_areas", [])))
            extra["rural"] = {k: v for k, v in r.items() if v not in (None, [], "")}
        vals["home_value"] = c.get("sd_median_value")
        vals["forest"] = p.get("forest_60km_pct")

        # --- social
        if c.get("m2049"):
            vals["women_ratio"] = round(100 * c["w2049"] / c["m2049"], 1)
        if c.get("pop"):
            vals["age_25_44"] = round(100 * c.get("p2544", 0) / c["pop"], 1)
            extra["people_25_44"] = c.get("p2544")
        vals["median_age"] = c.get("median_age")

        # --- economy
        occ = c.get("occ") or {}
        tot = sum(occ.values())
        if tot:
            extra["ai_gradual"] = round(100 * sum(occ.get(k, 0) * w[0] for k, w in AI_RESIST.items()) / tot, 1)
            extra["ai_fast"] = round(100 * sum(occ.get(k, 0) * w[1] for k, w in AI_RESIST.items()) / tot, 1)
            vals["ai"] = extra["ai_gradual"]
            extra["hands_on_pct"] = round(100 * sum(occ.get(k, 0) for k in ("3", "7", "8", "9")) / tot, 1)
            extra["desk_pct"] = round(100 * sum(occ.get(k, 0) for k in ("1", "2")) / tot, 1)
        ind = c.get("ind") or {}
        it = sum(ind.values())
        if it:
            vals["diversity"] = round(1 / sum((v / it) ** 2 for v in ind.values()), 1)
            extra["top_industries"] = [[k, round(100 * v / it, 1)] for k, v in sorted(ind.items(), key=lambda kv: -kv[1])[:3]]
        if c.get("pct_80k_plus") is not None:
            vals["backup_jobs"] = round(c["pct_80k_plus"], 1)
        extra["median_income_2020"] = c.get("median_emp_income")

        # --- business counts
        biz_keys = ("clients", "competition", "fun")
        b = (biz or {}).get(p["dguid"])
        if b and "total" in b:
            clients = b["total"] - b.get("construction", 0)
            contractors = b.get("building", 0) + b.get("trades", 0)
            vals["clients"] = clients
            vals["competition"] = round(100 * contractors / clients, 1) if clients else None
            vals["fun"] = round((b.get("food_drink", 0) + b.get("arts_rec", 0)) * 1000 / p["pop2021"], 2)
            for k in biz_keys:
                prov[k] = "StatCan business counts " + biz_period
            extra["businesses"] = b["total"]
            extra["contractors"] = contractors
        else:
            keep_old(biz_keys + ("businesses", "contractors"), "StatCan business counts")

        # --- housing demand
        if p.get("pop2016"):
            vals["growth"] = round(100 * (p["pop2021"] - p["pop2016"]) / p["pop2016"], 1)
        if c.get("households"):
            vals["old_housing"] = round(100 * c.get("hh_pre1981", 0) / c["households"], 1)
            vals["repairs"] = round(100 * (c.get("hh_major_repairs", 0) + c.get("hh_minor_repairs", 0)) / c["households"], 1)

        # --- travel (feasibility only, not a weighted preference)
        dv = p.get("drive_vancouver_h")
        dv = None if dv is None else round(dv * OSRM_CALIBRATION, 1)
        ah = p.get("airport_h")
        if ah is not None and not p.get("airport_note"):
            ah = round(ah * OSRM_CALIBRATION, 1)
        extra.update({"drive_vancouver_h": dv, "airport": p.get("airport"), "airport_h": ah,
                      "airport_note": p.get("airport_note"), "far_from_vancouver": bool(dv and dv > FAR_FROM_VANCOUVER_H)})

        places.append({
            "id": pid, "dguid": p["dguid"], "name": p["name"], "province": p["province"], "type": p["type"],
            "region": "Metro Vancouver" if p["cd"] == "5915" else "",
            "lat": p["lat"], "lon": p["lon"], "pop2021": p["pop2021"], "pop2016": p["pop2016"],
            "pick": TOP_PICKS.get(p["name"]),
            "values": {k: v for k, v in vals.items() if v is not None},
            "extra": {k: v for k, v in extra.items() if v is not None},
            "prov": prov,
        })

    # BC towns with no rent survey: 2021 census median renter shelter cost x the median ratio of
    # (CMHC 2-bed average / same census figure) in BC towns that have their OWN CMHC centre. Leave out
    # census metropolitan areas (their CMHC figure covers many municipalities) and identical values
    # (one centre shared by two towns).
    by_dguid = {q["dguid"]: q for q in places}
    calib = [q for q in places
             if q["province"] == "British Columbia" and q["values"].get("rent")
             and q["extra"].get("rent_source") == q["name"] and q["prov"].get("rent", "").startswith("CMHC")
             and norm(q["name"]) not in BC_CMA_CORES and xtra.get(q["dguid"], {}).get("census_rent_2021")]
    shared = {v for v in [q["values"]["rent"] for q in calib] if [q["values"]["rent"] for q in calib].count(v) > 1}
    ratios = sorted(q["values"]["rent"] / xtra[q["dguid"]]["census_rent_2021"] for q in calib if q["values"]["rent"] not in shared)
    rent_ratio = statistics.median(ratios) if ratios else None
    q1, q3 = (statistics.quantiles(ratios, n=4)[0], statistics.quantiles(ratios, n=4)[2]) if len(ratios) >= 4 else (None, None)
    for g, census_rent in pending_estimates:
        q = by_dguid[g]
        if rent_ratio:
            q["values"]["rent"] = int(round(census_rent * rent_ratio, -1))
            q["prov"]["rent"] = "ESTIMATE: 2021 census renter cost x %.2f (median CMHC/census ratio in %d BC towns)" % (rent_ratio, len(ratios))
            q["extra"].update({"rent_estimate": True, "rent_census_2021": int(census_rent),
                               "rent_source": "estimate from the 2021 census",
                               "rent_why": RENT_WHY.get(q["name"], "No rent survey covers this town.")})
            if q1:  # middle half and full spread of the comparison towns' ratios
                q["extra"]["rent_range"] = [int(round(census_rent * q1, -1)), int(round(census_rent * q3, -1))]
                q["extra"]["rent_range_wide"] = [int(round(census_rent * ratios[0], -1)), int(round(census_rent * ratios[-1], -1))]
    for q in places:  # an estimate must always travel with its flag
        assert not q["prov"].get("rent", "").startswith("ESTIMATE") or q["extra"].get("rent_estimate"), q["name"]

    # every value says where it came from
    default_prov = {"forest": "NRCan land cover 2020", "growth": "Census 2016-2021", "home_value": "Census 2021 (owner estimates)",
                    "backup_jobs": "Census 2021 (2020 incomes)", "ai": "Hand-set scenario on 2021 census occupations",
                    "diversity": "Census 2021 (industries)"}
    for q in places:
        for k in q["values"]:
            q["prov"].setdefault(k, default_prov.get(k, "Census 2021"))

    # names that appear twice (Langley City + Township, North Vancouver City + District) get their type
    counts = {}
    for p in places:
        counts[p["name"]] = counts.get(p["name"], 0) + 1
    for p in places:
        if counts[p["name"]] > 1:
            p["name"] = "%s (%s)" % (p["name"], "District" if p["type"] == "District municipality" else p["type"])

    source_meta = {
        "cmhc": {"label": "CMHC Rental Market Survey (average rent, reliability grade, vacancy)",
                 "reference_period": rent_period or (prev_meta.get("cmhc") or {}).get("reference_period")},
        "business_counts": {"label": "Statistics Canada, Canadian Business Counts with employees, by municipality",
                            "reference_period": biz_period or (prev_meta.get("business_counts") or {}).get("reference_period")},
        "census": {"label": "Statistics Canada, 2021 Census of Population", "reference_period": "May 2021 (income: calendar 2020)"},
        "landcover": {"label": "Natural Resources Canada, 2020 Land Cover of Canada", "reference_period": "2020"},
        "chsp": {"label": "Statistics Canada, Canadian Housing Statistics Program: median assessed value of single-detached "
                          "houses and vacant land by rural area (table 46-10-0093, from BC Assessment and Alberta municipal rolls)",
                 "reference_period": "%s assessment rolls; rural areas reachable within %d min by road (OSRM; in BC, no ferries); "
                                     "partly-reachable areas adjusted with 2021 census home values"
                                     % (max([(v.get("rural") or {}).get("year", "") for v in xtra.values()] + [""]), xtra_doc.get("rural_minutes", 60))},
        "ab_rent": {"label": "Government of Alberta, Apartment Vacancy and Rental Cost Survey (towns CMHC doesn't cover)",
                    "reference_period": next((v["rent_ab"]["period"] for v in xtra.values() if v.get("rent_ab")), "")},
        "bc_rent_estimate": {"label": "BC towns with no rent survey: 2021 census median renter shelter cost (BC Stats) x CMHC/census ratio",
                             "reference_period": "census May 2021; ratio from the latest CMHC survey" + (" (x%.2f)" % rent_ratio if rent_ratio else "")},
        "routing": {"label": "OSRM / OpenStreetMap routing, calibrated x%.2f against typical Google Maps times" % OSRM_CALIBRATION,
                    "reference_period": "built Nov 2026"},
    }
    out = {
        "criteria": [dict(key=k, label=l, unit=u, better=b, defaultWeight=w, hint=h, group=g)
                     for k, l, u, b, w, h, g in CRITERIA],
        "budgets": {"rent": 2000, "airportHours": 1.0, "farFromVancouverHours": FAR_FROM_VANCOUVER_H, "land": LAND_BUDGET},
        "sourceMeta": source_meta,
        "places": places,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    new_text = json.dumps(out, indent=0, separators=(",", ":"), sort_keys=True)
    old_text = open(OUT).read() if os.path.exists(OUT) else ""
    if new_text != old_text:
        open(OUT, "w").write(new_text)
        status["data_changed"] = checked
    else:
        status["data_changed"] = prev_status.get("data_changed")
    json.dump(status, open(STATUS, "w"), indent=1)
    print("places.json", "updated" if new_text != old_text else "unchanged", "|", len(places), "towns |",
          "DEGRADED: " + "; ".join(status["problems"]) if status["degraded"] else "all live sources OK")


if __name__ == "__main__":
    main()

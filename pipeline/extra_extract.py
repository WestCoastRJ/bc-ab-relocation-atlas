"""One-off: fills data gaps the core census tables leave -> pipeline/cache/extra.json

  * Rural property values near each town: Statistics Canada, Canadian Housing Statistics Program (CHSP),
    "Residential properties by characteristics, property use and ownership type" (table 46-10-0093),
    latest reference year. It publishes the median ASSESSED value (BC Assessment / Alberta municipal
    assessment) by property type for every census subdivision, including the small regional-district
    electoral areas and counties that the 2021 census value table (5,000+ people only) leaves out.
    We use two property types: "Single-detached house" (a house on a rural lot or acreage) and
    "Vacant land".
    "Within an hour" is measured to where people actually live: every 2021 census dissemination area
    (DA, a few hundred people each) inside each rural area, weighted by its dwellings occupied by usual
    residents (table 98-10-0015; so ski-resort condos and cottages don't count as where people live). Each DA is located by StatCan's dissemination-block representative points (which
    sit on the road network): in BC its 5 blocks with the most such dwellings (2021 Geographic Attribute File,
    BC Data Catalogue copy), each standing for its share of the DA's homes; in Alberta, where block counts
    can't be downloaded by a script, its 3 smallest blocks (the most built-up spots), equally weighted.
    Points are timed by road from the town centre with the OSRM public router (same 0.81 calibration as
    the Vancouver drive times). In BC, places whose fastest route uses a ferry don't count: each area's 3
    biggest reachable DAs are route-checked, and if any of them needs a ferry, every reachable point in
    that area is. An area's "share within the hour" is the share of its dwellings you can reach within
    RURAL_MINUTES. Because the StatCan median covers the whole area, it is scaled by how the 2021 census
    home values (census profile API, DA level) of the part within the hour compare with the whole area.
    Areas with no StatCan figure are estimated from those census values (flagged as estimates). The town's
    figure is the median of all those properties pooled: each area counts with (number of properties x
    share within the hour), so a big county mostly beyond the hour barely counts, and each area's values
    are modelled as lognormal around its median, with the typical spread implied by StatCan's medians and
    averages across all rural areas.
  * Rent for towns CMHC doesn't publish:
      - Alberta: Government of Alberta Apartment Vacancy and Rental Cost Survey (rural communities),
        latest year: average 1- and 2-bed rent and overall vacancy.
      - BC: 2021 census median monthly shelter cost of renters (BC Stats copy of the census profile).
        build.py turns it into a 2-bed estimate using towns that have both numbers.
Run after census_extract.py and geo_extract.py:  python pipeline/extra_extract.py [workdir]
Needs `unzip`, `grep` (table 46-10-0093 is a 15 GB CSV; grep filters it in about a minute) and openpyxl.
Drive times are cached in the work dir (about an hour from scratch, at the OSRM demo server's 1 request/second).
"""
import openpyxl  # Alberta's rent survey is an .xlsx (pip install openpyxl)
import csv, hashlib, io, json, math, os, re, statistics, subprocess, sys, time, urllib.error, urllib.parse, urllib.request, zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, ".extra_dl")
UA = {"User-Agent": "bc-ab-relocation-atlas/1.0 (github.com/WestCoastRJ/bc-ab-relocation-atlas)"}
RURAL_MINUTES = 60      # Ryan's "20 to 60 minute drive"
PREFILTER_KM = 120      # straight-line pre-filter (DA point) before asking the router
MIN_SHARE = 0.10        # an area counts when at least 10% of its homes are within the hour
DA_SNAP_M = 5000        # DA points in big rural DAs can sit a few km from the nearest road
# BC islands with only a few roads of their own and no bridge (Gambier, Keats, West Bamfield): the router can't
# route on such a small network, so the drive-time table snaps their points to a road across the water and times them
# from there. A point is dropped when (1) its own road is much closer than the road it was timed from (OSRM nearest,
# any network, vs the table's snap) and (2) the straight line to that road crosses water (off StatCan's shoreline-
# clipped cartographic DA polygons). Inland, (1) can happen on an unconnected private road, but (2) doesn't.
ISLAND_CHECK_M = 200    # only points the table snapped further than this are checked
ISLAND_RATIO, ISLAND_GAP_M, ISLAND_WATER_M = 0.5, 300, 100
CBF_DA_LAYER = "https://geo.statcan.gc.ca/geo_wa/rest/services/2021/Cartographic_boundary_files/MapServer/12/query"
MAINLAND = (49.2827, -123.1207)  # any point on the main road network: makes the table snap to that network
FERRY_CHECKS = 3        # per area: route-check its 3 biggest reachable DAs for a ferry (all points if one needs it)
DRIVE_VERSION = "db-v5"  # change whenever the way points are chosen or timed changes
DB_LAYER = "https://geo.statcan.gc.ca/geo_wa/rest/services/2021/Digital_boundary_files/MapServer/13/query"
BC_BLOCKS_PER_DA = 5     # BC: the DA's blocks with the most dwellings
AB_BLOCKS_PER_DA = 3     # AB: no block counts available, so the smallest (most built-up) blocks
BIG_BLOCK_KM2 = 100      # a block this big says little about where its homes are (its point is just somewhere on a road in it)
GAF_BC = ("https://catalogue.data.gov.bc.ca/dataset/4ed0c389-43ed-4b00-b1be-7c259f47b6a0/resource/"
          "6c8eeb86-bb56-48e8-b734-0d25d6a51a7b/download/2021_92-151_x_bc.csv")
SDMX_DA = "https://api.statcan.gc.ca/census-recensement/profile/sdmx/rest/data/STC_CP,DF_DA/A5.%s.1.1474.1"  # median value of dwellings
ADJUST_BELOW_SHARE, ADJUST_MIN, ADJUST_MAX = 0.85, 0.67, 1.5  # census adjustment of partly-reachable areas
WHOLE_MIN_SHARE = 0.5  # an area figure that couldn't be adjusted is used only if at least half the area is within the hour
SLOW_POINT_MIN = 15    # BC: a point this much slower than its DA's fastest one is route-checked for a ferry (Thetis)
FEW_PROPERTIES = 100   # a town figure resting on fewer property-equivalents than this gets a "treat as rough" note
NEAR_MISS_MINUTES = 75   # an area with 10%+ of its homes 61-75 min from the town centre: too close to call -> missing, not worst
OSRM_CALIBRATION = 0.81  # same factor build.py documents for Vancouver drive times
# Alberta improvement districts are national/provincial parks (Banff, Jasper, Kananaskis...): leases, not rural land
RURAL_TYPES = {"Regional district electoral area", "Municipal district", "Specialized municipality", "Special area"}
RURAL_CODES = {"RDA", "MD", "SM", "SA"}
# Electoral areas / specialized municipalities that are really urban: UBC lands, and the two Alberta
# specialized municipalities the atlas already shows as towns (Sherwood Park, Fort McMurray)
NOT_RURAL = {"Metro Vancouver A", "Strathcona County", "Wood Buffalo", "Jasper"}  # Jasper: the town, in a national park
STRATHCONA_ACREAGES = (53.56, -113.12)  # roughly Ardrossan / Josephburg / Cooking Lake, for the note above
MIN_PROPERTIES = 50  # per property type per area; smaller areas give noisy medians
CHSP_PID = "46100093"
PTYPES = {"Single-detached house": "house", "Vacant land": "land"}
BC_PROFILE = ("https://catalogue.data.gov.bc.ca/dataset/df87e3f8-fcbb-471e-a07c-af0bdd4a592c/resource/"
              "3561b881-c216-48ad-a447-5ea67ae45d55/download/98-401-x2021005_english_bc_csd.zip")
AB_AVRC = ("https://open.alberta.ca/dataset/fb9452c6-395e-45a8-8149-645956fd83ac/resource/"
           "8e11688b-2540-47a4-be28-d31863cb5c43/download/apartment-vacancy-and-rental-cost-survey-avs-data.xlsx")
CSD_LAYER = "https://geo.statcan.gc.ca/geo_wa/rest/services/2021/Digital_boundary_files/MapServer/9/query"
DA_LAYER = "https://geo.statcan.gc.ca/geo_wa/rest/services/2021/Digital_boundary_files/MapServer/12/query"
DA_COUNTS_PID = "98100015"


def get(url, tries=6, timeout=900, headers=None, ok404=False):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=dict(UA, **(headers or {}))), timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if ok404 and e.code == 404:
                return b""
            if i == tries - 1:
                raise
            time.sleep(3 * (i + 1))
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(3 * (i + 1))


def save_json(path, obj, **kw):
    """write via a temp file, so an interrupted run can't leave half a JSON file behind"""
    with open(path + ".tmp", "w") as f:
        json.dump(obj, f, **kw)
    os.replace(path + ".tmp", path)


def cached(name, url):
    os.makedirs(WORK, exist_ok=True)
    p = os.path.join(WORK, name)
    if not os.path.exists(p):
        open(p, "wb").write(get(url))
    return p


def km(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 12742 * math.asin(math.sqrt(h))


def point_in_rings(pt, rings):
    y, x = pt
    inside = False
    for ring in rings:
        for i in range(len(ring) - 1):
            x1, y1 = ring[i]
            x2, y2 = ring[i + 1]
            if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
                inside = not inside
    return inside


def chsp_bcab():
    """BC + AB census-subdivision rows of the 15 GB CHSP table: totals for period/use/ownership."""
    p = os.path.join(WORK, CHSP_PID + "_bcab_v2.csv")  # v2: with averages
    if not os.path.exists(p):
        z = cached(CHSP_PID + ".zip", "https://www150.statcan.gc.ca/n1/tbl/csv/%s-eng.zip" % CHSP_PID)
        pat = ('"2021A0005(59|48)[0-9]{5}","(Single-detached house|Vacant land)","Total, all periods of construction",'
               '"Total, all property use categories","Total, all ownership types","(Number of properties|Median assessment value|Average assessment value)"')
        with open(p + ".tmp", "wb") as f:
            unz = subprocess.Popen(["unzip", "-p", z, CHSP_PID + ".csv"], stdout=subprocess.PIPE)
            subprocess.run(["grep", "-E", pat], stdin=unz.stdout, stdout=f, env=dict(os.environ, LC_ALL="C"), check=True)
            unz.wait()
        os.replace(p + ".tmp", p)
    return p


def csd_shapes():
    p = os.path.join(WORK, "csd_bcab.json")
    if not os.path.exists(p):
        feats, off = [], 0
        while True:
            q = urllib.parse.urlencode({"where": "PRUID IN ('48','59')", "outFields": "DGUID,CSDNAME,CSDTYPE",
                                        "returnGeometry": "true", "outSR": "4326", "maxAllowableOffset": "0.01",
                                        "resultOffset": off, "resultRecordCount": 100, "orderByFields": "OBJECTID", "f": "json"})
            fs = json.loads(get(CSD_LAYER + "?" + q, timeout=180)).get("features", [])
            feats += fs
            off += len(fs)
            if len(fs) < 100:
                break
        json.dump(feats, open(p, "w"))
    return {f["attributes"]["DGUID"]: f["geometry"]["rings"] for f in json.load(open(p))}


def interior_point(rings):
    """a point inside the largest ring: its centroid if inside, else the middle of the widest
    east-west chord through the centroid's latitude"""
    ring = max(rings, key=len)
    a = cx = cy = 0.0
    for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
        c = x1 * y2 - x2 * y1
        a += c
        cx += (x1 + x2) * c
        cy += (y1 + y2) * c
    if abs(a) < 1e-12:
        x, y = ring[0]
        return (y, x)
    cx, cy = cx / (3 * a), cy / (3 * a)
    if point_in_rings((cy, cx), [ring]):
        return (cy, cx)
    xs = sorted((x1 + (cy - y1) * (x2 - x1) / (y2 - y1)) for (x1, y1), (x2, y2) in zip(ring, ring[1:])
                if (y1 > cy) != (y2 > cy))
    if len(xs) < 2:
        x, y = ring[0]
        return (y, x)
    best = max(zip(xs[0::2], xs[1::2]), key=lambda iv: iv[1] - iv[0])
    return (cy, (best[0] + best[1]) / 2)


def da_points():
    """{DA DGUID: (lat, lon)} for every BC + AB dissemination area (StatCan 2021 boundary file)"""
    p = os.path.join(WORK, "da_points_bcab.json")
    if not os.path.exists(p):
        pts, off = {}, 0
        while True:
            q = urllib.parse.urlencode({"where": "PRUID IN ('48','59')", "outFields": "DGUID", "returnGeometry": "true",
                                        "outSR": "4326", "maxAllowableOffset": "0.001", "geometryPrecision": "5",
                                        "resultOffset": off, "resultRecordCount": 1000, "orderByFields": "OBJECTID", "f": "json"})
            fs = json.loads(get(DA_LAYER + "?" + q, timeout=300)).get("features", [])
            for f in fs:
                if f.get("geometry", {}).get("rings"):
                    pts[f["attributes"]["DGUID"]] = interior_point(f["geometry"]["rings"])
            off += len(fs)
            if len(fs) < 1000:
                break
        json.dump(pts, open(p, "w"))
    return {k: tuple(v) for k, v in json.load(open(p)).items()}


def statcan_lambert_to_latlon(x, y):
    """inverse of NAD83 / Statistics Canada Lambert (EPSG:3347), used for block representative points"""
    a, f = 6378137.0, 1 / 298.257222101
    e = math.sqrt(2 * f - f * f)
    p1, p2, p0, l0 = map(math.radians, (49.0, 77.0, 63.390675, -91.86666666666666))
    m = lambda p: math.cos(p) / math.sqrt(1 - (e * math.sin(p)) ** 2)
    t = lambda p: math.tan(math.pi / 4 - p / 2) / ((1 - e * math.sin(p)) / (1 + e * math.sin(p))) ** (e / 2)
    n = (math.log(m(p1)) - math.log(m(p2))) / (math.log(t(p1)) - math.log(t(p2)))
    F = m(p1) / (n * t(p1) ** n)
    r0 = a * F * t(p0) ** n
    dx, dy = x - 6200000.0, r0 - (y - 3000000.0)
    tt = (math.copysign(math.hypot(dx, dy), n) / (a * F)) ** (1 / n)
    lon = math.atan2(dx, dy) / n + l0
    lat = math.pi / 2 - 2 * math.atan(tt)
    for _ in range(8):
        lat = math.pi / 2 - 2 * math.atan(tt * ((1 - e * math.sin(lat)) / (1 + e * math.sin(lat))) ** (e / 2))
    return (math.degrees(lat), math.degrees(lon))


def da_block_points():
    """{DAUID (8 digits): [(lat, lon, weight), ...]}: where each DA's homes are, as dissemination-block
    representative points (StatCan places them on the road network). BC: the BC_BLOCKS_PER_DA blocks with
    the most 2021 dwellings occupied by usual residents, weighted by them (Geographic Attribute File). Alberta: the
    AB_BLOCKS_PER_DA smallest blocks, equally weighted (no block counts available)."""
    p = os.path.join(WORK, "db_all_bcab.json")
    if not os.path.exists(p):
        blocks, off = {}, 0
        while True:
            q = urllib.parse.urlencode({"where": "PRUID IN ('48','59')", "outFields": "DBUID,DBRPLAMX,DBRPLAMY,LANDAREA",
                                        "returnGeometry": "false", "resultOffset": off, "resultRecordCount": 5000,
                                        "orderByFields": "OBJECTID", "f": "json"})
            fs = json.loads(get(DB_LAYER + "?" + q, timeout=300)).get("features", [])
            for f in fs:
                at = f["attributes"]
                if at.get("DBRPLAMX") is not None:
                    blocks[at["DBUID"]] = [at["DBRPLAMX"], at["DBRPLAMY"], at.get("LANDAREA") or 0]
            off += len(fs)
            if len(fs) < 5000:
                break
        json.dump(blocks, open(p + ".tmp", "w"))
        os.replace(p + ".tmp", p)
    blocks = json.load(open(p))
    dwell = {}
    with open(cached("gaf_bc.csv", GAF_BC), encoding="latin-1") as f:
        for row in csv.DictReader(f):
            try:
                dwell[row["DBUID_IDIDU"]] = int(row["DBURDWELL2021_IDRHLOG2021"])
            except (KeyError, ValueError):
                pass
    by_da = {}
    for db, (x, y, area) in blocks.items():
        by_da.setdefault(db[:8], []).append((db, x, y, area))
    out = {}
    for da, bs in by_da.items():
        if da.startswith("59"):
            homes = sorted(((dwell.get(db, 0), db, x, y, area) for db, x, y, area in bs), reverse=True)
            homes = [h for h in homes if h[0] > 0]
            # skip very big blocks when the DA has smaller ones with homes: the DA's homes are then placed
            # like those in its smaller blocks (its settlements) rather than at one arbitrary road point
            homes = ([h for h in homes if h[4] <= BIG_BLOCK_KM2] or homes)[:BC_BLOCKS_PER_DA]
            if homes:
                out[da] = [(*statcan_lambert_to_latlon(x, y), n) for n, _, x, y, _ in homes]
                continue
        small = sorted((area, db, x, y) for db, x, y, area in bs if area > 0)[:AB_BLOCKS_PER_DA]
        out[da] = [(*statcan_lambert_to_latlon(x, y), 1) for _, _, x, y in small]
    return out


def da_census_values(das):
    """{DA DGUID: 2021 census median value of dwellings ($, owner estimates) or None}: census profile SDMX API"""
    p = os.path.join(WORK, "da_value_1474.json")
    vals = json.load(open(p)) if os.path.exists(p) else {}
    todo = sorted(d for d in das if d not in vals)
    for i in range(0, len(todo), 100):
        chunk = todo[i:i + 100]
        text = get(SDMX_DA % "+".join(chunk), timeout=120, ok404=True,
                   headers={"Accept": "application/vnd.sdmx.data+csv;version=1.0.0"}).decode("utf-8-sig")
        got = {}
        for row in csv.DictReader(io.StringIO(text)):
            try:
                got[row["REF_AREA"]] = float(row["OBS_VALUE"])
            except (KeyError, ValueError, TypeError):
                pass
        for d in chunk:
            vals[d] = got.get(d)
        save_json(p, vals)
        time.sleep(1)
    return vals


def da_dwellings():
    """{DA DGUID: (private dwellings occupied by usual residents 2021, parent CSD DGUID)} for BC + AB (table
    98-10-0015). Usual residents, so ski-resort condos and cottages don't count as where people live."""
    z = cached(DA_COUNTS_PID + ".zip", "https://www150.statcan.gc.ca/n1/tbl/csv/%s-eng.zip" % DA_COUNTS_PID)
    zf = zipfile.ZipFile(z)
    parent = {}
    for row in csv.reader(io.TextIOWrapper(zf.open(DA_COUNTS_PID + "_MetaData.csv"), encoding="utf-8-sig")):
        if len(row) >= 5 and row[0] == "1" and row[3].isdigit():
            parent[row[3]] = row[4]
    by_member, out = {}, {}
    for row in csv.reader(io.TextIOWrapper(zf.open(DA_COUNTS_PID + ".csv"), encoding="utf-8-sig")):
        if len(row) > 6 and row[3].isdigit():
            by_member[row[3]] = (row[2], row[8])
    for member, (g, dw) in by_member.items():
        if g.startswith(("2021S051259", "2021S051248")):  # DAs in BC / AB
            csd = by_member.get(parent.get(member, ""), ("", ""))[0]
            try:
                out[g] = (int(float(dw)), csd)
            except ValueError:
                pass
    return out


def osrm_table(src, dests):
    """[(minutes, snapped (lat, lon), snap metres)] from src to each dest; minutes None when no route"""
    res = []
    for i in range(0, len(dests), 90):
        chunk = dests[i:i + 90]
        coords = ";".join("%f,%f" % (lo, la) for la, lo in [src] + chunk)
        d = json.loads(get("https://router.project-osrm.org/table/v1/driving/%s?sources=0&annotations=duration" % coords, timeout=120))
        for t, dst in zip(d["durations"][0][1:], d["destinations"][1:]):
            res.append((None if t is None else t * OSRM_CALIBRATION / 60, (dst["location"][1], dst["location"][0]), dst.get("distance", 0)))
        time.sleep(1)
    return res


def osrm_nearest_m(pt):
    """metres from pt to the nearest road of any kind (including small networks the table can't route on)"""
    d = json.loads(get("https://router.project-osrm.org/nearest/v1/driving/%f,%f?number=1" % (pt[1], pt[0]), timeout=60))
    time.sleep(1)
    return d["waypoints"][0]["distance"]


def water_to_main_road_m(pt):
    """longest stretch of water (m) on the straight line from pt to the main-network road the drive-time table times it
    from; land = StatCan 2021 cartographic (shoreline-clipped) DA polygons"""
    d = json.loads(get("https://router.project-osrm.org/table/v1/driving/%f,%f;%f,%f?sources=0&destinations=1&annotations=duration"
                       % (MAINLAND[1], MAINLAND[0], pt[1], pt[0]), timeout=60))
    time.sleep(1)
    lo, la = d["destinations"][0]["location"]
    s, w, n, e = min(pt[0], la) - 0.003, min(pt[1], lo) - 0.003, max(pt[0], la) + 0.003, max(pt[1], lo) + 0.003
    q = urllib.parse.urlencode({"geometry": "%f,%f,%f,%f" % (w, s, e, n), "geometryType": "esriGeometryEnvelope", "inSR": "4326",
                                "spatialRel": "esriSpatialRelIntersects", "outFields": "DGUID", "returnGeometry": "true",
                                "outSR": "4326", "maxAllowableOffset": "0.0001", "f": "json"})
    rings = [r for f in json.loads(get(CBF_DA_LAYER + "?" + q, timeout=120))["features"] for r in f["geometry"]["rings"]]
    steps = max(2, int(km(pt, (la, lo)) * 1000 / 25))
    run = best = 0
    for i in range(steps + 1):
        f = i / steps
        run = 0 if point_in_rings((pt[0] + (la - pt[0]) * f, pt[1] + (lo - pt[1]) * f), rings) else run + 1
        best = max(best, run)
    return best * 25


def uses_ferry(src, dst):
    coords = "%f,%f;%f,%f" % (src[1], src[0], dst[1], dst[0])
    d = json.loads(get("https://router.project-osrm.org/route/v1/driving/%s?overview=false&steps=true" % coords, timeout=120))
    time.sleep(0.5)
    if not d.get("routes"):
        return None
    return any(st.get("mode") == "ferry" for leg in d["routes"][0]["legs"] for st in leg["steps"])


def town_point_times(pt, das, check_ferry):
    """road minutes from pt to every block point of each candidate DA.
    das: {da: ([(lat, lon, weight)], area dguid, area rings, dwellings)}. Point keys are "<DA>#<index>".
    Returns {"t": {key: minutes or None (not reached, or snapped too far)}, "ferry": {key: bool}}.
    Ferry (BC): for each area, its FERRY_CHECKS biggest DAs with a point within the hour are checked at their
    heaviest such point; if any needs a ferry, every point of that area within the hour is checked too.
    (main() checks the nearest point of areas beyond the hour only when it needs a 'nearest area' note.)"""
    flat = [("%s#%d" % (k, i), k, q) for k in das for i, q in enumerate(das[k][0])]
    t, snap = {}, {}
    res = osrm_table(pt, [(q[0], q[1]) for _, _, q in flat]) if flat else []
    for (key, k, q), (mins, snapped, snap_m) in zip(flat, res):
        ok = mins is not None and (snap_m <= 1500 or (snap_m <= DA_SNAP_M and point_in_rings(snapped, das[k][2])))
        t[key] = round(mins, 1) if ok else None
        if ok:
            snap[key] = int(round(snap_m))
    ferry = {}
    if check_ferry:
        by_area = {}
        for key, k, q in flat:
            if t[key] is not None and t[key] <= RURAL_MINUTES:
                by_area.setdefault(das[k][1], {}).setdefault(k, []).append((q[2], key, q))
        for area, by_da in by_area.items():
            for k in sorted(by_da, key=lambda k: -das[k][3])[:FERRY_CHECKS]:
                _, key, q = max(by_da[k])
                ferry[key] = bool(uses_ferry(pt, q))
            if any(ferry.get(key) for ks in by_da.values() for _, key, _ in ks):
                for ks in by_da.values():
                    for _, key, q in ks:
                        if key not in ferry:
                            ferry[key] = bool(uses_ferry(pt, q))
    return {"t": t, "ferry": ferry, "snap": snap, "from": [round(pt[0], 5), round(pt[1], 5)]}


SIGMA_MIN, SIGMA_MAX = 0.05, 1.5


def lognormal_sigma(mean, median):
    """spread of a lognormal with this mean and median (mean = median x e^(sigma^2/2))"""
    if not mean or not median or mean <= median:
        return SIGMA_MIN
    return min(max(math.sqrt(2 * math.log(mean / median)), SIGMA_MIN), SIGMA_MAX)


def mixture_median(parts):
    """median of all properties pooled across areas. parts: (area median, lognormal sigma, number of
    properties counted). Avoids the jumps of a median of medians, which flips between two very different
    areas when one of them holds about half the properties."""
    parts = [q for q in parts if q[2] > 0]
    tot = sum(w for _, _, w in parts)
    nd = statistics.NormalDist()
    below = lambda x: sum(w * nd.cdf((math.log(x) - math.log(m)) / sg) for m, sg, w in parts) / tot
    lo, hi = min(m for m, _, _ in parts), max(m for m, _, _ in parts)
    for _ in range(60):
        mid = math.sqrt(lo * hi)
        lo, hi = (mid, hi) if below(mid) < 0.5 else (lo, mid)
    return math.sqrt(lo * hi)


def area_label(name, ctype):
    """readable rural-area name: 'MD of Taber' (not the town of Taber), 'Fraser-Fort George Area E'"""
    if ctype == "MD" and "County" not in name:
        return "MD of " + name
    if ctype == "RDA":
        m = re.match(r"^(.+?) ([A-Z]{1,2})( \(Part \d+\))?$", name)
        if m:
            return "%s Area %s%s" % (m.group(1), m.group(2), m.group(3) or "")
    return name


def and_list(names):
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def round_value(v):
    """$1,000s for big values, $100s below $100k (cheap rural lots)"""
    return int(round(v, -3)) if v >= 100000 else int(round(v, -2))


def main():
    geo = json.load(open(os.path.join(HERE, "cache", "geo.json")))["places"]
    census = json.load(open(os.path.join(HERE, "cache", "census.json")))["geos"]
    out = {g["dguid"]: {} for g in geo}

    # ---------------- rural assessed values (CHSP 46-10-0093), latest year, by property type
    chsp = {}
    for row in csv.reader(open(chsp_bcab())):
        year, gname, g, ptype, est, value = row[0], row[1], row[2], row[3], row[7], row[14]
        if ptype not in PTYPES or not value:
            continue
        parts = [x.strip() for x in gname.split(",")]
        d = chsp.setdefault(g, {"name": re.sub(r" \(prev:.*$", "", parts[0]), "type": parts[-1] if len(parts) >= 2 else "", "y": {}})
        d["y"].setdefault(year, {}).setdefault(PTYPES[ptype], {})[est] = float(value)
    shapes = csd_shapes()
    feats = json.load(open(os.path.join(WORK, "csd_bcab.json")))
    csd_name = {f["attributes"]["DGUID"]: area_label(f["attributes"]["CSDNAME"], f["attributes"]["CSDTYPE"]) for f in feats}
    rural_csds = {f["attributes"]["DGUID"] for f in feats
                  if f["attributes"]["CSDTYPE"] in RURAL_CODES and f["attributes"]["CSDNAME"] not in NOT_RURAL}
    rural = {}
    for g, d in chsp.items():
        if g not in rural_csds or not d["y"]:
            continue
        year = max(d["y"])
        rec = {"name": csd_name.get(g, d["name"]), "year": year, "small": {}}
        for k in PTYPES.values():
            v = d["y"][year].get(k, {})
            if "Median assessment value" in v and v.get("Number of properties", 0) >= MIN_PROPERTIES:
                rec[k] = (v["Median assessment value"], v["Number of properties"],
                          lognormal_sigma(v.get("Average assessment value"), v["Median assessment value"]))
            elif v.get("Number of properties"):
                rec["small"][k] = int(v["Number of properties"])
        rural[g] = rec

    pts, bpts, dw = da_points(), da_block_points(), da_dwellings()
    area_dw, area_das = {}, {}
    das = {}  # DA -> ([(lat, lon, weight)], area, area rings, dwellings): rural DAs with homes
    for da, (n, csd) in dw.items():
        if csd in rural_csds:
            area_dw[csd] = area_dw.get(csd, 0) + n
            if n > 0:
                area_das.setdefault(csd, []).append(da)
                dpts = bpts.get(da[-8:]) or ([(*pts[da], 1)] if da in pts else [])
                if dpts:
                    das[da] = (dpts, csd, shapes.get(csd, []), n)
    print("rural DAs with homes:", len(das), "in", len(area_dw), "rural areas", flush=True)

    # 2021 census home values by DA (owner estimates): used to scale each area's StatCan median to the
    # part of it within the hour, and to estimate the few areas StatCan publishes no figure for
    cval = da_census_values([da for v in area_das.values() for da in v])

    def census_level(pairs):
        """(DA, weight) -> (home-weighted geometric mean of the DAs' median values, DAs with a value, weight
        with a value). A mean, not a median of a handful of DA medians, which jumps from one DA to another
        when a little weight moves (e.g. onto a DA that is mostly a manufactured-home park)."""
        vals = [(cval[d], w) for d, w in pairs if cval.get(d) and w > 0]
        wv = sum(w for _, w in vals)
        return (math.exp(sum(w * math.log(v) for v, w in vals) / wv) if wv else None), len(vals), wv

    area_c = {}
    for g, ds in area_das.items():
        med, nda, wv = census_level([(d, dw[d][0]) for d in ds])
        if med and nda >= 3 and wv >= 0.5 * area_dw[g]:
            area_c[g] = med
    ratio, per_dw = {}, {}  # by province: StatCan assessed / census value, and houses per dwelling
    for g, r in rural.items():
        if "house" in r and g in area_c:
            ratio.setdefault(g[9:11], []).append(r["house"][0] / area_c[g])
            per_dw.setdefault(g[9:11], []).append(r["house"][1] / area_dw[g])
    ratio = {k: statistics.median(v) for k, v in ratio.items()}
    per_dw = {k: statistics.median(v) for k, v in per_dw.items()}
    # one spread per property type (median over all rural areas): an area's own mean/median ratio is
    # driven by a few very expensive properties, which would let one area's tail decide a town's median
    spread = {k: statistics.median(r[k][2] for r in rural.values() if k in r) for k in PTYPES.values()}
    print("lognormal spread:", {k: round(v, 2) for k, v in spread.items()}, flush=True)
    print("assessed/census ratio:", {k: round(v, 3) for k, v in ratio.items()},
          "houses per dwelling:", {k: round(v, 3) for k, v in per_dw.items()}, flush=True)

    meta = {"version": DRIVE_VERSION, "minutes": RURAL_MINUTES, "snap": DA_SNAP_M, "checks": FERRY_CHECKS,
            "calibration": OSRM_CALIBRATION, "blocks": [BC_BLOCKS_PER_DA, AB_BLOCKS_PER_DA], "prefilter_km": PREFILTER_KM}
    cache_p = os.path.join(WORK, "rural_drive_%s.json" % DRIVE_VERSION)
    cache = json.load(open(cache_p)) if os.path.exists(cache_p) else {}
    if cache.get("_meta") != meta:
        cache = {"_meta": meta}
    nearest_p = os.path.join(WORK, "osrm_nearest_bc.json")  # "lat,lon" -> {near: m to any road, water: m (island check)}
    nearest = json.load(open(nearest_p)) if os.path.exists(nearest_p) else {}
    for p in geo:
        pt = (p["lat"], p["lon"])
        bc = p["province"] == "British Columbia"
        cand = {k: v for k, v in das.items() if km(pt, v[0][0][:2]) <= PREFILTER_KM}
        sig = hashlib.md5(json.dumps(sorted((k, [[round(c, 5) for c in q] for q in v[0]]) for k, v in cand.items()))
                          .encode()).hexdigest()
        key = p["dguid"]
        c = cache.get(key)
        if not c or c.get("sig") != sig or c.get("from") != [round(pt[0], 5), round(pt[1], 5)]:
            # Alberta: only small free river crossings, so no ferry check
            cache[key] = dict(town_point_times(pt, cand, check_ferry=bc), sig=sig)
            save_json(cache_p, cache)
            print("drive times:", p["name"], len(cand), "DAs", sum(len(v[0]) for v in cand.values()), "points",
                  len(cache[key]["ferry"]), "ferry checks", flush=True)
        if bc and "snap" not in cache[key]:  # older cache entries: fetch snap distances only (times and ferry checks stay)
            flat = [("%s#%d" % (k, i), q) for k in cand for i, q in enumerate(cand[k][0])]
            res = osrm_table(pt, [(q[0], q[1]) for _, q in flat]) if flat else []
            cache[key]["snap"] = {pk: int(round(r[2])) for (pk, _), r in zip(flat, res) if cache[key]["t"].get(pk) is not None}
            save_json(cache_p, cache)
        t, ferry = dict(cache[key]["t"]), cache[key]["ferry"]
        if bc:
            for pk, sm in sorted(cache[key]["snap"].items()):
                if sm <= ISLAND_CHECK_M:
                    continue
                k, i = pk.split("#")
                q = cand[k][0][int(i)]
                nk = "%.5f,%.5f" % (q[0], q[1])
                if nk not in nearest:
                    nearest[nk] = {"near": round(osrm_nearest_m((q[0], q[1])))}
                    save_json(nearest_p, nearest)
                if nearest[nk]["near"] < ISLAND_RATIO * sm and sm - nearest[nk]["near"] > ISLAND_GAP_M:
                    if "water" not in nearest[nk]:
                        nearest[nk]["water"] = water_to_main_road_m((q[0], q[1]))
                        save_json(nearest_p, nearest)
                    if nearest[nk]["water"] >= ISLAND_WATER_M:
                        t[pk] = None  # on an island road network with no bridge: timed from across the water
            # a DA can span water (Saltair and Thetis Island are one DA): route-check any point within the hour
            # that is much slower than its DA's fastest point, in case the extra time is a ferry
            for k in sorted(cand):
                ts = sorted((t[pk], pk) for pk in ("%s#%d" % (k, i) for i in range(len(cand[k][0]))) if t.get(pk) is not None)
                for m, pk in ts:
                    if m <= RURAL_MINUTES and m - ts[0][0] >= SLOW_POINT_MIN and pk not in ferry:
                        q = cand[k][0][int(pk.split("#")[1])]
                        ferry[pk] = bool(uses_ferry(pt, (q[0], q[1])))
                        save_json(cache_p, cache)

        per_area = {}
        for k, (dpts, g, _, n) in cand.items():
            wsum = sum(q[2] for q in dpts)
            a = per_area.setdefault(g, {"within": 0.0, "ferry": 0.0, "near": None, "nm": [], "da": {}, "pts": []})
            for i, q in enumerate(dpts):
                pk = "%s#%d" % (k, i)
                m = t.get(pk)
                if m is None:
                    continue
                a["pts"].append((m, pk))
                homes = n * q[2] / wsum
                if RURAL_MINUTES < m <= NEAR_MISS_MINUTES:
                    a["nm"].append((homes, pk))
                if ferry.get(pk):
                    if m <= RURAL_MINUTES:
                        a["ferry"] += homes
                    continue
                if a["near"] is None or m < a["near"]:
                    a["near"] = m
                if m <= RURAL_MINUTES:
                    a["within"] += homes
                    a["da"][k] = a["da"].get(k, 0) + homes
        o = {"areas": [], "ferry_only": [], "no_data": [], "too_few": []}
        inc = []
        for g, a in per_area.items():
            share = a["within"] / area_dw[g] if area_dw.get(g) else 0
            if share < MIN_SHARE:
                if area_dw.get(g) and a["ferry"] / area_dw[g] >= MIN_SHARE:
                    o["ferry_only"].append(csd_name.get(g, g))
                continue
            r = rural.get(g) or {"name": csd_name.get(g, g)}
            cw, nda, wv = census_level(list(a["da"].items()))
            census_ok = bool(cw) and nda >= 2 and wv >= 0.5 * a["within"]
            row = {"g": g, "name": r["name"], "share": share, "near": a["near"], "info": {}}
            if "house" in r:
                # scale to the part within the hour; not when that part is nearly the whole area, and
                # never by more than half / a third (census DA values are owner estimates for all homes)
                f0 = cw / area_c[g] if census_ok and g in area_c and share < ADJUST_BELOW_SHARE else 1.0
                f = min(max(f0, ADJUST_MIN), ADJUST_MAX)
                row["house"] = (r["house"][0] * f, r["house"][1])
                if abs(f - 1) >= 0.005:
                    row["info"] = {"raw": int(r["house"][0]), "f": round(f, 2)}
                    if f != f0:
                        row["info"]["cap"] = True
                elif share < ADJUST_BELOW_SHARE and not (census_ok and g in area_c):
                    row["info"] = {"whole": True}  # partly within the hour, and census values couldn't adjust it
                    if share < WHOLE_MIN_SHARE:
                        # mostly beyond the hour and we can't tell what the part within it is worth (Big White in
                        # Kootenay Boundary E, for Kelowna): listed, but not used for the town's figures
                        row["info"]["unused"] = True
            elif census_ok and g[9:11] in ratio:
                row["house"] = (cw * ratio[g[9:11]], area_dw[g] * per_dw[g[9:11]])
                row["info"] = {"est": True, "census": int(cw), "ratio": round(ratio[g[9:11]], 2)}
            if "land" in r:
                row["land"] = r["land"]
            if "house" not in row and "land" not in row:
                (o["too_few"] if g in rural else o["no_data"]).append(r["name"])
                continue
            inc.append(row)
        for k in ("ferry_only", "no_data", "too_few"):
            o[k].sort()
        inc.sort(key=lambda x: x["near"])
        years = [rural[x["g"]]["year"] for x in inc if x["g"] in rural]
        if years:
            o["year"] = max(years)
        use = [x for x in inc if not x["info"].get("unused")]
        for ptype in PTYPES.values():
            pairs = [(x[ptype][0], x[ptype][1] * x["share"]) for x in use if ptype in x]
            if pairs:
                o[ptype] = round_value(mixture_median([(x[ptype][0], spread[ptype], x[ptype][1] * x["share"])
                                                       for x in use if ptype in x]))
                o[ptype + "_range"] = [round_value(min(v for v, _ in pairs)), round_value(max(v for v, _ in pairs))]
                o[ptype + "_props"] = int(round(sum(w for _, w in pairs)))
        est_w = sum(x["house"][1] * x["share"] for x in use if "house" in x and x["info"].get("est"))
        if est_w and o.get("house_props"):
            o["house_est_pct"] = round(100 * est_w / o["house_props"])
            o["house_est_areas"] = [x["name"] for x in use if "house" in x and x["info"].get("est")]
            sure = [(x["house"][0], spread["house"], x["house"][1] * x["share"]) for x in use if "house" in x and not x["info"].get("est")]
            if sure:  # the same figure without the estimated areas
                o["house_no_est"] = round_value(mixture_median(sure))
        cheap = [(round_value(x["house"][0]), x["name"], round(x["near"]), int(x["house"][1]), round(100 * x["share"]))
                 for x in use if "house" in x and not x["info"].get("est")]
        if cheap:
            o["cheapest"] = list(min(cheap))
        for x in inc:
            o["areas"].append([x["name"], round(x["near"]), round(100 * x["share"]),
                               *(([round_value(x[k][0]), int(x[k][1])] if k in x else [None, None]) for k in PTYPES.values()),
                               x["info"]])
        notes = []
        unused = [x["name"] for x in inc if x["info"].get("unused")]
        if not inc:
            # nearest homes in an area with a house figure that you can reach by road (no ferry): points in
            # time order, ferry-checked as needed, so a rebuild from the cache gives the same answer
            checks = 0
            for near, pk, g in sorted((m, pk, g) for g, a in per_area.items() if "house" in (rural.get(g) or {})
                                      for m, pk in a["pts"]):
                if near > 2 * RURAL_MINUTES or checks >= 12:
                    break
                if bc and pk not in ferry:
                    k, i = pk.split("#")
                    q = das[k][0][int(i)]
                    ferry[pk] = bool(uses_ferry(pt, (q[0], q[1])))
                    checks += 1
                    save_json(cache_p, cache)
                if not ferry.get(pk):
                    r = rural[g]
                    o["nearest_beyond"] = [r["name"], round(near), int(r["house"][0]), int(r["house"][1])]
                    break
        # No rural area within the hour is a real answer (scored as worst), not a data gap. Except:
        #  - areas within the hour exist but StatCan has no usable figure for them (a data gap)
        #  - 10%+ of a rural area's homes are only a little over the hour (61-75 min) from the town centre
        #    point: a big municipality's other neighbourhoods may well be within it, so it's left as unknown
        #  - Fort McMurray: its rural hamlets exist but have no separate StatCan figure
        gap = not use and bool(o["no_data"] or o["too_few"] or unused)
        nm = []
        if not inc and not gap:
            for g, a in sorted(per_area.items()):
                if "house" not in (rural.get(g) or {}) or not area_dw.get(g) or not a["nm"]:
                    continue
                pts_nm = sorted(a["nm"], key=lambda h: (-h[0], h[1]))
                if bc:  # the same heaviest points every run, so a rebuild from the cache gives the same answer
                    for _, pk in pts_nm[:40]:
                        if pk not in ferry:
                            k, i = pk.split("#")
                            q = das[k][0][int(i)]
                            ferry[pk] = bool(uses_ferry(pt, (q[0], q[1])))
                            save_json(cache_p, cache)
                share_nm = sum(h for h, pk in pts_nm if not ferry.get(pk)) / area_dw[g]
                if share_nm >= MIN_SHARE:
                    nm.append((share_nm, g))
            nm.sort()
        near_miss = bool(nm)
        o["none_within"] = not use and not gap and not near_miss and not p["name"].startswith("Fort McMurray")
        if gap and (o["no_data"] or o["too_few"]):
            notes.append("The rural areas within the hour (%s) have no usable StatCan figure."
                         % ", ".join(o["no_data"] + o["too_few"]))
        if len(unused) == 1:
            notes.append("Only a small part of %s is within the hour, and census values can't tell what that part is "
                         "worth, so it is listed but not used." % unused[0])
        elif unused:
            notes.append("Only small parts of %s are within the hour, and census values can't tell what those parts "
                         "are worth, so they are listed but not used." % and_list(unused))
        if use and o.get("house_props", FEW_PROPERTIES) < FEW_PROPERTIES:
            notes.append("The house figure rests on only about %d properties' worth of rural homes within the hour, so "
                         "treat it as rough." % o["house_props"])
        if near_miss:
            notes.append("Many homes in %s are 61 to %d min from the town centre, so from other parts of %s some may be "
                         "within the hour: left as unknown rather than scored."
                         % (and_list([rural[g]["name"] for _, g in sorted(nm, reverse=True)]), NEAR_MISS_MINUTES,
                            p["name"].split(" (")[0]))
        if p["name"].startswith("Lloydminster"):
            notes.append("Only the Alberta side is covered: Saskatchewan rural municipalities next to Lloydminster are not included.")
        if p["name"].startswith("Fort McMurray"):
            notes.append("Fort McMurray's rural hamlets (Saprae Creek, Anzac, Gregoire Lake) are inside Wood Buffalo, "
                         "and StatCan publishes no rural-only figure for it.")
        if km(pt, STRATHCONA_ACREAGES) <= 70:
            notes.append("Strathcona County's acreages (Ardrossan, Josephburg, Cooking Lake) aren't included: StatCan "
                         "publishes one figure for the whole county, and most of it is Sherwood Park itself.")
        if notes:
            o["note"] = " ".join(notes)
        out[p["dguid"]]["rural"] = o

    # ---------------- Alberta rural rent survey (AVRC)
    wb = openpyxl.load_workbook(cached("ab_avrc.xlsx", AB_AVRC), read_only=True)
    t2 = [r for r in wb["T2 Avg Rent, Range (Unit Type)"].iter_rows(values_only=True) if isinstance(r[0], int)]
    t3 = [r for r in wb["T3 Vacancies by Unit Type"].iter_rows(values_only=True) if isinstance(r[0], int)]
    year = max(r[0] for r in t2)
    vac = {}
    for r in t3:
        if r[0] == year and r[1]:
            units = sum((r[i] or 0) for i in (2, 5, 8, 11, 14) if isinstance(r[i], (int, float)))
            vacant = sum((r[i] or 0) for i in (3, 6, 9, 12, 15) if isinstance(r[i], (int, float)))
            if units:  # T3 marks communities with fewer than 30 units "Name*"
                vac[r[1].strip().rstrip("*").strip().lower()] = (round(100 * vacant / units, 1), r[1].strip().endswith("*"))
    avrc = {r[1].strip().rstrip("*").strip().lower(): r for r in t2 if r[0] == year and r[1]}
    for p in geo:
        if p["province"] != "Alberta":
            continue
        r = avrc.get(p["name"].lower())
        if r and isinstance(r[8], (int, float)):
            v, small = vac.get(p["name"].lower(), (None, False))
            out[p["dguid"]]["rent_ab"] = {"bed2": round(r[8]), "bed1": round(r[5]) if isinstance(r[5], (int, float)) else None,
                                          "vacancy": v, "period": "May-Aug %d" % year}
            if small or r[1].strip().endswith("*"):
                out[p["dguid"]]["rent_ab"]["small_sample"] = True

    # ---------------- BC 2021 census renter shelter cost (BC Stats copy of 98-401)
    shelter = {}
    with zipfile.ZipFile(cached("bc_profile.zip", BC_PROFILE)) as zf, zf.open("csd.csv") as f:
        for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")):
            if row["CHARACTERISTIC_NAME"].strip() == "Median monthly shelter costs for rented dwellings ($)":
                try:
                    shelter[row["DGUID"]] = float(row["C1_COUNT_TOTAL"])
                except ValueError:
                    pass
    for g, v in shelter.items():
        if g in out:
            out[g]["census_rent_2021"] = v

    save_json(os.path.join(HERE, "cache", "extra.json"), {"rural_minutes": RURAL_MINUTES, "chsp_table": CHSP_PID, "towns": out}, indent=0)
    print("rural:", sum(1 for v in out.values() if "rural" in v),
          "rural house:", sum(1 for v in out.values() if "house" in v.get("rural", {})),
          "AB rent:", sum(1 for v in out.values() if "rent_ab" in v), "BC census rent:", sum(1 for v in out.values() if "census_rent_2021" in v))


if __name__ == "__main__":
    main()

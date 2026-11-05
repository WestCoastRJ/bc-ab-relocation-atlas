"""One-off: pull 2021 Census tables from Statistics Canada's open data (WDS full-table CSVs),
keep BC + Alberta rows, and write pipeline/cache/census.json.

The census only changes every 5 years, so this is NOT part of the twice-daily refresh.
Run:  python pipeline/census_extract.py [workdir]
"""
import csv, io, json, os, re, subprocess, sys, time, urllib.request, zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, ".census_dl")
OUT = os.path.join(HERE, "cache", "census.json")
TABLES = {
    "age": "98100022",        # single-year age, median age, by gender
    "industry": "98100456",   # industry sector x occupation, by gender (place of residence)
    "income": "98100475",     # employment income groups, median employment income
    "dwell": "98100233",      # dwelling condition x period of construction
    "value": "98100257",      # owner-estimated dwelling value (CSDs 5,000+)
}
KEEP = re.compile(r",2021A000(5|3)(59|48)")  # CSD (A0005) and CD (A0003) rows in BC (59) / AB (48)


def fetch(pid):
    os.makedirs(WORK, exist_ok=True)
    out = os.path.join(WORK, pid + ".bcab.csv")
    if os.path.exists(out):
        return out
    z = os.path.join(WORK, pid + ".zip")
    url = "https://www150.statcan.gc.ca/n1/tbl/csv/%s-eng.zip" % pid
    for i in range(6):
        r = subprocess.run(["curl", "-sS", "-m", "900", "-C", "-", "-o", z, url])
        if r.returncode == 0 and zipfile.is_zipfile(z):
            break
        time.sleep(3 * (i + 1))
    with zipfile.ZipFile(z) as zf, open(out, "w", encoding="utf-8") as fo:
        with zf.open(pid + ".csv") as f:
            t = io.TextIOWrapper(f, encoding="utf-8-sig")
            fo.write(t.readline())
            for line in t:
                if KEEP.search(line):
                    fo.write(line)
    os.remove(z)
    return out


def rows(pid):
    r = csv.reader(open(fetch(pid), encoding="utf-8-sig"))
    return next(r), r


def num(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def main():
    data = {}

    def d(g):
        return data.setdefault(g, {})

    # --- age / gender ---
    h, r = rows(TABLES["age"])
    young = {"20 to 24 years", "25 to 29 years", "30 to 34 years", "35 to 39 years", "40 to 44 years", "45 to 49 years"}
    core = {"25 to 29 years", "30 to 34 years", "35 to 39 years", "40 to 44 years"}
    for row in r:
        if row[3] != "2021":
            continue
        g, age = row[2], row[4].strip()
        tot, men, wom = num(row[6]), num(row[8]), num(row[10])
        x = d(g)
        x["geo"] = row[1]
        if age == "Total - Age":
            x["pop"] = tot
        elif age == "Median age":
            x["median_age"] = tot
        elif age in young:
            x["m2049"] = x.get("m2049", 0) + (men or 0)
            x["w2049"] = x.get("w2049", 0) + (wom or 0)
            if age in core:
                x["p2544"] = x.get("p2544", 0) + (tot or 0)

    # --- industry and occupation mix ---
    h, r = rows(TABLES["industry"])
    for row in r:
        if row[5] != "Count" or row[4] != "Total - Gender":
            continue
        g, occ, ind, v = row[2], row[3], row[6], num(row[8])
        x = d(g)
        if occ.startswith("Total") and not ind.startswith("Total"):
            x.setdefault("ind", {})[ind.split(" ", 1)[0]] = v or 0
        elif ind.startswith("Total") and not occ.startswith("Total"):
            x.setdefault("occ", {})[occ.split(" ", 1)[0]] = v or 0
        elif ind.startswith("Total") and occ.startswith("Total"):
            x["workers"] = v

    # --- employment income ---
    h, r = rows(TABLES["income"])
    col = {name: i for i, name in enumerate(h)}
    c_med = [i for n, i in col.items() if "Median employment income" in n][0]
    c_with = [i for n, i in col.items() if n.endswith("With employment income[3]")][0]
    c_100 = [i for n, i in col.items() if "and over[16]" in n][0]
    c_80 = [i for n, i in col.items() if "[14]" in n][0]
    c_90 = [i for n, i in col.items() if "[15]" in n][0]
    for row in r:
        if row[3] != "Total - Place of work status":
            continue
        x = d(row[2])
        x["median_emp_income"] = num(row[c_med])
        w = num(row[c_with])
        if w:
            x["pct_80k_plus"] = 100 * sum(num(row[c]) or 0 for c in (c_80, c_90, c_100)) / w
            x["pct_100k_plus"] = 100 * (num(row[c_100]) or 0) / w

    # --- dwelling condition / age of housing ---
    h, r = rows(TABLES["dwell"])
    old = {"1920 or before", "1921 to 1945", "1946 to 1960", "1961 to 1970", "1971 to 1980"}
    for row in r:
        if row[4] != "Total - Structural type of dwelling" or row[5] != "Number of private households":
            continue
        x = d(row[2])
        period, cond, v = row[3], row[6], num(row[8]) or 0
        if period == "Total - Period of construction":
            if cond == "Total - Dwelling condition":
                x["households"] = v
            elif cond == "Major repairs needed":
                x["hh_major_repairs"] = v
            elif cond == "Minor repairs are needed":
                x["hh_minor_repairs"] = v
        elif period in old and cond == "Total - Dwelling condition":
            x["hh_pre1981"] = x.get("hh_pre1981", 0) + v

    # --- dwelling values (owner estimate), single-detached ---
    h, r = rows(TABLES["value"])
    for row in r:
        if not (row[3].startswith("Total") and row[4].startswith("Total") and row[5].startswith("Total")
                and row[6] == "Number of private households"):
            continue
        x = d(row[2])
        m = re.match(r"(.*) \((\d+)\) (\w+)$", row[1])
        if m:
            x["name"], x["csd_code"], x["csd_type"] = m.group(1), m.group(2), m.group(3)
        cat, sd = row[7], num(row[11])
        if cat == "Median value of dwelling $":
            x["sd_median_value"] = sd
        elif cat.startswith("Total"):
            x["sd_households"] = sd
        elif cat in ("Less than $100,000", "$100,000 to $149,999", "$150,000 to $199,999",
                     "$200,000 to $249,999", "$250,000 to $299,999"):
            x["sd_under_300k"] = x.get("sd_under_300k", 0) + (sd or 0)

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump({"source": "Statistics Canada, 2021 Census of Population (tables " + ", ".join(TABLES.values()) + ")",
               "geos": data}, open(OUT, "w"), separators=(",", ":"))
    print("wrote", OUT, len(data), "geographies")


if __name__ == "__main__":
    main()

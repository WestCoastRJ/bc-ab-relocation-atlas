# BC + Alberta Relocation Atlas

Interactive map of every BC and Alberta town of 10,000+ people (plus a faded 7,500 to 10,000 "maybe" layer), scored against Ryan's relocation criteria with adjustable weights.

**Live map:** https://westcoastrj.github.io/bc-ab-relocation-atlas/

## How it works
1. **Budget checks** (pass / fail / unknown): average rent within your budget, airport access only for towns more than ~8 h from Vancouver, and land, which is always **unverified** because no free data shows real parcels for sale.
2. **Preference score:** each factor is a percentile among all 106 towns (a fixed comparison group). A missing value counts as the 25th percentile and is flagged in the rankings. Default weights: RJC opportunity 25%, backup work 15%, rent 15%, land proxy 15%, forest 10%, resilience/AI 10%, social 10%.

The AI switch (gradual vs fast takeoff) is a hand-set stress test on broad occupation groups, not a forecast. Rankings flag towns whose rank moves 5+ places between the two.

Wildfire/flood risk, schools, hospitals and winter climate are deliberately not scored.

## Known limits (no free data)
Land parcels for sale, current asking rents/listings, current job vacancies, commercial premises and property managers, sole-operator contractors, municipal renovation permits, operating costs (heating, fuel, insurance). The rural land factor is a 2021 census proxy (existing rural houses in the census division, only areas with 5,000+ people). CMHC rents are survey averages, not asking rents.

## How it updates
`.github/workflows/refresh-and-deploy.yml` runs at 13:17 and 01:17 UTC. `pipeline/build.py` checks CMHC (rent, reliability grade, vacancy) and StatCan business counts, validates coverage before using them, keeps each town's previous values if a source fails, and writes `site/data/status.json` (last checked, last successful fetch, reference period). `places.json` is only committed when the data actually changes. A failed source makes the run red, so GitHub emails the repo owner; the map also shows a warning banner if a refresh failed or none has run in 18 h.

Static inputs, rebuilt rarely:
- `pipeline/census_extract.py` → `pipeline/cache/census.json` (2021 Census tables from Statistics Canada)
- `pipeline/geo_extract.py` → `pipeline/cache/geo.json` (locations, drive times, airports, land cover)

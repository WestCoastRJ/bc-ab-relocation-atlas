# BC + Alberta Relocation Atlas

Interactive map of every BC and Alberta town of 10,000+ people (plus a faded 7,500 to 10,000 "maybe" layer), scored against Ryan's relocation criteria with adjustable weights.

**Live map:** https://westcoastrj.github.io/bc-ab-relocation-atlas/

## What it scores
- **Cost of living:** CMHC average 2-bed rent (latest October survey), median home values
- **Rural land:** rural home values and share under $300k in the surrounding regional district / county, forest cover within 60 km
- **Social:** women per 100 men aged 20 to 49, share aged 25 to 44, median age, restaurants/bars/recreation per capita
- **Economy:** AI-resistant jobs (occupation mix, with a gradual vs fast-takeoff switch), economic diversity, well-paying backup jobs ($80k+)
- **RJC demand:** commercial client base (businesses per capita), competing contractors, growth, older homes, homes needing repairs
- **Travel:** airport access, only counted for towns more than ~8 h from Vancouver

Wildfire/flood risk, schools, hospitals and winter climate are deliberately not scored.

## How it updates
`.github/workflows/refresh-and-deploy.yml` runs twice a day: `pipeline/build.py` re-pulls CMHC rents and Statistics Canada business counts, rewrites `site/data/places.json`, and redeploys GitHub Pages.

Static inputs, rebuilt rarely:
- `pipeline/census_extract.py` → `pipeline/cache/census.json` (2021 Census tables from Statistics Canada)
- `pipeline/geo_extract.py` → `pipeline/cache/geo.json` (locations, drive times, airports, land cover)

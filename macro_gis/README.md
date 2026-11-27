# Macro GIS pipeline

Builds the intersection-level table for the crash model (`data/outputs/pgh_intersections.csv`) and,
once the model is fitted, the predicted risk surface and hotspot list.

## Setup

Run from the repo root. Needs Python 3.11 or newer (pandas 3 requires it).

```bash
python3.13 -m venv macro_gis/.venv
source macro_gis/.venv/bin/activate
pip install -r macro_gis/requirements.txt
```

## Layout

```text
macro_gis/
├── scripts/            # numbered pipeline steps, run in order from the repo root
├── data/raw/           # downloaded source layers (gitignored)
├── data/processed/     # intermediate GeoJSON/GeoTIFF (gitignored)
└── data/outputs/       # small CSVs shared with the team (committed)
```

Crash CSVs are not duplicated here: they live in the repo's top-level `data/` folder
(`WRPDC_2020_Crash_Data.csv`, `WRPDC_2021_Crash_Data.csv`, `WRPDC_2022_Crash_Data.csv`).

## Coordinate systems

All source layers are WGS84 (`EPSG:4326`) except the protected bike lanes (`EPSG:2272`, PA State Plane
South, feet). Do every buffer, distance and join in UTM 17N (`EPSG:26917`, meters), and export to
`EPSG:4326` for ArcGIS Online.

## Source layers in `data/raw/`

Downloaded 26 Nov 2026. Re-download from these URLs to reproduce.

| File | Rows | Used for | Source |
|---|---|---|---|
| `signalized_intersections.geojson` | 783 points | `has_signal` | [WPRDC: City of Pittsburgh Signalized Intersections](https://data.wprdc.org/dataset/city-of-pittsburgh-signalized-intersections) |
| `city_signs.geojson` | 74,535 points (8,304 stop signs, MUTCD `R1-1`) | `has_stop_sign` | WPRDC: City of Pittsburgh Signs (copied from the project folder) |
| `city_markings_img.geojson` | 7,316 lines: 4,652 crosswalks (typed), 2,628 stop lines, 36 cross-hatch | `has_crosswalk`, `has_hiviz_crosswalk` (continental or ladder) | [WPRDC: City of Pittsburgh Intersection Markings](https://data.wprdc.org/dataset/city-of-pittsburgh-markings) |
| `city_crosswalks_2018.geojson` | 4,648 crosswalks | cross-check of the markings layer | same dataset, "City of Pittsburgh Crosswalks" |
| `pgh_street_centerlines.geojson` | 19,683 lines | `max_lanes` (`num_lanes`), `road_class` (`domi_class`), `speed_limit` (`speedlimit`) | [WPRDC: Pittsburgh Street Centerline](https://data.wprdc.org/dataset/pittsburgh-street-centerlines) |
| `bikepgh_bike_lanes_2019.geojson` | 47 lines | `has_bike_lane` | [WPRDC: BikePGH Pittsburgh Bike Map, Aug 2019](https://data.wprdc.org/dataset/shape-files-for-bikepgh-s-pittsburgh-bike-map) |
| `bikepgh_protected_lanes_2019/protected-bike-lane.zip` | 8 lines, `EPSG:2272` | `has_bike_lane` | same dataset, "August 2019 Protected Bike Lanes" |
| `speed_humps.geojson` | 79 points | `has_speed_hump` | [WPRDC: City of Pittsburgh Speed Humps](https://data.wprdc.org/dataset/city-of-pittsburgh-speed-humps) |
| `prt_stops.geojson` | 6,358 points (county-wide) | transit exposure control | [WPRDC: Pittsburgh Regional Transit Stops](https://data.wprdc.org/dataset/prt-of-allegheny-county-transit-stops) |
| `neighborhoods.geojson` | 90 polygons (`hood`) | neighborhood fixed effects | [WPRDC: Neighborhoods](https://data.wprdc.org/dataset/neighborhoods2) |
| `pgh_city_boundary.geojson` | 3 polygons (dissolve before use) | clip and raster extent | City of Pittsburgh boundary (from ArcGIS Online) |
| `high_injury_network.geojson` | 111 lines | validation only, **not** a predictor | [WPRDC: High Injury Network](https://data.wprdc.org/dataset/high-injury-network) |

## Data vintage (checked 26 Nov 2026)

The crash window is 2020–2022. Argoverse 2 scenario timestamps are shifted (every one decodes to
6 Jan 1980, the GPS time origin), so the recording dates can't be recovered; the dataset was published at
NeurIPS 2021, so it was recorded in 2021 or earlier.

| Layer | Reflects | Fit with 2020–2022 |
|---|---|---|
| Crashes | 2020–2022 | defines the window |
| Bike lanes (BikePGH) | Aug 2019 | before the window; misses lanes added 2020–2022 |
| Speed humps | through 2021 (feed broke) | good |
| Signalized intersections | last updated Jun 2024; no install dates | snapshot after the window |
| City signs (stop signs) | through 2021 (WPRDC: feed broke in 2021; latest install date Jul 2021) | good |
| City intersection markings (crosswalks) | through 2021 (feed broke; the separate crosswalk layer stops in 2018 with nearly the same count) | good |
| OSM intersections | queried as of 1 Jul 2021 | good |
| Street centerlines | Apr 2026 bulk reload; no edit history | snapshot after the window |
| PRT stops | Oct 2026 schedule (feed 2610) | locations mostly stable; trip counts are 2026 service |
| Neighborhoods | 2010 census-based boundaries | static |
| High Injury Network | published 2026 | validation only |

## Decisions (26 Nov 2026)

- **Crash window** 2020–2022, City of Pittsburgh only, **interstate crashes dropped** (no at-grade
  intersections; PennDOT's roads, not DOMI's).
- **Intersections** come from OSM as of 1 Jul 2021: nodes within ~30 m merged into one junction, 3+ legs,
  freeway-only junctions dropped. Ramp ends that meet city streets are kept (`osm_top_class = motorway`).
- **25 m radius.** Each crash is assigned once, to its nearest intersection, if that is within 25 m.
  Check: 87% of police-coded intersection crashes fall within 25 m (median 6 m), but so do 27% of
  police-coded mid-block crashes, so the main outcome counts police-coded intersection crashes only.
- **Infrastructure as of 2021** (the middle of the crash window).
- **Crosswalks come from the city's markings inventory, not OSM.** A marked crossing lies within 25 m of
  8% of intersections in OSM (mid-2021) vs 24% in the city inventory; OSM was still being mapped then.
- **Controls added beyond the original plan:** stop signs (city signs, MUTCD `R1-1`) and intersection legs.

## Known data limits

- **Bike lanes:** the centerline layer's `bikelanetype1` and `bikelaneinstalled` fields are empty, so bike
  lanes come from BikePGH's 2019 map (55 segments in total). Lanes added during 2020–2022 are missing.
- **Traffic calming:** the only public layer is speed humps (79 points). WPRDC notes its feed stopped
  updating in 2021, which still covers most of the 2020–2022 crash window.
- **Speed limit:** about 94% of centerline segments are posted at 25 mph, so it varies little.
- **Street centerlines in `data/`:** the committed `pittsburgh_street_centerlines.csv` holds only a header
  row and has no geometry. Use the GeoJSON here instead.

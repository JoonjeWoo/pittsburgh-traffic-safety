"""
Step 1: clean and pool 2020–2022 crashes for the City of Pittsburgh.

Inputs
    data/WRPDC_{2020,2021,2022}_Crash_Data.csv   repo top-level data/ (WPRDC Allegheny County crash data)
    macro_gis/data/raw/pgh_city_boundary.geojson

Outputs (all in macro_gis/data/processed/, which Git ignores)
    pgh_crashes_2020_2022.csv       one row per crash, for inspection
    pgh_crashes_2020_2022.geojson   same rows, to upload to ArcGIS Online as one pooled layer
    01_crashes_preview.png          quick visual check

How this feeds the analysis
    Stage 1: step 3 assigns each crash to its nearest intersection (within 25 m). Those counts are the
             outcome Nick models, and the "observed" side of the hotspot ranking.
    Stage 2: the pedestrian/bicycle flags and hour of day tell Joon which conflicts to look for at each
             hotspot, and how much of a hotspot's crash history falls in daylight, when Argoverse
             vehicles were driving.

Run from anywhere:  macro_gis/.venv/bin/python macro_gis/scripts/01_clean_pool_crashes.py
"""

from pathlib import Path

import geopandas as gpd
import matplotlib
import pandas as pd

matplotlib.use("Agg")  # write PNGs without opening a window
import matplotlib.pyplot as plt

# --- Settings ------------------------------------------------------------------------------------------

REPO = Path(__file__).resolve().parents[2]
CRASH_FILES = {year: REPO / "data" / f"WRPDC_{year}_Crash_Data.csv" for year in (2020, 2021, 2022)}
BOUNDARY_FILE = REPO / "macro_gis" / "data" / "raw" / "pgh_city_boundary.geojson"
OUT_DIR = REPO / "macro_gis" / "data" / "processed"

PITTSBURGH_CODE = 2301       # PennDOT municipality code (written "02301" in the CSV, read as 2301)
UTM = "EPSG:26917"           # NAD83 / UTM 17N, in meters: used for every distance
WGS84 = "EPSG:4326"          # lat/lon: used for files going to ArcGIS Online
BOUNDARY_TOLERANCE_M = 100   # keep crashes up to 100 m outside the city line (streets on the boundary)

KEEP_COLS = [
    # identity and time
    "CRASH_CRN", "CRASH_YEAR", "CRASH_MONTH", "DAY_OF_WEEK", "HOUR_OF_DAY",
    # location
    "MUNICIPALITY", "DEC_LAT", "DEC_LONG",
    # road and intersection context (police-reported at the time of the crash)
    "INTERSTATE", "INTERSECTION", "INTERSECT_TYPE", "TCD_TYPE", "ILLUMINATION", "COLLISION_TYPE",
    # severity and who was involved
    "MAX_SEVERITY_LEVEL", "INJURY_OR_FATAL", "INJURY_COUNT", "FATAL_COUNT", "PED_COUNT", "BICYCLE_COUNT",
]


def main() -> None:
    funnel = []  # (step description, rows remaining) so every filter is visible

    # 1. Load the three years. utf-8-sig strips the byte-order mark at the start of the 2021 file.
    frames = []
    for year, path in CRASH_FILES.items():
        df = pd.read_csv(path, usecols=KEEP_COLS, encoding="utf-8-sig", low_memory=False)
        frames.append(df)
    # ignore_index: each yearly file numbers its rows from 0, so without it the row labels repeat.
    crashes = pd.concat(frames, ignore_index=True)
    funnel.append(("All Allegheny County crashes, 2020–2022", len(crashes)))

    # 2. City of Pittsburgh only.
    crashes = crashes[crashes["MUNICIPALITY"] == PITTSBURGH_CODE]
    funnel.append(("In Pittsburgh (MUNICIPALITY = 2301)", len(crashes)))

    # 3. Need coordinates to place a crash at an intersection.
    crashes = crashes.dropna(subset=["DEC_LAT", "DEC_LONG"])
    funnel.append(("With coordinates", len(crashes)))

    # 4. Drop interstate crashes: no at-grade intersections, and PennDOT's roads rather than DOMI's.
    crashes = crashes[crashes["INTERSTATE"] != 1]
    funnel.append(("Not on an interstate", len(crashes)))

    # 5. Coordinates should sit inside the city. Points far outside are geocoding errors.
    points = gpd.GeoDataFrame(
        crashes,
        geometry=gpd.points_from_xy(crashes["DEC_LONG"], crashes["DEC_LAT"]),
        crs=WGS84,
    ).to_crs(UTM)
    city = gpd.read_file(BOUNDARY_FILE).to_crs(UTM).dissolve()  # 3 polygons -> 1 city shape
    city_zone = city.geometry.iloc[0].buffer(BOUNDARY_TOLERANCE_M)
    inside = points.within(city_zone)
    dropped_outside = points[~inside]
    points = points[inside].copy()
    funnel.append((f"Inside the city boundary (+{BOUNDARY_TOLERANCE_M} m)", len(points)))

    # Each crash should appear once.
    duplicates = points["CRASH_CRN"].duplicated().sum()
    assert duplicates == 0, f"{duplicates} duplicate CRASH_CRN values"

    # 6. Flags used downstream (1 = yes, 0 = no).
    points["is_ped"] = (points["PED_COUNT"].fillna(0) > 0).astype(int)
    points["is_bike"] = (points["BICYCLE_COUNT"].fillna(0) > 0).astype(int)
    points["is_vru"] = ((points["is_ped"] + points["is_bike"]) > 0).astype(int)
    points["police_coded_intersection"] = (points["INTERSECTION"] == 1).astype(int)
    points["is_injury_or_fatal"] = (points["INJURY_OR_FATAL"] == 1).astype(int)

    # 7. Save. CSV keeps lat/lon columns; GeoJSON goes back to lat/lon for ArcGIS Online.
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = OUT_DIR / "pgh_crashes_2020_2022.csv"
    geojson_path = OUT_DIR / "pgh_crashes_2020_2022.geojson"
    points.drop(columns="geometry").to_csv(csv_path, index=False)
    points.to_crs(WGS84).to_file(geojson_path, driver="GeoJSON")

    # 8. Report what was generated.
    print("\nFILTER FUNNEL")
    for step, n in funnel:
        print(f"  {step:<45} {n:>7,}")
    if len(dropped_outside):
        far = dropped_outside.distance(city.geometry.iloc[0])
        print(f"  (dropped {len(dropped_outside)} outside the city; median {far.median():,.0f} m outside)")

    print("\nBY YEAR")
    summary = points.groupby("CRASH_YEAR").agg(
        crashes=("CRASH_CRN", "size"),
        police_coded_intersection=("police_coded_intersection", "sum"),
        pedestrian=("is_ped", "sum"),
        bicycle=("is_bike", "sum"),
        vru_any=("is_vru", "sum"),
        injury_or_fatal=("is_injury_or_fatal", "sum"),
    )
    summary.loc["Total"] = summary.sum()
    print(summary.to_string())

    print("\nSAVED")
    for path in (csv_path, geojson_path):
        print(f"  {path.relative_to(REPO)}  ({path.stat().st_size / 1e6:.1f} MB)")

    # 9. Preview map: left = all crashes by police coding, right = pedestrian and bicycle crashes.
    fig, axes = plt.subplots(1, 2, figsize=(14, 7))
    mid = points[points["police_coded_intersection"] == 0]
    at_int = points[points["police_coded_intersection"] == 1]
    ped = points[points["is_ped"] == 1]
    bike = points[points["is_bike"] == 1]

    for ax in axes:
        city.boundary.plot(ax=ax, color="black", linewidth=0.6)
        ax.set_axis_off()

    mid.plot(ax=axes[0], markersize=1, color="#9e9e9e", alpha=0.5, label=f"Mid-block ({len(mid):,})")
    at_int.plot(ax=axes[0], markersize=1, color="#d62728", alpha=0.6, label=f"At intersection ({len(at_int):,})")
    axes[0].set_title("Crashes 2020–2022 by police coding (interstates removed)")
    axes[0].legend(loc="lower left", markerscale=8)

    ped.plot(ax=axes[1], markersize=4, color="#1f77b4", alpha=0.7, label=f"Pedestrian ({len(ped):,})")
    bike.plot(ax=axes[1], markersize=4, color="#ff7f0e", alpha=0.8, label=f"Bicycle ({len(bike):,})")
    axes[1].set_title("Pedestrian and bicycle crashes 2020–2022")
    axes[1].legend(loc="lower left", markerscale=3)

    fig.tight_layout()
    png_path = OUT_DIR / "01_crashes_preview.png"
    fig.savefig(png_path, dpi=130)
    print(f"  {png_path.relative_to(REPO)}")


if __name__ == "__main__":
    main()

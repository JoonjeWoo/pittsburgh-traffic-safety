"""
Step 4: add daily traffic volumes (AADT) for the busier and quieter street at every intersection.

Why: crash counts rise with traffic, so without volumes the "riskiest" intersections are just the busiest
ones (Prof. Qian's comment on the proposal). With aadt_major and aadt_minor, Nick's model estimates risk
per vehicle, the standard approach for intersection crash models.

Inputs
    macro_gis/data/processed/pgh_intersections_joined.geojson   (step 3)
    macro_gis/data/raw/penndot_rmstraffic_pgh.geojson           PennDOT RMSTRAFFIC (downloaded if missing)
    macro_gis/data/raw/city_traffic_counts.geojson              City of Pittsburgh traffic counts (WPRDC)
    macro_gis/data/raw/pgh_street_centerlines.geojson

Outputs (same files as step 3, with four columns added)
    macro_gis/data/outputs/pgh_intersections.csv
    macro_gis/data/processed/pgh_intersections_joined.geojson
    macro_gis/data/processed/04_volumes_preview.png

Where each volume comes from, in order of preference
    penndot       PennDOT's annual average daily traffic on a state or federal-aid route within 25 m
    city_count    a city traffic count on one of the street segments meeting at the intersection,
                  scaled to PennDOT's annual average (the city's short counts run higher)
    class_median  no measurement: the typical (median) measured volume for that DOMI road class

How this feeds the analysis
    Stage 1: ln(aadt_major) and ln(aadt_minor) enter Nick's model as exposure, so the infrastructure
             coefficients describe risk per vehicle. They also become a raster layer.
    Stage 2: hotspots ranked on risk per vehicle (after Nick's model) are the ones Joon examines.

Run from anywhere:  macro_gis/.venv/bin/python macro_gis/scripts/04_add_traffic_volumes.py
"""

import json
from pathlib import Path

import geopandas as gpd
import matplotlib
import numpy as np
import pandas as pd
import requests

matplotlib.use("Agg")
import matplotlib.pyplot as plt

# --- Settings ------------------------------------------------------------------------------------------

REPO = Path(__file__).resolve().parents[2]
RAW = REPO / "macro_gis" / "data" / "raw"
PROCESSED = REPO / "macro_gis" / "data" / "processed"
OUTPUTS = REPO / "macro_gis" / "data" / "outputs"
TABLE_GEO = PROCESSED / "pgh_intersections_joined.geojson"
TABLE_CSV = OUTPUTS / "pgh_intersections.csv"
PENNDOT_FILE = RAW / "penndot_rmstraffic_pgh.geojson"
CITY_COUNTS_FILE = RAW / "city_traffic_counts.geojson"

PENNDOT_URL = "https://gis.penndot.pa.gov/gis/rest/services/opendata/roadwaytraffic/MapServer/0/query"
CITY_COUNTS_URL = ("https://data.wprdc.org/dataset/a45d5ee0-c7d2-4663-a153-8434e1a26779/resource/"
                   "6b8b7fc6-75f6-498f-9830-5df1bf5bdfa5/download/traffic_counter_img.geojson")

UTM = "EPSG:26917"
WGS84 = "EPSG:4326"
RADIUS_M = 25             # PennDOT routes and street segments counted as meeting at the intersection
COUNTER_SNAP_M = 20       # a city counter belongs to the street segment within 20 m
MATCH_M = 15              # PennDOT segment midpoint to city street segment, for the class medians
# Limited-access roads never enter an intersection at street level, so their traffic must not be counted
# at junctions they pass over or beside: I-279, I-376, I-579, and PA-28 (expressway through the city).
LIMITED_ACCESS = {"0279", "0376", "0579", "0028"}
CLASS_RANK = {"Principal Arterial": 1, "Minor Arterial": 2, "Collector": 3, "Local": 4, "Alley": 5}
VOLUME_COLS = ["aadt_major", "aadt_minor", "aadt_major_source", "aadt_minor_source"]


def download_inputs() -> None:
    """Fetch PennDOT volumes for the Pittsburgh area and the city's counts, unless already on disk."""
    if not PENNDOT_FILE.exists():
        city = gpd.read_file(RAW / "pgh_city_boundary.geojson").to_crs(UTM).dissolve()
        minx, miny, maxx, maxy = city.buffer(300).to_crs(WGS84).total_bounds
        fields = "OBJECTID,ST_RT_NO,CUR_AADT,VOL_CNT_DATE,SIDE_IND,DIR_IND,JURIS,SEG_LNGTH_FEET"
        features, offset = [], 0
        while True:  # the service returns at most 1,000 segments per request
            page = requests.get(PENNDOT_URL, timeout=120, params=dict(
                where="1=1", geometry=f"{minx},{miny},{maxx},{maxy}", geometryType="esriGeometryEnvelope",
                inSR=4326, spatialRel="esriSpatialRelIntersects", outFields=fields, outSR=4326,
                f="geojson", resultOffset=offset, resultRecordCount=1000, orderByFields="OBJECTID",
            )).json().get("features", [])
            features += page
            offset += len(page)
            if len(page) < 1000:
                break
        PENNDOT_FILE.write_text(json.dumps({"type": "FeatureCollection", "features": features}))
    if not CITY_COUNTS_FILE.exists():
        CITY_COUNTS_FILE.write_bytes(requests.get(CITY_COUNTS_URL, timeout=120).content)


def bearing(line) -> float:
    """Overall direction of a line, 0–180 degrees (so both directions of travel compare equal)."""
    coords = np.asarray(line.coords if line.geom_type == "LineString" else line.geoms[0].coords)
    (x0, y0), (x1, y1) = coords[0][:2], coords[-1][:2]
    return float(np.degrees(np.arctan2(y1 - y0, x1 - x0)) % 180)


def route_volumes(segs: pd.DataFrame) -> list:
    """Two-way volume of each PennDOT route at one intersection, busiest first.
    Divided roads and one-way pairs are recorded one direction per segment, so directions are added."""
    out = []
    for _, g in segs.groupby("ST_RT_NO"):
        two_way = g.loc[g["DIR_IND"] == "B", "CUR_AADT"].max()
        one_way = g[g["DIR_IND"] != "B"].groupby("DIR_IND")["CUR_AADT"].max().sum()
        out.append(max(0 if pd.isna(two_way) else two_way, one_way))
    return sorted(out, reverse=True)


def main() -> None:
    download_inputs()
    ints = gpd.read_file(TABLE_GEO).to_crs(UTM)
    ints = ints.drop(columns=[c for c in VOLUME_COLS if c in ints.columns])  # safe to re-run
    zones = gpd.GeoDataFrame({"int_id": ints["int_id"]}, geometry=ints.geometry.buffer(RADIUS_M), crs=UTM)

    penndot = gpd.read_file(PENNDOT_FILE).to_crs(UTM)
    penndot = penndot[~penndot["ST_RT_NO"].isin(LIMITED_ACCESS) & (penndot["CUR_AADT"] > 0)]
    streets = gpd.read_file(RAW / "pgh_street_centerlines.geojson", columns=["domi_class"]).to_crs(UTM)
    streets = streets.reset_index(names="seg_id")
    streets["rank"] = streets["domi_class"].map(CLASS_RANK).fillna(6)

    # 1. City counts -> the street segment they sit on.
    counts = gpd.read_file(CITY_COUNTS_FILE).to_crs(UTM)
    counts = counts[counts["average_daily_car_traffic"] > 0]
    counts = gpd.sjoin_nearest(counts[["average_daily_car_traffic", "geometry"]],
                               streets[["seg_id", "domi_class", "rank", "geometry"]], max_distance=COUNTER_SNAP_M)
    counts = counts[~counts.index.duplicated(keep="first")]

    # 2. PennDOT two-way segments -> the city street segment they run along (midpoint close, same direction).
    two_way = penndot[penndot["DIR_IND"] == "B"].copy()
    mids = gpd.GeoDataFrame({"CUR_AADT": two_way["CUR_AADT"].to_numpy(),
                             "b": two_way.geometry.apply(bearing).to_numpy()},
                            geometry=two_way.geometry.interpolate(0.5, normalized=True).to_numpy(), crs=UTM)
    cand = streets.assign(sb=streets.geometry.apply(bearing))[["seg_id", "domi_class", "sb", "geometry"]]
    matched = gpd.sjoin_nearest(mids, cand, max_distance=MATCH_M)
    matched = matched[~matched.index.duplicated(keep="first")]
    angle = (matched["b"] - matched["sb"]).abs()
    matched = matched[(angle < 30) | (angle > 150)]

    # 3. Put city counts on PennDOT's scale, using street segments measured by both.
    both = counts.merge(matched[["seg_id", "CUR_AADT"]], on="seg_id")
    ratio = float((both["average_daily_car_traffic"] / both["CUR_AADT"]).median())
    counts["volume"] = counts["average_daily_car_traffic"] / ratio

    # 4. PennDOT routes meeting at each intersection.
    near = gpd.sjoin(penndot[["ST_RT_NO", "DIR_IND", "CUR_AADT", "geometry"]], zones, predicate="intersects")
    routes = near.groupby("int_id")[["ST_RT_NO", "DIR_IND", "CUR_AADT"]].apply(route_volumes)

    # 5. City counts on the street segments meeting at each intersection: (class rank, volume) pairs.
    approach = gpd.sjoin(streets[["seg_id", "geometry"]], zones, predicate="intersects")
    approach = approach.merge(counts[["seg_id", "rank", "volume"]], on="seg_id")
    counted = approach.groupby("int_id")[["rank", "volume"]].apply(lambda g: list(zip(g["rank"], g["volume"])))

    # 6. Typical volume per road class, from every measurement matched to a city street.
    pooled = pd.concat([
        matched[["domi_class", "CUR_AADT"]].rename(columns={"CUR_AADT": "volume"}).assign(source="penndot"),
        counts[["domi_class", "volume"]].assign(source="city_count"),
    ])
    medians = pooled.groupby("domi_class")["volume"].median()

    # 7. Major and minor volume for each intersection.
    rows = []
    for _, r in ints.iterrows():
        vols = routes.get(r["int_id"], [])
        cnts = counted.get(r["int_id"], [])
        major_rank = CLASS_RANK.get(r["road_class_major"], 6)
        if vols:
            major, major_src = vols[0], "penndot"
        elif cnts:
            major, major_src = max(v for _, v in cnts), "city_count"
        else:
            major, major_src = medians.get(r["road_class_major"], np.nan), "class_median"
        quieter = [(k, v) for k, v in cnts if k > major_rank]  # counts on a lower-class street
        if len(vols) >= 2:
            minor, minor_src = vols[1], "penndot"
        elif quieter:
            minor, minor_src = max(quieter)[1], "city_count"  # the lowest-class counted street
        else:
            minor, minor_src = medians.get(r["road_class_minor"], np.nan), "class_median"
        rows.append((major, major_src, minor, minor_src))
    v = pd.DataFrame(rows, columns=["aadt_major", "aadt_major_source", "aadt_minor", "aadt_minor_source"],
                     index=ints.index)

    # Principal arterials: PennDOT records most of them one direction at a time, so few match in step 2.
    # Their typical value comes from the principal-arterial intersections that PennDOT does measure.
    pa = (ints["road_class_major"] == "Principal Arterial") & (v["aadt_major_source"] == "penndot")
    pa_median = float(v.loc[pa, "aadt_major"].median())
    for col in ("major", "minor"):
        fill = (ints[f"road_class_{col}"] == "Principal Arterial") & (v[f"aadt_{col}_source"] == "class_median")
        v.loc[fill, f"aadt_{col}"] = pa_median
    # Park roads, private roads and unknown classes are treated as local streets.
    for col in ("major", "minor"):
        fill = (ints[f"road_class_{col}"] == "Other") & (v[f"aadt_{col}_source"] == "class_median")
        v.loc[fill, f"aadt_{col}"] = medians.get("Local")
    # No city street data and no measurement: leave blank (Nick decides whether to drop these rows).
    for col in ("major", "minor"):
        blank = ints[f"road_class_{col}"].isna() & (v[f"aadt_{col}_source"] == "class_median")
        v.loc[blank, f"aadt_{col}"] = np.nan
        v.loc[blank, f"aadt_{col}_source"] = "no_street_data"
    # The quieter street can't carry more than the busier one (blanks stay blank).
    too_high = v["aadt_minor"] > v["aadt_major"]
    v.loc[too_high, "aadt_minor"] = v.loc[too_high, "aadt_major"]
    v[["aadt_major", "aadt_minor"]] = v[["aadt_major", "aadt_minor"]].round(0)

    ints = pd.concat([ints, v[VOLUME_COLS]], axis=1)

    # 8. Save over the step 3 files (same names, four columns added at the end).
    table = ints.drop(columns="geometry")
    table.to_csv(TABLE_CSV, index=False)
    ints.to_crs(WGS84).to_file(TABLE_GEO, driver="GeoJSON", COORDINATE_PRECISION=7)

    # 9. Report.
    print(f"\nPENNDOT SEGMENTS USED: {len(penndot):,} (limited-access roads removed) | "
          f"CITY COUNTS MATCHED: {len(counts):,}")
    print(f"CITY COUNT / PENNDOT AADT on {len(both)} segments measured by both: median {ratio:.2f} "
          f"-> city counts divided by {ratio:.2f}")

    print("\nTYPICAL DAILY VOLUME BY ROAD CLASS (used where nothing is measured)")
    tab = pooled.groupby(["domi_class", "source"]).size().unstack(fill_value=0)
    tab["median"] = medians.round(0)
    order = [c for c in CLASS_RANK if c in tab.index] + [c for c in tab.index if c not in CLASS_RANK]
    tab = tab.reindex(order)
    tab.loc["Principal Arterial", "median"] = round(pa_median)
    print(tab.astype(int).to_string())
    print("  (Principal Arterial: median of PennDOT-measured principal-arterial intersections; "
          "park, private and unknown roads use Local)")

    print("\nWHERE EACH VOLUME CAME FROM (share of intersections)")
    src = pd.DataFrame({c: ints[f"aadt_{c}_source"].value_counts(normalize=True) for c in ("major", "minor")})
    print((src.fillna(0) * 100).round(1).astype(str).add("%").to_string())

    print("\nVOLUME DISTRIBUTION (vehicles per day)")
    print(ints[["aadt_major", "aadt_minor"]].describe(percentiles=[.25, .5, .75]).round(0).to_string())

    # Exposure check: crashes should climb with traffic.
    ok = ints["aadt_major"].notna()
    ints.loc[ok, "volume_group"] = pd.qcut(ints.loc[ok, "aadt_major"].rank(method="first"), 5,
                                           labels=["lowest 20%", "2nd", "3rd", "4th", "highest 20%"])
    print("\nMEAN CRASHES PER INTERSECTION BY BUSIER-STREET VOLUME")
    print(ints[ok].groupby("volume_group", observed=True).agg(
        aadt_major_range=("aadt_major", lambda s: f"{s.min():,.0f}–{s.max():,.0f}"),
        mean_crashes_int=("crashes_int", "mean"), intersections=("int_id", "size")).round(2).to_string())

    # Preview of how the ranking changes on a per-vehicle basis (Nick's model will do this properly).
    entering = (ints["aadt_major"] + ints["aadt_minor"]) * 365 * 3 / 1e6  # million vehicles, 2020–2022
    ints["crashes_per_mev"] = ints["crashes_int"] / entering
    by_count = ints.sort_values(["crashes_int", "crashes_all"], ascending=False).head(15)
    by_rate = ints[ints["crashes_int"] >= 5].sort_values("crashes_per_mev", ascending=False).head(15)
    show = ["int_id", "street_names", "neighborhood", "crashes_int", "aadt_major", "aadt_minor",
            "aadt_major_source", "crashes_per_mev"]
    print("\nTOP 15 BY CRASHES PER MILLION ENTERING VEHICLES (at least 5 crashes; preview only)")
    print(by_rate[show].to_string(index=False, max_colwidth=34,
                                  formatters={"crashes_per_mev": "{:.2f}".format}))
    print(f"\nOverlap with the top 15 by raw crash count: {len(set(by_rate['int_id']) & set(by_count['int_id']))} of 15")

    print("\nSAVED (four columns added)")
    for p in (TABLE_CSV, TABLE_GEO):
        print(f"  {p.relative_to(REPO)}  ({p.stat().st_size / 1e6:.1f} MB)")

    # 10. Preview: volume map and the crash–volume relationship.
    fig, axes = plt.subplots(1, 2, figsize=(15, 7), gridspec_kw={"width_ratios": [1.5, 1]})
    city = gpd.read_file(RAW / "pgh_city_boundary.geojson").to_crs(UTM).dissolve()
    city.boundary.plot(ax=axes[0], color="black", linewidth=0.6)
    bins = [(0, 2000, "< 2,000", "#d9d9d9", 1), (2000, 5000, "2,000–5,000", "#9ecae1", 3),
            (5000, 10000, "5,000–10,000", "#4292c6", 6), (10000, 20000, "10,000–20,000", "#08519c", 10),
            (20000, 1e9, "20,000+", "#08306b", 16)]
    for lo, hi, label, color, size in bins:
        sub = ints[(ints["aadt_major"] >= lo) & (ints["aadt_major"] < hi)]
        sub.plot(ax=axes[0], color=color, markersize=size, label=f"{label} ({len(sub):,})", zorder=lo + 1)
    axes[0].set_title("Daily traffic on the busier street at each intersection")
    axes[0].legend(title="Vehicles per day", loc="lower left")
    axes[0].set_axis_off()

    grp = ints[ok].groupby("volume_group", observed=True)["crashes_int"].mean()
    axes[1].bar(range(len(grp)), grp.values, color="#4c78a8")
    axes[1].set_xticks(range(len(grp)), grp.index, rotation=0)
    axes[1].set_ylabel("Mean police-coded intersection crashes, 2020–2022")
    axes[1].set_title("Crashes rise with traffic on the busier street")
    axes[1].spines[["top", "right"]].set_visible(False)

    fig.tight_layout(rect=(0, 0, 1, 0.95))
    png = PROCESSED / "04_volumes_preview.png"
    fig.savefig(png, dpi=130)
    print(f"  {png.relative_to(REPO)}")


if __name__ == "__main__":
    main()

"""
Step 3: attach crashes and street features to every intersection. This is the table for Nick.

Inputs
    macro_gis/data/processed/pgh_intersections_2021.geojson   (step 2)
    macro_gis/data/processed/pgh_crashes_2020_2022.geojson    (step 1)
    macro_gis/data/raw/  signals, city signs, city markings, bike lanes, speed humps, transit stops,
                         street centerlines, neighborhoods  (sources and dates in macro_gis/README.md)
    data/top_15_intersections_15m.csv                         (Nick's earlier list; comparison only)

Outputs
    macro_gis/data/outputs/pgh_intersections.csv              one row per intersection (committed to Git)
    macro_gis/data/processed/pgh_intersections_joined.geojson same rows as points, for ArcGIS Online
    macro_gis/data/processed/03_intersections_preview.png

How this feeds the analysis
    Stage 1: Nick fits the negative binomial model on this table: crash counts are the outcome, the
             has_* columns are the infrastructure effects, and legs, lanes, road class and transit stops are
             controls. Each predictor later becomes one raster layer, weighted by Nick's coefficient.
    Stage 2: crash counts per intersection give the first hotspot list; int_id plus coordinates let Joon
             match Argoverse scenarios to intersections.

Traffic volumes are added in step 4.

Run from anywhere:  macro_gis/.venv/bin/python macro_gis/scripts/03_build_intersection_table.py
"""

import re
from pathlib import Path

import geopandas as gpd
import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt

# --- Settings ------------------------------------------------------------------------------------------

REPO = Path(__file__).resolve().parents[2]
RAW = REPO / "macro_gis" / "data" / "raw"
PROCESSED = REPO / "macro_gis" / "data" / "processed"
OUTPUTS = REPO / "macro_gis" / "data" / "outputs"
NICK_TOP15 = REPO / "data" / "top_15_intersections_15m.csv"

UTM = "EPSG:26917"
WGS84 = "EPSG:4326"
RADIUS_M = 25            # agreed radius for crashes and intersection features
HUMP_RADIUS_M = 100      # speed humps sit mid-block, so also count them on the approaches
TRANSIT_RADIUS_M = 100   # bus/rail stops nearby, a stand-in for pedestrian activity

# DOMI road classes, busiest first. Park roads, private roads and "Unknown" rank last.
CLASS_RANK = {"Principal Arterial": 1, "Minor Arterial": 2, "Collector": 3, "Local": 4, "Alley": 5}
HIVIZ_TYPES = {"Crosswalk - Continental", "Crosswalk - Ladder"}  # high-visibility striping


def load(path: Path, columns=None) -> gpd.GeoDataFrame:
    return gpd.read_file(path, columns=columns).to_crs(UTM)


def count_near(ints: gpd.GeoDataFrame, features: gpd.GeoDataFrame, radius: float) -> np.ndarray:
    """Number of features (points or lines) that come within `radius` meters of each intersection."""
    zones = gpd.GeoDataFrame({"int_id": ints["int_id"]}, geometry=ints.geometry.buffer(radius), crs=UTM)
    hits = gpd.sjoin(features[["geometry"]], zones, predicate="intersects")
    return hits.groupby("int_id").size().reindex(ints["int_id"], fill_value=0).to_numpy()


def tidy_street(name: str) -> str:
    """Title-case a city street name, fixing what str.title() gets wrong (for map labels and slides):
    23Rd St -> 23rd St, Mckees Rocks -> McKees Rocks, Blvd Of The Allies -> Blvd of the Allies."""
    name = name.title()
    name = re.sub(r"(\d)(St|Nd|Rd|Th)\b", lambda m: m.group(1) + m.group(2).lower(), name)
    name = re.sub(r"\bMc([a-z])", lambda m: "Mc" + m.group(1).upper(), name)
    return re.sub(r"(?<=\w )(Of|The|And)(?= )", lambda m: m.group(1).lower(), name)


def main() -> None:
    ints = load(PROCESSED / "pgh_intersections_2021.geojson")
    crashes = load(PROCESSED / "pgh_crashes_2020_2022.geojson")

    # 1. Crashes: each crash goes to its nearest intersection, if that is within 25 m.
    near = gpd.sjoin_nearest(crashes, ints[["int_id", "geometry"]], max_distance=RADIUS_M, distance_col="dist_m")
    near = near[~near.index.duplicated(keep="first")]  # a crash exactly between two intersections counts once
    pc = near["police_coded_intersection"] == 1
    outcomes = pd.DataFrame({
        "crashes_int": pc.groupby(near["int_id"]).sum(),
        "crashes_all": near.groupby("int_id").size(),
        "vru_crashes_int": (pc & (near["is_vru"] == 1)).groupby(near["int_id"]).sum(),
        "vru_crashes_all": (near["is_vru"] == 1).groupby(near["int_id"]).sum(),
        "injury_crashes_int": (pc & (near["is_injury_or_fatal"] == 1)).groupby(near["int_id"]).sum(),
    })
    ints = ints.merge(outcomes, left_on="int_id", right_index=True, how="left")
    ints[outcomes.columns] = ints[outcomes.columns].fillna(0).astype(int)

    # 2. Signals (DOMI).
    signals = load(RAW / "signalized_intersections.geojson")
    ints["has_signal"] = (count_near(ints, signals, RADIUS_M) > 0).astype(int)

    # 3. Stop signs (city sign inventory, MUTCD R1-1).
    signs = load(RAW / "city_signs.geojson", columns=["mutcd_code"])
    stops = signs[signs["mutcd_code"] == "R1-1"]
    ints["stop_sign_count"] = count_near(ints, stops, RADIUS_M)
    ints["has_stop_sign"] = (ints["stop_sign_count"] > 0).astype(int)

    # 4. Crosswalks (city markings inventory), and the high-visibility subset.
    marks = load(RAW / "city_markings_img.geojson", columns=["type"])
    crosswalks = marks[marks["type"].str.startswith("Crosswalk", na=False)]
    ints["has_crosswalk"] = (count_near(ints, crosswalks, RADIUS_M) > 0).astype(int)
    hiviz = crosswalks[crosswalks["type"].isin(HIVIZ_TYPES)]
    ints["has_hiviz_crosswalk"] = (count_near(ints, hiviz, RADIUS_M) > 0).astype(int)

    # 5. Bike lanes (BikePGH 2019: painted + protected; the protected file is in PA State Plane feet).
    lanes = load(RAW / "bikepgh_bike_lanes_2019.geojson")
    protected = load(RAW / "bikepgh_protected_lanes_2019" / "protected-bike-lane.zip")
    bike = pd.concat([lanes[["geometry"]], protected[["geometry"]]], ignore_index=True)
    ints["has_bike_lane"] = (count_near(ints, bike, RADIUS_M) > 0).astype(int)

    # 6. Speed humps, at the intersection and on its approaches.
    humps = load(RAW / "speed_humps.geojson")
    ints["has_speed_hump_25m"] = (count_near(ints, humps, RADIUS_M) > 0).astype(int)
    ints["has_speed_hump_100m"] = (count_near(ints, humps, HUMP_RADIUS_M) > 0).astype(int)

    # 7. Transit stops nearby (stop locations only; the trip counts in this file are 2026 service).
    transit = load(RAW / "prt_stops.geojson", columns=["stop_id"])
    ints["prt_stops_100m"] = count_near(ints, transit, TRANSIT_RADIUS_M)

    # 8. Street attributes from the city centerlines: the segments that meet at each intersection.
    cl = load(RAW / "pgh_street_centerlines.geojson", columns=["streetname", "domi_class", "num_lanes", "speedlimit"])
    zones = gpd.GeoDataFrame({"int_id": ints["int_id"]}, geometry=ints.geometry.buffer(RADIUS_M), crs=UTM)
    seg = gpd.sjoin(cl, zones, predicate="intersects")
    seg["rank"] = seg["domi_class"].map(CLASS_RANK).fillna(6)
    seg["num_lanes"] = seg["num_lanes"].where(seg["num_lanes"] > 0)  # 0 means not recorded
    rank_to_class = {v: k for k, v in CLASS_RANK.items()} | {6: "Other"}
    named = seg[~seg["streetname"].fillna("").str.upper().str.startswith("UNNAMED")]
    streets = pd.DataFrame({
        "max_lanes": seg.groupby("int_id")["num_lanes"].max(),
        "road_class_major": seg.groupby("int_id")["rank"].min().map(rank_to_class),
        "road_class_minor": seg.groupby("int_id")["rank"].max().map(rank_to_class),
        "speed_limit_max": seg.groupby("int_id")["speedlimit"].max(),
        "street_names": named.groupby("int_id")["streetname"].agg(
            lambda s: " & ".join(sorted(set(s.map(tidy_street))))),
    })
    ints = ints.merge(streets, left_on="int_id", right_index=True, how="left")

    # 9. Neighborhood for Nick's fixed effects (nearest polygon covers the few just outside the line).
    hoods = load(RAW / "neighborhoods.geojson", columns=["hood"])
    hood = gpd.sjoin_nearest(ints[["int_id", "geometry"]], hoods, how="left")
    hood = hood[~hood.index.duplicated(keep="first")]
    ints["neighborhood"] = hood["hood"]

    # 10. Column order and save.
    cols = [
        "int_id", "centroid_lat", "centroid_lon", "neighborhood", "street_names",
        "crashes_int", "crashes_all", "vru_crashes_int", "vru_crashes_all", "injury_crashes_int",
        "has_signal", "has_stop_sign", "stop_sign_count", "has_crosswalk", "has_hiviz_crosswalk",
        "has_bike_lane", "has_speed_hump_25m", "has_speed_hump_100m",
        "n_legs", "max_lanes", "road_class_major", "road_class_minor", "speed_limit_max", "osm_top_class",
        "prt_stops_100m",
    ]
    ints = ints[cols + ["geometry"]].sort_values("int_id").reset_index(drop=True)
    OUTPUTS.mkdir(parents=True, exist_ok=True)
    csv_path = OUTPUTS / "pgh_intersections.csv"
    geo_path = PROCESSED / "pgh_intersections_joined.geojson"
    ints.drop(columns="geometry").to_csv(csv_path, index=False)
    ints.to_crs(WGS84).to_file(geo_path, driver="GeoJSON", COORDINATE_PRECISION=7)

    # 11. Report.
    n = len(ints)
    print(f"\nINTERSECTIONS: {n:,}")
    print(f"CRASHES ASSIGNED (nearest intersection within {RADIUS_M} m): {len(near):,} of {len(crashes):,}"
          f" | police-coded intersection crashes: {int(pc.sum()):,} of {int((crashes['police_coded_intersection'] == 1).sum()):,}")

    print("\nOUTCOMES PER INTERSECTION (2020–2022)")
    rows = []
    for c in ["crashes_int", "crashes_all", "vru_crashes_int", "vru_crashes_all", "injury_crashes_int"]:
        x = ints[c]
        rows.append([c, x.sum(), x.mean(), x.var(), x.var() / x.mean(), (x == 0).mean(), x.max()])
    print(pd.DataFrame(rows, columns=["column", "total", "mean", "variance", "var/mean", "share zero", "max"])
          .to_string(index=False, formatters={"mean": "{:.2f}".format, "variance": "{:.2f}".format,
                                              "var/mean": "{:.1f}".format, "share zero": "{:.0%}".format}))

    print("\nSHARE OF INTERSECTIONS WITH EACH FEATURE")
    feats = ["has_signal", "has_stop_sign", "has_crosswalk", "has_hiviz_crosswalk", "has_bike_lane",
             "has_speed_hump_25m", "has_speed_hump_100m"]
    for f in feats:
        print(f"  {f:<22} {ints[f].mean():6.1%}  ({ints[f].sum():,})")
    print(f"  {'no centerline match':<22} {ints['road_class_major'].isna().mean():6.1%}  ({ints['road_class_major'].isna().sum():,})")
    print("\nROAD CLASS (busiest street at the intersection)")
    print("  " + ints["road_class_major"].value_counts(dropna=False).to_string().replace("\n", "\n  "))
    print(f"\nTRANSIT STOPS WITHIN {TRANSIT_RADIUS_M} m: mean {ints['prt_stops_100m'].mean():.2f}, "
          f"{(ints['prt_stops_100m'] > 0).mean():.0%} of intersections have at least one")
    print(f"NEIGHBORHOODS: {ints['neighborhood'].nunique()} (none missing: {ints['neighborhood'].notna().all()})")

    # Check the 2024 signal layer against what police recorded at each crash in 2020–2022.
    tcd = near[pc].merge(ints[["int_id", "has_signal", "has_stop_sign"]], on="int_id")
    print("\nCHECK: share of an intersection's crashes where police recorded a signal (TCD_TYPE 2) / stop sign (3)")
    for col, code, label in [("has_signal", 2, "signal"), ("has_stop_sign", 3, "stop sign")]:
        for v in (1, 0):
            sub = tcd[tcd[col] == v]
            print(f"  {col}={v}: police recorded a {label} at {(sub['TCD_TYPE'] == code).mean():.0%} of {len(sub):,} crashes")

    show = ["int_id", "street_names", "neighborhood", "crashes_int", "crashes_all", "vru_crashes_int",
            "has_signal", "has_crosswalk", "n_legs"]
    top = ints.sort_values(["crashes_int", "crashes_all"], ascending=False).head(15)
    print("\nTOP 15 BY POLICE-CODED INTERSECTION CRASHES")
    print(top[show].to_string(index=False, max_colwidth=38))

    # Compare with Nick's earlier top 15 (raw counts within 15 m of signal locations).
    if NICK_TOP15.exists():
        nick = pd.read_csv(NICK_TOP15)
        nick = gpd.GeoDataFrame(nick, geometry=gpd.points_from_xy(nick["longitude"], nick["latitude"]), crs=WGS84).to_crs(UTM)
        ranked = ints.sort_values(["crashes_int", "crashes_all"], ascending=False).reset_index(drop=True)
        ranked["our_rank"] = ranked.index + 1
        m = gpd.sjoin_nearest(nick[["rank", "intersection", "crash_count", "geometry"]],
                              ranked[["int_id", "our_rank", "crashes_int", "geometry"]], max_distance=30, how="left")
        m = m[~m.index.duplicated(keep="first")]
        print(f"\nNICK'S TOP 15 vs THIS TABLE: {int((m['our_rank'] <= 15).sum())} of 15 are also in our top 15; "
              f"{int((m['our_rank'] <= 50).sum())} in our top 50")
        print(m[["rank", "intersection", "crash_count", "our_rank", "crashes_int"]]
              .rename(columns={"rank": "nick_rank", "crash_count": "nick_count"}).to_string(index=False))

    print("\nSAVED")
    for p in (csv_path, geo_path):
        print(f"  {p.relative_to(REPO)}  ({p.stat().st_size / 1e6:.1f} MB)")

    # 12. Preview: crashes per intersection (map) and feature prevalence (bars).
    fig, axes = plt.subplots(1, 2, figsize=(15, 7), gridspec_kw={"width_ratios": [1.5, 1]})
    city = load(RAW / "pgh_city_boundary.geojson").dissolve()
    city.boundary.plot(ax=axes[0], color="black", linewidth=0.6)
    bins = [(-1, 0, "0", "#d9d9d9", 1), (0, 2, "1–2", "#fdae61", 4), (2, 5, "3–5", "#f46d43", 10),
            (5, 999, "6+", "#a50026", 22)]
    for lo, hi, label, color, size in bins:
        sub = ints[(ints["crashes_int"] > lo) & (ints["crashes_int"] <= hi)]
        sub.plot(ax=axes[0], color=color, markersize=size, label=f"{label} ({len(sub):,})", zorder=hi)
    axes[0].set_title("Police-coded intersection crashes per intersection, 2020–2022")
    axes[0].legend(title="Crashes", loc="lower left")
    axes[0].set_axis_off()

    shares = ints[feats].mean().sort_values()
    axes[1].barh([s.replace("has_", "").replace("_", " ") for s in shares.index], shares.values * 100, color="#4c78a8")
    for i, v in enumerate(shares.values):
        axes[1].text(v * 100 + 0.5, i, f"{v:.1%}", va="center", fontsize=9)
    axes[1].set_xlabel("% of intersections")
    axes[1].set_title("Infrastructure at intersections (within 25 m; humps also 100 m)")
    axes[1].spines[["top", "right"]].set_visible(False)

    fig.tight_layout(rect=(0, 0, 1, 0.95))
    png = PROCESSED / "03_intersections_preview.png"
    fig.savefig(png, dpi=130)
    print(f"  {png.relative_to(REPO)}")


if __name__ == "__main__":
    main()

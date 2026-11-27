"""
Step 2: build Pittsburgh's intersections from OpenStreetMap, as the street network stood on 1 Jul 2021.

Inputs
    macro_gis/data/raw/pgh_city_boundary.geojson
    macro_gis/data/raw/city_markings_img.geojson            (city crosswalk inventory; preview only)
    macro_gis/data/processed/pgh_crashes_2020_2022.geojson  (from step 1; checks and preview only)
    OpenStreetMap via the Overpass API, dated 1 Jul 2021 (the middle of the 2020–2022 crash window)

Outputs (macro_gis/data/processed/, which Git ignores)
    pgh_intersections_2021.geojson        one point per intersection: int_id, lat/lon, n_legs, osm_top_class
    pgh_intersection_buffers_25m.geojson  the same intersections as 25 m circles
    02_intersections_preview.png          citywide map + downtown close-up with crosswalks and crashes

How this feeds the analysis
    Stage 1: every intersection here becomes one row of Nick's table, including those with no crashes.
             n_legs is a control variable. Step 3 attaches crashes, signals, stop signs, crosswalks and
             the other layers to these rows. The 25 m circles are the zones used to read the risk raster
             when ranking hotspots.
    Stage 2: int_id is the key Joon uses to match each Argoverse scenario to its nearest intersection.

Why crosswalks are not taken from OSM: OSM in mid-2021 shows a marked crossing at 8% of intersections,
while the city's own markings inventory (2021) shows one at 24%. OSM was still being mapped, so the city
inventory is used instead (joined in step 3).

Run from anywhere:  macro_gis/.venv/bin/python macro_gis/scripts/02_build_intersections_osm.py
The first run downloads from Overpass (a few minutes); later runs reuse the cache.
"""

from pathlib import Path

import geopandas as gpd
import matplotlib
import osmnx as ox
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt

# --- Settings ------------------------------------------------------------------------------------------

REPO = Path(__file__).resolve().parents[2]
RAW = REPO / "macro_gis" / "data" / "raw"
OUT_DIR = REPO / "macro_gis" / "data" / "processed"
BOUNDARY_FILE = RAW / "pgh_city_boundary.geojson"
MARKINGS_FILE = RAW / "city_markings_img.geojson"
CRASHES_FILE = OUT_DIR / "pgh_crashes_2020_2022.geojson"

SNAPSHOT_DATE = "2021-07-01T00:00:00Z"  # infrastructure as of mid-2021 (team decision, 26 Nov)
UTM = "EPSG:26917"
WGS84 = "EPSG:4326"
QUERY_PAD_M = 300        # download a little past the city line so boundary intersections keep all their legs
KEEP_ZONE_M = 100        # keep intersections up to 100 m outside the city, matching step 1's crash filter
CONSOLIDATE_M = 15       # OSMnx circles each node with this radius and merges overlaps, so the separate
                         # nodes of one junction (e.g. a divided road) up to ~30 m apart become one intersection
BUFFER_M = 25            # the radius agreed for counting crashes and features at an intersection

FREEWAY = {"motorway", "motorway_link"}
CLASS_ORDER = ["motorway", "trunk", "primary", "secondary", "tertiary",
               "unclassified", "residential", "living_street"]

# Ask Overpass for the map as it was on SNAPSHOT_DATE. {timeout} and {maxsize} are filled in by OSMnx.
ox.settings.overpass_settings = f'[out:json][timeout:{{timeout}}]{{maxsize}}[date:"{SNAPSHOT_DATE}"]'
ox.settings.requests_timeout = 600
ox.settings.use_cache = True
ox.settings.cache_folder = str(OUT_DIR / "osmnx_cache")


def as_list(value):
    """OSM tags can hold one value or a list (when merged edges disagree)."""
    return value if isinstance(value, list) else [value]


def top_class(classes: set) -> str:
    """Highest-ranking road type among the streets meeting at a node (ramps count as their road)."""
    base = {c.replace("_link", "") for c in classes if isinstance(c, str)}
    ranked = [c for c in CLASS_ORDER if c in base]
    return ranked[0] if ranked else "other"


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    funnel = []

    # 1. Study area: the city boundary, plus padding for the download.
    city = gpd.read_file(BOUNDARY_FILE).to_crs(UTM).dissolve().geometry.iloc[0]
    keep_zone = city.buffer(KEEP_ZONE_M)
    query_area = gpd.GeoSeries([city.buffer(QUERY_PAD_M)], crs=UTM).to_crs(WGS84).iloc[0]

    # 2. Drivable street network as of mid-2021, projected to meters.
    print(f"Downloading the OSM drive network as of {SNAPSHOT_DATE[:10]} ...")
    G = ox.graph_from_polygon(query_area, network_type="drive")
    G = ox.project_graph(G, to_crs=UTM)
    funnel.append(("OSM street nodes (drive network)", G.number_of_nodes()))

    # 3. Merge the several OSM nodes that make up one real junction. Dead ends are dropped first.
    G = ox.consolidate_intersections(G, tolerance=CONSOLIDATE_M, rebuild_graph=True, dead_ends=False)
    nodes, edges = ox.graph_to_gdfs(G)
    funnel.append((f"After merging nodes within ~{2 * CONSOLIDATE_M} m", len(nodes)))

    # 4. Which road types meet at each node.
    e = edges.reset_index()[["u", "v", "highway"]]
    e["highway"] = e["highway"].apply(as_list)
    e = e.explode("highway")
    incident = pd.concat([
        e[["u", "highway"]].rename(columns={"u": "node"}),
        e[["v", "highway"]].rename(columns={"v": "node"}),
    ])
    classes = incident.groupby("node")["highway"].agg(lambda s: set(s.dropna())).reindex(nodes.index)
    nodes["osm_top_class"] = classes.apply(lambda s: top_class(s) if isinstance(s, set) else "other")
    freeway_only = classes.apply(lambda s: isinstance(s, set) and 0 < len(s) and s <= FREEWAY)

    # 5. Keep real intersections: 3+ streets meet, not a freeway-only junction, inside the study area.
    ints = nodes[nodes["street_count"] >= 3]
    funnel.append(("Where 3 or more streets meet", len(ints)))
    ints = ints[~freeway_only.reindex(ints.index)]
    funnel.append(("Not a freeway-only junction", len(ints)))
    ints = ints[ints.within(keep_zone)].copy()
    funnel.append((f"Inside the city (+{KEEP_ZONE_M} m)", len(ints)))

    # 6. Stable IDs (north to south, then west to east) and tidy columns for Nick, Joon and ArcGIS.
    ints = ints.sort_values(["y", "x"], ascending=[False, True])
    ints["int_id"] = range(1, len(ints) + 1)
    ints["n_legs"] = ints["street_count"].astype(int)
    lonlat = ints.geometry.to_crs(WGS84)
    ints["centroid_lat"] = lonlat.y.round(6)
    ints["centroid_lon"] = lonlat.x.round(6)
    ints = ints[["int_id", "centroid_lat", "centroid_lon", "n_legs", "osm_top_class", "geometry"]]
    ints = ints.reset_index(drop=True)
    buffers = ints.copy()
    # 32-sided circles: within 1% of a true circle's area, and a much smaller file for ArcGIS Online.
    buffers["geometry"] = ints.geometry.buffer(BUFFER_M, quad_segs=8)

    # 7. Save. 7 decimal places of lat/lon is about 1 cm: plenty here, and it keeps files small.
    paths = {
        "intersections": OUT_DIR / "pgh_intersections_2021.geojson",
        "buffers": OUT_DIR / f"pgh_intersection_buffers_{BUFFER_M}m.geojson",
    }
    for key, layer in [("intersections", ints), ("buffers", buffers)]:
        layer.to_crs(WGS84).to_file(paths[key], driver="GeoJSON", COORDINATE_PRECISION=7)

    # 8. Report.
    print("\nINTERSECTION FUNNEL")
    for step, n in funnel:
        print(f"  {step:<40} {n:>7,}")
    print("\nLEGS PER INTERSECTION")
    print("  " + ints["n_legs"].value_counts().sort_index().to_string().replace("\n", "\n  "))
    print("\nHIGHEST ROAD TYPE AT EACH INTERSECTION (OSM)")
    print("  " + ints["osm_top_class"].value_counts().to_string().replace("\n", "\n  "))

    # Check against step 1: police-coded intersection crashes should mostly sit near an intersection.
    crashes = None
    if CRASHES_FILE.exists():
        crashes = gpd.read_file(CRASHES_FILE).to_crs(UTM)
        near = gpd.sjoin_nearest(crashes, ints[["int_id", "geometry"]], distance_col="dist_m")
        near = near[~near.index.duplicated()]
        print(f"\nCHECK AGAINST STEP 1 CRASHES: share within {BUFFER_M} m of an intersection")
        for label, mask in [("Police coded at intersection", near["police_coded_intersection"] == 1),
                            ("Police coded mid-block", near["police_coded_intersection"] == 0)]:
            d = near.loc[mask, "dist_m"]
            print(f"  {label:<30} {(d <= BUFFER_M).mean():.0%}  (n={len(d):,}, median {d.median():.0f} m)")

    print("\nSAVED")
    for path in paths.values():
        print(f"  {path.relative_to(REPO)}  ({path.stat().st_size / 1e6:.1f} MB)")

    # 9. Preview: citywide intersections by legs; downtown close-up with circles, crosswalks, crashes.
    fig, axes = plt.subplots(1, 2, figsize=(15, 7.5))
    city_gs = gpd.GeoSeries([city], crs=UTM)
    colors = {3: "#9e9e9e", 4: "#1f77b4"}
    for ax in axes:
        city_gs.boundary.plot(ax=ax, color="black", linewidth=0.6)
        ax.set_axis_off()
    for legs, group in ints.groupby(ints["n_legs"].clip(upper=5)):
        label = f"{legs}+ legs" if legs == 5 else f"{legs} legs"
        group.plot(ax=axes[0], markersize=1.5, color=colors.get(legs, "#d62728"),
                   label=f"{label} ({len(group):,})")
    axes[0].set_title(f"Intersections as of {SNAPSHOT_DATE[:10]} ({len(ints):,})")
    axes[0].legend(loc="lower left", markerscale=6)

    # Downtown window around Grant St & Seventh Ave (Nick's #1 crash location).
    centre = gpd.GeoSeries.from_xy([-79.99498], [40.44215], crs=WGS84).to_crs(UTM).iloc[0]
    half = 450
    ax = axes[1]
    edges.plot(ax=ax, color="#cccccc", linewidth=0.8, zorder=1)
    buffers.boundary.plot(ax=ax, color="#1f77b4", linewidth=0.8, zorder=2)
    ints.plot(ax=ax, color="#1f77b4", markersize=6, zorder=3)
    if MARKINGS_FILE.exists():
        marks = gpd.read_file(MARKINGS_FILE).to_crs(UTM)
        cross = marks[marks["type"].str.startswith("Crosswalk", na=False)]
        hiviz = cross["type"].isin(["Crosswalk - Continental", "Crosswalk - Ladder"])
        cross[~hiviz].plot(ax=ax, color="#2ca02c", linewidth=2, zorder=4, label="Crosswalk (city, standard)")
        cross[hiviz].plot(ax=ax, color="#9467bd", linewidth=2, zorder=4, label="Crosswalk (city, high-visibility)")
    if crashes is not None:
        crashes.plot(ax=ax, color="#d62728", markersize=5, alpha=0.7, zorder=5, label="Crash (step 1)")
    ax.set_xlim(centre.x - half, centre.x + half)
    ax.set_ylim(centre.y - half, centre.y + half)
    ax.set_title(f"Downtown close-up: {BUFFER_M} m circles, crosswalks, crashes")
    ax.legend(loc="lower left")

    fig.tight_layout(rect=(0, 0, 1, 0.95))  # leave room so the titles aren't clipped
    png = OUT_DIR / "02_intersections_preview.png"
    fig.savefig(png, dpi=130)
    print(f"  {png.relative_to(REPO)}")


if __name__ == "__main__":
    main()

"""MCP server for the deforestation detection project.

Exposes the results in outputs/ as tools a language model can call, so that
every answer about an alert comes with what PRODES says about it and how far
alerts of that kind can be trusted.
Run by Claude Desktop over stdio: nothing may be printed to stdout.
"""

import json
from pathlib import Path
from typing import Annotated, Literal

import geopandas as gpd
from fastmcp import FastMCP
from pydantic import Field
from shapely.geometry import Point, box

PROJECT_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PROJECT_DIR / "outputs"
METRIC_CRS = "EPSG:32720"   # UTM 20S, the grid of the analysis, for distances in metres

LandStatus = Literal["mapped by PRODES", "cleared before 2019", "forest, not in PRODES", "mixed"]
Severity = Literal["high", "medium", "low"]



def read_json(name):
    with open(OUTPUT_DIR / name) as f:
        return json.load(f)


# Loaded once, when Claude Desktop starts the server.
ALERTS = gpd.read_file(OUTPUT_DIR / "alerts_enriched.geojson").sort_values("priority_rank")
ALERTS_METRIC = ALERTS.to_crs(METRIC_CRS)
VALIDATION = read_json("validation_metrics.json")
PRITHVI = read_json("prithvi_metrics.json")
RUN_INFO = read_json("run_info.json")

lon_min, lat_min, lon_max, lat_max = RUN_INFO["aoi_bbox"]
mcp = FastMCP(
    name="deforestation",
    instructions=(
        "Results of a deforestation analysis of one 10 x 10 km area in Rondonia, Brazil "
        f"(longitude {lon_min:.2f} to {lon_max:.2f}, latitude {lat_min:.2f} to {lat_max:.2f}), comparing "
        "the dry seasons of 2018 and 2023. Use these tools for any question about the analysis, the "
        "alerts or this area. They say nothing about other places or years: for such questions, first "
        "say that the analysis does not cover them. Report numbers as the tools return them instead of "
        "computing new ones, and keep the caveats the tools give."
    ),
)


def ordinal(n):
    """1 -> '1st', 2 -> '2nd', 11 -> '11th', 91 -> '91st'."""
    n = int(round(n))
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def trust_note(alert):
    """How far this alert can be trusted, given the ground it sits on."""
    status = alert["land_status"]
    if status == "mapped by PRODES":
        years = alert["prodes_years"] or "2019-2023"
        return (f"Confirmed: {alert['prodes_share']:.0%} of the alert matches a clearing that PRODES, "
                f"the official map of INPE, recorded in {years}.")
    if status == "cleared before 2019":
        share = VALIDATION["false_positives"]["share_on_land_already_cleared"]
        return (f"Cannot be confirmed: {alert['cleared_before_2019_share']:.0%} of the alert lies on land "
                "that PRODES already considered deforested before 2019 and no longer monitors. The drop in "
                "vegetation is real, but it most likely reflects a change of land use (pasture, crops, "
                "regrowth cleared again) rather than new deforestation of primary forest. "
                f"{share:.0%} of all alert area not confirmed by PRODES falls on such land.")
    if status == "forest, not in PRODES":
        precision = VALIDATION["pixel_level"]["precision_forest_only"]
        return (f"Disagreement: {alert['forest_share']:.0%} of the alert lies on forest that PRODES still "
                f"monitors, and PRODES did not map it as cleared. On this kind of land {precision:.0%} of "
                "alert area does match PRODES, so this alert is in the minority that does not. Possible "
                "reasons: a clearing under the 6.25 ha PRODES minimum, degradation or fire without clear-cut, "
                "a clearing in August-September 2023 (PRODES year 2024), or the edge of a mapped clearing.")
    return ("Mixed: no single situation covers half of the alert "
            f"(PRODES {alert['prodes_share']:.0%}, cleared before 2019 {alert['cleared_before_2019_share']:.0%}, "
            f"forest {alert['forest_share']:.0%}).")


def alert_summary(alert):
    """The few fields needed to list an alert."""
    return {
        "priority_rank": int(alert["priority_rank"]),
        "area_ha": round(float(alert["area_ha"]), 2),
        "severity": alert["severity"],
        "land_status": alert["land_status"],
        "centroid_lat": round(float(alert["centroid_lat"]), 5),
        "centroid_lon": round(float(alert["centroid_lon"]), 5),
    }


def alert_card(alert):
    """Everything known about one alert, with the judgement on how far to trust it."""
    return {
        **alert_summary(alert),
        "mean_ndvi_change": round(float(alert["mean_ndvi_change"]), 3),
        "mean_bare_soil_change": round(float(alert["mean_bsi_change"]), 3),
        "share_mapped_by_prodes_2019_2023": float(alert["prodes_share"]),
        "share_cleared_before_2019": float(alert["cleared_before_2019_share"]),
        "share_forest_not_in_prodes": float(alert["forest_share"]),
        "prodes_years_touching_the_alert": alert["prodes_years"] or None,
        "how_far_to_trust": trust_note(alert),
        "foundation_model": (
            f"Prithvi-EO-2.0 places the 480 m area around this alert at the "
            f"{ordinal(alert['prithvi_percentile'])} percentile of change in the study area "
            f"(cosine distance {alert['prithvi_distance']:.3f}). This measures how much the area changed, "
            "in any direction, at a coarse scale: it is context, not a confirmation."
        ),
        "change_between": f"{RUN_INFO['baseline_window']} and {RUN_INFO['recent_window']}",
    }


@mcp.tool
def list_alerts(
    land_status: Annotated[LandStatus | None, Field(
        description="Keep only alerts on this kind of ground. 'mapped by PRODES' are confirmed "
                    "deforestation; 'cleared before 2019' are on land deforested long ago.")] = None,
    severity: Annotated[Severity | None, Field(description="Keep only alerts of this severity.")] = None,
    min_area_ha: Annotated[float, Field(ge=0, description="Minimum alert area in hectares.")] = 0,
    limit: Annotated[int, Field(ge=1, le=129, description="Maximum number of alerts returned.")] = 10,
) -> dict:
    """List the vegetation loss alerts, largest first, optionally filtered.

    Use this to answer questions such as "which are the largest alerts?",
    "show me the confirmed deforestation", or "how many alerts are on forest?".
    The priority rank orders alerts by area only: a large alert can sit on
    land cleared long ago, so check land_status before calling an alert
    deforestation. Use get_alert for the full card of one alert.
    """
    selected = ALERTS
    if land_status is not None:
        selected = selected[selected["land_status"] == land_status]
    if severity is not None:
        selected = selected[selected["severity"] == severity]
    selected = selected[selected["area_ha"] >= min_area_ha]

    by_status = ALERTS.groupby("land_status")["area_ha"].agg(["count", "sum"]).round(1)
    return {
        "matching_alerts": int(len(selected)),
        "matching_area_ha": round(float(selected["area_ha"].sum()), 1),
        "alerts": [alert_summary(row) for _, row in selected.head(limit).iterrows()],
        "all_alerts_by_land_status": {status: {"alerts": int(row["count"]), "area_ha": float(row["sum"])}
                                      for status, row in by_status.iterrows()},
        "note": "Ranks follow area, not the likelihood of deforestation. Most of the largest alerts "
                "lie on land PRODES considered cleared before 2019.",
    }


@mcp.tool
def get_alert(
    priority_rank: Annotated[int, Field(ge=1, description="The alert's priority rank, 1 being the largest.")],
) -> dict:
    """Full card of one alert: size, location, NDVI drop, what PRODES says, and how far to trust it.

    Use this whenever the user asks about a specific alert, or whether an
    alert is real deforestation. Always report the how_far_to_trust field
    together with the numbers.
    """
    match = ALERTS[ALERTS["priority_rank"] == priority_rank]
    if match.empty:
        return {"error": f"No alert with priority rank {priority_rank}. Ranks go from 1 to {len(ALERTS)}."}
    return alert_card(match.iloc[0])


@mcp.tool
def whats_here(
    lat: Annotated[float, Field(ge=-90, le=90, description="Latitude in decimal degrees (WGS84).")],
    lon: Annotated[float, Field(ge=-180, le=180, description="Longitude in decimal degrees (WGS84).")],
) -> dict:
    """What the analysis says about a point: the alert it falls in, or the nearest one.

    Use this when the user gives coordinates or asks about a specific place.
    Points outside the study area are reported as such: the analysis says
    nothing about them.
    """
    lon_min, lat_min, lon_max, lat_max = RUN_INFO["aoi_bbox"]
    if not (lon_min <= lon <= lon_max and lat_min <= lat <= lat_max):
        point = gpd.GeoSeries([Point(lon, lat)], crs="EPSG:4326").to_crs(METRIC_CRS).iloc[0]
        study_area = gpd.GeoSeries([box(lon_min, lat_min, lon_max, lat_max)],
                                   crs="EPSG:4326").to_crs(METRIC_CRS).iloc[0]
        return {
            "inside_study_area": False,
            "distance_to_study_area_km": round(point.distance(study_area) / 1000, 1),
            "message": f"The point is outside the study area (longitude {lon_min} to {lon_max}, "
                       f"latitude {lat_min} to {lat_max}). No alert can be reported there, and the "
                       "absence of an alert does not mean the forest is intact.",
        }

    point = gpd.GeoSeries([Point(lon, lat)], crs="EPSG:4326").to_crs(METRIC_CRS).iloc[0]
    inside = ALERTS_METRIC[ALERTS_METRIC.contains(point)]
    if not inside.empty:
        rank = int(inside.iloc[0]["priority_rank"])
        return {"inside_study_area": True, "inside_alert": True,
                "alert": alert_card(ALERTS[ALERTS["priority_rank"] == rank].iloc[0])}

    distances = ALERTS_METRIC.distance(point)
    nearest = distances.idxmin()
    return {
        "inside_study_area": True,
        "inside_alert": False,
        "message": "No alert at this point: the NDVI did not drop by more than 0.25 over at least half "
                   "a hectare here. Clearings that had already regrown by 2023 can be missed.",
        "nearest_alert_distance_m": round(float(distances[nearest])),
        "nearest_alert": alert_summary(ALERTS.loc[nearest]),
    }


@mcp.tool
def get_validation_summary() -> dict:
    """How far the alerts can be trusted overall, measured against PRODES, INPE's official map.

    Use this when the user asks how accurate or reliable the alerts are, how
    much deforestation the system misses, or how it compares with official
    data. Report precision on the whole area and on forest together, with the
    explanation of why they differ, and the caveats.
    """
    pixel = VALIDATION["pixel_level"]
    fp = VALIDATION["false_positives"]
    polygons = VALIDATION["polygon_level"]
    return {
        "reference": VALIDATION["reference"],
        "precision_whole_area": pixel["precision_whole_aoi"],
        "precision_on_land_prodes_still_monitors": pixel["precision_forest_only"],
        "recall": pixel["recall"],
        "share_of_study_area_not_forest_in_2018": VALIDATION["grid"]["share_not_forest_in_2018"],
        "share_of_unconfirmed_alert_area_on_land_cleared_before_2019": fp["share_on_land_already_cleared"],
        "prodes_clearings_2019_2023": {
            "total": polygons["n_polygons"],
            "found_at_least_half": polygons["found"],
            "partly_found": polygons["partial"],
            "missed_entirely": polygons["missed"],
            "mean_ndvi_change_inside": polygons["mean_ndvi_change_by_group"],
        },
        "how_to_read": (
            "Precision is the share of alert area that PRODES also maps; recall is the share of PRODES "
            "clearings the alerts find. Over the whole area precision looks low, but most of the area was "
            "already deforested before 2019 and PRODES no longer monitors it, so most unconfirmed alerts "
            "fall where PRODES cannot judge. On land PRODES still monitors, most alert area matches a "
            "PRODES clearing. The main weakness is recall: clearings whose vegetation had grown back by "
            "2023 show almost no NDVI change and are missed by a comparison of two dry-season composites, each the median of five scenes."
        ),
        "caveats": VALIDATION["caveats"],
    }


@mcp.tool
def get_foundation_model_summary() -> dict:
    """What the Prithvi-EO-2.0 foundation model adds to the analysis, and its limits.

    Use this when the user asks about Prithvi, the foundation model, AI or
    embeddings, or whether the model confirms the alerts. The model was used
    without any training on deforestation: its distance measures how much a
    480 m area changed, in any direction, not whether forest was cleared.
    """
    missed = [c for c in PRITHVI["missed_clearings"] if c["coverage"] == 0]
    return {
        "model": PRITHVI["model"],
        "patch_size_m": PRITHVI["patch_size_m"],
        "what_the_distance_measures": (
            "The cosine distance between the model's descriptions of the same 480 m area in 2018 and "
            "2023. It has no sign: it follows how much an area changed, including regrowth and crop "
            "cycles, not only vegetation loss."
        ),
        "spearman_with_ndvi_change": PRITHVI["spearman_with_ndvi"],
        "auc_separating_prodes_patches": {
            "all_patches": PRITHVI["auc_all_patches"],
            "patches_mostly_forest_in_2018": PRITHVI["auc_mostly_forest"],
            "patches_with_a_prodes_clearing": PRITHVI["n_patches_with_prodes"],
            "patches_with_a_prodes_clearing_mostly_forest": PRITHVI.get("n_patches_with_prodes_mostly_forest"),
            "how_to_read": "AUC is the probability that an area containing a PRODES clearing ranks above "
                           "one without: 0.5 is chance, 1 is perfect. With a few dozen positive areas, "
                           "differences of a few hundredths are within the noise.",
        },
        "clearings_missed_entirely_by_the_alerts": missed,
        "limits": [
            "A 480 m area covers about 23 ha, so a clearing is mixed with its surroundings.",
            "Used zero-shot, without fine-tuning, on Sentinel-2 L2A rather than the HLS data it was "
            "trained on.",
            "A high distance means something changed, not that forest was cleared.",
        ],
    }


@mcp.tool
def get_run_info() -> dict:
    """Describe how the detection was run: study area, dates, scenes and thresholds.

    Use this when the user asks where or when the analysis was done, which
    satellite images were used, or which settings produced the alerts.
    """
    return RUN_INFO


if __name__ == "__main__":
    mcp.run()

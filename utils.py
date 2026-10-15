"""Functions for the deforestation detection and validation notebooks.

Grouped in the order the pipeline uses them: finding scenes, cleaning
pixels, computing indices, turning the change map into alerts, and
comparing the alerts with PRODES.
"""

import warnings

import folium
import geopandas as gpd
import matplotlib
import matplotlib.colors as mcolors
import numpy as np
import odc.stac
import pandas as pd
import rioxarray  # noqa: F401  (registers the .rio accessor)
import xarray as xr
from rasterio import features
from shapely.geometry import box, mapping, shape


# ---------------------------------------------------------------------------
# Finding and loading scenes
# ---------------------------------------------------------------------------

def search_wide_area(catalog, search_area, time_window,
                     collection="sentinel-2-l2a", max_cloud=30):
    """Return all scenes touching a wide area around the site of interest.

    Used once, before the AOI exists, to see which tiles cover the region.
    `search_area` is a shapely geometry in EPSG:4326. Cloud cover refers to
    the whole tile, not to the area.
    """
    search = catalog.search(
        collections=[collection],
        intersects=mapping(search_area),
        datetime=time_window,
        query={"eo:cloud_cover": {"lt": max_cloud}},
    )
    return list(search.items())


def choose_aoi(items, half_size=0.05, inset_deg=0.15):
    """Place a square AOI well inside the footprint of the largest scene.

    The largest footprint is usually the least clipped by the swath edge.
    Shrinking it by `inset_deg` before picking the centre keeps the AOI away
    from the edge, so that a single scene can cover it entirely.
    Returns the bbox as [lon_min, lat_min, lon_max, lat_max] and the same
    square as a GeoJSON dict.
    """
    largest = max(items, key=lambda item: shape(item.geometry).area)
    scene_footprint = shape(largest.geometry)
    centre = scene_footprint.buffer(-inset_deg).representative_point()

    bbox = [centre.x - half_size, centre.y - half_size,
            centre.x + half_size, centre.y + half_size]
    return bbox, mapping(box(*bbox))


def find_candidates(catalog, aoi_geojson, time_window,
                    collection="sentinel-2-l2a", max_cloud=40, pool_size=30):
    """Return the least cloudy scenes over the AOI in a time window.

    A cheap first filter based only on metadata: nothing is downloaded.
    Scenes are sorted by tile-level cloud cover and the first `pool_size`
    are kept for the more expensive per-AOI check in `pick_scenes`.
    """
    search = catalog.search(
        collections=[collection],
        intersects=aoi_geojson,
        datetime=time_window,
        query={"eo:cloud_cover": {"lt": max_cloud}},
    )
    found = list(search.items())
    found.sort(key=lambda item: item.properties["eo:cloud_cover"])
    return found[:pool_size]


def clear_fraction_in_aoi(item, bbox, bad_classes):
    """Share of AOI pixels that are usable in a scene, between 0 and 1.

    Reads only the SCL band at its native 20 m, which is enough to judge
    clouds and much lighter than loading the full scene. A pixel counts as
    usable if its SCL class is not in `bad_classes`.
    """
    scl = odc.stac.load([item], bands=["SCL"], bbox=bbox,
                        resolution=20, chunks={})
    scl = scl.isel(time=0)["SCL"]
    return float((~scl.isin(bad_classes)).mean().values)


def pick_scenes(candidates, bbox, bad_classes, how_many=5, min_clear=0.6):
    """Select the scenes with the most usable pixels over the AOI.

    When the same acquisition appears more than once (reprocessed by ESA),
    only the version with the highest id is kept. If fewer than `how_many`
    scenes reach `min_clear`, the best available ones are returned instead,
    with a warning.
    """
    scored = [(clear_fraction_in_aoi(item, bbox, bad_classes), item)
              for item in candidates]

    by_datetime = {}
    for clear_fraction, item in scored:
        dt = item.properties["datetime"]
        if dt not in by_datetime or item.id > by_datetime[dt][1].id:
            by_datetime[dt] = (clear_fraction, item)
    unique_scenes = list(by_datetime.values())

    unique_scenes.sort(key=lambda pair: pair[0], reverse=True)
    good_ones = [item for clear_fraction, item in unique_scenes
                 if clear_fraction >= min_clear]

    if len(good_ones) < how_many:
        warnings.warn(
            f"only {len(good_ones)} scenes above {min_clear:.0%} clear, "
            "using the best available instead"
        )
        return [item for _, item in unique_scenes[:how_many]]
    return good_ones[:how_many]


def load_scene(item, bbox, bands, resolution=10):
    """Prepare the pixels of one scene over the AOI, without downloading yet.

    All bands are put on the same 10 m grid so they can be combined pixel
    by pixel. Loading is lazy: the data is fetched only when values are
    actually needed (a computation, a plot, `.compute()`).
    Returns a Dataset with dimensions (y, x), one variable per band.
    """
    ds = odc.stac.load([item], bands=bands, bbox=bbox,
                       resolution=resolution, chunks={})
    return ds.isel(time=0)


# ---------------------------------------------------------------------------
# Cleaning pixels and computing indices
# ---------------------------------------------------------------------------

def mask_and_scale(scene_data, item, bad_classes=(0, 1, 3, 8, 9, 10, 11)):
    """Turn raw Sentinel-2 values into clean surface reflectance.

    Pixels flagged by the SCL band as clouds, shadows, snow, saturated or
    missing are set to NaN. Scenes processed with baseline 04.00 or later
    carry a radiometric offset of 1000 that Planetary Computer does not
    remove, so it is subtracted here before scaling to the 0-1 range.
    Returns a Dataset with the seven reflectance bands, without SCL.
    """
    scl = scene_data["SCL"]
    is_clear = ~scl.isin(list(bad_classes))

    processing_baseline = item.properties.get("s2:processing_baseline", "00.00")
    needs_offset = float(processing_baseline.split(".")[0]) >= 4  # baseline 04.00 onward

    raw = scene_data[["B02", "B03", "B04", "B08", "B8A", "B11", "B12"]].astype("float32")
    if needs_offset:
        raw = raw - 1000  # BOA_ADD_OFFSET, standard value since processing baseline 04.00

    reflectance = raw / 10000.0
    reflectance = reflectance.where(is_clear)
    return reflectance


def valid_observation_count(period_name, scenes, band="B04"):
    """Count, for each pixel, how many scenes of a period have usable data.

    A pixel is usable in a scene if it survived masking, that is if it is
    not NaN. One band is enough to check, since the mask is the same for
    all of them. Returns a DataArray (y, x) of integers.
    """
    period_scenes = [info["clean"][band] for info in scenes.values() if info["period"] == period_name]
    stack = xr.concat(period_scenes, dim="time")
    return (~stack.isnull()).sum(dim="time")


def ndvi(reflectance):
    """Normalized Difference Vegetation Index, from NIR and red.

    High over dense green vegetation, close to zero over bare soil,
    negative over water.
    """
    nir, red = reflectance["B08"], reflectance["B04"]
    return (nir - red) / (nir + red)


def ndmi(reflectance):
    """Normalized Difference Moisture Index, from NIR and SWIR.

    Tracks the water content of the canopy, so it reacts to drying and
    to disturbance that leaves the vegetation green but thinner.
    """
    nir, swir1 = reflectance["B08"], reflectance["B11"]
    return (nir - swir1) / (nir + swir1)


def evi(reflectance):
    """Enhanced Vegetation Index, from NIR, red and blue.

    Like NDVI it measures vegetation vigour, but it saturates less over
    dense canopy and is less affected by the atmosphere and the soil.
    """
    nir, red, blue = reflectance["B08"], reflectance["B04"], reflectance["B02"]
    return 2.5 * (nir - red) / (nir + 6 * red - 7.5 * blue + 1)


def bsi(reflectance):
    """Bare Soil Index, from SWIR, red, NIR and blue.

    Rises where soil is exposed and falls over vegetation, so it moves
    in the opposite direction to NDVI when land is cleared.
    """
    swir1, red, nir, blue = reflectance["B11"], reflectance["B04"], reflectance["B08"], reflectance["B02"]
    return ((swir1 + red) - (nir + blue)) / ((swir1 + red) + (nir + blue))


def false_color(reflectance):
    """Build a false colour image (NIR, red, green) ready for imshow.

    Each band is stretched between its own 2nd and 98th percentile and
    clipped to 0-1, which gives good contrast but means colours cannot
    be compared between two images. Masked pixels are shown as black.
    Returns a numpy array of shape (y, x, 3).
    """
    def normalize(band):
        values = band.values
        valid = np.isfinite(values)
        if valid.sum() == 0:
            return np.zeros_like(values)
        low, high = np.nanpercentile(values[valid], [2, 98])
        if high == low:
            high = low + 1e-6
        stretched = np.clip((values - low) / (high - low), 0, 1)
        return np.nan_to_num(stretched, nan=0.0)

    red = normalize(reflectance["B08"])
    green = normalize(reflectance["B04"])
    blue = normalize(reflectance["B03"])
    return np.dstack([red, green, blue])


def period_median(index_name, period_name, scenes):
    """Collapse the scenes of a period into one map of an index.

    Takes the per-pixel median across the scenes, ignoring masked values.
    The median fills the gaps left by clouds and is not thrown off by a
    single odd acquisition. Returns a DataArray (y, x).
    """
    period_scenes = [info[index_name] for info in scenes.values() if info["period"] == period_name]
    stack = xr.concat(period_scenes, dim="time")
    return stack.median(dim="time", skipna=True)


# ---------------------------------------------------------------------------
# From change map to alerts
# ---------------------------------------------------------------------------

def vectorize_mask(mask, grid):
    """Turn a boolean raster into polygons, one per patch of True pixels.

    `grid` is the DataArray the mask was computed from: its transform puts
    the polygons in the right place and its CRS is attached to the result.
    Returns a GeoDataFrame in the CRS of the grid.
    """
    values = np.asarray(mask).astype("uint8")
    polygons = [
        shape(geom)
        for geom, _ in features.shapes(values, mask=values == 1,
                                       transform=grid.rio.transform())
    ]
    return gpd.GeoDataFrame(geometry=polygons, crs=grid.rio.crs)


def vectorize_loss(change, threshold, min_area_m2):
    """Polygons where the change map drops below a threshold.

    Pixels below `threshold` are merged into polygons, and polygons smaller
    than `min_area_m2` are dropped as noise. Areas are measured in the CRS
    of the raster, which must be metric (UTM here).
    Returns a GeoDataFrame with an `area_m2` column, in the raster CRS.
    """
    loss = vectorize_mask(change < threshold, change)
    loss["area_m2"] = loss.geometry.area
    return loss[loss["area_m2"] >= min_area_m2].reset_index(drop=True)


def water_like_polygons(baseline, recent, threshold=-0.1):
    """Polygons that stay below an NDVI threshold in both periods.

    Land that is equally low in 2018 and 2023 is most likely water. It
    should not show up as a change, but an alert that touches it is
    treated as a false positive and removed.
    """
    return vectorize_mask((baseline < threshold) & (recent < threshold), baseline)


def mean_value_in_polygon(geometry, raster, crs):
    """Mean of the raster values inside a polygon, ignoring missing pixels."""
    clipped = raster.rio.clip([geometry], crs)
    return float(clipped.mean(skipna=True))


def severity_label(area_ha, mean_ndvi_change):
    """Rough priority for a field team: high, medium or low.

    High needs both a large area and a steep NDVI drop; the cut-offs are
    round numbers chosen by judgement, not calibrated values.
    """
    if area_ha > 5 and mean_ndvi_change < -0.35:
        return "high"
    if area_ha > 1:
        return "medium"
    return "low"


def build_alerts(loss, ndvi_change, bsi_change, water, bsi_threshold=0.05):
    """From candidate loss polygons to a ranked list of alerts.

    Three filters, in this order: the mean BSI change inside the polygon
    must exceed `bsi_threshold` (bare soil confirms the NDVI drop), the
    polygon must not touch a water-like area, and it must already have
    passed the minimum area filter in `vectorize_loss`. The survivors are
    ranked by area, largest first, and labelled by severity.
    Returns a GeoDataFrame in the CRS of `loss`.
    """
    alerts = loss.copy()
    alerts["mean_bsi_change"] = alerts.geometry.apply(
        mean_value_in_polygon, args=(bsi_change, alerts.crs))
    alerts = alerts[alerts["mean_bsi_change"] > bsi_threshold]

    if len(water) > 0:
        touches_water = alerts.geometry.intersects(water.to_crs(alerts.crs).union_all())
        alerts = alerts[~touches_water]

    alerts = alerts.copy()
    alerts["mean_ndvi_change"] = alerts.geometry.apply(
        mean_value_in_polygon, args=(ndvi_change, alerts.crs))
    alerts["area_ha"] = alerts["area_m2"] / 10_000
    alerts["severity"] = [severity_label(a, n) for a, n in
                          zip(alerts["area_ha"], alerts["mean_ndvi_change"])]

    centroids = alerts.geometry.centroid.to_crs(4326)
    alerts["centroid_lon"] = centroids.x
    alerts["centroid_lat"] = centroids.y

    alerts = alerts.sort_values("area_ha", ascending=False).reset_index(drop=True)
    alerts["priority_rank"] = alerts.index + 1
    return alerts


def summarise_alerts(alerts, start_year, end_year):
    """A short plain-language summary of the alerts, for non-specialists."""
    if len(alerts) == 0:
        return (
            f"No confirmed vegetation loss was found between {start_year} and "
            f"{end_year} under the current thresholds. The area may be stable, "
            "or the thresholds may be too strict for it."
        )
    top = alerts.iloc[0]
    n_high = int((alerts["severity"] == "high").sum())
    return (
        f"Between {start_year} and {end_year}, {len(alerts)} areas show a loss of "
        f"vegetation confirmed by two independent signals, a drop in NDVI and a "
        f"rise in bare soil, for about {alerts['area_ha'].sum():.1f} hectares in "
        f"total. {n_high} of them are high severity, meaning both large and "
        f"strongly affected. The largest covers about {top['area_ha']:.1f} "
        f"hectares, centred near latitude {top['centroid_lat']:.4f}, longitude "
        f"{top['centroid_lon']:.4f}."
    )


def raster_to_overlay(data_array, cmap_name, vmin, vmax, name):
    """Turn a raster into a coloured image layer for a folium map.

    The raster is reprojected to EPSG:4326, coloured with a matplotlib
    colormap between `vmin` and `vmax`, and made transparent where there
    is no data.
    """
    reprojected = data_array.rio.reproject("EPSG:4326")
    values = reprojected.values

    norm = mcolors.Normalize(vmin=vmin, vmax=vmax)
    rgba = matplotlib.colormaps[cmap_name](norm(values))
    rgba[..., 3] = np.where(np.isnan(values), 0, 0.75)

    west, south, east, north = reprojected.rio.bounds()
    return folium.raster_layers.ImageOverlay(
        image=rgba, bounds=[[south, west], [north, east]], name=name, overlay=True)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def to_mask(gdf, grid):
    """Draw polygons on a grid: True inside, False outside.

    The polygons are reprojected to the CRS of `grid` first, so any vector
    layer can be compared pixel by pixel with the raster. An empty layer
    gives an all-False mask.
    """
    shape_yx = grid.shape[-2:]
    if len(gdf) == 0:
        return np.zeros(shape_yx, dtype=bool)
    projected = gdf.to_crs(grid.rio.crs)
    mask = features.rasterize(projected.geometry, out_shape=shape_yx,
                              transform=grid.rio.transform(), fill=0, dtype="uint8")
    return mask.astype(bool)


def polygon_coverage(reference, alerts):
    """Share of each reference polygon covered by the alerts, from 0 to 1.

    The alerts are merged first, so land covered by two alerts is not
    counted twice. Both layers must be in the same metric CRS.
    Returns a Series aligned with `reference`.
    """
    reference = reference[["geometry"]].copy()
    reference["ref_id"] = range(len(reference))
    merged = alerts[["geometry"]].dissolve()

    pieces = gpd.overlay(reference, merged, how="intersection")
    covered = pieces.geometry.area.groupby(pieces["ref_id"]).sum()

    covered_m2 = reference["ref_id"].map(covered).fillna(0)
    return pd.Series((covered_m2 / reference.geometry.area).values,
                     index=reference.index, name="coverage")



# ---------------------------------------------------------------------------
# Foundation model (Prithvi-EO-2.0)
# ---------------------------------------------------------------------------


def fit_to_grid(values, size):
    """Cut or extend the last two axes of an array to exactly `size` pixels.

    Extra rows or columns are dropped from the bottom and right; missing
    ones are added there by repeating the edge pixels. The top-left corner,
    and so the georeferencing, does not move.
    """
    values = values[..., :size, :size]
    pad_y = size - values.shape[-2]
    pad_x = size - values.shape[-1]
    padding = [(0, 0)] * (values.ndim - 2) + [(0, pad_y), (0, pad_x)]
    return np.pad(values, padding, mode="edge")


def block_mean(values, block):
    """Average non-overlapping blocks of `block` x `block` pixels.

    Each output pixel is the mean of the pixels it covers, the same as
    average resampling. The last two axes must be multiples of `block`.
    """
    *lead, h, w = values.shape
    blocks = values.reshape(*lead, h // block, block, w // block, block)
    return np.nanmean(blocks, axis=(-3, -1))


def prepare_for_prithvi(composite, mean, std, size, block=3, scale=10_000):
    """From a 10 m reflectance composite to a normalised 30 m model input.

    `composite` is a DataArray (band, y, x) with reflectance between 0 and 1,
    bands in the order the model expects. The values are scaled to HLS units,
    gaps are filled with the band median, blocks of `block` x `block` pixels
    are averaged, and each band is standardised with the pretraining `mean`
    and `std`, so that 0 means a typical value for that band.
    Returns a float32 numpy array (band, size / block, size / block).
    """
    mean = np.asarray(mean, dtype="float32")[:, None, None]
    std = np.asarray(std, dtype="float32")[:, None, None]

    values = fit_to_grid(composite.values.astype("float32"), size) * scale
    band_medians = np.nanmedian(values, axis=(1, 2), keepdims=True)
    values = np.where(np.isnan(values), band_medians, values)
    values = block_mean(values, block)
    return ((values - mean) / std).astype("float32")


def tile_origins(size, tile=224, patch=16):
    """Top-left corners (row, col) of the tiles covering a square image.

    Two tiles per side, one at each edge. They overlap in the middle, and
    the overlap is a whole number of patches so that the patch grids of
    all tiles line up.
    """
    last = size - tile
    assert 0 <= last <= tile, "two tiles per side must cover the image"
    assert last % patch == 0, "tiles must start on the patch grid"
    starts = [0, last]
    return [(row, col) for row in starts for col in starts]


def embed(image, model, tile=224, patch=16):
    """One embedding per patch over the whole image.

    The image is cut into overlapping tiles, all tiles go through the model
    in one batch, the summary token is dropped and the remaining tokens are
    put back on the patch grid. Where tiles overlap, the embeddings of the
    same patch are averaged.
    Returns an array (patches_y, patches_x, embedding_size).
    """
    import torch

    origins = tile_origins(image.shape[-1], tile, patch)
    tiles = np.stack([image[:, r:r + tile, c:c + tile] for r, c in origins])

    with torch.no_grad():
        tokens = model(torch.from_numpy(tiles))[-1][:, 1:, :]   # last block, no summary token

    per_tile = tile // patch
    tokens = tokens.reshape(len(origins), per_tile, per_tile, -1).numpy()

    n = image.shape[-1] // patch
    total = np.zeros((n, n, tokens.shape[-1]), dtype="float32")
    count = np.zeros((n, n, 1), dtype="float32")
    for emb, (r, c) in zip(tokens, origins):
        pr, pc = r // patch, c // patch
        total[pr:pr + per_tile, pc:pc + per_tile] += emb
        count[pr:pr + per_tile, pc:pc + per_tile] += 1
    return total / count


def cosine_distance(a, b):
    """1 minus the cosine of the angle between two vectors, along the last axis.

    0 when the vectors point the same way, up to 2 when they point in
    opposite directions. Their length does not matter.
    """
    a = a / np.linalg.norm(a, axis=-1, keepdims=True)
    b = b / np.linalg.norm(b, axis=-1, keepdims=True)
    return 1 - (a * b).sum(axis=-1)


def patch_raster(values, reference, patch_size_m, name):
    """Put a grid of patch values on the map, as a DataArray (y, x).

    The grid starts at the top-left corner of `reference`, the raster the
    patches were cut from, with square cells of `patch_size_m` metres.
    """
    transform = reference.rio.transform()
    x0, y0 = transform.c, transform.f
    n_y, n_x = values.shape
    x_centres = x0 + patch_size_m * (np.arange(n_x) + 0.5)
    y_centres = y0 - patch_size_m * (np.arange(n_y) + 0.5)

    raster = xr.DataArray(values, dims=("y", "x"),
                          coords={"y": y_centres, "x": x_centres}, name=name)
    return raster.rio.write_crs(reference.rio.crs)




def patch_mean(values, size, block):
    """Mean of a 10 m raster over each model patch.

    The raster is first brought to `size` x `size` pixels with `fit_to_grid`,
    like the model input, then averaged over blocks of `block` x `block`
    pixels. Works on any 2-D array: index values, or a boolean mask, in
    which case the result is the share of each patch covered by it.
    """
    return block_mean(fit_to_grid(np.asarray(values, dtype="float32"), size), block)


def auc_score(scores, labels):
    """How well a score separates positives from negatives, from 0 to 1.

    The probability that a randomly picked positive gets a higher score
    than a randomly picked negative: 0.5 is no better than chance, 1 is a
    perfect separation. Computed with the Mann-Whitney U statistic, so it
    only depends on the ranking of the scores, not on their scale.
    """
    from scipy.stats import mannwhitneyu

    scores = np.asarray(scores).ravel()
    labels = np.asarray(labels, dtype=bool).ravel()
    positives, negatives = scores[labels], scores[~labels]
    u = mannwhitneyu(positives, negatives).statistic
    return float(u / (len(positives) * len(negatives)))




# ---------------------------------------------------------------------------
# Context for each alert (used by the assistant)
# ---------------------------------------------------------------------------

def share_in_polygons(polygons, mask, grid):
    """Share of each polygon covered by a boolean mask, from 0 to 1.

    `mask` is a 2-D boolean array drawn on `grid` (for instance with
    `to_mask`); `polygons` must be in the CRS of the grid. The share is the
    mean of the mask over the pixels whose centre falls in the polygon.
    Returns a Series aligned with `polygons`.
    """
    layer = grid.copy(data=np.asarray(mask, dtype="float32"))
    return polygons.geometry.apply(mean_value_in_polygon, args=(layer, polygons.crs))


def land_status(prodes_share, cleared_share, threshold=0.5):
    """What PRODES says about the ground under an alert, in a few words.

    - "mapped by PRODES": most of the alert matches a PRODES clearing of the
      years compared, so PRODES confirms it;
    - "cleared before 2019": most of it was already deforested by 2018, land
      PRODES no longer monitors, so PRODES can neither confirm nor reject it;
    - "forest, not in PRODES": most of it was forest that PRODES still
      monitors and did not map as cleared, a real disagreement;
    - "mixed": none of the three covers at least `threshold` of the alert.
    """
    forest_share = 1 - prodes_share - cleared_share
    if prodes_share >= threshold:
        return "mapped by PRODES"
    if cleared_share >= threshold:
        return "cleared before 2019"
    if forest_share >= threshold:
        return "forest, not in PRODES"
    return "mixed"
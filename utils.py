import warnings

import numpy as np
import pandas as pd
import geopandas as gpd
import rioxarray
import xarray as xr
import matplotlib.pyplot as plt
import contextily as cx
from shapely.geometry import box, mapping, shape, Point

import pystac_client
import planetary_computer as pc
import odc.stac
from rasterio.features import shapes as raster_to_shapes
import folium
from folium.plugins import GroupedLayerControl
import matplotlib.cm as cm
import matplotlib.colors as mcolors


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


def mask_and_scale(scene_data, item):
    """Turn raw Sentinel-2 values into clean surface reflectance.

    Pixels flagged by the SCL band as clouds, shadows, snow, saturated or
    missing are set to NaN. Scenes processed with baseline 04.00 or later
    carry a radiometric offset of 1000 that Planetary Computer does not
    remove, so it is subtracted here before scaling to the 0-1 range.
    Returns a Dataset with the seven reflectance bands, without SCL.
    """
    cloud_related_classes = [0, 1, 3, 8, 9, 10, 11]
    scl = scene_data["SCL"]
    is_clear = ~scl.isin(cloud_related_classes)

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

def mean_value_in_polygon(geometry, raster, crs):
    """Mean of the raster values inside a polygon, ignoring missing pixels."""
    clipped = raster.rio.clip([geometry], crs)
    return float(clipped.mean(skipna=True))


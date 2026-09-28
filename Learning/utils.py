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




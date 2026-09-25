# Learning log


## Day 1 — Finding and loading the data (STAC, Planetary Computer, odc-stac)

### Environment setup (the part nobody warns you about)
- Created a dedicated conda env (`deforestation`) with all geospatial libraries from **conda-forge** in a single `conda create` command, so conda resolves compatible versions all at once. Exported it to `environment.yml`.

- **Windows 11 Smart App Control** blocked unsigned DLLs (first rasterio, then numpy) with *"An Application Control policy has blocked this file"*. Not a Python error. Turning it off fixed it without reinstalling anything.


### STAC and the Planetary Computer catalog
- Hierarchy: **Catalog** (all of Planetary Computer) → **Collection** (`sentinel-2-l2a`) → **Item** (one scene: one tile, one acquisition) → **Asset** (the actual files, one per band).
- Acquisition-level metadata (date, cloud cover, processing baseline, tile, EPSG) lives on the **item**. File-level info (href, format, resolution) lives on each **asset**.
- The catalog root (`/api/stac/v1`) is just a JSON document with links to `/collections`, `/search`, `/queryables`. `pystac_client` only makes HTTP requests to these URLs.
- This server requires `collections=` in every search (`item-search#require-collections` in `conformsTo`).

### Searching
- `bbox=[lon_min, lat_min, lon_max, lat_max]` or `intersects=<GeoJSON geometry>`: one or the other, not both. **Longitude first.**
- Forgetting the spatial filter searches the whole planet.
- `search.matched()` gives the number of results without downloading them; `max_items=` is a safety net.
- `intersects` means "touches", so a wide search returns several items per overpass (one per tile) plus duplicate processing versions.

### Anatomy of an item
- The item id encodes satellite, level, sensing time, relative orbit, MGRS tile and processing time. Processing time is what distinguishes the duplicates I found in the original notebook.
- `item.geometry` is the real footprint (often irregular, cut by the swath); `item.bbox` is the rectangle around it. This difference is what caused my blank images at the start of the project.
- `eo:cloud_cover` refers to the whole tile, not my AOI.

### Assets
- Bands come at different native resolutions (10 m vs 20 m), which is why `odc.stac.load` needs a common `resolution`.
- Assets are COGs with built-in **overviews**: `rioxarray.open_rasterio(href, overview_level=N)` reads a downsampled version without downloading the full tile.
- The `product-metadata` / `granule-metadata` XML assets contain the real BOA_ADD_OFFSET value, currently hardcoded as -1000 in my notebook.

### Why signing is needed
- The files sit on private Azure storage. `pc.sign_inplace` appends a temporary **SAS token** to every asset href: `st` (start), `se` (expiry), `sp` (permissions, read-only), `sig` (cryptographic signature; editing any other field invalidates it).
- My token was valid for about 25 hours. After `se`, reads fail with confusing rasterio errors. Fix: rerun the search to get fresh tokens.
- `sign_inplace` modifies the item itself; `pc.sign` returns a signed copy and leaves the original untouched.

### Loading pixels: eager vs lazy
- Without `chunks`, `odc.stac.load` downloads immediately. Time: <!-- fill in -->
- With `chunks={}`, it returns dask arrays almost instantly. Nothing is downloaded until `.compute()`, `.values` or a plot. Time for `.compute()`: <!-- fill in -->
- The time doesn't disappear, it moves. Lazy loading pays off when chaining operations (many scenes → index → median), because dask can plan the whole computation and only fetch what's needed.
- Even a single item gets a `time` dimension of length 1, hence `.isel(time=0)` in the notebook.

## Day 2 — Working with rasters (xarray, rioxarray, rasterio)

### From item to pixels
- A `pystac.Item` holds no pixels, only metadata and file addresses. `odc.stac.load` is the bridge to xarray.
- What `odc.stac.load` does internally: reads band metadata from the assets → builds the output grid (*GeoBox*: CRS, resolution, extent snapped to pixels; inspect it with `ds.odc.geobox`) → groups items in time (`groupby="solar_day"` merges tiles from the same overpass) → reads only the needed window of each COG and resamples it onto the grid → returns an `xarray.Dataset`.
- Default resampling is **nearest**. For SCL it's the only correct choice: it's a map of classes, and averaging "vegetation" with "cloud" gives a class that doesn't exist. Reflectance bands could use `"bilinear"`; `resampling=` accepts a per-band dict.
- Pipeline in object types: `URL → Client → ItemSearch → [Item] → Dataset(time, y, x) → DataArray(y, x) → boolean mask → polygons → GeoDataFrame → files`.

### xarray
- xarray wraps numpy arrays with labels, so meaning travels with the data.
- **DataArray** = values (numpy or dask) + `dims` (axis names) + `coords` (labels along each axis) + `attrs` (free metadata) + name.
- **Dataset** = several DataArrays sharing the same coordinates. `ds["B04"]` is a DataArray.
- Dimension coordinates (marked `*`) have one label per position. Non-dimension coordinates store extra info: `spatial_ref` holds the CRS; after `.isel(time=0)`, `time` survives as a **scalar** coordinate.
- Arithmetic aligns pixels **by coordinate labels**, not by position. Grids offset by half a pixel give a nearly empty result with no error, hence `reproject_match` before differencing two periods.
- Operations return **new objects** (reassign the result) and **drop `attrs`** (my NDVI lost the band's nodata and name).
- `.where(cond)` keeps values where the condition is true and sets NaN elsewhere, preserving shape and coordinates.

### NDVI, step by step
- Value 0 = nodata → turn it into NaN with `.where(band != 0)` **before** applying the offset, otherwise it becomes a fake negative reflectance.
- The processing baseline is a string (`"05.09"`): `float(baseline) >= 4.0` decides whether to subtract 1000 before dividing by 10000.
- Resulting NDVI: dims `(y, x)`, coords `x`, `y` (UTM metres), `spatial_ref`, scalar `time`.

### `.isel` vs `.sel`
- `.isel` = by **position** ("the third carriage"); `.sel` = by **label** ("carriage number 12"). Sorting the data changes what `.isel` returns but not what `.sel` returns. Same as pandas `.iloc` vs `.loc`.
- `.sel` on x/y needs `method="nearest"`: coordinates are pixel centres every 10 m, an exact match is almost impossible (`KeyError` otherwise).
- Trap: `y` **decreases** down the rows (row 0 is the northernmost). With `.sel`, a y-slice must go `slice(y_max, y_min)`; the wrong order returns an **empty** array, not an error.
- `slice(0, 100)` = indices 0 to 99. A window keeps its real-world coordinates.

### rioxarray
- xarray knows nothing about geography. `import rioxarray` registers the `.rio` accessor on every DataArray/Dataset (forgetting the import = `.rio` errors).
- CRS lives in the `spatial_ref` coordinate's attributes; the affine transform is **derived from the coordinates**.
- `write_crs` **declares** what system the numbers are in; `reproject` **transforms** them. Declaring the wrong CRS sends the pixels to the wrong place without any error.
- Used in my notebook: `reproject_match` (align grids before differencing), `clip` (mean BSI inside each polygon), `to_raster` (COG export), `open_rasterio` (read back).

### The affine transform
- Six numbers converting (row, column) ↔ map coordinates. For north-up images: `a` = pixel width (10), `e` = pixel height (**-10**: going down a row means going south), `c`, `f` = x, y of the **top-left corner** of the image; `b`, `d` = rotation (0).
- Corner of a pixel: `x = c + col·a`, `y = f + row·e`. Inverse: `col = (x − c)/a`, `row = (y − f)/e`.
- xarray coordinates are pixel **centres**, the transform refers to **corners**: half a pixel (5 m) apart. Centre: `x = c + (col + 0.5)·a`.
- Used whenever you go array ↔ map: `rasterio.features.shapes(..., transform=...)` (without it, polygons land near (0, 0) in the Gulf of Guinea), `to_raster` (how QGIS knows where to place the image), `crop_transform` in the Prithvi section.

### Reprojecting to EPSG:4326
- Resolution goes from `(10, -10)` metres to ~0.00009 degrees: 10 m / ~111 km per degree of latitude. Longitude degrees shrink with latitude (cos φ), so pixels in degrees aren't square on the ground.
- The UTM grid is slightly rotated relative to lat/lon, so the reprojected raster gets extra rows/columns and **NaN triangles** at the corners.
- Every reprojection resamples and alters the data: compute in the native CRS, reproject only at the end for display or export.
- Saved the NDVI with `.rio.to_raster` and checked it in <!-- QGIS / contextily -->: <!-- did it land in the right place? -->
- Resampling bonus (10 → 30 m, nearest vs `Resampling.average`)

### Errors that taught me something
- `'generator' object has no attribute 'items'`: I had called `.items()` twice, once at the end of `catalog.search(...)` and again afterwards.
- `'Item' object has no attribute 'attrs'`: `.attrs` belongs to xarray; a pystac Item uses `.properties`, `.assets`, `.id`, `.datetime`, `.geometry`, `.bbox`.
- First move on a surprising `AttributeError`: `type(variable)`. `dir(obj)` or `obj.` + Tab lists what the object offers.
- Three different "bbox" in my code: `item.bbox` (rectangle around the scene's footprint, always EPSG:4326), the `bbox=` search parameter (where I search), and my AOI `bbox` (my ~10 km study area).



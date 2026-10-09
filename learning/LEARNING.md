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

## Day 3 — From pixels to polygons (geopandas, rasterio)

### Raster vs vector
- A raster answers "what is the value at this point?"; a vector layer answers "which objects are there, and what are they like?". The project starts from the first question (where did NDVI drop) and ends at the second (which areas to inspect, in what order).
- A **GeoDataFrame** is a table where each row is an object and the `geometry` column holds its shape. I don't get one from the raster: I build it.

### `rasterio.features.shapes`
- Groups neighbouring pixels with the same value into "islands", then traces each island's outline along the pixel edges. That's why polygons are stair-shaped and their vertices sit on pixel corners.
- `source` must be an integer array, not boolean: `.values.astype("uint8")`.
- `mask=` restricts it to the pixels I care about; without it the background is vectorised too.
- `transform=` defaults to the identity: without it, coordinates come out in rows and columns, and the polygons would land near (0, 0) in the Gulf of Guinea.
- `connectivity=4` joins pixels that share a side; `8` also joins diagonal neighbours.
- Returns pairs `(GeoJSON dict, value)`; `shape()` turns each dict into a shapely geometry.

### Areas and CRS in geopandas
- `crs=` when building a GeoDataFrame is a **label**, like `write_crs`; `.to_crs()` actually transforms every vertex (through pyproj).
- `.area` computes plane geometry on the numbers, whatever the CRS. In UTM it gives m²; in EPSG:4326 it gives "square degrees", which mean nothing on the ground. geopandas warns, but computes anyway.
- One 10 m pixel = 100 m². Half a hectare = 5000 m² = the `MIN_AREA_M2` filter.
- contextily tiles are always in Web Mercator (EPSG:3857): reproject the data before plotting on a basemap.

### Passing data between notebooks
- Save the raster as GeoTIFF (`float32`, `compress="deflate"`), reopen with `rioxarray.open_rasterio(..., masked=True).squeeze("band", drop=True)`. Unlike a `.npy`, the GeoTIFF keeps CRS and transform.
- Raw data goes in `data/` (git-ignored); results meant to be seen go in `outputs/`.

### How to read documentation without getting lost
- Start from "what do I have, what do I want", then look for a function whose input and output match.
- Read the **signature** (parameters and their defaults), the **Returns** section and the examples. Defaults explain a lot of "mysterious" behaviour.
- When a function is unclear, feed it a tiny hand-made input (a 5 × 5 array) and predict the result before running it.

## Refactoring

### Moving functions out of the notebook
- Functions go in a `.py` file (`utils.py`), not in a notebook: a notebook can't be imported cleanly, and its JSON diffs are unreadable on GitHub. The MCP server will need plain imports.
- The main rule: **a function receives everything it uses as a parameter**. Test: could I paste it into an empty file with only the imports and have it work?
- Names that say what the function returns or does (`choose_aoi`, `load_scene`), no magic numbers inside (`inset_deg=0.15`), a short docstring on why.
- Settings in uppercase constants at the top of the notebook (`BASELINE_WINDOW`, `BAD_SCL_CLASSES`): they're already the future config file.
- Calculations go in functions; one-off diagnostic plots can stay in the notebook. If a plot cell also computes something needed elsewhere, that part becomes a function.
- `%load_ext autoreload` + `%autoreload 2`: edits to `utils.py` are picked up without restarting the kernel.

### Regression check
- The refactoring must change the structure, not the results. Compared the new `ndvi_change.tif` with the original using `np.allclose(..., equal_nan=True)`, plus polygon count and total area: **identical**.

### Lazy loading, in practice
- The summary table was very slow: with `chunks={}`, every `.values` re-ran the whole recipe from the download, and dask doesn't keep intermediate results. 4 indices × 10 scenes = 40 downloads of the same bands.
- Fix: `.compute()` once on the cleaned reflectance. That cell gets slow, everything after is instant, and expired tokens stop mattering.
- When dask is worth it: data that doesn't fit in memory, results that only need part of what was "loaded", parallel work on chunks. For a 10 km AOI (~350 MB for 10 scenes) it isn't essential.
- Rule: stay lazy while reducing the data, `.compute()` the result you'll reuse many times.

### Errors that taught me something
- `IndentationError: expected an indented block after function definition`: the `def` lines had four spaces in front. In Python indentation defines the blocks.
- The same error kept coming back because Python was still reading the **old file**: unsaved, a different copy, or a kernel not restarted. Check what's really on disk.
- `NameError` for `pd` and `plt` at the end of a slow cell: imports missing in the notebook. Expensive work first, crash on the last line.
- `NotGeoreferencedWarning` during loading: checked per-band means for every scene to rule out a failed read (a failed band would show exactly 0.0 or -0.1, since `mask_and_scale` doesn't mask 0).

## Validation against PRODES

### What PRODES is, and its rules
- INPE's yearly map of deforestation in the Brazilian Amazon. Rules that shape the comparison:
  - it maps only **clear-cut of primary forest**;
  - an area is recorded **once**: after that, nothing that happens there is mapped again;
  - minimum mapped area **6.25 ha** (mine is 0.5 ha);
  - a PRODES year runs **August to July**.
- Downloaded the full GeoPackage (~745 MB), read only my AOI with `gpd.read_file(path, layer=..., bbox=...)` (the spatial index makes it fast), saved a small clip to `data/prodes_aoi.gpkg`.
- `gpd.list_layers` shows the layers: yearly deforestation, accumulated to 2007, no forest, hydrography, residual.
- PRODES is in **EPSG:4674 (SIRGAS 2000)**: degrees, < 1 m from EPSG:4326 here, but geopandas needs matching CRS anyway.
- Years compared: **2019–2023**. 2018 is excluded (already cleared in my baseline scenes), 2023 is included (ends in July, before my recent scenes).
- In my AOI: 76 polygons from 2008–2018, only 30 from 2019–2023.

### Pixel-level comparison
- Validation notebook kept separate from detection: it only reads `outputs/` and never downloads satellite data.
- Common grid = the NDVI change raster (EPSG:32720, 1108 × 1100). Its values aren't used, only its shape, transform and CRS.
- `rasterio.features.rasterize` is the reverse of `shapes`: polygons → 0/1 array on a given grid (`out_shape`, `transform`, `fill`, `dtype`). The output is plain numpy, safe only because every mask uses the same grid.
- Check: pixels × 100 m² ≈ polygon area. PRODES ≈ 409 ha, alerts ≈ 1050 ha.
- Masks combine with `&` (and), `|` (or), `~` (not); `.sum()` counts, `.mean()` gives a fraction.

| Metric | Value |
|---|---|
| Precision, whole AOI | 22.8% |
| Recall | 58.5% |
| F1 | 32.8% |
| Accuracy | 92.0% (useless: most pixels are "no change" for both) |

- Precision had a ceiling before any comparison: true positives can't exceed PRODES pixels, so ≤ ~39%.

### The key result: most disagreement is definitional
- Built a mask of land **no longer primary forest in 2018**: accumulated deforestation to 2007 ∪ PRODES 2008–2018. Overlap between the two = 0, as expected. No forest and hydrography are empty over the AOI (checked with `rows=5` without bbox that the layers do contain data elsewhere).
- **77.3%** of the AOI was already cleared by 2018, almost all of it before 2008.

| | Value |
|---|---|
| False positives on land already cleared | 750.4 ha (92.5%) |
| False positives on standing forest | 60.6 ha (7.5%) |
| **Precision where PRODES still looks** | **79.8%** |

- On forest, when the pipeline flags something, PRODES agrees four times out of five. The limit is recall.
- The 750 ha on cleared land are **not confirmed**: PRODES can neither confirm nor reject them (secondary vegetation cleared? ploughed fields? dry pasture?).

### Polygon-level comparison
- Pixel recall mixes "clearing missed" with "clearing found with a smaller outline". Per PRODES polygon: share covered by alerts, via `dissolve` (so overlaps aren't counted twice) + `gpd.overlay(how="intersection")` + `groupby().sum()` + `.map()`.
- Groups (cut-off 0.5): found <!-- n -->, partial <!-- n -->, missed <!-- n -->.
- Mean coverage by year: <!-- does it increase from 2019 to 2023? -->
- Mean NDVI change inside missed polygons: <!-- close to -0.25 (threshold too strict) or close to 0 (regrowth)? -->
- Residual layer over the AOI: <!-- n polygons -->

### Results saved for the assistant
- `outputs/validation_metrics.json`: the main numbers plus a list of **caveats**, so the numbers never travel without their limits.
- `json` can't write numpy types (`int64 is not JSON serializable`): convert with `float()` / `int()`. JSON keys must be strings.

### Errors that taught me something
- Used `shapes` (raster → polygons) when I needed `rasterize` (polygons → raster). And `.astype("uint8")` on a float NDVI change truncates -0.3 and 0.4 to 0: always compare first (`ndvi < 0.3`), then convert.
- `prodes1_utm = prodes_change.to_crs(...)` instead of `prodes_change1`: the accumulated mask came out identical to the 2019–2023 one. No error. Caught by counting pixels after every mask. Names that differ by one character invite this: `accumulated`, `accumulated_utm`, `accumulated_mask`.
- Same name for two things: `false_pos` was a number in one section and a mask in another. Re-running cells out of order would give wrong results silently.
- `set_index` moves a column into the index; I needed the opposite. `overlay` drops the index but keeps columns, so ids must live in a column.
- `KeyError: ['prodes_id'] not in index` after Restart & Run All: the cell creating the column had been run once and then lost. The order cells were **run** in and the order they are **written** in can differ; only a full restart shows it.
- `NameError: ndvi_raster` after moving a function to `utils.py`: a function only sees its parameters and the variables of its own file, not the notebook's. Pass the raster as a parameter.

## Extra practice

### xarray (toy cube 4 × 3 × 5)
- `.isel` counts from 0: "second date, first row, third column" = `isel(time=1, y=0, x=2)`. Rows are `y`, columns are `x`. An `IndexError` is better than a swapped index that silently returns the wrong pixel.
- Labels aren't guessed, they're read from the object (`ndvi.x.values`). `.sel` is for when the label comes from the real world (a date, a centroid).
- Reductions: **the dimension you name is the one that disappears**. `mean("time")` → `(y, x)`; `mean(["y", "x"])` → `(time,)`; `max()` → one value.
- `.where(cond)` keeps where the condition is **true**: write what to keep. `>` vs `>=` decides which side the threshold falls on.
- Count valid values: `masked.notnull().sum("time")`. `skipna=True` is the default and gives a value even from a single observation, silently.
- Alignment: `a - b` on grids sharing one column keeps only that column; `a.values - b.values` subtracts different places and returns a plausible-looking wrong result. **Going to `.values` drops the protection of coordinates.**
- `xr.concat` stacks along a dimension; `groupby("time.year").median()` builds one composite per year from the dates themselves.

### rioxarray (toy raster 4 × 5, 10 m, UTM 20S)
- `write_crs` doesn't change the numbers: bounds stay identical, only the label changes. "Degrees" of 580,000 and 8,950,000 don't exist, and nothing complains.
- `reproject(crs, resolution=20)` builds a new grid from parameters; `reproject_match(other)` copies another raster's exact grid (passing it a number gives `'int' object has no attribute 'rio'`).
- 50 m / 20 m = 2.5 columns → rounded up to 3, the extra one partly outside the data.
- Nearest takes one small pixel per large pixel; `Resampling.average` takes the mean of the four. Same choice as the 10 → 30 m step in the Prithvi section.



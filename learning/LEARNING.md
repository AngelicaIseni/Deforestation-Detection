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
- Check: pixels × 100 m² ≈ polygon area. PRODES 413.9 ha from the mask vs 415.8 ha from the polygons (pixel centres miss thin edges), alerts 1050.3 vs 1050.3 (they were born on this grid).
- Masks combine with `&` (and), `|` (or), `~` (not); `.sum()` counts, `.mean()` gives a fraction.

| Metric | Value |
|---|---|
| Precision, whole AOI | 23.0% |
| Recall | 58.5% |
| F1 | 33.1% |
| Accuracy | 92.0% (useless: flagging nothing at all would score 96.6%) |

- Precision had a ceiling before any comparison: true positives can't exceed PRODES pixels, so ≤ ~39%.

### The key result: most disagreement is definitional
- Built a mask of land **no longer primary forest in 2018**: accumulated deforestation to 2007 ∪ PRODES 2008–2018. Overlap between the two = 0, as expected. No forest and hydrography are empty over the AOI (checked with `rows=5` without bbox that the layers do contain data elsewhere).
- **77.5%** of the AOI was already cleared by 2018, almost all of it before 2008 (70% accumulated to 2007, 8% in 2008–2018).

| | Value |
|---|---|
| False positives on land already cleared | 753.9 ha (93.3%) |
| False positives on standing forest | 54.3 ha (6.7%) |
| **Precision where PRODES still looks** | **81.7%** |

- On forest, when the pipeline flags something, PRODES agrees four times out of five. The limit is recall.
- The 750 ha on cleared land are **not confirmed**: PRODES can neither confirm nor reject them (secondary vegetation cleared? ploughed fields? dry pasture?).

### Polygon-level comparison
- Pixel recall mixes "clearing missed" with "clearing found with a smaller outline". Per PRODES polygon: share covered by alerts, via `dissolve` (so overlaps aren't counted twice) + `gpd.overlay(how="intersection")` + `groupby().sum()` + `.map()`.
- Groups (cut-off 0.5): **found 16, partial 9, missed 5** of 30. Seen per polygon, 25 of 30 clearings were touched by at least one alert, a kinder story than the 58.5% pixel recall. Three polygons sit at 0.44–0.48: with a cut-off of 0.4 it would be 19 / 6 / 5.
- Mean coverage by year: 2019 0.66, 2020 0.27, 2021 0.48, 2022 0.29, 2023 0.70. No trend, and 4–9 polygons per year are too few to find one. What matters is probably not the age of a clearing but what it became: pasture stays low-NDVI in the dry season and is found years later, abandoned land regrows and is lost.
- Mean NDVI change: found −0.33, partial −0.14, missed −0.06. The missed ones are regrowth, not a threshold just too strict.
- Of the 5 missed, 3 are under 1.5 ha, far below the PRODES minimum: most likely fragments of larger polygons cut by the clip at the grid edge. The real misses are 2, both around −0.03, vegetation back by 2023.
- Residual layer over the AOI: 10 polygons, 65.4 ha, years 2011–2024, of which 6.7 ha covered by alerts. Not in the "not forest in 2018" mask, so part of the 54 ha "false positives on forest" may sit on land PRODES added later.

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

## Looking back at the whole pipeline

A full read of both notebooks, cell by cell, with the outputs in front of me. Things I had not seen while writing them:

- **The AOI is not where I thought.** `choose_aoi` uses the reference point only to find tiles, then centres the AOI on the largest footprint: the AOI ended up about 100 km from the reference point, outside the search circle. Still in the deforestation arc, and it explains why 77% of it was cleared long ago.
- **Same orbit, same tile.** All ten scenes are tile T20LMQ, relative orbit R053: same viewing geometry in both years. One more thing held fixed, like the months.
- **Why `astype("float32")` comes before `- 1000`.** In uint16 there are no negatives: 800 − 1000 wraps to 65,336. NaN also exists only in floats.
- **Why the offset faked a loss.** NDVI = (N − R) / (N + R). Adding c to both bands leaves the numerator unchanged and grows the denominator: forest at N = 0.30, R = 0.03 goes from 0.82 to 0.51 with c = 0.1. Normalised indices resist multiplication, not addition.
- **EVI assumes reflectance between 0 and 1** (the `+ 1`). Left on the 0–10,000 scale, NDVI would be identical and EVI wrong.
- **Within-season change is bigger than between-year change.** NDVI varies by ~0.15 inside a dry season and by ~0.006 between the two years' means: hence medians of five scenes, and pixel-level change instead of AOI means.
- **The histogram is not "one peak and a thin tail".** There is a shoulder around −0.28, a second population of changed pixels, and the −0.25 threshold cuts through it. That is the technical root of the 58.5% recall: lowering the threshold would recover real pixels and eat into the central peak.
- **The minimum-area filter removes many polygons and little area.** 2,228 raw polygons, 2,098 under 0.5 ha (942 single pixels), but only 87 ha of 1,138.
- **The BSI check has no teeth because BSI is not independent of NDVI.** Both use NIR and red: when NIR falls and red rises, BSI rises almost by construction. All 130 candidates passed, the lowest mean BSI change was 0.072. "Two independent signals" in the summary is too strong.
- **`intersects` is true for polygons that only share an edge**: a large clearing touching a pond by one pixel is dropped whole.
- **The horizontal bands are roads.** They appear in all ten acquisitions, and the most negative rows of the change map are where the big clearing in the north-west is. Landscape, not sensor.
- **The false colour comparison mixes seasons**: it shows the clearest scene of each period, 19 August 2018 against 29 June 2023.
- **Two edges in the PRODES years.** PRODES 2019 starts in August 2018, inside my baseline window; my recent window runs to September 2023, which is PRODES 2024. A clearing in August 2023 is in my images but counts as a false positive.
- **`vegetation_loss` is a measurement, `deforestation_alerts` is an interpretation.** The first says "green went down here", the second "worth checking". With the filters doing almost nothing they nearly coincide (130 vs 129), and "deforestation alerts" is an optimistic name: "vegetation loss alerts, to be verified" is more honest.

## Foundation model (Prithvi-EO-2.0)

### What it is
- A **Vision Transformer** pretrained by IBM and NASA on 4.2 million HLS samples (harmonised Landsat and Sentinel-2, 30 m, six bands, global).
- Trained as a **masked autoencoder**: hide 75% of the image, reconstruct it. To do that it has to learn what the land surface looks like. No labels needed, which is why it can see millions of images.
- I use it **zero-shot**: no training, only its description of each patch. It was never trained to detect deforestation, and that is exactly what the comparison with PRODES tests.

### Inside the model
- The 224 × 224 input is cut into **patches of 16 × 16** (480 m at 30 m): 14 × 14 = 196 patches. Each patch, 16 × 16 × 6 = 1,536 numbers, goes through one learned 1024 × 1536 matrix: one vector of 1,024 per patch. Same idea as my `block_mean`, with learned weights instead of 1/9.
- A **position embedding** is added (attention alone doesn't know where a patch is), plus a **CLS token** in front that ends up summarising the image: 197 tokens. I drop the CLS, I want a map.
- **24 blocks**, each: LayerNorm → self-attention → add back → LayerNorm → MLP (1024 → 4096 → 1024) → add back.
- **Self-attention**: every token makes a query, a key and a value; the 197 × 197 table of query·key scores, after softmax, says how much each token listens to each other; the new token is the weighted mean of the values. 16 heads in parallel. This is how a patch of pasture learns it sits at the edge of a forest.
- The "+ x" (residual) adds an update instead of replacing the vector. LayerNorm is the same idea as my input normalisation, applied inside the network.
- Parameter count, checked by hand: one block ≈ 4.2 M (attention) + 8.4 M (MLP) ≈ 12.6 M, × 24 ≈ 302 M. The notebook printed **304 M**.
- TerraTorch returns **all 24 block outputs**, (B, 197, 1024) each, because segmentation heads mix early and late levels. I use the last one; an earlier block is a possible experiment.

### Preparing the input
The model doesn't fail on wrong input, it describes something it has never seen. Three things must match its training data:
- **Bands**: B02, B03, B04, **B8A**, B11, B12, in that order. HLS uses B8A (narrow NIR), not B08, because it matches Landsat's NIR.
- **Values**: × 10,000 (HLS scale), then `(x − mean) / std` with **Prithvi's own statistics** (`PRITHVI_V2_MEAN = [1087, 1342, 1433, 2734, 1958, 1363]`). Without × 10,000 every pixel becomes about −0.66 / −1.48 everywhere: a flat image, distances near zero, the bug of the first version.
- **Why their statistics and not mine**: mine would force each image to mean 0, erasing what makes the AOI different from the world, and, worse, normalising 2018 and 2023 separately would erase part of the change before the model sees it. Fixed statistics for both years: change one thing only.
- **Gaps**: filled with the band median **before** normalising. Filled with 0 instead, a gap becomes −0.66, an extreme value. (No gaps here: 5 clear observations everywhere.)
- **Pixel size**: 30 m. At 10 m a patch would cover 160 m, objects three times bigger than anything the model knows. **Average** of 3 × 3 pixels, not nearest: a 30 m sensor measures the light of its whole footprint.
- `block_mean` with `reshape(..., h // 3, 3, w // 3, 3).mean(axis=(-3, -1))`: an average pooling with stride 3, written with shapes. Numpy stores rows contiguously, so the reshape groups neighbours.
- `fit_to_grid`: cut or pad (edge) only at the bottom and right, so the top-left corner, the origin of the affine transform, doesn't move. 1108 × 1100 → 1104 × 1104 at 10 m → 368 × 368 at 30 m = 23 × 23 patches.
- My normalised means came out between −0.26 and +0.87, standard deviations 0.07–0.75: the AOI is more uniform than the whole planet, as it should be.

### Covering the AOI
- Four tiles of 224 starting at 0 and 144. The overlap, 80 pixels, is **exactly five patches**, so every tile's patch grid lines up with the others (`assert last % patch == 0`). A tile starting at 150 would shift its patches by 6 pixels.
- The four tiles go through the model as one batch (4, 6, 224, 224). Tokens are put back on the 23 × 23 grid with a `total` and a `count` array: corners seen once, edges twice, the centre four times, each time with a different context. Their mean is the patch embedding.
- Deterministic (`eval()` + `no_grad()`), and both years go through the same tiles in the same order.
- The **dry run** with zeros only checks shapes, but those shapes are what `embed` relies on: a list of 24, 197 tokens with the CLS first. If the model returned 196, dropping one token would shift the whole map without any error.

### Comparing two descriptions
- **Cosine distance**: normalise both vectors to length 1, their dot product is the cosine of the angle. Only direction counts. Check: an embedding against itself gives ~1e-7, float32 precision.
- Distances: min 0.011, median 0.047, max 0.246. Alive, not collapsed.
- `patch_raster`: centres at `x0 + 480 · (i + 0.5)`, y going down; rioxarray rebuilds the transform from regularly spaced coordinates. The 23 × 23 raster overlays exactly on the NDVI map.

### Results (04_prithvi_vs_prodes)
- **The distance has no sign.** Spearman with the signed mean NDVI change per patch −0.34, with the mean *absolute* change **0.84**, with the share of loss pixels 0.70, with the share of gain pixels 0.18. It measures how much a patch changed, not whether it lost vegetation.
- **AUC**: probability that a patch with a PRODES clearing ranks above one without. Only the ranking matters, so different scales can be compared. Computed from the Mann-Whitney U statistic (sum of ranks of the positives).

| Score per 480 m patch | All (44 / 529) | Mostly forest (23 / 104) |
|---|---|---|
| Prithvi distance | 0.87 | 0.97 |
| Share of NDVI loss pixels | 0.87 | 0.92 |
| Share covered by alerts | 0.85 | 0.90 |
| Mean absolute NDVI change | 0.80 | 0.92 |

- With so few positive patches, a few hundredths is noise: "matches the index, maybe slightly better on forest".
- Of the two clearings > 5 ha the alerts missed entirely, the 11 ha one (2021) is at the **80th percentile** of distance with almost no NDVI loss; the 6 ha one (2020) at the 21st. One out of two: an anecdote, not a result.
- Spearman p-values are not reported: neighbouring patches are not independent, so p would look far more certain than it is.

### Environment and errors
- Separate environment (`environment-gfm.yml`): PyTorch and TerraTorch are gigabytes. `utils.py` imports `torch` **inside** `embed`, so the main environment can still import it. The gfm environment also needed `odc-stac` and `folium`, imported at the top of `utils.py`.
- `pretrained=True` downloads ~1.3 GB from Hugging Face. In Jupyter there is no progress bar, so a stuck download looks like a frozen cell. Downloading from the terminal (`snapshot_download`) shows it; `HF_HUB_DISABLE_XET=1` switches to plain HTTP. Weights stay in `~/.cache/huggingface`, then `HF_HUB_OFFLINE=1`. The symlink warning on Windows only costs disk space.
- Environment variables must be set **before** the import that reads them.
- **The kernel died with no message** on `spearmanr` after the model had run. SciPy alone worked, numpy came from conda-forge: the cause was PyTorch (pip) and numpy/SciPy (conda) each bringing their own OpenMP runtime, which clash once both are active. `KMP_DUPLICATE_LIB_OK` didn't help. Fix: split the work. `03` runs the model and saves the distance map; `04` reads files only and runs in the main environment, without PyTorch. A clearer structure anyway, like `01` and `02`.
- The kernel's real error goes to the terminal where `jupyter lab` was started, not to the notebook.
- `overlay` warning "dropped geometries of different types": where a PRODES polygon and an alert only share an edge, the intersection is a line, with no area.

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



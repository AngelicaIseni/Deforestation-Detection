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



# Deforestation Detection

Notebook for monitoring forest cover loss using satellite observation, built as a home assignment for the EO AI Engineer role.

## Area and period
Rondônia deforestation arc, Brazil, a ~10x10 km AOI chosen to guarantee full coverage inside a single Sentinel-2 swath. Comparison between two dry season windows, June-September 2018 (baseline) and June-September 2023 (recent), kept in the same months on both sides so seasonal effects don't get mixed up with the actual change being measured.

## Data backend
Microsoft Planetary Computer — Sentinel-2 L2A, cloud/quality masking via the SCL band.

## Pipeline
- 10 real Sentinel-2 acquisitions (5 baseline 2018, 5 recent 2023)
- Cloud/quality masking via SCL, plus a radiometric scaling correction for Sentinel-2 processing baseline 04.00+ (the BOA_ADD_OFFSET introduced by ESA in Jan 2022, which Planetary Computer doesn't apply automatically). Details and the debugging process are in the notebook.
- Indices computed per acquisition: NDVI, NDMI, EVI, BSI
- Outputs: false-colour composite, NDVI time series, COG raster (`ndvi_change.tif`), GeoJSON vectors (`vegetation_loss.geojson`, `deforestation_alerts.geojson`), interactive map

## Decision intelligence
Double-confirmation rule (NDVI threshold + BSI confirmation, minimum area filter, water body exclusion) to generate a prioritised list of areas to inspect, exported as an alert GeoJSON with a plain-language summary for non-technical stakeholders.

## Bonus completed
Geospatial Foundation Model (Prithvi-EO-2.0 via terratorch), with quantitative comparison against band-math results. The MCP + LLM bonus was not attempted due to time constraints.

## Limitations
Covered in full in the notebook's final section, including threshold calibration against ground truth (e.g. PRODES), the AOI size trade-off, and the fact that comparing only two snapshots (2018, 2023) can't see what happened in between.

## Requirements
Main dependencies: `pystac-client`, `planetary-computer`, `odc-stac`, `rioxarray`, `rasterio`, `geopandas`, `shapely`, `contextily`, `folium`, `terratorch`, `torch`. All installs are handled in the first cells of the notebook, no separate setup needed if running in Colab.

## How to run
Open `Deforestation_Detection.ipynb` in Colab and run all cells in order. Files in `outputs/` are already included in the repo for direct inspection without rerunning the pipeline.

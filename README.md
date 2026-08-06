# Deforestation Detection

Notebook for monitoring forest cover loss using satellite observation, built as a home assignment for the EO AI Engineer role.

## Data backend
Microsoft Planetary Computer — Sentinel-2 L2A, cloud/quality masking via the SCL band.

## Pipeline
- 10 real Sentinel-2 acquisitions (5 baseline 2018, 5 recent 2023)
- Indices computed: NDVI, NDMI, EVI, BSI
- Outputs: false-colour composite, NDVI time series, COG raster (`ndvi_change.tif`), GeoJSON vectors (`vegetation_loss.geojson`, `deforestation_alerts.geojson`), interactive map

## Decision intelligence
Double-confirmation rule (NDVI threshold + BSI confirmation, minimum area filter, water body exclusion) to generate a prioritised list of areas to inspect, exported as an alert GeoJSON.

## Bonus completed
Geospatial Foundation Model (Prithvi-EO-2.0 via terratorch), with quantitative comparison against band-math results. The MCP + LLM bonus was not attempted due to time constraints.

## How to run
Open `Deforestation_Detection.ipynb` in Colab (or Jupyter with the `conda-geoenv` environment) and run all cells in order. Files in `outputs/` are already included in the repo for direct inspection without rerunning the pipeline.

# Data sources and licences

All sources allow **commercial use**. Ship the attribution below in your game credits.
The same strings are written to the `attribution` field of every `.sdhx` / `meta.json`.

| Data | Used for | Licence | Access |
|---|---|---|---|
| [Copernicus DEM GLO-90](https://spacedata.copernicus.eu/collections/copernicus-digital-elevation-model) | Elevation stats, mountains/hills | Copernicus DEM licence, free incl. commercial, attribution required | AWS Open Data COGs `copernicus-dem-90m` |
| [ESA WorldCover 2021 v200](https://esa-worldcover.org/) | Land-cover fractions, dominant class | CC BY 4.0 | AWS Open Data COGs `esa-worldcover` |
| [Natural Earth 1:10m](https://www.naturalearthdata.com/) | Land, lakes, countries, admin-1 provinces | Public domain | `naciscdn.org` |
| [HydroRIVERS v1.0](https://www.hydrosheds.org/products/hydrorivers) | Rivers, discharge-based navigability | HydroSHEDS licence: free incl. commercial, citation required | `data.hydrosheds.org` |

## Required attribution

```
Contains modified Copernicus DEM GLO-90 data, provided under COPERNICUS by the European Union and ESA.
ESA WorldCover 2021 v200, (c) ESA WorldCover project / Contains modified Copernicus Sentinel data (2021) processed by ESA WorldCover consortium. CC BY 4.0.
HydroRIVERS v1.0: Lehner, B., Grill G. (2013), Hydrological Processes 27(15): 2171-2186. Data available at www.hydrosheds.org.
Made with Natural Earth. Free vector and raster map data @ naturalearthdata.com.
```

## Notes

- **Borders are political choices.** Natural Earth shows *de facto* boundaries with its own
  disputed-area conventions (e.g. Kosovo, Crimea, Cyprus, Western Sahara). Review these before
  release, especially for a Balkans-focused game.
- **Not used:** Google Earth Engine (free only for non-commercial/research use without a commercial
  agreement) and Google Maps/Earth imagery (not licensed for redistribution in games).
- Always check the current licence pages before shipping. This file is guidance, not legal advice.

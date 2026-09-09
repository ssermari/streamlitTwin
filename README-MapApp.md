# Warehouse Zone Map Editor

A Streamlit app that loads a warehouse **BaseMap** (a grid of `0`/`1` movable
cells) from MongoDB, lets you paint zone types onto it on an interactive
x/y grid, and saves versioned **ZoneMap** documents back to MongoDB.

## What it does

- **Displays** the grid as a colored x/y map (Plotly), one square per cell.
- **Annotate**: pick a type from the palette, then either
  - click a cell, or drag a box / lasso around several cells on the map and
    click **Apply to selection**, or
  - type an exact row/col rectangle and click **Apply to rectangle**.
- **Undo** the last paint operation, or **revert** all edits back to the
  map you loaded.
- **Saves** a new version to MongoDB, wrapped in metadata:
  ```json
  {
    "metadata": {
      "versionId": 1757430000,       // unix epoch timestamp of creation
      "changeDate": "2026-09-09 14:32:01",   // plain text, editable
      "mapType": "ZoneMap",
      "mapName": "warehouse-01-base",
      "sourceMapId": "warehouse-01-base",
      "width": 16,
      "height": 16,
      "notes": "..."
    },
    "grid": [["T","T","1", ...], ...]
  }
  ```
- Lets you re-open any previously saved version and keep iterating on it.
- Also offers a **Download as JSON** button, independent of MongoDB, as a
  local backup of whatever you're about to save.

## The palette

| Code | Meaning |
|---|---|
| `S` | Storage / Stow Areas |
| `P` | Pick Stations |
| `L` | Load (Put) Stations |
| `T` | Traffic Lanes |
| `Q` | Queue Areas |
| `1` | Same as BaseMap — legal/movable |
| `0` | Same as BaseMap — illegal/not movable |

## MongoDB layout

- **Source collection** (default `base_maps`): one document per BaseMap,
  e.g. `{"mapId": "warehouse-01-base", "mapType": "BaseMap", "name": "...",
  "width": 16, "height": 16, "grid": [[1,1,...], ...]}`.
- **Target collection** (default `zone_maps`): one document per saved
  iteration, in the metadata-wrapped shape shown above. Every save inserts
  a *new* document (a new version) rather than overwriting — that's what
  gives you version history per `mapName`.

## Running it

```bash
pip install -r requirements.txt
export MONGODB_URI="mongodb+srv://user:pass@your-cluster/..."   # optional
export MONGODB_DB="warehouse_mapping"                            # optional
streamlit run app.py
```

Both of those are also editable directly in the app's sidebar, so env vars
are just convenient defaults.

### No MongoDB instance handy?

Toggle **Demo mode** in the sidebar. This switches to an in-memory database
(via `mongomock`) so you can seed the sample BaseMap and try the whole
workflow immediately — nothing is persisted between runs, so switch it off
and point the URI at a real MongoDB once you're ready to keep your work.

### First run against a real database

1. Enter your connection details in the sidebar and click **Test
   connection**.
2. Open **Start from a Base Map** and click **Seed sample BaseMap into
   MongoDB** — this inserts the example 16x16 BaseMap from the original
   spec (`mapId: "warehouse-01-base"`) into your source collection so
   there's something to load. You can otherwise insert your own BaseMap
   documents directly into that collection with the same shape.
3. Load it, paint zones, and save.

## Notes / limits

- Grid values are always normalized to upper-case strings internally
  (`"0"`, `"1"`, `"S"`, `"P"`, `"L"`, `"T"`, `"Q"`); anything else is
  rejected on load with a validation error.
- The click/box/lasso selection on the map is a genuine Plotly chart
  selection (not a custom component), so it degrades gracefully — if a
  future Streamlit/Plotly combination changes selection behavior, the
  coordinate-based rectangle tool still works as a reliable fallback.
- Tested with `streamlit==1.63`, `plotly==7.0`, `pymongo==4.18`.

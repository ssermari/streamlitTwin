"""
Warehouse Zone Map Editor
=========================
A Streamlit app for turning a movable/non-movable BaseMap grid into an
annotated ZoneMap (Storage, Pick, Load, Traffic, Queue areas), backed by
MongoDB for storage of the source BaseMap(s) and versioned ZoneMap(s).

Run with:
    streamlit run app.py

Configuration (all optional, set via environment variables and not shown in the UI):
    MONGO_URI         - connection string (default: mongodb://localhost:27017)
    MONGO_DB          - database name (default: warehouse_mapping)
    MONGO_COLLECTION  - source collection, holding BaseMap docs (default: base_maps)

The target collection (where ZoneMap versions are saved) is always a plain
sidebar text field the user can edit; it defaults to "target_logical_maps".
"""

from __future__ import annotations

import copy
import json
import os
import time
from datetime import datetime

import pandas as pd
import plotly.express as px
import streamlit as st
from pymongo import MongoClient
from pymongo.errors import PyMongoError

try:
    import mongomock
    MONGOMOCK_AVAILABLE = True
except ImportError:
    MONGOMOCK_AVAILABLE = False


# --------------------------------------------------------------------------
# Palette / domain constants
# --------------------------------------------------------------------------

# code -> (label, color). Order here drives legend + palette-picker order.
PALETTE: dict[str, tuple[str, str]] = {
    "S": ("Storage / Stow Areas", "#F4A300"),
    "P": ("Pick Stations", "#2ECC71"),
    "L": ("Load (Put) Stations", "#3498DB"),
    "T": ("Traffic Lanes", "#9B59B6"),
    "Q": ("Queue Areas", "#F1C40F"),
    "1": ("Legal / Movable (Base)", "#FAFAFA"),
    "0": ("Illegal / Not Movable", "#8B8B8B"),
}
VALID_CODES = set(PALETTE.keys())

# The sample BaseMap supplied with this app (0 = non-movable, 1 = movable).
SAMPLE_BASE_MAP = [
    [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
    [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
    [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
    [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
    [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0],
    [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0],
    [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0],
    [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0],
    [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
    [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
    [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
    [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
    [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0],
    [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0],
    [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0],
    [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0],
]
SAMPLE_BASE_MAP_ID = "warehouse-01-base"

DEFAULT_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
DEFAULT_DB = os.environ.get("MONGO_DB", "warehouse_mapping")
DEFAULT_SOURCE_COLL = os.environ.get("MONGO_COLLECTION", "base_maps")
DEFAULT_TARGET_COLL = "target_logical_maps"

UNDO_LIMIT = 25


# --------------------------------------------------------------------------
# Mongo helpers
# --------------------------------------------------------------------------

@st.cache_resource(show_spinner=False)
def get_client(uri: str, use_mock: bool):
    """Cached Mongo client. A new (uri, use_mock) pair creates a new client."""
    if use_mock:
        return mongomock.MongoClient()
    return MongoClient(uri, serverSelectionTimeoutMS=4000)


def test_connection(client, use_mock: bool) -> tuple[bool, str]:
    if use_mock:
        return True, "Using in-memory demo database (mongomock). Data will not persist."
    try:
        client.admin.command("ping")
        return True, "Connected to MongoDB."
    except PyMongoError as exc:
        return False, f"Could not reach MongoDB: {exc}"
    except Exception as exc:  # noqa: BLE001 - surface any driver error to the UI
        return False, f"Could not reach MongoDB: {exc}"


def to_str_grid(raw_grid: list[list]) -> list[list[str]]:
    """Normalize a grid (ints or strings) to a grid of upper-case strings."""
    return [[str(v).strip().upper() for v in row] for row in raw_grid]


def validate_grid(grid: list[list[str]], allowed: set[str]) -> list[str]:
    problems = []
    if not grid or not isinstance(grid, list):
        return ["Grid is empty."]
    width = len(grid[0])
    for r, row in enumerate(grid):
        if len(row) != width:
            problems.append(f"Row {r} has length {len(row)}, expected {width} (ragged grid).")
        for c, v in enumerate(row):
            if v not in allowed:
                problems.append(f"Cell (row {r}, col {c}) has invalid value '{v}'.")
    if len(problems) > 8:
        problems = problems[:8] + [f"...and {len(problems) - 8} more issue(s)."]
    return problems


def list_base_maps(db, source_coll: str) -> list[dict]:
    try:
        cursor = db[source_coll].find({}, {"grid": 0}).sort("mapId", 1)
        return list(cursor)
    except PyMongoError:
        return []


def load_base_map(db, source_coll: str, doc_id) -> dict | None:
    try:
        return db[source_coll].find_one({"_id": doc_id})
    except PyMongoError:
        return None


def list_zone_map_names(db, target_coll: str) -> list[str]:
    try:
        return sorted(db[target_coll].distinct("metadata.mapName"))
    except PyMongoError:
        return []


def list_zone_map_versions(db, target_coll: str, map_name: str) -> list[dict]:
    try:
        cursor = (
            db[target_coll]
            .find({"metadata.mapName": map_name}, {"grid": 0})
            .sort("metadata.versionId", -1)
        )
        return list(cursor)
    except PyMongoError:
        return []


def load_zone_map(db, target_coll: str, doc_id) -> dict | None:
    try:
        return db[target_coll].find_one({"_id": doc_id})
    except PyMongoError:
        return None


def seed_sample_base_map(db, source_coll: str) -> tuple[bool, str]:
    try:
        if db[source_coll].find_one({"mapId": SAMPLE_BASE_MAP_ID}):
            return False, "Sample base map already exists in this collection."
        doc = {
            "mapId": SAMPLE_BASE_MAP_ID,
            "mapType": "BaseMap",
            "name": "Warehouse 01 - Sample Base Layout",
            "width": len(SAMPLE_BASE_MAP[0]),
            "height": len(SAMPLE_BASE_MAP),
            "grid": SAMPLE_BASE_MAP,
            "createdDate": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        db[source_coll].insert_one(doc)
        return True, "Sample base map inserted."
    except PyMongoError as exc:
        return False, f"Could not seed sample base map: {exc}"


def build_zone_map_document(
    grid: list[list[str]],
    map_name: str,
    source_map_id: str,
    notes: str,
    change_date_text: str,
) -> dict:
    height = len(grid)
    width = len(grid[0]) if height else 0
    return {
        "metadata": {
            "versionId": int(time.time()),  # unix timestamp (epoch) of creation
            "changeDate": change_date_text,  # plain text
            "mapType": "ZoneMap",
            "mapName": map_name,
            "sourceMapId": source_map_id,
            "width": width,
            "height": height,
            "notes": notes,
        },
        "grid": grid,
    }


def save_zone_map(db, target_coll: str, document: dict) -> tuple[bool, str]:
    try:
        result = db[target_coll].insert_one(copy.deepcopy(document))
        return True, str(result.inserted_id)
    except PyMongoError as exc:
        return False, f"Could not save zone map: {exc}"


# --------------------------------------------------------------------------
# Grid / chart helpers
# --------------------------------------------------------------------------

def make_figure(grid: list[list[str]]):
    rows = len(grid)
    cols = len(grid[0]) if rows else 0

    xs, ys, types, hover = [], [], [], []
    for r in range(rows):
        for c in range(cols):
            t = grid[r][c] if grid[r][c] in PALETTE else "0"
            label = PALETTE[t][0]
            xs.append(c)
            ys.append(r)
            types.append(t)
            hover.append(f"Row (Y): {r}<br>Col (X): {c}<br>Type: {t} — {label}")

    df = pd.DataFrame({"x": xs, "y": ys, "type": types, "hover": hover})

    color_map = {code: color for code, (_, color) in PALETTE.items()}
    category_order = list(PALETTE.keys())

    fig = px.scatter(
        df,
        x="x",
        y="y",
        color="type",
        color_discrete_map=color_map,
        category_orders={"type": category_order},
        custom_data=["hover"],
    )
    fig.update_traces(
        hovertemplate="%{customdata[0]}<extra></extra>",
        marker=dict(symbol="square", line=dict(width=1, color="rgba(0,0,0,0.25)")),
        selected=dict(marker=dict(opacity=1)),
        unselected=dict(marker=dict(opacity=0.55)),
    )

    cell_px = max(12, min(34, int(600 / max(rows, cols, 1))))
    fig_width = cell_px * cols + 260
    fig_height = cell_px * rows + 90

    fig.update_traces(marker=dict(size=cell_px * 0.92))
    fig.update_layout(
        width=fig_width,
        height=fig_height,
        margin=dict(l=10, r=10, t=10, b=10),
        dragmode="select",
        legend_title_text="Type",
        plot_bgcolor="white",
    )
    tick_step = 1 if max(rows, cols) <= 40 else 5
    fig.update_xaxes(
        range=[-0.5, cols - 0.5],
        tickmode="linear",
        tick0=0,
        dtick=tick_step,
        title="X (col)",
        showgrid=False,
        zeroline=False,
    )
    fig.update_yaxes(
        range=[rows - 0.5, -0.5],  # row 0 at the top, like the source grid
        tickmode="linear",
        tick0=0,
        dtick=tick_step,
        title="Y (row)",
        showgrid=False,
        zeroline=False,
        scaleanchor="x",
        scaleratio=1,
    )
    return fig, fig_width, fig_height


def extract_selected_cells(event, rows: int, cols: int) -> list[tuple[int, int]]:
    cells: set[tuple[int, int]] = set()
    if not event:
        return []
    points = (event.get("selection") or {}).get("points") or []
    for p in points:
        cx, cy = p.get("x"), p.get("y")
        if cx is None or cy is None:
            continue
        col, row = int(round(float(cx))), int(round(float(cy)))
        if 0 <= row < rows and 0 <= col < cols:
            cells.add((row, col))
    return sorted(cells)


def push_undo():
    st.session_state.undo_stack.append(copy.deepcopy(st.session_state.grid))
    if len(st.session_state.undo_stack) > UNDO_LIMIT:
        st.session_state.undo_stack.pop(0)


def legend_chips_html() -> str:
    chips = []
    for code, (label, color) in PALETTE.items():
        text_color = "#111" if code in ("1",) else "#fff" if code not in ("Q",) else "#111"
        chips.append(
            f'<span style="display:inline-flex;align-items:center;margin:2px 8px 2px 0;'
            f'padding:2px 8px;border-radius:12px;background:{color};color:{text_color};'
            f'font-size:12px;border:1px solid rgba(0,0,0,0.15)">{code} · {label}</span>'
        )
    return "<div>" + "".join(chips) + "</div>"


# --------------------------------------------------------------------------
# Streamlit app
# --------------------------------------------------------------------------

def init_state():
    defaults = {
        "grid": None,
        "grid_meta": {},
        "undo_stack": [],
        "use_mock": False,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


def sidebar_connection() -> tuple[object, str, str, str, bool]:
    st.sidebar.header("1. MongoDB connection")
    st.sidebar.caption(
        "Connection URI, database, and source collection are configured via "
        "environment variables and are not shown here."
    )
    uri = DEFAULT_URI
    db_name = DEFAULT_DB
    source_coll = DEFAULT_SOURCE_COLL
    target_coll = st.sidebar.text_input(
        "Target collection (ZoneMaps)", value=DEFAULT_TARGET_COLL,
        help="Where new ZoneMap versions are saved. Defaults to 'target_logical_maps'; "
             "type any collection name you'd like to use instead.",
    )

    col_a, col_b = st.sidebar.columns(2)
    if col_a.button("Test connection", width="stretch"):
        client = get_client(uri, False)
        ok, msg = test_connection(client, False)
        (st.sidebar.success if ok else st.sidebar.error)(msg)
        if not ok and not MONGOMOCK_AVAILABLE:
            st.sidebar.info("Install `mongomock` to try the app without a real MongoDB instance.")

    if MONGOMOCK_AVAILABLE:
        st.session_state.use_mock = col_b.toggle(
            "Demo mode", value=st.session_state.use_mock,
            help="Use an in-memory database instead of the configured connection. Nothing is persisted.",
        )
    else:
        st.session_state.use_mock = False

    use_mock = st.session_state.use_mock
    client = get_client(uri if not use_mock else "mock", use_mock)
    if use_mock:
        st.sidebar.caption("🧪 Demo mode: in-memory MongoDB (mongomock). Data resets on restart.")
    db = client[db_name]
    return db, source_coll, target_coll, uri, use_mock


def sidebar_load(db, source_coll: str, target_coll: str):
    st.sidebar.header("2. Load a map")

    with st.sidebar.expander("Start from a Base Map", expanded=st.session_state.grid is None):
        if st.button("Seed sample BaseMap into MongoDB"):
            ok, msg = seed_sample_base_map(db, source_coll)
            (st.success if ok else st.warning)(msg)

        base_maps = list_base_maps(db, source_coll)
        if base_maps:
            options = {
                f'{d.get("mapId", str(d["_id"]))} — {d.get("name", "")} '
                f'({d.get("width", "?")}x{d.get("height", "?")})': d["_id"]
                for d in base_maps
            }
            choice = st.selectbox("Available base maps", list(options.keys()))
            if st.button("Load base map", type="primary"):
                doc = load_base_map(db, source_coll, options[choice])
                if doc is None:
                    st.error("Could not reload that base map document.")
                elif "grid" not in doc:
                    st.error(
                        "That base map document has no 'grid' field, so it can't be loaded. "
                        "Check that it's a valid BaseMap document."
                    )
                else:
                    grid = to_str_grid(doc["grid"])
                    problems = validate_grid(grid, {"0", "1"})
                    if problems:
                        st.error("Base map failed validation:\n\n" + "\n".join(problems))
                    else:
                        st.session_state.grid = grid
                        st.session_state.grid_meta = {
                            "mapName": doc.get("mapId", "warehouse-map"),
                            "sourceMapId": doc.get("mapId", str(doc["_id"])),
                        }
                        st.session_state.undo_stack = []
                        st.session_state["_backup_stale"] = True
                        st.rerun()
        else:
            st.caption("No base maps found in this collection yet. Seed the sample above, or insert your own BaseMap document with fields: mapId, grid, width, height.")

    with st.sidebar.expander("...or continue a saved Zone Map"):
        names = list_zone_map_names(db, target_coll)
        if names:
            map_name = st.selectbox("Map name", names, key="zm_name_select")
            versions = list_zone_map_versions(db, target_coll, map_name)
            if versions:
                v_options = {
                    f'v{d["metadata"]["versionId"]} — {d["metadata"].get("changeDate", "")}': d["_id"]
                    for d in versions
                }
                v_choice = st.selectbox("Version", list(v_options.keys()), key="zm_version_select")
                if st.button("Load this version", type="primary"):
                    doc = load_zone_map(db, target_coll, v_options[v_choice])
                    if doc is None:
                        st.error("Could not reload that zone map document.")
                    elif "grid" not in doc:
                        st.error(
                            "That zone map document has no 'grid' field, so it can't be loaded. "
                            "Check that it's a valid ZoneMap document."
                        )
                    else:
                        grid = to_str_grid(doc["grid"])
                        problems = validate_grid(grid, VALID_CODES)
                        if problems:
                            st.error("Saved zone map failed validation:\n\n" + "\n".join(problems))
                        else:
                            st.session_state.grid = grid
                            st.session_state.grid_meta = {
                                "mapName": doc["metadata"].get("mapName", map_name),
                                "sourceMapId": doc["metadata"].get("sourceMapId", ""),
                            }
                            st.session_state.undo_stack = []
                            st.session_state["_backup_stale"] = True
                            st.rerun()
        else:
            st.caption("No saved zone maps yet. Save one below once you've made edits.")


def sidebar_save(db, target_coll: str):
    st.sidebar.header("3. Save")
    if st.session_state.grid is None:
        st.sidebar.caption("Load a map first.")
        return

    meta = st.session_state.grid_meta
    map_name = st.sidebar.text_input("Map name", value=meta.get("mapName", "warehouse-map"))
    source_map_id = st.sidebar.text_input("Source map ID", value=meta.get("sourceMapId", ""))
    change_date = st.sidebar.text_input(
        "Change date (plain text)",
        value=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )
    notes = st.sidebar.text_area("Notes (optional)", value="")

    document = build_zone_map_document(
        st.session_state.grid, map_name, source_map_id, notes, change_date
    )

    if st.sidebar.button("💾 Save new version to MongoDB", type="primary"):
        ok, info = save_zone_map(db, target_coll, document)
        if ok:
            st.session_state.grid_meta["mapName"] = map_name
            st.session_state.grid_meta["sourceMapId"] = source_map_id
            st.session_state["_last_save_msg"] = (
                f"Saved as versionId={document['metadata']['versionId']} (_id={info})"
            )
            st.rerun()
        else:
            st.sidebar.error(info)

    if st.session_state.get("_last_save_msg"):
        st.sidebar.success(st.session_state.pop("_last_save_msg"))

    st.sidebar.download_button(
        "⬇️ Download this version as JSON",
        data=json.dumps(document, indent=2),
        file_name=f'{map_name}_v{document["metadata"]["versionId"]}.json',
        mime="application/json",
        width="stretch",
    )


def main_editor():
    grid = st.session_state.grid
    rows, cols = len(grid), len(grid[0])

    st.subheader("Palette")
    codes = list(PALETTE.keys())
    apply_type = st.radio(
        "Type to paint",
        codes,
        format_func=lambda c: f"{c} — {PALETTE[c][0]}",
        horizontal=True,
        label_visibility="collapsed",
    )
    st.markdown(legend_chips_html(), unsafe_allow_html=True)

    st.subheader("Map")
    st.caption(
        "Click a cell, or drag a box / lasso to select multiple cells, then use "
        "**Apply to selection** below. You can also target an exact rectangle by "
        "coordinates further down."
    )
    fig, fig_w, fig_h = make_figure(grid)
    event = st.plotly_chart(
        fig,
        key="grid_chart",
        on_select="rerun",
        width=fig_w,
        height=fig_h,
        config={"displaylogo": False},
    )

    selected_cells = extract_selected_cells(event, rows, cols)

    c1, c2, c3 = st.columns([2, 1, 1])
    with c1:
        st.caption(f"**{len(selected_cells)}** cell(s) currently selected on the map.")
    with c2:
        apply_clicked = st.button(
            f"Apply '{apply_type}' to selection",
            disabled=not selected_cells,
            type="primary",
        )
    with c3:
        undo_clicked = st.button("Undo last change", disabled=not st.session_state.undo_stack)

    if apply_clicked and selected_cells:
        push_undo()
        for r, c in selected_cells:
            st.session_state.grid[r][c] = apply_type
        st.rerun()

    if undo_clicked and st.session_state.undo_stack:
        st.session_state.grid = st.session_state.undo_stack.pop()
        st.rerun()

    with st.expander("Apply to a rectangle by coordinates"):
        rc1, rc2, rc3, rc4, rc5 = st.columns([1, 1, 1, 1, 1.3])
        row_start = rc1.number_input("Row start (Y)", 0, rows - 1, 0)
        row_end = rc2.number_input("Row end (Y)", 0, rows - 1, rows - 1)
        col_start = rc3.number_input("Col start (X)", 0, cols - 1, 0)
        col_end = rc4.number_input("Col end (X)", 0, cols - 1, cols - 1)
        if rc5.button(f"Apply '{apply_type}' to rectangle"):
            push_undo()
            r0, r1 = sorted((int(row_start), int(row_end)))
            c0, c1_ = sorted((int(col_start), int(col_end)))
            for r in range(r0, r1 + 1):
                for c in range(c0, c1_ + 1):
                    st.session_state.grid[r][c] = apply_type
            st.rerun()

    with st.expander("Reset"):
        if st.button("Revert all edits back to the originally loaded map"):
            st.session_state.grid = to_str_grid(st.session_state.get("_loaded_grid_backup", grid))
            st.rerun()

    with st.expander("Cell type counts"):
        counts: dict[str, int] = {code: 0 for code in PALETTE}
        for row in grid:
            for v in row:
                counts[v if v in counts else "0"] += 1
        counts_df = pd.DataFrame(
            [{"Code": c, "Label": PALETTE[c][0], "Count": n} for c, n in counts.items()]
        )
        st.dataframe(counts_df, hide_index=True, width="stretch")

    with st.expander("Raw grid JSON"):
        st.json(grid)

    with st.expander("Document that will be saved to MongoDB"):
        preview = build_zone_map_document(
            grid,
            st.session_state.grid_meta.get("mapName", "warehouse-map"),
            st.session_state.grid_meta.get("sourceMapId", ""),
            "",
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
        st.json(preview)


def main():
    st.set_page_config(page_title="Warehouse Zone Map Editor", layout="wide")
    st.title("🏭 Warehouse Zone Map Editor")
    st.caption(
        "Load a BaseMap, paint zones (Storage, Pick, Load, Traffic, Queue) onto it, "
        "and save versioned ZoneMaps to MongoDB."
    )

    init_state()
    db, source_coll, target_coll, uri, use_mock = sidebar_connection()
    sidebar_load(db, source_coll, target_coll)
    sidebar_save(db, target_coll)

    if st.session_state.grid is None:
        st.info("👈 Load a Base Map or a saved Zone Map from the sidebar to begin editing.")
        return

    if "_loaded_grid_backup" not in st.session_state or st.session_state.get("_backup_stale", True):
        st.session_state["_loaded_grid_backup"] = copy.deepcopy(st.session_state.grid)
        st.session_state["_backup_stale"] = False

    main_editor()


if __name__ == "__main__":
    main()

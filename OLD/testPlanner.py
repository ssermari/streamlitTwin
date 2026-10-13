"""
testPlanner.py - Streamlit page for building and sending JSON test calls
to the AI path planning agent (AWS Bedrock AgentCore).

Environment variables:
    AWS_KEY        (or AWS-KEY)
    AWS_SECRET     (or AWS-SECRET)
    AWS_AGENT_ARN  (or AWS-AGENT-ARN)
"""

import hashlib
import json
import os
import time
from datetime import date

import boto3
import pymongo
from pymongo.errors import PyMongoError
import streamlit as st

# ---------------------------------------------------------------------------
# Page config (must be the first Streamlit call)
# ---------------------------------------------------------------------------
PAGE_ICON = ":material/science:"  # lab beaker/flask icon, also used as the browser-tab icon
st.set_page_config(page_title="Radiant PathPlanning Engine - Test Harness",
                   page_icon=PAGE_ICON, layout="wide")

# ---------------------------------------------------------------------------
# Password protection (one-way SHA-256 hash; plaintext is never stored)
# ---------------------------------------------------------------------------
PASSWORD_HASH = "fde302715bb7034fb5cf0115b76799180b2871140329ea424b2bed99b80ab0d4"


def check_password() -> bool:
    if st.session_state.get("tp_authenticated"):
        return True

    def _on_submit():
        entered = st.session_state.get("tp_password", "")
        if hashlib.sha256(entered.encode("utf-8")).hexdigest() == PASSWORD_HASH:
            st.session_state["tp_authenticated"] = True
            st.session_state.pop("tp_password", None)
        else:
            st.session_state["tp_authenticated"] = False
            st.session_state["tp_bad_password"] = True

    st.title(f"{PAGE_ICON} Radiant PathPlanning Engine - Test Harness")
    st.text_input("Password", type="password", key="tp_password", on_change=_on_submit)
    if st.session_state.get("tp_bad_password"):
        st.error("Incorrect password")
    return False


if not check_password():
    st.stop()

# ---------------------------------------------------------------------------
# AWS / AgentCore helpers
# ---------------------------------------------------------------------------
def _env(*names):
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return None


AWS_KEY = _env("AWS_KEY", "AWS-KEY")
AWS_SECRET = _env("AWS_SECRET", "AWS-SECRET")
AGENT_ARN = _env("AWS_AGENT_ARN", "AWS-AGENT-ARN")


@st.cache_resource
def get_agent_client(key, secret):
    session = boto3.Session(
        aws_access_key_id=key,
        aws_secret_access_key=secret,
        region_name="us-east-1",
    )
    return session.client("bedrock-agentcore")


def invoke_agent(agent_core_client, agent_arn, session_id, prompt):
    payload = json.dumps({"prompt": prompt}).encode()
    response = agent_core_client.invoke_agent_runtime(
        agentRuntimeArn=agent_arn,
        runtimeSessionId=session_id,
        payload=payload,
    )
    return response


def process_response(response):
    content_type = response.get("contentType", "")
    if "text/event-stream" in content_type:
        content = [
            line.decode("utf-8")[6:]
            for line in response["response"].iter_lines(chunk_size=10)
            if line and line.decode("utf-8").startswith("data: ")
        ]
        return content
    elif content_type == "application/json":
        content = json.loads(b"".join(response.get("response", [])).decode("utf-8"))
        return content
    else:
        return response


def flatten_prompt(prompt):
    return prompt.replace("\r\n", "  ").replace("\n", "  ")


def describe_json_error(text: str, err: json.JSONDecodeError) -> str:
    lines = text.splitlines()
    bad_line = lines[err.lineno - 1] if 0 < err.lineno <= len(lines) else ""
    pointer = " " * max(err.colno - 1, 0) + "^"
    return (
        f"{err.msg} (line {err.lineno}, column {err.colno})\n"
        f"{bad_line}\n{pointer}"
    )


def show_error(msg: str):
    """First line as an error box; any following lines (the offending JSON
    line and the ^ pointer) in a monospaced block so the pointer lines up."""
    first, _, rest = msg.partition("\n")
    st.error(first)
    if rest.strip():
        st.code(rest, language="text")


def parse_meta_prompt(text: str):
    """(dict, error_message). Blank = no extra fields; otherwise it must be a
    JSON object, since its fields are merged into the mission."""
    if not text or not text.strip():
        return {}, None
    try:
        meta = json.loads(text)
    except json.JSONDecodeError as e:
        return None, f"Meta Prompt is not valid JSON: {describe_json_error(text, e)}"
    if not isinstance(meta, dict):
        return None, ("Meta Prompt must be a JSON object, e.g. "
                      '{"carrier_type": "C52X52"}')
    return meta, None


def to_pretty(result):
    """Turn whatever the agent returned into (raw_lines, pretty_json_or_None)."""
    if isinstance(result, list):  # event-stream lines
        raw_lines = [str(x) for x in result]
        # Try the whole stream as one JSON doc, then each line individually.
        joined = "".join(raw_lines)
        for candidate in (joined, "\n".join(raw_lines)):
            try:
                parsed = json.loads(candidate)
                # Event streams often send a JSON-encoded string containing JSON
                if isinstance(parsed, str):
                    try:
                        parsed = json.loads(parsed)
                    except json.JSONDecodeError:
                        pass
                return raw_lines, parsed
            except json.JSONDecodeError:
                pass
        parsed_lines = []
        for ln in raw_lines:
            try:
                p = json.loads(ln)
                if isinstance(p, str):
                    try:
                        p = json.loads(p)
                    except json.JSONDecodeError:
                        pass
                parsed_lines.append(p)
            except json.JSONDecodeError:
                parsed_lines.append(ln)
        return raw_lines, parsed_lines
    if isinstance(result, dict):
        # Unknown content type returns the raw boto response; drop the stream object
        if "ResponseMetadata" in result:
            return None, {k: v for k, v in result.items() if k != "response"}
        return None, result
    return [str(result)], None


# ---------------------------------------------------------------------------
# Defaults & state
# ---------------------------------------------------------------------------
DEFAULT_JSON = """{
  "mission": {
    "carriers": [
      {
        "carrier_id": "s1",
        "flow": "stow_load"
      },
      {
        "carrier_id": "s2",
        "flow": "stow_load"
      },
      {
        "carrier_id": "s3",
        "flow": "stow_load"
      },
      {
        "carrier_id": "s4",
        "flow": "stow_load"
      },
      {
        "carrier_id": "s5",
        "flow": "stow_load"
      },
      {
        "carrier_id": "s6",
        "flow": "stow_load"
      },
      {
        "carrier_id": "s7",
        "flow": "stow_load"
      },
      {
        "carrier_id": "s8",
        "parked": true
      },
      {
        "carrier_id": "s9",
        "parked": true
      },
      {
        "carrier_id": "s10",
        "parked": true
      },
      {
        "carrier_id": "s11",
        "parked": true
      },
      {
        "carrier_id": "s12",
        "parked": true
      },
      {
        "carrier_id": "s13",
        "parked": true
      },
      {
        "carrier_id": "s14",
        "parked": true
      },
      {
        "carrier_id": "e1",
        "flow": "induct_build"
      },
      {
        "carrier_id": "e2",
        "flow": "induct_build"
      },
      {
        "carrier_id": "e3",
        "flow": "induct_build"
      }
    ],
    "continue": true,
    "parked_carriers": 28,
    "budget_s": 20
  }
}"""

# Default for Input boxes when the selected map name starts with "Sortation"
# (case-insensitive). test_id / map below are replaced by the header values
# at run time.
SORTATION_DEFAULT_JSON = """{
  "mission": {
    "map": "Sortation-Example-1",
    "carrier_type": "C9X9",
    "pallets": [
      {
        "carrier_id": "m1",
        "start": "L1",
        "steps": [
          {
            "flow": "put_pick",
            "pick_station": "P6"
          },
          "pick_put",
          {
            "flow": "put_pick",
            "pick_station": "P3"
          },
          "pick_put"
        ]
      },
      {
        "carrier_id": "m2",
        "start": "L2",
        "steps": [
          {
            "flow": "put_pick",
            "pick_station": "P7"
          },
          "pick_put",
          {
            "flow": "put_pick",
            "pick_station": "P11"
          },
          "pick_put"
        ]
      },
      {
        "carrier_id": "m3",
        "start": "L3",
        "steps": [
          {
            "flow": "put_pick",
            "pick_station": "P1"
          },
          "pick_put",
          {
            "flow": "put_pick",
            "pick_station": "P2"
          },
          "pick_put"
        ]
      },
      {
        "carrier_id": "m4",
        "start": "L4",
        "steps": [
          {
            "flow": "put_pick",
            "pick_station": "P9"
          },
          "pick_put",
          {
            "flow": "put_pick",
            "pick_station": "P2"
          },
          "pick_put"
        ]
      },
      {
        "carrier_id": "m5",
        "start": "L5",
        "steps": [
          {
            "flow": "put_pick",
            "pick_station": "P6"
          },
          "pick_put",
          {
            "flow": "put_pick",
            "pick_station": "P10"
          },
          "pick_put"
        ]
      },
      {
        "carrier_id": "m6",
        "start": "L6",
        "steps": [
          {
            "flow": "put_pick",
            "pick_station": "P1"
          },
          "pick_put",
          {
            "flow": "put_pick",
            "pick_station": "P9"
          },
          "pick_put"
        ]
      },
      {
        "carrier_id": "m7",
        "start": "L7",
        "steps": [
          {
            "flow": "put_pick",
            "pick_station": "P4"
          },
          "pick_put",
          {
            "flow": "put_pick",
            "pick_station": "P1"
          },
          "pick_put"
        ]
      },
      {
        "carrier_id": "m8",
        "start": "L8",
        "steps": [
          {
            "flow": "put_pick",
            "pick_station": "P2"
          },
          "pick_put",
          {
            "flow": "put_pick",
            "pick_station": "P7"
          },
          "pick_put"
        ]
      },
      {
        "carrier_id": "m9",
        "start": "L9",
        "steps": [
          {
            "flow": "put_pick",
            "pick_station": "P7"
          },
          "pick_put",
          {
            "flow": "put_pick",
            "pick_station": "P2"
          },
          "pick_put"
        ]
      },
      {
        "carrier_id": "m10",
        "start": "L10",
        "steps": [
          {
            "flow": "put_pick",
            "pick_station": "P4"
          },
          "pick_put",
          {
            "flow": "put_pick",
            "pick_station": "P2"
          },
          "pick_put"
        ]
      },
      {
        "carrier_id": "m11",
        "start": "L11",
        "steps": [
          {
            "flow": "put_pick",
            "pick_station": "P9"
          },
          "pick_put",
          {
            "flow": "put_pick",
            "pick_station": "P7"
          },
          "pick_put"
        ]
      },
      {
        "carrier_id": "m12",
        "start": "L12",
        "steps": [
          {
            "flow": "put_pick",
            "pick_station": "P1"
          },
          "pick_put",
          {
            "flow": "put_pick",
            "pick_station": "P10"
          },
          "pick_put"
        ]
      },
      {
        "carrier_id": "m13",
        "start": "L13",
        "steps": [
          {
            "flow": "put_pick",
            "pick_station": "P2"
          },
          "pick_put",
          {
            "flow": "put_pick",
            "pick_station": "P4"
          },
          "pick_put"
        ]
      },
      {
        "carrier_id": "m14",
        "start": "L14",
        "steps": [
          {
            "flow": "put_pick",
            "pick_station": "P11"
          },
          "pick_put",
          {
            "flow": "put_pick",
            "pick_station": "P11"
          },
          "pick_put"
        ]
      },
      {
        "carrier_id": "m15",
        "start": "L15",
        "steps": [
          {
            "flow": "put_pick",
            "pick_station": "P10"
          },
          "pick_put",
          {
            "flow": "put_pick",
            "pick_station": "P1"
          },
          "pick_put"
        ]
      },
      {
        "carrier_id": "m16",
        "start": "L16",
        "steps": [
          {
            "flow": "put_pick",
            "pick_station": "P10"
          },
          "pick_put",
          {
            "flow": "put_pick",
            "pick_station": "P10"
          },
          "pick_put"
        ]
      }
    ]
  }
}"""


def is_sortation_map(map_name) -> bool:
    return bool(map_name) and str(map_name).lower().startswith("sortation")


def default_input_json(map_name) -> str:
    return SORTATION_DEFAULT_JSON if is_sortation_map(map_name) else DEFAULT_JSON


# Session ID: created once per browser page load (a reload = new session state)
if "tp_session_id" not in st.session_state:
    # AgentCore requires runtimeSessionId >= 33 chars: "TEST-" (5) + 28-digit zero-padded epoch
    st.session_state["tp_session_id"] = f"TEST-{int(time.time()):028d}"

st.session_state.setdefault("tp_gen", 0)        # bumped on reset -> fresh widgets
st.session_state.setdefault("tp_batches", 1)    # number of Input boxes
st.session_state.setdefault("tp_ran", set())    # indices of inputs that were run
st.session_state.setdefault("tp_outputs", [])   # list of result dicts


def reset_all():
    st.session_state["tp_gen"] += 1
    st.session_state["tp_batches"] = 1
    st.session_state["tp_ran"] = set()
    st.session_state["tp_outputs"] = []


def add_batch():
    st.session_state["tp_batches"] += 1


gen = st.session_state["tp_gen"]
SESSION_ID = st.session_state["tp_session_id"]

# ---------------------------------------------------------------------------
# Zone Map selection (same MongoDB secrets / collection as dtMongo.py).
# Only the map name + versionId are used here; the map itself is never drawn.
# ---------------------------------------------------------------------------
DEFAULT_ZONE_MAP_COLLECTION = "target_logical_maps"
META_PROMPT_DEFAULT = '{"carrier_type": "C52X52"}'
SORTATION_META_PROMPT_DEFAULT = ""  # Sortation maps: no Meta Prompt by default


def default_meta_prompt(map_name) -> str:
    return SORTATION_META_PROMPT_DEFAULT if is_sortation_map(map_name) else META_PROMPT_DEFAULT


@st.cache_resource
def get_mongo_client(uri):
    return pymongo.MongoClient(uri)


def get_zone_db():
    """(db, collection_name, error_message). Never raises."""
    try:
        client = get_mongo_client(st.secrets["MONGO_URI"])
        db = client[st.secrets["MONGO_DB"]]
        coll = st.secrets.get("MONGO_ZONE_COLLECTION", DEFAULT_ZONE_MAP_COLLECTION)
        return db, coll, None
    except Exception as e:
        return None, None, f"MongoDB connection failed: {e}"


def list_zone_map_names(db, coll):
    try:
        return sorted(db[coll].distinct("metadata.mapName"))
    except PyMongoError:
        return []


def list_zone_map_versions(db, coll, map_name):
    """Newest versionId first; the grid itself is never fetched."""
    try:
        return list(
            db[coll]
            .find({"metadata.mapName": map_name}, {"grid": 0})
            .sort("metadata.versionId", -1)
        )
    except PyMongoError:
        return []



# ---------------------------------------------------------------------------
# CSS: frozen header, 100-char Test ID box, orange "ran" buttons
# ---------------------------------------------------------------------------
ran_css = "\n".join(
    f"""
    .st-key-run_{gen}_{i} button {{
        background-color: #FF8C00 !important;
        border-color: #FF8C00 !important;
        color: #FFFFFF !important;
    }}"""
    for i in st.session_state["tp_ran"]
)
st.markdown(
    f"""
    <style>
    /* Remove Streamlit's default ~6rem top gap */
    [data-testid="stMainBlockContainer"], .block-container {{
        padding-top: 1rem !important;
    }}
    /* Let the toolbar strip float transparently so content can start at the top */
    [data-testid="stHeader"] {{
        background: transparent !important;
    }}
    /* Frozen header. Newer Streamlit wraps each container in a
       stLayoutWrapper exactly as tall as the container, which leaves a
       sticky element nowhere to stick, so pin the wrapper instead. The rule
       on the container itself covers older Streamlit without the wrapper. */
    [data-testid="stLayoutWrapper"]:has(> .st-key-tp_header) {{
        position: sticky;
        top: 0;
        z-index: 999;
    }}
    .st-key-tp_header {{
        position: sticky;
        top: 0;
        z-index: 999;
        padding-top: 0.25rem;
        padding-bottom: 0.75rem;
        border-bottom: 1px solid rgba(128, 128, 128, 0.45);  /* the header divider */
    }}
    /* Header background = the page background in any theme, updated live:
       Streamlit paints the theme colour on .stApp, and every element
       between it and the header (all transparent anyway) inherits it, so
       the header always matches, even after switching light/dark. */
    .stApp :has(.st-key-tp_header), .st-key-tp_header {{
        background-color: inherit !important;
    }}
    .st-key-tp_header h1 {{
        font-size: 2.25rem !important;  /* slightly smaller than Streamlit's 2.75rem */
        padding-top: 0 !important;
        padding-bottom: 0.25rem !important;
    }}
    /* Meta Prompt: one line tall by default, user can drag it taller */
    .st-key-tp_header [class*="st-key-meta_"] [data-testid="stTextAreaRootElement"] {{
        height: auto !important;
        min-height: 0 !important;
    }}
    .st-key-tp_header [class*="st-key-meta_"] textarea {{
        height: 40px;
        min-height: 40px !important;
        padding-top: 8px !important;
        padding-bottom: 8px !important;
        resize: vertical !important;
    }}
    [class*="st-key-testid_"] {{
        width: calc(100ch + 2rem) !important;
        max-width: 100% !important;
    }}
    {ran_css}
    </style>
    """,
    unsafe_allow_html=True,
)

# ---------------------------------------------------------------------------
# Frozen header
# ---------------------------------------------------------------------------
with st.container(key="tp_header"):
    st.title(f"{PAGE_ICON} Radiant PathPlanning Engine - Test Harness")

    zdb, zcoll, zerr = get_zone_db()
    sel_map_name, sel_version_id = None, None
    mc1, mc2, mc3, _mc4 = st.columns([2, 2, 1.2, 1.8], vertical_alignment="bottom")
    with mc3:
        st.button("CLEAR AND RESET", on_click=reset_all, use_container_width=True)
    if zerr:
        st.error(zerr)
    else:
        zm_names = list_zone_map_names(zdb, zcoll)
        if not zm_names:
            st.warning(f"No saved Zone Maps found in the '{zcoll}' collection.")
        else:
            with mc1:
                sel_map_name = st.selectbox("Map name", zm_names, key=f"tp_map_{gen}")
            versions = list_zone_map_versions(zdb, zcoll, sel_map_name)
            with mc2:
                if versions:
                    v_ids = [d["metadata"]["versionId"] for d in versions]
                    v_dates = {d["metadata"]["versionId"]: d["metadata"].get("changeDate", "")
                               for d in versions}
                    sel_version_id = st.selectbox(
                        "Version", v_ids, key=f"tp_ver_{gen}_{sel_map_name}",
                        format_func=lambda v: f"v{v} — {v_dates.get(v, '')}",
                    )
                else:
                    st.caption("No versions found for that map.")

    meta_key = f"meta_{gen}"

    test_id = st.text_input(
        "Enter Test ID",
        value=f"test-name-{date.today().isoformat()}",
        key=f"testid_{gen}",
    )
    # Meta Prompt default depends on the selected map; an untouched default
    # follows map changes, an edited Meta Prompt is left alone.
    _want_meta = default_meta_prompt(sel_map_name)
    _cur_meta = st.session_state.get(meta_key)
    if _cur_meta is None or (_cur_meta in (META_PROMPT_DEFAULT, SORTATION_META_PROMPT_DEFAULT)
                             and _cur_meta != _want_meta):
        st.session_state[meta_key] = _want_meta
    meta_raw = st.text_area(
        "Meta Prompt",
        key=meta_key,
        height=68,  # Streamlit's minimum; CSS below trims it to one line
        help="Meta Prompt fields as a JSON object, such as carrier type. They are "
             "added to the mission right after test_id / map. Drag the bottom-right "
             "corner to make the box taller.",
    )
    _, _meta_err = parse_meta_prompt(meta_raw)
    if _meta_err:
        show_error(_meta_err)

# ---------------------------------------------------------------------------
# Body: left = inputs, right = outputs
# ---------------------------------------------------------------------------
left, right = st.columns(2, gap="large")


def run_test(idx: int, meta_raw: str, raw_json: str):
    """Validate, build the prompt, call the agent, record the output."""
    entry = {"idx": idx, "test_id": test_id, "time": time.strftime("%H:%M:%S")}

    meta, meta_err = parse_meta_prompt(meta_raw)
    if meta_err:
        entry["error"] = meta_err
        st.session_state["tp_outputs"].append(entry)
        return

    try:
        parsed = json.loads(raw_json)
    except json.JSONDecodeError as e:
        entry["error"] = f"Input - {idx} is not valid JSON: {describe_json_error(raw_json, e)}"
        st.session_state["tp_outputs"].append(entry)
        return

    # Stamp the Test ID into the mission object (last key, overwrites any existing one)
    mission = parsed.get("mission") if isinstance(parsed, dict) else None
    if not isinstance(mission, dict):
        entry["error"] = (
            f'Input - {idx} must contain a "mission" object so the test_id can be added.'
        )
        st.session_state["tp_outputs"].append(entry)
        return
    # test_id: replaced in place if the input already has one; otherwise it
    # takes the place of the input's "map" if there is one, else it goes at
    # the end. Right after it come "map" (the selected map name, or with no
    # map selected any "map" typed in the input) and then every Meta Prompt
    # field, which replaces the same field in the input.
    map_value = sel_map_name if sel_map_name else mission.get("map")
    meta = {k: v for k, v in meta.items() if k not in ("test_id", "map")}
    if "test_id" not in mission and "map" in mission:
        mission = {("test_id" if k == "map" else k): v for k, v in mission.items()}
        parsed["mission"] = mission
    for k in ("map", *meta):
        mission.pop(k, None)
    if "test_id" not in mission:
        mission["test_id"] = None
    rebuilt = {}
    for k, v in mission.items():
        if k == "test_id":
            rebuilt["test_id"] = test_id.strip()
            if map_value:
                rebuilt["map"] = map_value
            rebuilt.update(meta)
        else:
            rebuilt[k] = v
    mission.clear()
    mission.update(rebuilt)

    missing = [n for n, v in (("AWS_KEY", AWS_KEY), ("AWS_SECRET", AWS_SECRET),
                              ("AWS_AGENT_ARN", AGENT_ARN)) if not v]
    if missing:
        entry["error"] = "Missing environment variable(s): " + ", ".join(missing)
        st.session_state["tp_outputs"].append(entry)
        return

    prompt = json.dumps(parsed, separators=(",", ":"))
    entry["prompt"] = prompt
    entry["sent_json"] = parsed

    start = time.perf_counter()
    try:
        client = get_agent_client(AWS_KEY, AWS_SECRET)
        response = invoke_agent(client, AGENT_ARN, SESSION_ID, prompt)
        result = process_response(response)
        entry["raw_lines"], entry["pretty"] = to_pretty(result)
    except Exception as e:  # surface AWS / network errors in the output pane
        entry["error"] = f"Agent call failed: {type(e).__name__}: {e}"
    entry["seconds"] = time.perf_counter() - start

    st.session_state["tp_ran"].add(idx)
    st.session_state["tp_outputs"].append(entry)
    st.rerun()  # re-render so the button turns orange immediately


with left:

    # Input defaults depend on the selected map. A box still holding an
    # untouched default follows map changes; an edited box is left alone.
    want_default = default_input_json(sel_map_name)
    for i in range(1, st.session_state["tp_batches"] + 1):
        in_key = f"in_{gen}_{i}"
        cur = st.session_state.get(in_key)
        if cur is None or (cur in (DEFAULT_JSON, SORTATION_DEFAULT_JSON) and cur != want_default):
            st.session_state[in_key] = want_default

    for i in range(1, st.session_state["tp_batches"] + 1):
        lc, bc = st.columns([3, 2], vertical_alignment="bottom")
        lc.markdown(f"**Input - {i}**")
        clicked = bc.button(f"RUN TEST: Input - {i}", key=f"run_{gen}_{i}")
        raw = st.text_area(
            f"Input - {i}",
            key=f"in_{gen}_{i}",  # default set above from the selected map
            height=300,  # ~12 lines, scrollable
            label_visibility="collapsed",
        )
        if clicked:
            with st.spinner(f"Running Input - {i}..."):
                run_test(i, meta_raw, raw)

    st.button("Add chained batch", on_click=add_batch)

with right:
    st.markdown("**Output**")
    st.caption(f"Session ID: `{SESSION_ID}`")
    if not st.session_state["tp_outputs"]:
        st.info("Run a test to see results here.")
    # Newest result first
    for entry in reversed(st.session_state["tp_outputs"]):
        with st.container(border=True):
            st.caption(f"{entry['time']} · Test ID: {entry['test_id']}")
            if "seconds" in entry:
                st.markdown(
                    f"**Executed planning input - {entry['idx']} in {entry['seconds']:.2f} seconds.**"
                )
            if entry.get("error"):
                show_error(entry["error"])
                continue
            with st.expander("Prompt sent"):
                st.markdown("Mission JSON (incl. test_id, map and Meta Prompt):")
                st.code(json.dumps(entry["sent_json"], indent=2), language="json")
                st.caption("Exact payload sent (compact JSON in one string):")
                st.code(json.dumps({"prompt": entry["prompt"]}), language="json", wrap_lines=True)
            if entry.get("raw_lines"):
                st.markdown("Returned lines:")
                st.code("\n".join(entry["raw_lines"]), language="text")
            if entry.get("pretty") is not None:
                with st.expander("Returned output"):
                    st.code(json.dumps(entry["pretty"], indent=2, default=str), language="json")

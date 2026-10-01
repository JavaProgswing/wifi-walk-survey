#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════╗
║  WiFi Survey — Mobile Import & Normalizer  v1.0            ║
║  Converts a mobile_survey.json (from mobile_server.py)     ║
║  into the dashboard format and generates the HTML report.  ║
╠══════════════════════════════════════════════════════════════╣
║  Usage:                                                      ║
║    python import_mobile.py mobile_survey.json               ║
║    python import_mobile.py *.json --merge --out combined    ║
║    python import_mobile.py survey.json --no-gps             ║
╚══════════════════════════════════════════════════════════════╝
"""

import sys, os, json, math, argparse, datetime
from pathlib import Path

VERSION = "1.0"

# ── Colour helpers (terminal) ──────────────────────────────────────────────────
class C:
    if sys.stdout.isatty():
        R="\033[0m"; B="\033[1m"; D="\033[2m"
        GRN="\033[92m"; YLW="\033[93m"; CYN="\033[96m"
        RED="\033[91m"; MGT="\033[95m"
    else:
        R=B=D=GRN=YLW=CYN=RED=MGT=""

def ok(s):   print(f"  {C.GRN}✓{C.R}  {s}")
def warn(s): print(f"  {C.YLW}⚠{C.R}  {s}")
def err(s):  print(f"  {C.RED}✗{C.R}  {s}")
def info(s): print(f"  {C.CYN}·{C.R}  {s}")

# ── GPS → local XY ─────────────────────────────────────────────────────────────
# Uses equirectangular projection — accurate to < 1% for areas up to ~5 km
# This is the same approximation used by NetSpot and most RF survey tools.

def haversine_m(lat1, lon1, lat2, lon2):
    """Great-circle distance in metres between two GPS coordinates."""
    R = 6_371_000
    φ1, φ2 = math.radians(lat1), math.radians(lat2)
    dφ = math.radians(lat2 - lat1)
    dλ = math.radians(lon2 - lon1)
    a = math.sin(dφ/2)**2 + math.cos(φ1)*math.cos(φ2)*math.sin(dλ/2)**2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1-a))

def latlon_to_xy(lat, lon, origin_lat, origin_lon):
    """
    Convert GPS coordinates to local X/Y in metres.
    Origin is the southwest corner (min lat, min lon) of the survey bounding box.
    X → East (positive),  Y → North (positive)
    """
    dx = (lon - origin_lon) * 111_320 * math.cos(math.radians(origin_lat))
    dy = (lat - origin_lat) * 111_320
    return dx, dy

def compute_bounding_box(waypoints_with_gps):
    """Find min/max lat/lon from waypoints that have GPS data."""
    lats = [w["lat"] for w in waypoints_with_gps if w.get("lat") is not None]
    lons = [w["lon"] for w in waypoints_with_gps if w.get("lon") is not None]
    if not lats:
        return None
    return {
        "min_lat": min(lats), "max_lat": max(lats),
        "min_lon": min(lons), "max_lon": max(lons),
    }

def gps_waypoints_to_xy(waypoints, padding_m=2.0):
    """
    Convert all GPS waypoints to local XY.
    Returns (converted_waypoints, area_width_m, area_height_m, bbox)
    """
    gps_wps = [w for w in waypoints if w.get("lat") is not None]
    bbox    = compute_bounding_box(gps_wps)
    if not bbox:
        return waypoints, 20.0, 15.0, None

    origin_lat = bbox["min_lat"]
    origin_lon = bbox["min_lon"]

    # Compute area dimensions in metres
    area_w = haversine_m(origin_lat, origin_lon, origin_lat, bbox["max_lon"]) + 2*padding_m
    area_h = haversine_m(origin_lat, origin_lon, bbox["max_lat"], origin_lon) + 2*padding_m

    converted = []
    for wp in waypoints:
        wp = dict(wp)  # shallow copy
        if wp.get("lat") is not None:
            x, y = latlon_to_xy(wp["lat"], wp["lon"], origin_lat, origin_lon)
            # Add padding so edge waypoints aren't on the border
            x += padding_m
            y += padding_m
        elif wp.get("manual_x") is not None:
            x, y = float(wp["manual_x"]), float(wp["manual_y"])
        else:
            x, y = 0.0, 0.0
        wp["x"] = round(x, 2)
        wp["y"] = round(y, 2)
        converted.append(wp)

    return converted, round(area_w, 1), round(area_h, 1), bbox

def manual_waypoints_to_xy(waypoints):
    """For surveys using manual XY (no GPS). Just read manual_x/manual_y."""
    converted = []
    max_x, max_y = 0, 0
    for wp in waypoints:
        wp = dict(wp)
        x = float(wp.get("manual_x") or wp.get("x") or 0)
        y = float(wp.get("manual_y") or wp.get("y") or 0)
        wp["x"] = x; wp["y"] = y
        max_x = max(max_x, x); max_y = max(max_y, y)
        converted.append(wp)
    area_w = max_x + 2.0
    area_h = max_y + 2.0
    return converted, round(area_w, 1), round(area_h, 1), None

# ── AP normalisation ───────────────────────────────────────────────────────────
def normalise_ap(ap):
    """
    Normalise an AP dict from mobile format to dashboard format.
    Mobile format (termux-wifi-scaninfo normalised by mobile_server.py):
      {ssid, bssid, band, channel, rssi, security, freq_mhz}
    Dashboard format:
      {ssid, bssid, band, channel, rssi, security}
    """
    return {
        "ssid":     ap.get("ssid", "<Hidden>") or "<Hidden>",
        "bssid":    (ap.get("bssid", "") or "").upper(),
        "band":     ap.get("band", "Unknown"),
        "channel":  str(ap.get("channel", "?")),
        "rssi":     int(ap.get("rssi", -100)),
        "security": ap.get("security", ""),
    }

# ── Merge multiple survey files ────────────────────────────────────────────────
def merge_surveys(survey_list):
    """Merge multiple mobile survey dicts into one, renumbering waypoints."""
    merged_waypoints = []
    ctr = 1
    for s in survey_list:
        for wp in s.get("waypoints", []):
            wp = dict(wp)
            wp["id"]          = ctr
            wp["source_file"] = s.get("_source_file", "unknown")
            merged_waypoints.append(wp)
            ctr += 1
    meta = survey_list[0].get("meta", {}) if survey_list else {}
    meta["merged_from"] = len(survey_list)
    meta["merged_at"]   = datetime.datetime.now().isoformat()
    return {"meta": meta, "waypoints": merged_waypoints}

# ── Build dashboard-compatible survey dict ─────────────────────────────────────
def build_survey_dict(mobile_data, area_w, area_h, converted_wps,
                      source_files=None, bbox=None):
    meta = mobile_data.get("meta", {})
    waypoints_out = []
    for wp in converted_wps:
        waypoints_out.append({
            "id":        wp["id"],
            "timestamp": wp.get("timestamp", datetime.datetime.now().isoformat()),
            "x":         wp.get("x", 0.0),
            "y":         wp.get("y", 0.0),
            "label":     wp.get("label", f"WP{wp['id']}"),
            "aps":       [normalise_ap(a) for a in wp.get("aps", [])],
            # Keep raw GPS for reference
            "gps": {
                "lat":      wp.get("lat"),
                "lon":      wp.get("lon"),
                "accuracy": wp.get("accuracy"),
            } if wp.get("lat") is not None else None,
        })

    return {
        "survey_info": {
            "created":      meta.get("created", datetime.datetime.now().isoformat()),
            "interface":    "mobile-android",
            "os":           meta.get("platform", "android"),
            "area_width":   area_w,
            "area_height":  area_h,
            "tool_version": VERSION,
            "source":       "mobile_import",
            "source_files": source_files or [],
            "gps_bbox":     bbox,
        },
        "waypoints": waypoints_out,
    }

# ── Load the dashboard generator from wifi_survey.py ──────────────────────────
def load_generate_dashboard():
    """Try to import generate_dashboard from wifi_survey.py in the same dir."""
    script_dir = Path(__file__).parent
    for candidate in [script_dir / "wifi_survey.py",
                      Path("wifi_survey.py"),
                      Path("../wifi_survey.py")]:
        if candidate.exists():
            import importlib.util
            spec   = importlib.util.spec_from_file_location("wifi_survey", candidate)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module.generate_dashboard, str(candidate)
    return None, None

# ── Stats printer ─────────────────────────────────────────────────────────────
def print_stats(survey_dict):
    wps = survey_dict["waypoints"]
    si  = survey_dict["survey_info"]
    all_aps = {}
    for wp in wps:
        for ap in wp["aps"]:
            if ap["bssid"] not in all_aps:
                all_aps[ap["bssid"]] = ap
    print()
    print(f"  {C.B}{'─'*48}{C.R}")
    info(f"Waypoints:    {C.B}{len(wps)}{C.R}")
    info(f"Unique APs:   {C.B}{len(all_aps)}{C.R}")
    info(f"Area:         {C.B}{si['area_width']}m × {si['area_height']}m{C.R}")
    if si.get("gps_bbox"):
        b = si["gps_bbox"]
        info(f"GPS bbox:     {b['min_lat']:.5f},{b['min_lon']:.5f} → "
             f"{b['max_lat']:.5f},{b['max_lon']:.5f}")
    # AP summary
    by_band = {}
    for ap in all_aps.values():
        by_band.setdefault(ap["band"], 0)
        by_band[ap["band"]] += 1
    for band, cnt in sorted(by_band.items()):
        info(f"  {band}: {cnt} APs")
    # RSSI stats
    all_rssi = [a["rssi"] for wp in wps for a in wp["aps"]]
    if all_rssi:
        info(f"RSSI range:   {min(all_rssi)} dBm  →  {max(all_rssi)} dBm  "
             f"(avg {round(sum(all_rssi)/len(all_rssi))} dBm)")
    print(f"  {C.B}{'─'*48}{C.R}")

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(
        description="Import mobile Wi-Fi survey JSON and generate dashboard",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    ap.add_argument("files", nargs="+", metavar="FILE",
                    help="mobile_survey.json file(s) to import")
    ap.add_argument("--out",     metavar="NAME", default="survey",
                    help="Output file prefix (default: survey)")
    ap.add_argument("--merge",   action="store_true",
                    help="Merge multiple input files into one survey")
    ap.add_argument("--no-gps",  action="store_true",
                    help="Ignore GPS coords and use manual_x/manual_y only")
    ap.add_argument("--padding", type=float, default=2.0, metavar="METRES",
                    help="Padding around GPS bounding box in metres (default: 2.0)")
    ap.add_argument("--no-dashboard", action="store_true",
                    help="Only write normalised JSON, skip HTML dashboard")
    args = ap.parse_args()

    print(f"\n{C.B}{C.CYN}"
          f"╔══════════════════════════════════════════╗\n"
          f"║  Mobile Survey Importer  v{VERSION:<15}║\n"
          f"╚══════════════════════════════════════════╝{C.R}\n")

    # ── Load input files ─────────────────────────────────────────────────────
    loaded = []
    for fpath in args.files:
        p = Path(fpath)
        if not p.exists():
            err(f"File not found: {fpath}")
            sys.exit(1)
        try:
            with open(p) as f:
                data = json.load(f)
            data["_source_file"] = str(p)
            loaded.append(data)
            ok(f"Loaded: {p.name}  ({len(data.get('waypoints',[]))} waypoints)")
        except json.JSONDecodeError as e:
            err(f"Invalid JSON in {fpath}: {e}")
            sys.exit(1)

    # ── Merge if needed ──────────────────────────────────────────────────────
    if args.merge and len(loaded) > 1:
        mobile_data = merge_surveys(loaded)
        info(f"Merged {len(loaded)} surveys → {len(mobile_data['waypoints'])} waypoints total")
    elif len(loaded) == 1:
        mobile_data = loaded[0]
    else:
        # Multiple files but no --merge flag: process each separately
        for data in loaded:
            stem = Path(data["_source_file"]).stem
            _process_single(data, stem, args)
        return

    _process_single(mobile_data, args.out, args,
                    source_files=[d["_source_file"] for d in loaded])

def _process_single(mobile_data, out_prefix, args, source_files=None):
    waypoints = mobile_data.get("waypoints", [])
    if not waypoints:
        warn("No waypoints in survey data — nothing to do.")
        return

    # ── Detect coordinate type ───────────────────────────────────────────────
    has_gps    = any(w.get("lat") is not None for w in waypoints)
    has_manual = any(w.get("manual_x") is not None for w in waypoints)

    if has_gps and not args.no_gps:
        info(f"Coordinate mode: GPS  ({sum(1 for w in waypoints if w.get('lat'))} waypoints with GPS)")
        converted, area_w, area_h, bbox = gps_waypoints_to_xy(waypoints, args.padding)
        ok(f"Projected to local XY: {area_w}m × {area_h}m floor plan")
    elif has_manual:
        info("Coordinate mode: Manual XY")
        converted, area_w, area_h, bbox = manual_waypoints_to_xy(waypoints)
        ok(f"Using manual coordinates: {area_w}m × {area_h}m")
    else:
        warn("No coordinates found — using placeholder XY (0, 0) for all waypoints")
        warn("Re-run with proper GPS data or use Manual XY mode in the app")
        for i, wp in enumerate(waypoints):
            wp["x"] = float(i % 5) * 2.0
            wp["y"] = float(i // 5) * 2.0
        converted = waypoints
        area_w, area_h, bbox = 10.0, 10.0, None

    # ── Build survey dict ────────────────────────────────────────────────────
    survey = build_survey_dict(mobile_data, area_w, area_h, converted,
                               source_files=source_files, bbox=bbox)
    print_stats(survey)

    # ── Save normalised JSON ─────────────────────────────────────────────────
    json_path = f"{out_prefix}.json"
    with open(json_path, "w") as f:
        json.dump(survey, f, indent=2)
    ok(f"Normalised JSON → {C.B}{json_path}{C.R}")

    if args.no_dashboard:
        return

    # ── Generate HTML dashboard ──────────────────────────────────────────────
    gen_fn, found_at = load_generate_dashboard()

    if gen_fn:
        info(f"Using dashboard generator from: {found_at}")
        html_path = f"{out_prefix}_dashboard.html"
        gen_fn(survey, html_path)
        ok(f"Dashboard → {C.B}{html_path}{C.R}")
        print(f"\n  Open in browser: {Path(html_path).resolve()}\n")
    else:
        # Fallback: inline minimal dashboard generator (no dependency on wifi_survey.py)
        warn("wifi_survey.py not found in the same directory — using built-in generator")
        html_path = f"{out_prefix}_dashboard.html"
        _inline_generate_dashboard(survey, html_path)
        ok(f"Dashboard → {C.B}{html_path}{C.R}")
        print(f"\n  Open in browser: {Path(html_path).resolve()}\n")
        print(f"  {C.YLW}Tip: Place wifi_survey.py in the same directory for the full-featured dashboard{C.R}\n")

# ── Inline minimal dashboard (fallback when wifi_survey.py is not present) ─────
def _inline_generate_dashboard(survey_dict, out_path):
    """
    Generate a self-contained dashboard HTML from a normalised survey dict.
    Reads the template from wifi_survey.py if available, otherwise uses a
    minimal built-in template.
    """
    json_str = json.dumps(survey_dict, separators=(",", ":"))

    # Try to steal the full template from wifi_survey.py
    for candidate in [Path(__file__).parent / "wifi_survey.py",
                      Path("wifi_survey.py")]:
        if candidate.exists():
            src  = candidate.read_text()
            # Extract HTML_TEMPLATE = r"""..."""
            import re
            m = re.search(r'HTML_TEMPLATE\s*=\s*r?"""(.*?)"""', src, re.DOTALL)
            if m:
                tmpl = m.group(1)
                html = tmpl.replace("__SURVEY_DATA__", json_str)
                with open(out_path, "w", encoding="utf-8") as f:
                    f.write(html)
                return

    # True fallback: dead-simple but functional HTML
    html = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<title>Mobile WiFi Survey</title>
<style>
body{{background:#080d17;color:#c8d8f0;font-family:monospace;padding:20px}}
h1{{color:#00c8e8;margin-bottom:20px}}
pre{{background:#0d1525;padding:20px;border-radius:8px;overflow:auto;font-size:12px}}
</style></head><body>
<h1>📡 Mobile WiFi Survey</h1>
<p>Place <code>wifi_survey.py</code> in the same folder and re-run
<code>import_mobile.py</code> to get the full interactive dashboard.</p>
<p>Raw survey data (paste into <a href="https://jsoncrack.com" target="_blank">jsoncrack.com</a>):</p>
<pre id="d"></pre>
<script>
document.getElementById('d').textContent =
  JSON.stringify({json_str}, null, 2).substring(0, 50000);
</script>
</body></html>"""
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)


if __name__ == "__main__":
    main()

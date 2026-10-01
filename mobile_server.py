#!/usr/bin/env python3
"""WiFi Survey Mobile Server v1.2 - clean rewrite, all fixes included.
Run in Termux. Open http://localhost:8765 in phone browser.
Setup: pkg install python termux-api
"""
import json, os, datetime, subprocess, threading, time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

PORT = 8765
VERSION = "1.2"
DATA_FILE = "mobile_survey.json"

# ── survey store ──────────────────────────────────────────────
survey_store = {
    "meta": {
        "created": datetime.datetime.now().isoformat(),
        "source": "mobile",
        "version": VERSION,
        "platform": "android-termux",
    },
    "waypoints": [],
}
store_lock = threading.Lock()


def save_store():
    with store_lock:
        with open(DATA_FILE, "w") as f:
            json.dump(survey_store, f, indent=2)


def load_store_if_exists():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE) as f:
                data = json.load(f)
            with store_lock:
                survey_store.update(data)
            print(f"  Resumed: {len(survey_store['waypoints'])} waypoints")
        except Exception as e:
            print(f"  Load error: {e}")


# ── scan cache ────────────────────────────────────────────────
# Android allows ~4 scans per 2 min. Cache for 25s to stay under limit.
# On record: bust cache so next spot always gets a fresh reading.
_last_good_scan = []
_last_scan_time = 0.0
_scan_lock = threading.Lock()
SCAN_CACHE_SECS = 2


def bust_scan_cache():
    """Zero timestamp → next call hits hardware immediately."""
    global _last_scan_time
    with _scan_lock:
        _last_scan_time = 0.0


def scan_age_seconds():
    with _scan_lock:
        return round(time.time() - _last_scan_time, 1)


def termux_wifi_scan():
    """Returns (ap_list, error_or_None). Serves cached data when throttled."""
    global _last_good_scan, _last_scan_time
    now = time.time()
    with _scan_lock:
        if now - _last_scan_time < SCAN_CACHE_SECS and _last_good_scan:
            return _last_good_scan, None
    try:
        r = subprocess.run(
            ["termux-wifi-scaninfo"], capture_output=True, text=True, timeout=20
        )
        with _scan_lock:
            _last_scan_time = time.time()
        raw = r.stdout.strip()
        if not raw:
            return _last_good_scan, None  # Android throttled → return last good
        data = json.loads(raw)
        if not isinstance(data, list):
            return _last_good_scan, "Unexpected format"
        if data:
            with _scan_lock:
                _last_good_scan = data
        return _last_good_scan, None
    except FileNotFoundError:
        return [], "termux-wifi-scaninfo not found - run: pkg install termux-api"
    except subprocess.TimeoutExpired:
        return _last_good_scan, "Scan timed out"
    except json.JSONDecodeError:
        return _last_good_scan, None  # partial JSON from throttle
    except Exception as e:
        return _last_good_scan, str(e)


# ── AP normalisation ──────────────────────────────────────────
# Termux fields: frequency_mhz (NOT frequency), rssi (NOT level)
def freq_to_band(mhz):
    if mhz == 0:
        return "Unknown"
    if mhz < 3000:
        return "2.4 GHz"
    if mhz < 6000:
        return "5 GHz"
    return "6 GHz"


def freq_to_channel(mhz):
    if 2412 <= mhz <= 2484:
        return "14" if mhz == 2484 else str((mhz - 2407) // 5)
    if 5170 <= mhz <= 5825:
        return str((mhz - 5000) // 5)
    if 5955 <= mhz <= 7115:
        return str((mhz - 5950) // 5)
    return "?"


def normalise_ap(raw):
    freq = raw.get("frequency_mhz", 0)  # ← correct field name
    caps = raw.get("capabilities", "")
    if "WPA3" in caps:
        sec = "WPA3"
    elif "WPA2" in caps:
        sec = "WPA2"
    elif "WPA" in caps:
        sec = "WPA"
    elif "WEP" in caps:
        sec = "WEP"
    else:
        sec = "Open"
    return {
        "ssid": (raw.get("ssid", "") or "<Hidden>"),
        "bssid": raw.get("bssid", "").upper(),
        "band": freq_to_band(freq),
        "channel": freq_to_channel(freq),
        "rssi": int(raw.get("rssi", -100)),  # ← correct field name
        "security": sec,
        "freq_mhz": freq,
    }


# ── GPS ───────────────────────────────────────────────────────
def termux_location(provider="gps"):
    try:
        r = subprocess.run(
            ["termux-location", "-p", provider, "-r", "once"],
            capture_output=True,
            text=True,
            timeout=25,
        )
        return json.loads(r.stdout), None
    except Exception as e:
        return None, str(e)


# ── HTTP handler ──────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        path = (args[0] if args else "").split(" ")[1] if args else ""
        if "/api/scan" not in path and "/api/status" not in path:
            print(f"  [{datetime.datetime.now().strftime('%H:%M:%S')}] {fmt%args}")

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, html):
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/" or "/")
        if not path:
            path = "/"

        if path in ("/", "/index.html"):
            self.send_html(PWA_HTML)
            return

        if path == "/api/scan":
            aps, err = termux_wifi_scan()
            normed = [normalise_ap(a) for a in aps]
            normed.sort(key=lambda a: -a["rssi"])
            age = scan_age_seconds()
            self.send_json(
                {
                    "ok": True,
                    "aps": normed,
                    "count": len(normed),
                    "timestamp": datetime.datetime.now().isoformat(),
                    "scan_age_s": age,
                    "from_cache": age > 3,
                    "warning": err,
                }
            )
            return

        if path == "/api/location":
            qs = parse_qs(parsed.query)
            loc, err = termux_location(qs.get("provider", ["gps"])[0])
            self.send_json({"ok": err is None, **(loc or {}), "error": err})
            return

        if path == "/api/status":
            with store_lock:
                self.send_json(
                    {
                        "ok": True,
                        "waypoint_count": len(survey_store["waypoints"]),
                        "meta": dict(survey_store["meta"]),
                        "version": VERSION,
                    }
                )
            return

        if path == "/api/export":
            with store_lock:
                body = json.dumps(survey_store, indent=2, ensure_ascii=False).encode()
            fname = f"mobile_survey_{datetime.date.today()}.json"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", len(body))
            self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
            print(f"  Exported {len(survey_store['waypoints'])} waypoints")
            return

        if path == "/api/clear":
            with store_lock:
                survey_store["waypoints"] = []
                survey_store["meta"]["created"] = datetime.datetime.now().isoformat()
            save_store()
            self.send_json({"ok": True})
            print("  Survey cleared")
            return

        self.send_json({"ok": False, "error": "Not found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path == "/api/waypoint":
            try:
                length = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(length))
            except:
                self.send_json({"ok": False, "error": "Invalid JSON"}, 400)
                return

            with store_lock:
                wp_id = len(survey_store["waypoints"]) + 1
                wp = {
                    "id": wp_id,
                    "timestamp": datetime.datetime.now().isoformat(),
                    "label": data.get("label") or f"WP{wp_id}",
                    "lat": data.get("lat"),
                    "lon": data.get("lon"),
                    "accuracy": data.get("accuracy"),
                    "altitude": data.get("altitude"),
                    "manual_x": data.get("manual_x"),
                    "manual_y": data.get("manual_y"),
                    "aps": data.get("aps", []),
                }
                survey_store["waypoints"].append(wp)

            save_store()
            bust_scan_cache()  # ← force fresh scan for the next spot

            lat_s = f"{wp['lat']:.5f}" if wp.get("lat") else "manual"
            print(f"  WP#{wp_id} '{wp['label']}'  {lat_s}  APs={len(wp['aps'])}")
            self.send_json({"ok": True, "id": wp_id, "waypoint": wp})
            return

        if path == "/api/meta":
            try:
                length = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(length))
                with store_lock:
                    survey_store["meta"].update(data)
                save_store()
                self.send_json({"ok": True})
            except:
                self.send_json({"ok": False, "error": "Invalid JSON"}, 400)
            return

        self.send_json({"ok": False, "error": "Not found"}, 404)


# ── PWA (served at /) ─────────────────────────────────────────
PWA_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<meta name="mobile-web-app-capable" content="yes">
<title>WiFi Survey</title>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;700&family=Space+Grotesk:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#07080d;--bg2:#0c0f1a;--bg3:#111522;--bg4:#161c2e;
  --bdr:#1e2640;--bdr2:#28325a;
  --txt:#c2cce8;--txt2:#6b7faa;--txt3:#3d4d72;
  --amber:#ffb700;--amber2:#cc8f00;
  --green:#00e676;--red:#ff3d3d;--cyan:#00c8e8;
  --mono:'JetBrains Mono',monospace;--sans:'Space Grotesk',sans-serif;--r:10px;
}
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
html,body{height:100%;background:var(--bg);color:var(--txt);font-family:var(--sans);overscroll-behavior:none}
::-webkit-scrollbar{width:4px}::-webkit-scrollbar-thumb{background:var(--bdr2);border-radius:2px}

.header{display:flex;align-items:center;padding:12px 16px;background:var(--bg2);
  border-bottom:1px solid var(--bdr);position:sticky;top:0;z-index:100}
.logo{font-family:var(--mono);font-size:10px;font-weight:700;color:var(--amber);
  letter-spacing:.12em;border:1px solid var(--amber2);padding:5px 10px;
  border-radius:5px;margin-right:12px;flex-shrink:0}
.htitle{font-size:14px;font-weight:600;flex:1}
.hcount{font-family:var(--mono);font-size:18px;font-weight:700;color:var(--amber);line-height:1}
.hclabel{font-size:10px;color:var(--txt3);text-align:right}

.sbar{display:grid;grid-template-columns:1fr 1fr;gap:1px;
  background:var(--bdr);border-bottom:1px solid var(--bdr)}
.spanel{background:var(--bg2);padding:10px 14px}
.slabel{font-family:var(--mono);font-size:9px;text-transform:uppercase;
  letter-spacing:.1em;color:var(--txt3);margin-bottom:4px;
  display:flex;align-items:center;gap:5px}
.dot{width:6px;height:6px;border-radius:50%;background:var(--txt3);flex-shrink:0;transition:background .3s}
.dot.ok{background:var(--green);box-shadow:0 0 6px var(--green)}
.dot.warn{background:var(--amber);box-shadow:0 0 6px var(--amber)}
.dot.err{background:var(--red)}
.dot.pulse{animation:pulse 1.5s ease-in-out infinite}
@keyframes pulse{0%,100%{box-shadow:0 0 4px var(--green)}50%{box-shadow:0 0 14px var(--green)}}
.sval{font-family:var(--mono);font-size:13px;font-weight:600;color:var(--txt);line-height:1.3}
.sval.ok{color:var(--green)}.sval.warn{color:var(--amber)}.sval.err{color:var(--red)}
.ssub{font-size:10px;color:var(--txt3);margin-top:2px}

/* freshness progress bar */
.fbar{height:3px;background:var(--bdr)}
.ffill{height:100%;width:100%;background:var(--green);transition:width .5s linear,background .5s}

.sechdr{padding:8px 14px 6px;background:var(--bg2);border-bottom:1px solid var(--bdr);
  display:flex;align-items:center;justify-content:space-between}
.sectitle{font-family:var(--mono);font-size:9px;text-transform:uppercase;
  letter-spacing:.1em;color:var(--txt3);font-weight:600}
.badge{font-family:var(--mono);font-size:10px;font-weight:700;color:var(--amber);
  background:#ffb70018;border:1px solid #ffb70030;padding:2px 8px;border-radius:3px}

.aplist{background:var(--bg);max-height:200px;overflow-y:auto;border-bottom:1px solid var(--bdr)}
.aprow{display:flex;align-items:center;gap:10px;padding:7px 14px;border-bottom:1px solid #1e264008}
.apbars{display:flex;gap:2px;align-items:flex-end;flex-shrink:0}
.apbars span{width:4px;border-radius:1px}
.apssid{flex:1;font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.apbssid{font-family:var(--mono);font-size:9px;color:var(--txt3)}
.apmeta{text-align:right;flex-shrink:0}
.aprssi{font-family:var(--mono);font-size:12px;font-weight:600}
.apband{font-size:9px;color:var(--txt3)}
.ex{color:#00e676}.gd{color:#a3e635}.ok2{color:#ffb700}.fr{color:#f97316}.pr{color:#ff3d3d}
.apempty{padding:20px 14px;text-align:center;color:var(--txt3);font-size:12px;font-family:var(--mono)}

.recsec{padding:14px 16px;background:var(--bg2);border-bottom:1px solid var(--bdr)}
.lrow{display:flex;gap:8px;margin-bottom:12px;align-items:center}
.lrow label{font-size:11px;color:var(--txt2);white-space:nowrap}
.linput{flex:1;background:var(--bg3);border:1px solid var(--bdr2);color:var(--txt);
  font-family:var(--sans);font-size:14px;padding:8px 12px;border-radius:var(--r)}
.linput:focus{outline:none;border-color:var(--amber)}
.linput::placeholder{color:var(--txt3)}
.ctabs{display:flex;border:1px solid var(--bdr2);border-radius:var(--r);overflow:hidden;margin-bottom:10px}
.ctab{flex:1;padding:7px;text-align:center;font-size:11px;font-weight:500;
  cursor:pointer;background:var(--bg3);color:var(--txt2);border:none;transition:all .15s}
.ctab.active{background:var(--amber);color:#000;font-weight:700}
.mxy{display:flex;gap:8px;margin-bottom:10px}
.xyf{flex:1;background:var(--bg3);border:1px solid var(--bdr2);color:var(--txt);
  font-family:var(--mono);font-size:14px;padding:8px 12px;border-radius:var(--r)}
.xyf:focus{outline:none;border-color:var(--amber)}

/* record button state classes */
.rbtn{width:100%;padding:18px;border-radius:var(--r);font-family:var(--sans);
  font-size:17px;font-weight:700;border:none;cursor:pointer;
  letter-spacing:.02em;transition:background .3s}
.rbtn:active{transform:scale(0.97)}
.rbtn.fresh{background:var(--green);color:#000}
.rbtn.okay{background:var(--amber);color:#000}
.rbtn.stale{background:#884400;color:#ffb70080;cursor:not-allowed}
.rbtn.saving{background:var(--cyan);color:#000}
.agenotice{margin-top:8px;font-size:11px;text-align:center;
  font-family:var(--mono);min-height:16px;transition:color .3s}

.wplog{background:var(--bg);max-height:180px;overflow-y:auto}
.wprow{display:flex;align-items:center;gap:10px;padding:8px 14px;border-bottom:1px solid #1e264020}
.wpid{font-family:var(--mono);font-size:10px;color:var(--txt3);
  background:var(--bg3);border:1px solid var(--bdr);padding:2px 6px;border-radius:3px;flex-shrink:0}
.wplabel{flex:1;font-size:13px;font-weight:500}
.wpcoords{font-family:var(--mono);font-size:9px;color:var(--txt3);text-align:right;flex-shrink:0}
.wpaps{font-family:var(--mono);font-size:9px;color:var(--amber);
  background:#ffb70015;padding:2px 6px;border-radius:3px;flex-shrink:0}
.wpempty{padding:20px 14px;text-align:center;color:var(--txt3);font-size:12px;font-family:var(--mono)}

.botbar{display:flex;gap:8px;padding:10px 14px;
  padding-bottom:calc(10px + env(safe-area-inset-bottom,0px));
  background:var(--bg2);border-top:1px solid var(--bdr);position:sticky;bottom:0}
.tbtn{flex:1;padding:10px 8px;border-radius:var(--r);font-family:var(--sans);
  font-size:12px;font-weight:600;border:1px solid var(--bdr2);
  background:var(--bg3);color:var(--txt);cursor:pointer;text-align:center;transition:all .12s}
.tbtn:active{transform:scale(0.96)}
.tbtn.exp{border-color:var(--cyan);color:var(--cyan)}
.tbtn.del{border-color:var(--red);color:var(--red)}
.tbtn.ref{border-color:var(--amber);color:var(--amber)}

#toast{position:fixed;bottom:80px;left:50%;transform:translateX(-50%);
  background:var(--bg4);border:1px solid var(--bdr2);color:var(--txt);
  padding:10px 20px;border-radius:20px;font-size:13px;font-weight:500;
  z-index:999;opacity:0;transition:opacity .2s;pointer-events:none;white-space:nowrap}
#toast.show{opacity:1}#toast.s{border-color:var(--green);color:var(--green)}
#toast.e{border-color:var(--red);color:var(--red)}
.spin{display:inline-block;width:10px;height:10px;border:1.5px solid var(--bdr2);
  border-top-color:var(--amber);border-radius:50%;animation:spin .6s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
</style>
</head>
<body>

<div class="header">
  <div class="logo">📡 SURVEY</div>
  <div class="htitle">WiFi Walk<br><span style="color:var(--txt3);font-size:11px">Mobile v1.2</span></div>
  <div style="text-align:right">
    <div class="hcount" id="wpCount">0</div>
    <div class="hclabel">waypoints</div>
  </div>
</div>

<div class="sbar">
  <div class="spanel">
    <div class="slabel"><div class="dot" id="gpsDot"></div>GPS</div>
    <div class="sval" id="gpsVal">—</div>
    <div class="ssub" id="gpsSub">Waiting…</div>
  </div>
  <div class="spanel">
    <div class="slabel"><div class="dot" id="wifiDot"></div>Wi-Fi Scan</div>
    <div class="sval" id="wifiVal">—</div>
    <div class="ssub" id="wifiSub">Not started</div>
  </div>
</div>

<div class="fbar"><div class="ffill" id="ffill"></div></div>

<div class="sechdr">
  <div class="sectitle">Nearby networks</div>
  <div style="display:flex;align-items:center;gap:8px">
    <span class="spin" id="spinner" style="display:none"></span>
    <div class="badge" id="apCount">0 APs</div>
  </div>
</div>
<div class="aplist" id="apList"><div class="apempty">Tap Refresh to start scanning</div></div>

<div class="recsec">
  <div class="lrow">
    <label>Label:</label>
    <input class="linput" id="labelInput" type="text"
           placeholder="Kitchen · Entry · Desk · Corner…" maxlength="40">
  </div>
  <div class="ctabs">
    <button class="ctab active" id="tabGPS"    onclick="setMode('gps')">📍 GPS Auto</button>
    <button class="ctab"        id="tabManual" onclick="setMode('manual')">✏️ Manual XY</button>
  </div>
  <div class="mxy" id="mxyDiv" style="display:none">
    <input class="xyf" id="manX" type="number" step="0.1" placeholder="X (m)" min="0">
    <input class="xyf" id="manY" type="number" step="0.1" placeholder="Y (m)" min="0">
  </div>
  <button class="rbtn okay" id="recBtn" onclick="record()">◉ &nbsp;RECORD WAYPOINT</button>
  <div class="agenotice" id="ageNotice"> </div>
</div>

<div class="sechdr">
  <div class="sectitle">Recorded waypoints</div>
  <div class="badge" id="logCount">0</div>
</div>
<div class="wplog" id="wpLog"><div class="wpempty">No waypoints yet</div></div>

<div class="botbar">
  <button class="tbtn ref" onclick="doScan()">🔄 Refresh</button>
  <button class="tbtn exp" onclick="doExport()">⬇ Export</button>
  <button class="tbtn del" onclick="doClear()">🗑 Clear</button>
</div>
<div id="toast"></div>

<script>
var coordMode='gps', gpsPos=null, curAPs=[], waypoints=[];
var scanAge=0, scanning=false, scanTimer=null, ageTimer=null;
var CACHE=25; // must match server SCAN_CACHE_SECS

function toast(msg,type,ms){
  ms=ms||2200; var el=document.getElementById('toast');
  el.textContent=msg; el.className='show '+(type||'');
  clearTimeout(el._t); el._t=setTimeout(function(){el.className=''},ms);
}
function setMode(m){
  coordMode=m;
  document.getElementById('tabGPS').classList.toggle('active',m==='gps');
  document.getElementById('tabManual').classList.toggle('active',m==='manual');
  document.getElementById('mxyDiv').style.display=m==='manual'?'flex':'none';
}
function setV(id,txt,cls){
  var el=document.getElementById(id);
  el.textContent=txt; el.className='sval'+(cls?' '+cls:'');
}

// GPS via browser geolocation
function startGPS(){
  if(!navigator.geolocation){setV('gpsVal','N/A','err');setMode('manual');return;}
  navigator.geolocation.watchPosition(function(pos){
    gpsPos=pos;
    var lat=pos.coords.latitude.toFixed(6), lon=pos.coords.longitude.toFixed(6);
    var acc=pos.coords.accuracy.toFixed(1);
    setV('gpsVal',lat+', '+lon, acc<15?'ok':'warn');
    document.getElementById('gpsSub').textContent='±'+acc+'m';
    document.getElementById('gpsDot').className=acc<15?'dot ok pulse':'dot warn';
  },function(e){
    setV('gpsVal','Error','err');
    document.getElementById('gpsSub').textContent=e.message.substring(0,40);
    document.getElementById('gpsDot').className='dot err';
  },{enableHighAccuracy:true,maximumAge:3000,timeout:20000});
}

// Scan
async function doScan(){
  if(scanning)return;
  scanning=true;
  document.getElementById('spinner').style.display='inline-block';
  document.getElementById('wifiDot').className='dot warn';
  try{
    var r=await fetch('/api/scan'), d=await r.json();
    curAPs=d.aps||[]; scanAge=d.scan_age_s||0;
    renderAPs(curAPs);
    // update wifi status
    var age=d.scan_age_s||0, fresh=age<15, stale=age>=CACHE;
    setV('wifiVal',curAPs.length+' networks', fresh?'ok':stale?'err':'warn');
    var cached=d.from_cache?('cached '+Math.round(age)+'s ago'):'fresh scan ✓';
    document.getElementById('wifiSub').textContent=
      cached+'  '+(d.timestamp||'').replace('T',' ').substring(11,19);
    document.getElementById('wifiDot').className=fresh?'dot ok pulse':stale?'dot err':'dot warn';
    if(d.warning) toast('⚠ '+d.warning,'e',4000);
    updateRecBtn(age);
    resetAgeTicker(age);
  }catch(e){
    setV('wifiVal','Server error','err');
    document.getElementById('wifiDot').className='dot err';
    document.getElementById('apList').innerHTML=
      '<div class="apempty" style="color:var(--red)">Cannot reach server</div>';
  }finally{
    scanning=false;
    document.getElementById('spinner').style.display='none';
  }
}

function resetAgeTicker(startAge){
  clearInterval(ageTimer);
  scanAge=startAge||0;
  ageTimer=setInterval(function(){
    scanAge+=1;
    updateRecBtn(scanAge);
    var pct=Math.max(0,100-(scanAge/CACHE)*100);
    var col=pct>60?'var(--green)':pct>30?'var(--amber)':'var(--red)';
    var f=document.getElementById('ffill');
    f.style.width=pct+'%'; f.style.background=col;
  },1000);
}

function updateRecBtn(age){
  var btn=document.getElementById('recBtn');
  var notice=document.getElementById('ageNotice');
  if(btn.className.includes('saving'))return;
  if(age<15){
    btn.className='rbtn fresh';
    notice.style.color='var(--green)';
    notice.textContent='✓ fresh ('+Math.round(age)+'s) — ready to record';
  }else if(age<CACHE){
    btn.className='rbtn okay';
    notice.style.color='var(--amber)';
    notice.textContent='⚡ '+Math.round(age)+'s old — walk slowly or tap Refresh';
  }else{
    btn.className='rbtn stale';
    notice.style.color='var(--red)';
    notice.textContent='⚠ '+Math.round(age)+'s old — tap Refresh Scan first!';
  }
}

// Render AP list
function rssiCls(v){return v>=-60?'ex':v>=-65?'gd':v>=-70?'ok2':v>=-80?'fr':'pr'}
function bars(rssi){
  var n=rssi>=-60?4:rssi>=-70?3:rssi>=-80?2:1;
  var h=[14,10,7,5];
  var cols={ex:'#00e676',gd:'#a3e635',ok2:'#ffb700',fr:'#f97316',pr:'#ff3d3d'};
  var c=cols[rssiCls(rssi)];
  return h.map(function(ht,i){
    return '<span style="height:'+ht+'px;background:'+(i<n?c:'var(--bdr2)')+'"></span>';
  }).join('');
}
function renderAPs(aps){
  document.getElementById('apCount').textContent=aps.length+' APs';
  var el=document.getElementById('apList');
  if(!aps.length){el.innerHTML='<div class="apempty">No networks — is Wi-Fi on?</div>';return;}
  el.innerHTML=aps.slice(0,50).map(function(ap){
    return '<div class="aprow">'+
      '<div class="apbars">'+bars(ap.rssi)+'</div>'+
      '<div><div class="apssid">'+ap.ssid+'</div>'+
           '<div class="apbssid">'+ap.bssid+' · ch'+ap.channel+'</div></div>'+
      '<div class="apmeta">'+
        '<div class="aprssi '+rssiCls(ap.rssi)+'">'+ap.rssi+' dBm</div>'+
        '<div class="apband">'+ap.band+'</div>'+
      '</div></div>';
  }).join('');
}

// Record waypoint
async function record(){
  var btn=document.getElementById('recBtn');
  if(btn.className.includes('stale')){toast('⚠ Tap Refresh first — scan too old','e');return;}
  if(!curAPs.length){toast('⚠ No APs yet — tap Refresh','e');return;}

  var lat=null,lon=null,accuracy=null,altitude=null,manual_x=null,manual_y=null;
  if(coordMode==='gps'){
    if(!gpsPos){toast('⚠ No GPS fix — use Manual XY','e');return;}
    lat=gpsPos.coords.latitude; lon=gpsPos.coords.longitude;
    accuracy=gpsPos.coords.accuracy; altitude=gpsPos.coords.altitude;
  }else{
    var mx=parseFloat(document.getElementById('manX').value);
    var my=parseFloat(document.getElementById('manY').value);
    if(isNaN(mx)||isNaN(my)){toast('⚠ Enter X and Y','e');return;}
    manual_x=mx; manual_y=my;
  }

  var label=document.getElementById('labelInput').value.trim();
  btn.className='rbtn saving'; btn.textContent='⏳  Saving…';

  try{
    var r=await fetch('/api/waypoint',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({lat:lat,lon:lon,accuracy:accuracy,altitude:altitude,
                           manual_x:manual_x,manual_y:manual_y,label:label,aps:curAPs})
    });
    var d=await r.json();
    if(d.ok){
      waypoints.push(d.waypoint);
      renderWPLog();
      document.getElementById('wpCount').textContent=waypoints.length;
      document.getElementById('logCount').textContent=waypoints.length;
      document.getElementById('labelInput').value='';
      toast('✓ WP#'+d.id+'  '+curAPs.length+' APs','s');
      // server busted its cache — scan immediately for the next spot
      setTimeout(doScan,500);
    }else{toast('✗ '+d.error,'e');}
  }catch(e){toast('✗ Server unreachable','e');}
  finally{
    btn.textContent='◉  RECORD WAYPOINT';
    updateRecBtn(0); // will be corrected by the doScan that fires in 500ms
  }
}

// Waypoint log
function renderWPLog(){
  var el=document.getElementById('wpLog');
  if(!waypoints.length){el.innerHTML='<div class="wpempty">No waypoints yet</div>';return;}
  el.innerHTML=[].concat(waypoints).reverse().map(function(wp){
    var coord=wp.lat!=null
      ?(wp.lat.toFixed(5)+','+wp.lon.toFixed(5))
      :('('+wp.manual_x+'m,'+wp.manual_y+'m)');
    return '<div class="wprow">'+
      '<div class="wpid">#'+wp.id+'</div>'+
      '<div><div class="wplabel">'+wp.label+'</div>'+
           '<div class="wpcoords">'+coord+'</div></div>'+
      '<div class="wpaps">'+wp.aps.length+' APs</div>'+
    '</div>';
  }).join('');
  el.scrollTop=0;
}

function doExport(){
  if(!waypoints.length){toast('Nothing to export','e');return;}
  window.location.href='/api/export';
  toast('✓ Downloading '+waypoints.length+' waypoints','s',3000);
}
async function doClear(){
  if(!waypoints.length){toast('Nothing to clear');return;}
  if(!confirm('Clear all '+waypoints.length+' waypoints?'))return;
  await fetch('/api/clear');
  waypoints=[];renderWPLog();
  document.getElementById('wpCount').textContent=0;
  document.getElementById('logCount').textContent=0;
  toast('Cleared');
}

// Load existing waypoints on open
async function loadExisting(){
  try{
    var r=await fetch('/api/export'), d=await r.json();
    waypoints=d.waypoints||[];
    if(waypoints.length){
      renderWPLog();
      document.getElementById('wpCount').textContent=waypoints.length;
      document.getElementById('logCount').textContent=waypoints.length;
      toast('Resumed: '+waypoints.length+' waypoints','s',3000);
    }
  }catch(e){}
}

// Start
startGPS();
loadExisting();
doScan();
resetAgeTicker(0);
// auto-rescan every 28s
scanTimer=setInterval(function(){doScan();},28000);
</script>
</body>
</html>"""


# ── entry point ───────────────────────────────────────────────
def main():
    load_store_if_exists()
    print(f"\n{'='*44}")
    print(f"  WiFi Survey Mobile Server  v{VERSION}")
    print(f"{'='*44}")
    print(f"\n  Open in phone browser:  http://localhost:{PORT}")
    print(f"  Data saved to:          {DATA_FILE}")
    print(f"\n  Ctrl-C to stop\n{'─'*44}\n")
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        n = len(survey_store["waypoints"])
        print(f"\n  {n} waypoints saved to {DATA_FILE}")
        print(f"  On PC: python import_mobile.py {DATA_FILE}\n")
        server.server_close()


if __name__ == "__main__":
    main()

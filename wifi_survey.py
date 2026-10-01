#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════╗
║  WiFi Walk-Survey Tool v1.1                         ║
║  DIY alternative to NetSpot / Ekahau AI Pro         ║
║  Supports: Ubuntu Linux + Windows 10/11             ║
╚══════════════════════════════════════════════════════╝

Usage:
  python3 wifi_survey.py                 # Start new survey
  python3 wifi_survey.py --demo          # Generate demo dashboard (no adapter needed)
  python3 wifi_survey.py --load FILE     # Load existing .json and regenerate dashboard
  python3 wifi_survey.py --out NAME      # Set output file prefix (default: survey)
"""

import sys, os, json, time, platform, subprocess, datetime
import threading, re, math, argparse, random, shutil
from pathlib import Path

# ──────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────
VERSION       = "1.1"
SCAN_INTERVAL = 4          # seconds between background scans
DEFAULT_W     = 20         # default area width in metres
DEFAULT_H     = 15         # default area height in metres

IS_WIN   = platform.system() == "Windows"
IS_LINUX = platform.system() == "Linux"

# ──────────────────────────────────────────────────────
# ANSI colours (disabled automatically on Windows cmd)
# ──────────────────────────────────────────────────────
class C:
    if sys.stdout.isatty() and not IS_WIN:
        RST="\033[0m"; BOLD="\033[1m"; DIM="\033[2m"
        RED="\033[91m"; GRN="\033[92m"; YLW="\033[93m"
        CYN="\033[96m"; WHT="\033[97m"; GRY="\033[90m"
        MGT="\033[95m"
    else:
        RST=BOLD=DIM=RED=GRN=YLW=CYN=WHT=GRY=MGT=""

def rssi_color(dbm):
    if dbm >= -60: return C.GRN
    if dbm >= -70: return C.YLW
    if dbm >= -80: return C.MGT
    return C.RED

def rssi_bar(dbm, width=10):
    norm = max(0, min(1, (dbm + 100) / 70))
    filled = round(norm * width)
    return "█" * filled + "░" * (width - filled)

def rssi_label(dbm):
    if dbm >= -60: return "Excellent"
    if dbm >= -70: return "Good     "
    if dbm >= -80: return "Fair     "
    return "Poor     "

# ──────────────────────────────────────────────────────
# Wi-Fi Scanner
# ──────────────────────────────────────────────────────
class WiFiScanner:
    def __init__(self):
        self.interface = self._detect_interface()
        self._check_permissions()

    def _detect_interface(self):
        if not IS_LINUX:
            return None
        # Try nmcli first
        try:
            r = subprocess.run(["nmcli","-t","-f","DEVICE,TYPE","dev"],
                               capture_output=True, text=True, timeout=5)
            for line in r.stdout.splitlines():
                parts = line.split(":")
                if len(parts) >= 2 and parts[1].strip() == "wifi":
                    return parts[0].strip()
        except Exception:
            pass
        # Fallback: /sys/class/net
        try:
            for iface in os.listdir("/sys/class/net"):
                if os.path.exists(f"/sys/class/net/{iface}/wireless"):
                    return iface
        except Exception:
            pass
        return "wlan0"

    def _check_permissions(self):
        if IS_LINUX:
            if not shutil.which("nmcli") and not shutil.which("iw"):
                print(f"{C.RED}✗ Neither nmcli nor iw found. Install: sudo apt install network-manager iw{C.RST}")
            if self.interface:
                print(f"{C.GRN}✓ Wi-Fi interface detected: {C.BOLD}{self.interface}{C.RST}")
            else:
                print(f"{C.RED}✗ No Wi-Fi interface found. Check VMware USB passthrough (see README).{C.RST}")

    # ── Public ──────────────────────────────────────────
    def scan(self):
        """Return list of AP dicts. Empty list on failure."""
        try:
            if IS_LINUX:  return self._scan_linux()
            if IS_WIN:    return self._scan_windows()
        except Exception as e:
            return []
        return []

    # ── Linux ────────────────────────────────────────────
    def _scan_linux(self):
        # Primary: nmcli
        if shutil.which("nmcli"):
            try:
                r = subprocess.run(
                    ["nmcli","--terse","--fields",
                     "SSID,BSSID,CHAN,FREQ,SIGNAL,SECURITY",
                     "dev","wifi","list","--rescan","yes"],
                    capture_output=True, text=True, timeout=25)
                if r.returncode == 0 and r.stdout.strip():
                    return self._parse_nmcli(r.stdout)
            except Exception:
                pass

        # Fallback: iw scan (may need sudo)
        if shutil.which("iw") and self.interface:
            try:
                r = subprocess.run(
                    ["sudo","iw","dev", self.interface,"scan"],
                    capture_output=True, text=True, timeout=25)
                if r.returncode == 0:
                    return self._parse_iw(r.stdout)
            except Exception:
                pass
        return []

    def _split_nmcli_line(self, line):
        """Split nmcli -t colon-separated line, respecting \\: escapes."""
        parts, cur, i = [], [], 0
        while i < len(line):
            if line[i] == "\\" and i+1 < len(line) and line[i+1] == ":":
                cur.append(":"); i += 2
            elif line[i] == ":":
                parts.append("".join(cur)); cur = []; i += 1
            else:
                cur.append(line[i]); i += 1
        parts.append("".join(cur))
        return parts

    def _parse_nmcli(self, raw):
        nets, seen = [], set()
        for line in raw.strip().splitlines():
            p = self._split_nmcli_line(line.strip())
            if len(p) < 5: continue
            ssid, bssid, chan, freq_s, sig_s = (p[i].strip() for i in range(5))
            sec = p[5].strip() if len(p) > 5 else ""
            if not bssid or bssid in seen: continue
            seen.add(bssid)
            ssid = ssid or "<Hidden>"
            freq = int(re.match(r"(\d+)", freq_s).group(1)) if re.match(r"(\d+)", freq_s) else 0
            try: rssi = self._pct_to_dbm(int(sig_s))
            except: rssi = -100
            nets.append(dict(ssid=ssid, bssid=bssid, band=self._band(freq),
                             channel=chan, rssi=rssi, security=sec))
        return nets

    def _parse_iw(self, raw):
        nets, cur = [], {}
        for line in raw.splitlines():
            s = line.strip()
            if s.startswith("BSS "):
                if cur.get("bssid"): nets.append(cur)
                cur = {"bssid": s.split()[1].split("(")[0]}
            elif s.startswith("SSID:"):
                cur["ssid"] = s[5:].strip() or "<Hidden>"
            elif "signal:" in s.lower():
                m = re.search(r"(-?\d+\.?\d*)\s*dBm", s)
                if m: cur["rssi"] = float(m.group(1))
            elif s.startswith("DS Parameter"):
                m = re.search(r"channel\s+(\d+)", s)
                if m: cur["channel"] = m.group(1)
            elif s.lower().startswith("freq:"):
                m = re.search(r"(\d+)", s)
                if m: freq=int(m.group(1)); cur["band"]=self._band(freq)
        if cur.get("bssid"): nets.append(cur)
        result = []
        for n in nets:
            result.append(dict(ssid=n.get("ssid","<Hidden>"), bssid=n.get("bssid",""),
                               band=n.get("band","Unknown"), channel=n.get("channel","?"),
                               rssi=n.get("rssi",-100), security=""))
        return result

    # ── Windows ──────────────────────────────────────────
    def _scan_windows(self):
        flags = getattr(subprocess,"CREATE_NO_WINDOW", 0)
        r = subprocess.run(
            ["netsh","wlan","show","networks","mode=bssid"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=15, creationflags=flags)
        return self._parse_netsh(r.stdout)

    def _parse_netsh(self, raw):
        nets, cur_ssid, bdat = [], None, {}
        def flush():
            if bdat.get("bssid"):
                nets.append(self._make_win_ap(cur_ssid, bdat))
        for line in raw.splitlines():
            s = line.strip()
            m = re.match(r"SSID\s+\d+\s*:\s*(.*)", s)
            if m: flush(); cur_ssid = m.group(1).strip() or "<Hidden>"; bdat={}; continue
            m = re.match(r"BSSID\s+\d+\s*:\s*(.*)", s)
            if m: flush(); bdat={"bssid": m.group(1).strip()}; continue
            m = re.match(r"Signal\s*:\s*(\d+)%", s)
            if m and bdat: bdat["pct"] = int(m.group(1))
            m = re.match(r"Channel\s*:\s*(\d+)", s)
            if m and bdat: bdat["ch"] = m.group(1)
            m = re.match(r"Radio type\s*:\s*(.*)", s)
            if m and bdat: bdat["radio"] = m.group(1).strip()
        flush()
        return [n for n in nets if n]

    def _make_win_ap(self, ssid, d):
        if not d.get("bssid"): return None
        ch = int(d["ch"]) if d.get("ch","").isdigit() else 0
        radio = d.get("radio","").lower()
        if ch > 182: band = "6 GHz"
        elif ch > 14 or "ac" in radio or ("ax" in radio and ch > 14): band = "5 GHz"
        else: band = "2.4 GHz"
        return dict(ssid=ssid or "<Hidden>", bssid=d["bssid"], band=band,
                    channel=d.get("ch","?"), rssi=self._pct_to_dbm(d.get("pct",0)), security="")

    # ── Helpers ──────────────────────────────────────────
    def _band(self, mhz):
        if mhz == 0:   return "Unknown"
        if mhz < 3000: return "2.4 GHz"
        if mhz < 6000: return "5 GHz"
        return "6 GHz"

    def _pct_to_dbm(self, pct):
        """0-100 → approximate dBm using standard formula."""
        return int((pct / 2) - 100)


# ──────────────────────────────────────────────────────
# Survey Session
# ──────────────────────────────────────────────────────
class SurveySession:
    def __init__(self, area_w=DEFAULT_W, area_h=DEFAULT_H):
        self.area_w = float(area_w)
        self.area_h = float(area_h)
        self.waypoints = []
        self.created = datetime.datetime.now().isoformat()
        self._ctr = 0

    def add_waypoint(self, x, y, label, aps):
        self._ctr += 1
        wp = dict(id=self._ctr,
                  timestamp=datetime.datetime.now().isoformat(),
                  x=float(x), y=float(y),
                  label=label or f"WP{self._ctr}",
                  aps=aps)
        self.waypoints.append(wp)
        return wp

    def save(self, path, interface="unknown"):
        data = dict(
            survey_info=dict(
                created=self.created,
                interface=interface,
                os=platform.system(),
                area_width=self.area_w,
                area_height=self.area_h,
                tool_version=VERSION),
            waypoints=self.waypoints)
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        return path


# ──────────────────────────────────────────────────────
# Demo Data Generator
# ──────────────────────────────────────────────────────
def generate_demo_data():
    """Create realistic-looking demo survey data for testing the dashboard."""
    random.seed(42)
    aps_pool = [
        ("HomeNet_5G",       "AA:BB:CC:11:22:33", "5 GHz",   "36", -55),
        ("HomeNet_2G",       "AA:BB:CC:11:22:34", "2.4 GHz", "6",  -62),
        ("Neighbor_WiFi",    "DD:EE:FF:44:55:66", "2.4 GHz", "11", -78),
        ("Office_Corp",      "11:22:33:44:55:66", "5 GHz",   "149",-70),
        ("Guest_Network",    "77:88:99:AA:BB:CC", "2.4 GHz", "1",  -85),
        ("IoT_Network",      "CC:DD:EE:FF:00:11", "2.4 GHz", "6",  -68),
        ("Starlink_5G",      "22:33:44:55:66:77", "5 GHz",   "100",-60),
    ]
    session = SurveySession(area_w=15, area_h=10)
    session.created = datetime.datetime(2024, 6, 15, 9, 30, 0).isoformat()
    waypoints_grid = [
        (1,1,"Entry"), (5,1,"Living Room W"), (10,1,"Living Room E"), (14,1,"Kitchen"),
        (1,5,"Hallway"), (5,5,"Center"),       (10,5,"Study"),         (14,5,"Bathroom"),
        (1,9,"Bedroom1 W"),(5,9,"Bedroom1 E"), (10,9,"Bedroom2"),     (14,9,"Bedroom2 far"),
    ]
    for x, y, label in waypoints_grid:
        scan_aps = []
        for ssid, bssid, band, ch, base_rssi in aps_pool:
            # Distance-based signal falloff from a virtual AP position
            ap_x = {"HomeNet_5G":7,"HomeNet_2G":7,"Neighbor_WiFi":0,
                     "Office_Corp":14,"Guest_Network":7,"IoT_Network":2,
                     "Starlink_5G":10}.get(ssid, 7)
            ap_y = {"HomeNet_5G":5,"HomeNet_2G":5,"Neighbor_WiFi":5,
                     "Office_Corp":5,"Guest_Network":9,"IoT_Network":2,
                     "Starlink_5G":2}.get(ssid, 5)
            dist = math.sqrt((x-ap_x)**2 + (y-ap_y)**2)
            rssi = base_rssi - dist * 1.8 + random.uniform(-3, 3)
            rssi = round(max(-95, min(-30, rssi)))
            if rssi > -90:
                scan_aps.append(dict(ssid=ssid,bssid=bssid,band=band,
                                     channel=ch,rssi=rssi,security="WPA2"))
        session.waypoints.append(dict(
            id=len(session.waypoints)+1,
            timestamp=(datetime.datetime(2024,6,15,9,30,0) +
                       datetime.timedelta(minutes=len(session.waypoints)*2)).isoformat(),
            x=x, y=y, label=label, aps=scan_aps))
    return session


# ──────────────────────────────────────────────────────
# Interactive CLI
# ──────────────────────────────────────────────────────
class InteractiveCLI:
    def __init__(self, scanner: WiFiScanner, session: SurveySession):
        self.scanner = scanner
        self.session = session
        self._current_aps = []
        self._lock = threading.Lock()
        self._running = False
        self._scan_count = 0
        self._last_scan_time = None

    def _bg_scan(self):
        while self._running:
            aps = self.scanner.scan()
            with self._lock:
                self._current_aps = aps
                self._scan_count += 1
                self._last_scan_time = datetime.datetime.now().strftime("%H:%M:%S")
            time.sleep(SCAN_INTERVAL)

    def _show_aps(self):
        with self._lock:
            aps = list(self._current_aps)
            t = self._last_scan_time
            cnt = self._scan_count
        if not aps:
            print(f"  {C.YLW}⚠ No APs found yet. Scanning…{C.RST}")
            return
        print(f"\n  {C.CYN}{'─'*72}{C.RST}")
        print(f"  {C.DIM}Scan #{cnt}  at {t}  │  {len(aps)} networks visible{C.RST}")
        print(f"  {C.CYN}{'─'*72}{C.RST}")
        print(f"  {C.BOLD}{'SSID':<28} {'BSSID':<19} {'Band':<8} {'Ch':<4} {'dBm':<6} {'Bar':<12} {'Quality'}{C.RST}")
        print(f"  {'─'*70}")
        for ap in sorted(aps, key=lambda a: -a["rssi"]):
            col = rssi_color(ap["rssi"])
            bar = rssi_bar(ap["rssi"])
            lbl = rssi_label(ap["rssi"])
            ssid = ap["ssid"][:27]
            print(f"  {ssid:<28} {ap['bssid']:<19} {ap['band']:<8} "
                  f"{ap['channel']:<4} {col}{ap['rssi']:<6}{C.RST} "
                  f"{col}{bar}{C.RST} {col}{lbl}{C.RST}")
        print(f"  {C.CYN}{'─'*72}{C.RST}")

    def _record_waypoint(self):
        print(f"\n  {C.BOLD}{C.CYN}── Record Waypoint ──────────────────────{C.RST}")
        try:
            x = float(input(f"  X position (metres, 0–{self.session.area_w}): ").strip())
            y = float(input(f"  Y position (metres, 0–{self.session.area_h}): ").strip())
            label = input("  Label (optional, e.g. 'Kitchen'): ").strip()
        except (ValueError, EOFError):
            print(f"  {C.RED}✗ Invalid input, waypoint not saved.{C.RST}")
            return

        x = max(0, min(self.session.area_w, x))
        y = max(0, min(self.session.area_h, y))

        with self._lock:
            aps = list(self._current_aps)

        if not aps:
            print(f"  {C.YLW}⚠ No APs in current scan — waypoint will have empty AP list.{C.RST}")

        wp = self.session.add_waypoint(x, y, label, aps)
        print(f"\n  {C.GRN}✓ Waypoint #{wp['id']} saved:{C.RST} ({x:.1f}m, {y:.1f}m) "
              f"\"{wp['label']}\" — {C.BOLD}{len(aps)} APs{C.RST}")

    def run(self):
        print(f"\n{C.BOLD}{C.CYN}"
              f"╔══════════════════════════════════════════════╗\n"
              f"║   WiFi Walk-Survey Tool v{VERSION:<20}║\n"
              f"╚══════════════════════════════════════════════╝{C.RST}")
        print(f"  Area: {self.session.area_w}m × {self.session.area_h}m  │  "
              f"Interface: {self.scanner.interface or 'N/A'}\n")
        print(f"  {C.BOLD}Commands:{C.RST}")
        print(f"    {C.YLW}[Enter]{C.RST}  Record waypoint at current location")
        print(f"    {C.YLW}s{C.RST}        Show current scan results")
        print(f"    {C.YLW}q{C.RST}        Finish and generate dashboard")
        print(f"    {C.YLW}h{C.RST}        Show this help\n")
        print(f"  {C.DIM}Tip: Walk to a spot, wait ~4s, then press Enter to record.{C.RST}\n")

        self._running = True
        t = threading.Thread(target=self._bg_scan, daemon=True)
        t.start()

        # Wait for first scan
        sys.stdout.write(f"  {C.DIM}Waiting for first scan…{C.RST}")
        sys.stdout.flush()
        for _ in range(30):
            if self._current_aps or self._scan_count > 0:
                break
            time.sleep(0.5)
        print()

        try:
            while True:
                try:
                    cmd = input(f"\n  {C.BOLD}[{len(self.session.waypoints)} waypoints]{C.RST} > ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    cmd = "q"

                if cmd == "":
                    self._record_waypoint()
                elif cmd == "s":
                    self._show_aps()
                elif cmd in ("q", "quit", "done", "exit"):
                    break
                elif cmd == "h":
                    print(f"  Enter  → record waypoint   s → show APs   q → quit & generate")
                else:
                    print(f"  {C.YLW}Unknown command. Press Enter to record, s to show, q to quit.{C.RST}")
        finally:
            self._running = False

        print(f"\n  {C.GRN}✓ Survey complete. {len(self.session.waypoints)} waypoints recorded.{C.RST}")


# ──────────────────────────────────────────────────────
# HTML Dashboard Template
# ──────────────────────────────────────────────────────
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>WiFi Survey Dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;700&family=Outfit:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>
:root {
  --bg:      #080d17;
  --bg2:     #0d1525;
  --bg3:     #111c30;
  --bg4:     #162038;
  --border:  #1e2d4a;
  --border2: #243558;
  --text:    #c8d8f0;
  --text2:   #7890b8;
  --text3:   #4a6090;
  --cyan:    #00c8e8;
  --cyan2:   #0096b8;
  --green:   #22c55e;
  --yellow:  #f59e0b;
  --orange:  #f97316;
  --red:     #ef4444;
  --purple:  #a855f7;
  --mono:    'JetBrains Mono', monospace;
  --sans:    'Outfit', sans-serif;
  --radius:  8px;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body { height: 100%; background: var(--bg); color: var(--text); font-family: var(--sans); font-size: 14px; }

/* ── Scrollbar ─────────────────────────────────── */
::-webkit-scrollbar { width: 6px; height: 6px; }
::-webkit-scrollbar-track { background: var(--bg2); }
::-webkit-scrollbar-thumb { background: var(--border2); border-radius: 3px; }

/* ── Header ────────────────────────────────────── */
header {
  background: var(--bg2);
  border-bottom: 1px solid var(--border);
  padding: 14px 24px;
  display: flex; align-items: center; gap: 20px;
  position: sticky; top: 0; z-index: 100;
}
.logo {
  font-family: var(--mono); font-size: 11px; font-weight: 700;
  color: var(--cyan); letter-spacing: 0.12em; text-transform: uppercase;
  border: 1px solid var(--cyan2); padding: 6px 12px; border-radius: var(--radius);
}
header h1 { font-size: 18px; font-weight: 600; flex: 1; }
header h1 span { color: var(--cyan); }
.meta { font-family: var(--mono); font-size: 11px; color: var(--text3); }
.btn {
  padding: 7px 16px; border-radius: var(--radius); cursor: pointer;
  font-family: var(--sans); font-size: 13px; font-weight: 500;
  border: 1px solid var(--border2); background: var(--bg3); color: var(--text);
  transition: all .15s;
}
.btn:hover { border-color: var(--cyan); color: var(--cyan); }
.btn-primary { background: var(--cyan); color: #000; border-color: var(--cyan); font-weight: 600; }
.btn-primary:hover { background: #00e8ff; }

/* ── Stats Row ─────────────────────────────────── */
.stats-row {
  display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px;
  padding: 16px 24px;
}
.stat-card {
  background: var(--bg2); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 14px 18px;
  display: flex; flex-direction: column; gap: 4px;
}
.stat-card .label { font-size: 11px; text-transform: uppercase;
  letter-spacing: .08em; color: var(--text3); font-weight: 500; }
.stat-card .value { font-family: var(--mono); font-size: 26px;
  font-weight: 700; color: var(--cyan); line-height: 1; }
.stat-card .sub { font-size: 12px; color: var(--text2); }

/* ── Main Layout ───────────────────────────────── */
.main { display: grid; grid-template-columns: 280px 1fr; gap: 0;
  height: calc(100vh - 185px); }

/* ── Sidebar ───────────────────────────────────── */
.sidebar {
  background: var(--bg2); border-right: 1px solid var(--border);
  overflow-y: auto; display: flex; flex-direction: column;
}
.sidebar-section { padding: 14px 16px; border-bottom: 1px solid var(--border); }
.sidebar-section h3 {
  font-size: 10px; text-transform: uppercase; letter-spacing: .1em;
  color: var(--text3); font-weight: 600; margin-bottom: 10px;
}

/* Band toggles */
.band-buttons { display: flex; gap: 6px; }
.band-btn {
  flex: 1; padding: 5px 4px; border-radius: 5px; cursor: pointer;
  font-family: var(--mono); font-size: 10px; font-weight: 600;
  border: 1px solid var(--border2); background: var(--bg3);
  color: var(--text2); transition: all .15s; text-align: center;
}
.band-btn.active { border-color: var(--cyan); color: var(--cyan); background: #00c8e810; }

/* Signal slider */
.slider-row { display: flex; align-items: center; gap: 8px; }
.slider-row label { font-size: 12px; color: var(--text2); white-space: nowrap; }
input[type=range] { flex: 1; accent-color: var(--cyan); }
#rssiVal { font-family: var(--mono); font-size: 12px; color: var(--cyan);
  min-width: 40px; text-align: right; }

/* SSID search */
.ssid-search {
  width: 100%; padding: 7px 10px; border-radius: 5px;
  border: 1px solid var(--border2); background: var(--bg3);
  color: var(--text); font-family: var(--sans); font-size: 13px;
  margin-bottom: 8px;
}
.ssid-search:focus { outline: none; border-color: var(--cyan); }
.ssid-search::placeholder { color: var(--text3); }

/* AP list */
.ap-list { display: flex; flex-direction: column; gap: 3px; }
.ap-item {
  display: flex; align-items: center; gap: 8px; padding: 7px 8px;
  border-radius: 5px; cursor: pointer; transition: background .1s;
  border: 1px solid transparent;
}
.ap-item:hover { background: var(--bg3); }
.ap-item.selected { background: #00c8e810; border-color: #00c8e840; }
.ap-item input[type=radio] { accent-color: var(--cyan); cursor: pointer; }
.ap-ssid { flex: 1; font-size: 12px; overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap; }
.ap-badge {
  font-family: var(--mono); font-size: 10px; font-weight: 700;
  padding: 2px 6px; border-radius: 3px; flex-shrink: 0;
}
.badge-24 { background: #a855f720; color: var(--purple); }
.badge-5  { background: #00c8e820; color: var(--cyan); }
.badge-6  { background: #22c55e20; color: var(--green); }
.ap-rssi  { font-family: var(--mono); font-size: 11px; color: var(--text2); min-width: 32px; text-align: right; }

/* Legend */
.legend {
  display: flex; gap: 6px; align-items: center; flex-wrap: wrap;
}
.legend-item { display: flex; align-items: center; gap: 4px; font-size: 11px; color: var(--text2); }
.legend-dot { width: 12px; height: 12px; border-radius: 50%; flex-shrink: 0; }

/* ── Map Area ───────────────────────────────────── */
.map-area {
  background: var(--bg); position: relative; overflow: hidden;
  display: flex; flex-direction: column;
}
.map-toolbar {
  display: flex; align-items: center; gap: 10px; padding: 10px 16px;
  border-bottom: 1px solid var(--border); background: var(--bg2);
  flex-wrap: wrap;
}
.map-toolbar span { font-size: 12px; color: var(--text2); }
.map-toolbar .ap-selector {
  font-size: 12px; color: var(--text); font-family: var(--mono);
  font-weight: 600;
}

#canvasWrap {
  flex: 1; position: relative; overflow: hidden;
  display: flex; align-items: center; justify-content: center;
  padding: 20px;
}
canvas#heatmap { border: 1px solid var(--border2); border-radius: var(--radius);
  cursor: crosshair; display: block; max-width: 100%; max-height: 100%; }

/* Tooltip */
#tooltip {
  position: absolute; pointer-events: none; display: none;
  background: var(--bg2); border: 1px solid var(--border2);
  border-radius: var(--radius); padding: 10px 14px;
  font-size: 12px; min-width: 200px; z-index: 50;
  box-shadow: 0 8px 32px #00000080;
}
#tooltip .tt-title { font-family: var(--mono); font-weight: 700;
  color: var(--cyan); margin-bottom: 6px; }
#tooltip .tt-ap { padding: 2px 0; display: flex; justify-content: space-between; gap: 12px; }

/* ── Table ─────────────────────────────────────── */
.table-section {
  border-top: 1px solid var(--border); background: var(--bg2);
  max-height: 220px; overflow-y: auto;
}
.table-section table { width: 100%; border-collapse: collapse; }
.table-section th {
  position: sticky; top: 0; background: var(--bg3);
  padding: 8px 12px; font-size: 10px; text-transform: uppercase;
  letter-spacing: .08em; color: var(--text3); font-weight: 600;
  text-align: left; white-space: nowrap; cursor: pointer;
}
.table-section th:hover { color: var(--cyan); }
.table-section td {
  padding: 7px 12px; font-size: 12px; border-top: 1px solid var(--border);
  font-family: var(--mono); white-space: nowrap;
}
.table-section tr:hover td { background: var(--bg3); }
.table-section tr.active-row td { background: #00c8e808; }
.rssi-good { color: var(--green); }
.rssi-ok   { color: var(--yellow); }
.rssi-fair { color: var(--orange); }
.rssi-bad  { color: var(--red); }

/* ── Responsive ────────────────────────────────── */
@media (max-width: 900px) {
  .main { grid-template-columns: 1fr; grid-template-rows: auto 1fr; }
  .sidebar { max-height: 200px; flex-direction: row; overflow-x: auto; }
  .stats-row { grid-template-columns: repeat(3, 1fr); }
}
</style>
</head>
<body>

<header>
  <div class="logo">📡 WiFi Survey</div>
  <h1>Signal <span>Coverage Dashboard</span></h1>
  <div class="meta" id="metaInfo"></div>
  <button class="btn" onclick="exportCSV()">⬇ Export CSV</button>
  <button class="btn btn-primary" onclick="window.print()">🖨 Print</button>
</header>

<div class="stats-row">
  <div class="stat-card"><div class="label">Unique APs</div>
    <div class="value" id="s-aps">—</div><div class="sub">networks detected</div></div>
  <div class="stat-card"><div class="label">Waypoints</div>
    <div class="value" id="s-wps">—</div><div class="sub">survey locations</div></div>
  <div class="stat-card"><div class="label">Coverage Area</div>
    <div class="value" id="s-area">—</div><div class="sub">m²</div></div>
  <div class="stat-card"><div class="label">Best Signal</div>
    <div class="value" id="s-best" style="color:var(--green)">—</div><div class="sub">dBm</div></div>
  <div class="stat-card"><div class="label">Avg Signal</div>
    <div class="value" id="s-avg">—</div><div class="sub">dBm selected AP</div></div>
</div>

<div style="display:flex;flex-direction:column;height:calc(100vh - 185px)">
<div class="main">
  <!-- Sidebar -->
  <div class="sidebar">
    <div class="sidebar-section">
      <h3>Select AP to Map</h3>
      <input class="ssid-search" type="text" id="ssidSearch"
             placeholder="Search SSIDs…" oninput="filterAPList()">
      <div class="ap-list" id="apList"></div>
    </div>
    <div class="sidebar-section">
      <h3>Band Filter</h3>
      <div class="band-buttons">
        <div class="band-btn active" data-band="2.4 GHz" onclick="toggleBand(this)">2.4 GHz</div>
        <div class="band-btn active" data-band="5 GHz"   onclick="toggleBand(this)">5 GHz</div>
        <div class="band-btn active" data-band="6 GHz"   onclick="toggleBand(this)">6 GHz</div>
      </div>
    </div>
    <div class="sidebar-section">
      <h3>Min Signal Threshold</h3>
      <div class="slider-row">
        <label>dBm ≥</label>
        <input type="range" id="rssiSlider" min="-100" max="-30" value="-95"
               oninput="updateSlider(this)">
        <span id="rssiVal">-95</span>
      </div>
    </div>
    <div class="sidebar-section">
      <h3>Signal Legend</h3>
      <div class="legend">
        <div class="legend-item"><div class="legend-dot" style="background:#22c55e"></div> Excellent (≥-60)</div>
        <div class="legend-item"><div class="legend-dot" style="background:#f59e0b"></div> Good (-60–-70)</div>
        <div class="legend-item"><div class="legend-dot" style="background:#f97316"></div> Fair (-70–-80)</div>
        <div class="legend-item"><div class="legend-dot" style="background:#ef4444"></div> Poor (&lt;-80)</div>
      </div>
    </div>
    <div class="sidebar-section" style="flex:1">
      <h3>Options</h3>
      <label style="display:flex;align-items:center;gap:8px;cursor:pointer;font-size:13px;">
        <input type="checkbox" id="showGrid" checked onchange="redraw()"
               style="accent-color:var(--cyan)"> Show grid
      </label>
      <br>
      <label style="display:flex;align-items:center;gap:8px;cursor:pointer;font-size:13px;">
        <input type="checkbox" id="showLabels" checked onchange="redraw()"
               style="accent-color:var(--cyan)"> Show waypoint labels
      </label>
      <br>
      <label style="display:flex;align-items:center;gap:8px;cursor:pointer;font-size:13px;">
        <input type="checkbox" id="showHeat" checked onchange="redraw()"
               style="accent-color:var(--cyan)"> Show heatmap
      </label>
    </div>
  </div>

  <!-- Map -->
  <div class="map-area">
    <div class="map-toolbar">
      <span>Showing:</span>
      <span class="ap-selector" id="currentAPLabel">— select an AP →</span>
      <span style="margin-left:auto;color:var(--text3);font-size:11px">
        Click a waypoint on the map for details</span>
    </div>
    <div id="canvasWrap">
      <canvas id="heatmap"></canvas>
      <div id="tooltip"></div>
    </div>
  </div>
</div>

<!-- Table -->
<div class="table-section" id="tableSection">
  <table>
    <thead>
      <tr>
        <th onclick="sortTable('id')">#</th>
        <th onclick="sortTable('label')">Label</th>
        <th onclick="sortTable('x')">X (m)</th>
        <th onclick="sortTable('y')">Y (m)</th>
        <th onclick="sortTable('apCount')">APs Seen</th>
        <th onclick="sortTable('bestRssi')">Best RSSI</th>
        <th onclick="sortTable('selRssi')">Selected AP RSSI</th>
        <th>Timestamp</th>
      </tr>
    </thead>
    <tbody id="tableBody"></tbody>
  </table>
</div>
</div>

<script>
// ── Data (injected by Python) ────────────────────────────────────────────────
const SURVEY = __SURVEY_DATA__;

// ── State ────────────────────────────────────────────────────────────────────
let selectedBSSID = null;
let activeBands = new Set(["2.4 GHz","5 GHz","6 GHz"]);
let minRSSI = -95;
let sortKey = "id", sortDir = 1;
let hoveredWP = null;

// ── Process data ─────────────────────────────────────────────────────────────
const info = SURVEY.survey_info;
const waypoints = SURVEY.waypoints;

// Collect all unique APs across all waypoints
const apMap = new Map(); // bssid → {ssid, bssid, band, bestRssi, seenAt}
for (const wp of waypoints) {
  for (const ap of wp.aps) {
    if (!apMap.has(ap.bssid)) {
      apMap.set(ap.bssid, { ssid: ap.ssid, bssid: ap.bssid, band: ap.band,
                             channel: ap.channel, bestRssi: ap.rssi, count: 0 });
    }
    const entry = apMap.get(ap.bssid);
    entry.bestRssi = Math.max(entry.bestRssi, ap.rssi);
    entry.count++;
  }
}
const allAPs = [...apMap.values()].sort((a,b) => b.bestRssi - a.bestRssi);

// ── Canvas setup ─────────────────────────────────────────────────────────────
const canvas = document.getElementById("heatmap");
const ctx = canvas.getContext("2d");
const AREA_W = info.area_width;
const AREA_H = info.area_height;
const CANVAS_W = 900;
const CANVAS_H = Math.round(CANVAS_W * (AREA_H / AREA_W));
canvas.width  = CANVAS_W;
canvas.height = CANVAS_H;

function mToC(x, y) {
  return { cx: (x / AREA_W) * CANVAS_W, cy: (y / AREA_H) * CANVAS_H };
}
function cToM(cx, cy) {
  return { x: (cx / CANVAS_W) * AREA_W, y: (cy / CANVAS_H) * AREA_H };
}

// ── Colour helpers ────────────────────────────────────────────────────────────
function rssiToNorm(dbm) {
  return Math.max(0, Math.min(1, (dbm + 100) / 70));  // -100=0, -30=1
}
function lerpRGB(c1, c2, t) {
  return { r: c1.r+(c2.r-c1.r)*t, g: c1.g+(c2.g-c1.g)*t, b: c1.b+(c2.b-c1.b)*t };
}
function normToRGB(t) {
  const stops = [
    {t:0,    c:{r:239,g:68, b:68 }},   // red
    {t:0.28, c:{r:249,g:115,b:22 }},   // orange
    {t:0.50, c:{r:245,g:158,b:11 }},   // yellow
    {t:0.72, c:{r:132,g:204,b:16 }},   // lime
    {t:1,    c:{r:34, g:197,b:94 }},   // green
  ];
  for (let i=0; i<stops.length-1; i++) {
    if (t >= stops[i].t && t <= stops[i+1].t) {
      const lt = (t-stops[i].t)/(stops[i+1].t-stops[i].t);
      return lerpRGB(stops[i].c, stops[i+1].c, lt);
    }
  }
  return stops[stops.length-1].c;
}

// ── IDW Interpolation ─────────────────────────────────────────────────────────
// Inverse Distance Weighting — the same algorithm real survey tools use
function idw(cx, cy, pts, power=2.5) {
  let num=0, den=0, minDist=Infinity;
  for (const p of pts) {
    const d = Math.sqrt((cx-p.cx)**2 + (cy-p.cy)**2);
    if (d < 0.0001) return {val: p.val, minDist: 0};
    minDist = Math.min(minDist, d);
    const w = 1 / Math.pow(d, power);
    num += w * p.val; den += w;
  }
  return { val: den===0 ? 0 : num/den, minDist };
}

// ── Heatmap renderer ──────────────────────────────────────────────────────────
function buildHeatPoints() {
  if (!selectedBSSID) return [];
  const pts = [];
  for (const wp of waypoints) {
    const ap = wp.aps.find(a => a.bssid === selectedBSSID);
    if (!ap) continue;
    const {cx, cy} = mToC(wp.x, wp.y);
    pts.push({cx, cy, val: rssiToNorm(ap.rssi), rssi: ap.rssi});
  }
  return pts;
}

function renderHeatmap(pts) {
  const res = 6;  // pixel resolution of the grid
  const cols = Math.ceil(CANVAS_W / res);
  const rows = Math.ceil(CANVAS_H / res);
  const imageData = ctx.createImageData(CANVAS_W, CANVAS_H);
  const d = imageData.data;

  // Influence radius: fraction of canvas size
  const radius = Math.max(CANVAS_W, CANVAS_H) * 0.25;

  for (let gy=0; gy<rows; gy++) {
    for (let gx=0; gx<cols; gx++) {
      const cx = gx*res + res/2;
      const cy = gy*res + res/2;
      const {val, minDist} = idw(cx, cy, pts);

      // Fade out based on distance to nearest waypoint
      const alpha = pts.length ? Math.max(0, Math.min(200, 200*(1-minDist/radius))) : 0;
      if (alpha < 2) continue;

      const rgb = normToRGB(val);
      // Fill res×res pixels
      for (let dy=0; dy<res && gy*res+dy<CANVAS_H; dy++) {
        for (let dx=0; dx<res && gx*res+dx<CANVAS_W; dx++) {
          const idx = ((gy*res+dy)*CANVAS_W + (gx*res+dx)) * 4;
          d[idx]   = rgb.r;
          d[idx+1] = rgb.g;
          d[idx+2] = rgb.b;
          d[idx+3] = alpha;
        }
      }
    }
  }
  ctx.putImageData(imageData, 0, 0);
}

// ── Draw ──────────────────────────────────────────────────────────────────────
function redraw() {
  ctx.clearRect(0,0,CANVAS_W,CANVAS_H);

  // Background
  ctx.fillStyle = "#080d17";
  ctx.fillRect(0,0,CANVAS_W,CANVAS_H);

  // Grid
  if (document.getElementById("showGrid").checked) {
    ctx.strokeStyle = "#1a2540";
    ctx.lineWidth = 1;
    for (let x=0; x<=AREA_W; x++) {
      const cx = (x/AREA_W)*CANVAS_W;
      ctx.beginPath(); ctx.moveTo(cx,0); ctx.lineTo(cx,CANVAS_H); ctx.stroke();
    }
    for (let y=0; y<=AREA_H; y++) {
      const cy = (y/AREA_H)*CANVAS_H;
      ctx.beginPath(); ctx.moveTo(0,cy); ctx.lineTo(CANVAS_W,cy); ctx.stroke();
    }
    // Grid labels
    ctx.fillStyle = "#243558"; ctx.font = "10px JetBrains Mono";
    for (let x=0; x<=AREA_W; x+=2) {
      const {cx} = mToC(x,0);
      ctx.fillText(`${x}m`, cx+3, 12);
    }
    for (let y=0; y<=AREA_H; y+=2) {
      const {cy} = mToC(0,y);
      ctx.fillText(`${y}m`, 3, cy-3);
    }
  }

  // Heatmap
  if (document.getElementById("showHeat").checked && selectedBSSID) {
    const pts = buildHeatPoints();
    if (pts.length) renderHeatmap(pts);
  }

  // Waypoint markers
  for (const wp of waypoints) {
    // Find signal for selected AP at this waypoint
    const selAP = selectedBSSID ? wp.aps.find(a => a.bssid === selectedBSSID) : null;
    const {cx, cy} = mToC(wp.x, wp.y);

    // Outer ring
    const isHovered = hoveredWP && hoveredWP.id === wp.id;
    const ringR = isHovered ? 14 : 10;

    if (selAP) {
      const norm = rssiToNorm(selAP.rssi);
      const rgb = normToRGB(norm);
      ctx.strokeStyle = `rgba(${rgb.r},${rgb.g},${rgb.b},0.8)`;
    } else {
      ctx.strokeStyle = "#2a4070";
    }
    ctx.lineWidth = isHovered ? 2.5 : 1.5;
    ctx.beginPath(); ctx.arc(cx,cy,ringR,0,Math.PI*2); ctx.stroke();

    // Fill dot
    ctx.fillStyle = selAP
      ? (() => { const {r,g,b}=normToRGB(rssiToNorm(selAP.rssi));
                 return `rgba(${r},${g},${b},0.9)`; })()
      : "#1e3060";
    ctx.beginPath(); ctx.arc(cx,cy,6,0,Math.PI*2); ctx.fill();

    // White centre dot
    ctx.fillStyle = "#ffffff";
    ctx.beginPath(); ctx.arc(cx,cy,2,0,Math.PI*2); ctx.fill();

    // Label
    if (document.getElementById("showLabels").checked) {
      ctx.fillStyle = isHovered ? "#ffffff" : "#7890b8";
      ctx.font = isHovered ? "bold 11px Outfit" : "10px Outfit";
      ctx.fillText(wp.label, cx+13, cy+4);
    }
  }
}

// ── Tooltip & hover ───────────────────────────────────────────────────────────
const tooltip = document.getElementById("tooltip");
const canvasWrap = document.getElementById("canvasWrap");

canvas.addEventListener("mousemove", e => {
  const rect = canvas.getBoundingClientRect();
  const scaleX = CANVAS_W / rect.width;
  const scaleY = CANVAS_H / rect.height;
  const cx = (e.clientX - rect.left) * scaleX;
  const cy = (e.clientY - rect.top)  * scaleY;

  // Find nearest waypoint within 20px (scaled)
  let nearest = null, nearDist = 20 * scaleX;
  for (const wp of waypoints) {
    const pc = mToC(wp.x, wp.y);
    const d = Math.sqrt((cx-pc.cx)**2 + (cy-pc.cy)**2);
    if (d < nearDist) { nearDist = d; nearest = wp; }
  }

  if (nearest !== hoveredWP) {
    hoveredWP = nearest;
    redraw();
  }

  if (nearest) {
    canvas.style.cursor = "pointer";
    const aps = [...nearest.aps].sort((a,b)=>b.rssi-a.rssi).slice(0,8);
    tooltip.innerHTML = `
      <div class="tt-title">📍 ${nearest.label}  (${nearest.x}m, ${nearest.y}m)</div>
      <div style="color:var(--text3);font-size:11px;margin-bottom:6px">${nearest.timestamp.replace('T',' ')}</div>
      ${aps.map(ap => {
        const col = ap.rssi>=-60?'#22c55e':ap.rssi>=-70?'#f59e0b':ap.rssi>=-80?'#f97316':'#ef4444';
        return `<div class="tt-ap">
          <span>${ap.ssid.substring(0,22)}</span>
          <span style="color:${col};font-family:var(--mono)">${ap.rssi} dBm</span>
        </div>`;
      }).join('')}
      ${nearest.aps.length > 8 ? `<div style="color:var(--text3);font-size:11px">+${nearest.aps.length-8} more</div>` : ''}`;
    tooltip.style.display = "block";
    // Position tooltip avoiding edges
    const wrapRect = canvasWrap.getBoundingClientRect();
    let tx = e.clientX - wrapRect.left + 15;
    let ty = e.clientY - wrapRect.top  + 15;
    if (tx + 230 > wrapRect.width) tx -= 245;
    tooltip.style.left = tx + "px";
    tooltip.style.top  = ty + "px";
  } else {
    canvas.style.cursor = "crosshair";
    tooltip.style.display = "none";
  }
});
canvas.addEventListener("mouseleave", () => {
  hoveredWP = null; tooltip.style.display = "none"; redraw();
});

// ── AP List ───────────────────────────────────────────────────────────────────
function renderAPList() {
  const query = document.getElementById("ssidSearch").value.toLowerCase();
  const list  = document.getElementById("apList");
  list.innerHTML = "";

  const filtered = allAPs.filter(ap =>
    ap.ssid.toLowerCase().includes(query) &&
    activeBands.has(ap.band) &&
    ap.bestRssi >= minRSSI
  );

  for (const ap of filtered) {
    const item = document.createElement("div");
    item.className = "ap-item" + (ap.bssid === selectedBSSID ? " selected" : "");
    const bandClass = ap.band==="2.4 GHz"?"badge-24":ap.band==="5 GHz"?"badge-5":"badge-6";
    const bandShort = ap.band==="2.4 GHz"?"2.4G":ap.band==="5 GHz"?"5G":"6G";
    const rssiCol = ap.bestRssi>=-60?"color:var(--green)":
                    ap.bestRssi>=-70?"color:var(--yellow)":
                    ap.bestRssi>=-80?"color:var(--orange)":"color:var(--red)";
    item.innerHTML = `
      <input type="radio" name="apsel" ${ap.bssid===selectedBSSID?"checked":""}>
      <span class="ap-ssid" title="${ap.ssid}\n${ap.bssid}">${ap.ssid}</span>
      <span class="ap-badge ${bandClass}">${bandShort}</span>
      <span class="ap-rssi" style="${rssiCol}">${ap.bestRssi}</span>`;
    item.onclick = () => selectAP(ap);
    list.appendChild(item);
  }

  if (!filtered.length) {
    list.innerHTML = `<div style="color:var(--text3);font-size:12px;padding:8px">No APs match filters</div>`;
  }
}

function selectAP(ap) {
  selectedBSSID = ap.bssid;
  document.getElementById("currentAPLabel").textContent =
    `${ap.ssid}  │  ${ap.bssid}  │  ch ${ap.channel} ${ap.band}`;
  updateStats();
  renderAPList();
  renderTable();
  redraw();
}

// ── Filters ───────────────────────────────────────────────────────────────────
function filterAPList() { renderAPList(); }

function toggleBand(el) {
  const b = el.dataset.band;
  if (activeBands.has(b)) { activeBands.delete(b); el.classList.remove("active"); }
  else { activeBands.add(b); el.classList.add("active"); }
  renderAPList();
}

function updateSlider(el) {
  minRSSI = parseInt(el.value);
  document.getElementById("rssiVal").textContent = el.value;
  renderAPList();
}

// ── Stats ─────────────────────────────────────────────────────────────────────
function updateStats() {
  document.getElementById("s-aps").textContent  = allAPs.length;
  document.getElementById("s-wps").textContent  = waypoints.length;
  document.getElementById("s-area").textContent = (info.area_width * info.area_height).toFixed(0);

  let bestAll = -Infinity;
  for (const ap of allAPs) bestAll = Math.max(bestAll, ap.bestRssi);
  document.getElementById("s-best").textContent = bestAll > -Infinity ? bestAll+" dBm" : "—";

  if (selectedBSSID) {
    let sum=0, cnt=0;
    for (const wp of waypoints) {
      const ap = wp.aps.find(a => a.bssid === selectedBSSID);
      if (ap) { sum += ap.rssi; cnt++; }
    }
    document.getElementById("s-avg").textContent = cnt ? Math.round(sum/cnt)+" dBm" : "—";
  }
}

// ── Table ─────────────────────────────────────────────────────────────────────
function rssiClass(v) {
  return v >= -60 ? "rssi-good" : v >= -70 ? "rssi-ok" : v >= -80 ? "rssi-fair" : "rssi-bad";
}

function renderTable() {
  const tbody = document.getElementById("tableBody");
  const rows = waypoints.map(wp => {
    const bestRssi = wp.aps.length ? Math.max(...wp.aps.map(a=>a.rssi)) : -999;
    const selAP = selectedBSSID ? wp.aps.find(a=>a.bssid===selectedBSSID) : null;
    return {...wp, apCount: wp.aps.length, bestRssi, selRssi: selAP?.rssi ?? null};
  });
  rows.sort((a,b) => sortDir * (typeof a[sortKey]==="number"
    ? a[sortKey]-b[sortKey] : a[sortKey]?.localeCompare(b[sortKey]||"")));
  tbody.innerHTML = rows.map(r => `
    <tr class="${hoveredWP?.id===r.id?'active-row':''}">
      <td>${r.id}</td>
      <td>${r.label}</td>
      <td>${r.x.toFixed(1)}</td>
      <td>${r.y.toFixed(1)}</td>
      <td>${r.apCount}</td>
      <td class="${rssiClass(r.bestRssi)}">${r.bestRssi} dBm</td>
      <td class="${r.selRssi!==null?rssiClass(r.selRssi):''}">
        ${r.selRssi!==null?r.selRssi+" dBm":"—"}</td>
      <td style="color:var(--text3)">${r.timestamp.replace('T',' ').substring(0,19)}</td>
    </tr>`).join("");
}

function sortTable(key) {
  if (sortKey===key) sortDir=-sortDir; else { sortKey=key; sortDir=1; }
  renderTable();
}

// ── Export CSV ────────────────────────────────────────────────────────────────
function exportCSV() {
  const rows = [["WP_ID","Label","X_m","Y_m","SSID","BSSID","Band","Channel","RSSI_dBm","Timestamp"]];
  for (const wp of waypoints) {
    for (const ap of wp.aps) {
      rows.push([wp.id,wp.label,wp.x,wp.y,
        `"${ap.ssid}"`,ap.bssid,ap.band,ap.channel,ap.rssi,wp.timestamp]);
    }
  }
  const csv = rows.map(r=>r.join(",")).join("\n");
  const a = document.createElement("a");
  a.href = "data:text/csv;charset=utf-8," + encodeURIComponent(csv);
  a.download = "wifi_survey.csv"; a.click();
}

// ── Meta info ─────────────────────────────────────────────────────────────────
document.getElementById("metaInfo").innerHTML =
  `${info.created.replace('T',' ').substring(0,16)}  │  ` +
  `${info.area_width}×${info.area_height} m  │  ` +
  `${info.os}  │  ${info.interface}`;

// ── Init ──────────────────────────────────────────────────────────────────────
renderAPList();
updateStats();
renderTable();
redraw();

// Auto-select the strongest AP on load
if (allAPs.length) selectAP(allAPs[0]);
</script>
</body>
</html>
"""


# ──────────────────────────────────────────────────────
# Dashboard Generator
# ──────────────────────────────────────────────────────
def generate_dashboard(survey_data: dict, output_path: str):
    json_str = json.dumps(survey_data, separators=(",", ":"))
    html = HTML_TEMPLATE.replace("__SURVEY_DATA__", json_str)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    return output_path


# ──────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="WiFi Walk-Survey Tool — DIY NetSpot/Ekahau alternative",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    parser.add_argument("--demo",   action="store_true",
                        help="Generate a demo dashboard without a real adapter")
    parser.add_argument("--load",   metavar="FILE",
                        help="Load existing survey JSON and regenerate dashboard")
    parser.add_argument("--out",    metavar="NAME", default="survey",
                        help="Output file prefix (default: survey)")
    args = parser.parse_args()

    out_json = f"{args.out}.json"
    out_html = f"{args.out}_dashboard.html"

    # ── Demo mode ────────────────────────────────────────
    if args.demo:
        print(f"{C.CYN}Generating demo survey data…{C.RST}")
        session = generate_demo_data()
        session.save(out_json, interface="demo")
        data = json.loads(Path(out_json).read_text())
        generate_dashboard(data, out_html)
        print(f"{C.GRN}✓ Demo dashboard → {C.BOLD}{out_html}{C.RST}")
        print(f"  Open in your browser: {Path(out_html).resolve()}")
        return

    # ── Load mode ─────────────────────────────────────────
    if args.load:
        data = json.loads(Path(args.load).read_text())
        generate_dashboard(data, out_html)
        print(f"{C.GRN}✓ Dashboard regenerated → {C.BOLD}{out_html}{C.RST}")
        return

    # ── Survey mode ───────────────────────────────────────
    print(f"\n{C.BOLD}Survey Area Setup{C.RST}")
    print(f"  Define the physical area you'll be walking in (used for coordinate scale).")
    try:
        w = float(input(f"  Width  (metres) [{DEFAULT_W}]: ").strip() or DEFAULT_W)
        h = float(input(f"  Height (metres) [{DEFAULT_H}]: ").strip() or DEFAULT_H)
    except (ValueError, EOFError):
        w, h = DEFAULT_W, DEFAULT_H

    scanner = WiFiScanner()
    session = SurveySession(area_w=w, area_h=h)
    cli = InteractiveCLI(scanner, session)

    try:
        cli.run()
    except KeyboardInterrupt:
        print(f"\n  {C.YLW}Interrupted. Saving data…{C.RST}")

    if not session.waypoints:
        print(f"{C.YLW}No waypoints recorded. Exiting without saving.{C.RST}")
        return

    session.save(out_json, interface=scanner.interface or "unknown")
    print(f"  {C.GRN}✓ Survey data saved → {out_json}{C.RST}")

    data = json.loads(Path(out_json).read_text())
    generate_dashboard(data, out_html)
    print(f"  {C.GRN}✓ Dashboard generated → {C.BOLD}{out_html}{C.RST}")
    print(f"\n  Open in browser: {Path(out_html).resolve()}\n")


if __name__ == "__main__":
    main()

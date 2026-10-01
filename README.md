# 📡 WiFi Walk-Survey Tool

A practical DIY Wi-Fi site survey tool — a lightweight open-source alternative to
**NetSpot** and **Ekahau AI Pro**. Scans nearby access points, logs them to GPS-like
coordinates as you walk a building, and produces a fully interactive HTML dashboard
with an IDW-interpolated signal heatmap.

---

## Features

| Feature | Detail |
|---|---|
| 🔍 Continuous scanning | Background thread scans every 4 s via `nmcli` / `netsh` |
| 📊 Logs SSID, BSSID, Band, Channel, RSSI | Per-waypoint AP snapshots |
| 🗺 Interactive heatmap | IDW interpolation — same math as professional tools |
| 🎛 Filters | Per-AP, per-band (2.4/5/6 GHz), RSSI threshold slider |
| 💾 Exports | JSON survey file + CSV export from dashboard |
| 🖥 Cross-platform | Ubuntu Linux + Windows 10/11 |

---

## Quick Start

### Requirements

```bash
# Ubuntu / Debian
sudo apt update
sudo apt install python3 network-manager iw

# Windows — no extra packages needed (uses built-in netsh)
# Python 3.8+ required: https://python.org/downloads
```

### Run

```bash
# Demo mode — no Wi-Fi adapter needed, generates sample data
python3 wifi_survey.py --demo

# Live survey mode
python3 wifi_survey.py

# Regenerate dashboard from a saved survey file
python3 wifi_survey.py --load my_survey.json

# Custom output name
python3 wifi_survey.py --out office_floor2
```

### During a survey

```
Commands:
  [Enter]   Record a waypoint — prompts you for X/Y coordinates + label
  s         Show the current scan results live
  q         Finish and generate the HTML dashboard
  h         Help
```

**Workflow:**
1. Launch the script, enter your survey area dimensions (e.g. 10m × 8m)
2. Walk to the first spot, wait ~4 s for the scan to update
3. Press **Enter**, type coordinates and a label ("Kitchen", "3.5", "2.0")
4. Walk to the next spot, repeat
5. Press **q** when done → `survey_dashboard.html` is generated
6. Open the HTML file in any browser — no server needed

---

## VMware Workstation Pro — Network Adapter Configuration

This is the most important section. **Wi-Fi scanning inside a VM requires specific
setup.** Standard VMware networking (NAT, Bridged) will NOT work for scanning.

---

### ❌ Why Standard Adapters Won't Work

#### NAT (Network Address Translation)
```
VM  →  VMware virtual NIC  →  VMware NAT engine  →  Host Wi-Fi adapter  →  Router
```
- The VM sees a **virtual Ethernet card** (vmnet8), not Wi-Fi
- Wi-Fi management frames (probe requests, beacon frames) are consumed by the host
- `nmcli` inside the VM will find no wireless interfaces
- **Good for:** general internet access in the VM
- **Bad for:** Wi-Fi scanning

#### Bridged (Bridge to Host Wi-Fi adapter)
```
VM  →  VMware virtual NIC  →  VMware bridge driver  →  Host Wi-Fi adapter
```
- Still a virtual Ethernet NIC from the VM's perspective
- VMware bridges at Layer 2 (Ethernet), but Wi-Fi 802.11 management is handled by the
  host OS. The guest cannot issue `SCAN` commands to the chipset.
- `nmcli dev wifi list` inside the VM: **no Wi-Fi interface found**
- **Good for:** VM appearing on the same LAN subnet as the host
- **Bad for:** Wi-Fi scanning

#### Host-only
- Completely isolated virtual network between VM and host only
- No internet, no Wi-Fi scanning capability
- **Good for:** secure isolated VM development
- **Bad for:** everything else here

---

### ✅ The Only Working Method: USB Wi-Fi Adapter Passthrough

Pass a **physical USB Wi-Fi dongle** directly to the VM. The VM owns the hardware
entirely — VMware's virtual NIC layer is bypassed. The guest OS sees the actual
chipset and can scan.

```
[USB Wi-Fi Dongle] ──USB──► [VMware USB Controller] ──► [Ubuntu VM]
                                                         (nmcli sees wlan0)
```

#### Step 1 — Get a Compatible USB Wi-Fi Adapter

Any adapter with good Linux driver support works. Recommended chips:

| Chip | Example Products | Linux Support |
|---|---|---|
| **Realtek RTL8812AU** | Alfa AWUS036ACH, TP-Link Archer T4U | Excellent (5 GHz AC) |
| **Realtek RTL8188EUS** | Lots of $8 Amazon dongles | Excellent (2.4 GHz only) |
| **Mediatek MT7612U** | Alfa AWUS036ACM | Excellent (dual band) |
| **Realtek RTL8814AU** | Alfa AWUS1900 | Good (3×3 AC) |

**Avoid:** Broadcom-based USB adapters (poor Linux support).

#### Step 2 — Configure VMware USB Controller

1. In VMware Workstation Pro, right-click your Ubuntu VM → **Settings**
2. Click **USB Controller** in the Hardware tab
3. Change **USB compatibility** to **USB 3.1** (or at minimum USB 3.0)
4. Enable **"Show all USB input devices"** checkbox
5. Enable **"Automatically connect new USB devices"** ← important
6. Click **OK**

```
VM Settings → Hardware → USB Controller
┌─────────────────────────────────────────┐
│ USB compatibility:    [USB 3.1      ▼]  │  ← Use 3.1 for maximum speed
│ ☑ Show all USB input devices            │
│ ☑ Automatically connect new USB devices │  ← Key setting
└─────────────────────────────────────────┘
```

**Why USB 3.1?** Some Wi-Fi adapters enumerate incorrectly on USB 2.0 compatibility
mode inside VMware. USB 3.1 mode handles the descriptors more accurately.

#### Step 3 — Connect the Adapter to the VM

1. Plug the USB Wi-Fi dongle into your PC
2. If the VM is running: **VM menu → Removable Devices → [Your Adapter] → Connect**
3. If the VM is powered off: in Settings → USB Controller → click **Add** → select the adapter

You can also right-click the USB icon in the VMware taskbar at the bottom of the VM
window and select the adapter from the list.

```
VMware Workstation taskbar (bottom of VM window):
  [🖥] [📀] [🔊] [🖱] [🔌 USB▼]
                              └── Click here → select your Wi-Fi dongle
```

#### Step 4 — Install Drivers in Ubuntu (if needed)

Most modern USB Wi-Fi adapters work out of the box on Ubuntu 22.04+. If yours
doesn't appear as a Wi-Fi device, install the driver:

```bash
# Check if the adapter is detected at all
lsusb
# Look for your adapter's vendor (Realtek = 0bda:xxxx, MediaTek = 0e8d:xxxx)

# For Realtek RTL8812AU (most common):
sudo apt install dkms git build-essential linux-headers-$(uname -r)
git clone https://github.com/aircrack-ng/rtl8812au.git
cd rtl8812au
sudo make dkms_install

# For Realtek RTL8188EUS:
sudo apt install realtek-rtl88xxau-dkms

# Verify interface appeared
nmcli dev status
# Should show something like: wlan0  wifi  disconnected
```

#### Step 5 — Verify Everything Works

```bash
# Check interface is visible
ip link show | grep wlan

# Test scan (should list nearby APs)
nmcli dev wifi list --rescan yes

# Check interface name (might be wlan0, wlx... or wlp...)
nmcli -t -f DEVICE,TYPE dev | grep wifi
```

Now run the survey tool — it will auto-detect the interface.

---

### VMware Network Adapter Settings Summary

```
┌─────────────────┬──────────────┬──────────────────────────────────────────┐
│ Mode            │ Works for    │ Wi-Fi Scanning                           │
│                 │ Internet?    │                                          │
├─────────────────┼──────────────┼──────────────────────────────────────────┤
│ NAT             │ ✅ Yes       │ ❌ No — VM sees virtual Ethernet          │
│ Bridged         │ ✅ Yes       │ ❌ No — Still virtual NIC, no 802.11 mgmt│
│ Host-only       │ ❌ No        │ ❌ No                                     │
│ USB Passthrough │ ✅ Yes*      │ ✅ YES — VM owns physical chipset         │
└─────────────────┴──────────────┴──────────────────────────────────────────┘
* Internet via the passed-through adapter works if connected to a network
  OR keep a second virtual NIC in NAT mode for internet + USB adapter for scanning.
```

**Pro tip:** Use **two network adapters** in your VM:
- Adapter 1: **NAT** — for internet access while surveying
- Adapter 2: **USB Wi-Fi dongle passthrough** — for scanning

This way you can scan with `nmcli` on `wlan0` while still having internet on `eth0`.

---

## Dashboard Guide

### Heatmap

The heatmap uses **Inverse Distance Weighting (IDW)** interpolation — the same
algorithm used by NetSpot, Ekahau, and professional RF survey tools. Signal values
at your measured waypoints are blended across the floor plan, fading out beyond
your measured coverage area.

**Select an AP** from the sidebar to show its signal heatmap. The colour scale:

```
  🔴 Red     — Poor     (< -80 dBm)  — 1-2 bars on your phone
  🟠 Orange  — Fair     (-80 to -70) — 2-3 bars
  🟡 Yellow  — Good     (-70 to -60) — 3-4 bars
  🟢 Green   — Excellent (> -60 dBm) — Full signal
```

### Tips for Better Surveys

- **Waypoint density:** Take readings every 2–3 metres. Fewer points = less accurate
  interpolation.
- **Edge coverage:** Include waypoints near walls and corners — these define the
  coverage boundary and prevent the heatmap from extrapolating wildly.
- **Scan interval:** The background scan runs every 4 seconds. After moving to a new
  spot, wait 5+ seconds before recording to ensure the scan is fresh.
- **AP selection:** Each AP has its own heatmap. Survey each AP separately to identify
  coverage holes per network.
- **Multiple floors:** Run a separate survey per floor, save with `--out floor1`,
  `--out floor2`, etc.

---

## File Outputs

| File | Description |
|---|---|
| `survey.json` | Raw survey data — all waypoints + AP readings |
| `survey_dashboard.html` | Self-contained interactive dashboard (no server needed) |

The JSON file can be loaded later with `--load survey.json` to regenerate the
dashboard after adjustments.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| "No Wi-Fi interface found" | Ensure USB adapter is connected to VM (not host) |
| nmcli shows no APs | Try `sudo nmcli dev wifi list --rescan yes` |
| Scan returns empty on Linux | Check `systemctl status NetworkManager` — must be running |
| Windows: "No networks found" | Run cmd.exe as Administrator |
| Very slow scanning | Normal — `nmcli --rescan yes` takes 5–15 s depending on channels |
| Dashboard heatmap empty | Need at least 2 waypoints with the selected AP visible |
| USB adapter keeps disconnecting | In VMware USB settings, enable "Automatically connect" |

---

## How IDW Interpolation Works

At each pixel of the heatmap canvas, the tool:

1. Finds all waypoints where the selected AP was visible
2. Computes the distance from the pixel to each waypoint
3. Applies the IDW formula: `value = Σ(signal_i / dist_i²) / Σ(1 / dist_i²)`
4. Points far from any waypoint get reduced alpha (transparency) — the heatmap
   "fades out" honestly rather than extrapolating to areas you didn't measure

This produces smooth, physically plausible signal maps without any server or
heavy dependencies.

---

*WiFi Walk-Survey Tool v1.1 — MIT License*

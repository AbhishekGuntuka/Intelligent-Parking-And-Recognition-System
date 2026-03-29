<div align="center">

<img src="https://img.shields.io/badge/IPRS-v10.0-blue?style=for-the-badge&logo=parking&logoColor=white" alt="IPRS Version"/>
<img src="https://img.shields.io/badge/ESP32-IoT-red?style=for-the-badge&logo=espressif&logoColor=white" alt="ESP32"/>
<img src="https://img.shields.io/badge/YOLOv8n-ONNX-purple?style=for-the-badge&logo=pytorch&logoColor=white" alt="YOLOv8"/>
<img src="https://img.shields.io/badge/Python-3.11-blue?style=for-the-badge&logo=python&logoColor=white" alt="Python"/>
<img src="https://img.shields.io/badge/Flask-3.0+-black?style=for-the-badge&logo=flask&logoColor=white" alt="Flask"/>
<img src="https://img.shields.io/badge/License-MIT-green?style=for-the-badge" alt="License"/>

# 🚗 IPRS — Intelligent Parking & Recognition System

**A production-grade, full-stack IoT solution for automated parking lot management.**  
Real-time ANPR · Edge AI on ESP32 · SmartCoin Wallet · Blockchain Receipts

[Features](#-features) · [Architecture](#-system-architecture) · [Hardware](#-hardware) · [Setup](#-quick-start) · [API Reference](#-api-reference) · [Contributing](#-contributing)

</div>

---

## 📖 Overview

IPRS is a complete end-to-end embedded IoT system that **fully automates parking lot operations** — eliminating manual booth staffing, paper logs, and cash handling. The system orchestrates two ESP32 microcontrollers, a YOLOv8n computer vision pipeline, a Python-Flask backend, and dual browser dashboards, all communicating in real time over WebSocket, MQTT, and HTTP REST.

### What It Does

| Capability | Details |
|---|---|
| 🔍 **ANPR** | YOLOv8n + Tesseract OCR — 5 preprocessing variants, 4 PSM modes, Indian plate regex |
| 🚧 **Automated Gates** | IR-triggered servo barrier with 15s cooldown guard and FreeRTOS safety task |
| 🅿️ **Slot Tracking** | 4-slot real-time occupancy via IR sensors + dual-colour LED indicators |
| 💰 **SmartCoin Wallet** | Token-based payment, pre-booking, QR token, 50-coin welcome bonus |
| 🔗 **Blockchain Receipts** | SHA-256 hash-linked receipt chain — tamper-evident transaction history |
| 📡 **OTA Updates** | Wireless firmware updates on both ESP32 nodes, no physical access required |
| 🖥️ **Dual Dashboards** | Owner live-monitoring SPA + Customer booking portal (browser-native, no app install) |

---

## ✨ Features

<details>
<summary><b>🏗️ System Features (click to expand)</b></summary>

- **Full Entry/Exit Automation** — IR trigger → capture → OCR → gate open → slot update in one pipeline  
- **Best-of-3 Frame Capture** — ESP32-CAM selects sharpest JPEG by file size, with 150ms flash pre-warm  
- **Vision Pipeline** — 3-thread YOLO + OCR + MQTT pipeline; ~12 MB model, ~180 ms/frame on CPU  
- **Gate Cooldown** — `_safe_gate_open()` enforces 15-second refractory period to prevent double-triggers  
- **Delayed Exit Capture** — 2.5s post-gate-open trigger so vehicle clears the barrier before photo  
- **SSE Serial Monitor** — Real-time ESP32 serial log streaming to the Owner Dashboard via Server-Sent Events  
- **Watchdog & Heap Guard** — ESP32-CAM skips capture if free heap < 50 KB; WROOM boots with WDT enabled  
- **NTP-Synced Timestamps** — All records in IST (UTC+5:30)  
- **SQLite WAL Mode** — Concurrent read/write without locking; single-file database (`iprs_v9.db`)  

</details>

<details>
<summary><b>📊 Owner Dashboard Features</b></summary>

- Live slot grid with real-time MQTT updates  
- Vehicle entry/exit records with captured plate images  
- Blacklist management  
- Manual gate open/close controls  
- Board health status (WiFi, heap, uptime, RSSI)  
- Serial monitor with SSE stream  
- Chain integrity verifier  

</details>

<details>
<summary><b>👤 Customer Dashboard Features</b></summary>

- SmartCoin wallet with tier system (Bronze / Silver / Gold / Platinum)  
- Live slot availability grid with fare preview  
- Advance slot reservation with countdown timer  
- QR token generation on booking confirmation  
- Booking extension and early exit options  
- Transaction history with blockchain hash  
- Print-ready digital receipts  

</details>

---

## 🏛️ System Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                         IPRS Architecture                           │
├──────────────────┬──────────────────────┬───────────────────────────┤
│  HARDWARE LAYER  │    BACKEND LAYER     │   PRESENTATION LAYER      │
│                  │                      │                           │
│  ESP32-WROOM-32  │  iprs_server_v9.py   │  owner_dashboard.html     │
│  ├─ IR Sensors   │  ├─ Flask REST API   │  ├─ Live slot grid        │
│  ├─ Servo Gate   │  ├─ WebSocket Hub    │  ├─ Gate controls         │
│  ├─ Slot LEDs    │  ├─ SQLite DB        │  └─ Serial monitor        │
│  └─ MQTT Client  │  └─ MQTT Bridge      │                           │
│                  │                      │  customer_dashboard.html  │
│  ESP32-CAM       │  iprs_vision_        │  ├─ SmartCoin wallet      │
│  ├─ OV2640 Cam   │  pipeline.py         │  ├─ Slot booking          │
│  ├─ MJPEG Stream │  ├─ YOLOv8n ONNX     │  ├─ QR tokens             │
│  ├─ JPEG Capture │  ├─ Tesseract OCR    │  └─ Receipts              │
│  └─ Flash LED    │  └─ MQTT Publisher   │                           │
└──────────────────┴──────────────────────┴───────────────────────────┘
         │                    │                        │
         └────────────────────┼────────────────────────┘
                              │
              ┌───────────────┼────────────────────┐
              │        Communication Bus           │
              ├────────────────────────────────────┤
              │  WebSocket :5000/ws  (HW → Server) │
              │  WebSocket :5000/ws/dashboard       │
              │  MQTT TCP  :1883     (HW + Vision)  │
              │  MQTT WS   :1884     (Browsers)     │
              │  HTTP REST :5000     (Commands)     │
              │  HTTP      :8080     (ESP32-CAM)    │
              │  SSE       :5000/serial/stream      │
              └────────────────────────────────────┘
```

### Data Flow — Entry Sequence

```
Vehicle Detected (IR GPIO14)
        │
        ▼
WROOM: IR_TRIGGER → WebSocket → Flask Server
        │
        ▼
Server: TRIGGER → ESP32-CAM via WebSocket
        │
        ▼
CAM: best-of-3 JPEG capture (flash pre-warm 150ms)
        │
        ▼
POST /upload/ENTRY  (X-Api-Key header)
        │
        ▼
OCR Module: 5 variants × 4 PSM → best plate
        │
        ▼
Plate validated (Indian regex SS-DD-LLL-4444 / BH series)
        │
        ▼
Server: assign slot → insert DB record → _safe_gate_open()
        │
        ├── MQTT: iprs/gate/ENTRY/cmd → WROOM (servo OPEN)
        ├── WS: NEW_ENTRY + SLOT_UPDATE → Owner Dashboard
        └── Vision pipeline: MQTT confirm → update DB row
```

---

## 🔧 Hardware

### Bill of Materials

| Component | Model / Spec | Qty | Role |
|---|---|---|---|
| MCU — Gate | ESP32-WROOM-32 (240 MHz, 520KB SRAM) | 1 | Gate control, IR, LEDs, MQTT |
| MCU — Camera | ESP32-CAM AI Thinker (OV2640, 4MB PSRAM) | 1 | MJPEG stream, capture, OCR upload |
| Servo Motor | SG90 / MG996R (5V, PWM 50Hz) | 1 | Physical gate barrier arm |
| IR Sensors — Gate | FC-51 (digital output) | 2 | Vehicle detection at entry/exit |
| IR Sensors — Slots | FC-51 (INPUT_PULLUP) | 4 | Per-slot occupancy sensing |
| LEDs — Green | 5mm, 3.3V | 4+ | Slot EMPTY indicators |
| LEDs — Red | 5mm, 3.3V | 4+ | Slot OCCUPIED/RESERVED indicators |
| OLED Display | SSD1306 128×64 I2C | 1 | ESP32-CAM status display |
| Server Host | PC or Raspberry Pi 4 | 1 | Flask server + vision pipeline |

### ESP32-WROOM-32 Pinout

```
GPIO 14 → IR Entry Gate    │  GPIO 18 → Servo PWM
GPIO 27 → IR Exit Gate     │  GPIO 15 → Green LED Slot 1
GPIO 26 → IR Slot 1        │  GPIO 2  → Green LED Slot 2
GPIO 25 → IR Slot 2        │  GPIO 4  → Green LED Slot 3 (LEDC)
GPIO 33 → IR Slot 3        │  GPIO 16 → Green LED Slot 4 (LEDC)
GPIO 32 → IR Slot 4        │  GPIO 21 → Red LED Slot 1 (LEDC)
                           │  GPIO 22 → Red LED Slot 2 (LEDC)
                           │  GPIO 23 → Red LED Slot 3 (LEDC)
                           │  GPIO 13 → Red LED Slot 4 (LEDC)
                           │  GPIO 19 → Gate Event LED + Buzzer
```

---

## 📁 Project Structure

```
IPRS/
├── backend/
│   ├── iprs_server_v9.py          # Flask server — WebSocket hub, REST API, SQLite (~63 KB)
│   ├── iprs_vision_pipeline.py    # 3-thread YOLO+OCR+MQTT vision pipeline (~36 KB)
│   ├── ocr_module.py              # Production OCR engine, 5 preprocessing variants (~14 KB)
│   └── model_setup.py             # One-click YOLOv8n ONNX download + benchmark (~12 KB)
│
├── firmware/
│   ├── iprswroom_v2/
│   │   └── iprswroom_v2.ino       # ESP32-WROOM FreeRTOS firmware v17.2 (~35 KB)
│   └── iprs_cam_main/
│       └── iprs_cam_main.ino      # ESP32-CAM firmware v9.1 (~46 KB)
│
├── frontend/
│   ├── owner_dashboard.html       # Owner SPA — real-time monitoring & control (~65 KB)
│   └── customer_dashboard.html    # Customer SmartCoin portal (~95 KB)
│
├── database/
│   └── iprs_v9.db                 # SQLite database (WAL mode, auto-created on first run)
│
├── docs/
│   └── IPRS_Project_Document_v10.docx
│
├── nano_steps.md                  # Step-by-step cold start deployment guide
└── README.md
```

---

## 🚀 Quick Start

### Prerequisites

**Server (Ubuntu/Debian/Raspberry Pi OS)**
```bash
sudo apt update
sudo apt install -y python3.11 python3-pip tesseract-ocr mosquitto mosquitto-clients
```

**Python dependencies**
```bash
pip install flask>=3.0.0 flask-sock>=0.7.0 paho-mqtt>=1.6.0 \
            opencv-python>=4.8.0 pytesseract>=0.3.10 numpy>=1.24.0 \
            ultralytics>=8.0.0 onnx>=1.14.0 onnxruntime>=1.16.0 \
            requests>=2.31.0 psutil>=5.9.0
```

**YOLOv8n ONNX model setup** (run once)
```bash
cd backend/
python model_setup.py
```

### Mosquitto MQTT Broker Configuration

Create/edit `/etc/mosquitto/mosquitto.conf`:
```conf
listener 1883
allow_anonymous true

listener 1884
protocol websockets
allow_anonymous true
```
```bash
sudo systemctl restart mosquitto
```

### Start the Backend

```bash
cd backend/
python iprs_server_v9.py
```
Server starts on `http://0.0.0.0:5000`. Open `http://<YOUR_SERVER_IP>:5000` in a browser to load the Owner Dashboard.

### Start the Vision Pipeline

In a second terminal:
```bash
cd backend/
python iprs_vision_pipeline.py
```

### Flash the ESP32 Firmware

**Arduino IDE setup — Board Manager URL:**
```
https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json
```

**Required Arduino Libraries:**

| Library | Used By |
|---|---|
| ESP32Servo | WROOM — servo gate motor |
| WebSocketsClient | Both nodes — Flask WebSocket |
| PubSubClient | WROOM — MQTT pub/sub |
| ESPAsyncWebServer | CAM — non-blocking HTTP |
| AsyncTCP | CAM — ESPAsyncWebServer dependency |
| Adafruit GFX Library | CAM — OLED graphics |
| Adafruit SSD1306 | CAM — SSD1306 I2C driver |

**Before flashing `iprswroom_v2.ino`, update these defines:**
```cpp
#define WIFI_SSID       "YourNetworkSSID"
#define WIFI_PASSWORD   "YourNetworkPassword"
#define SERVER_IP       "10.121.81.78"   // Your server IP
#define MQTT_SERVER     "10.121.81.78"
```

**For ESP32-CAM flashing** — ground IO0 pin to GND during upload, then release for normal operation.

### Verify the Installation

```bash
# Server health
curl http://localhost:5000/status
# Expected: {"ok": true, "server": "IPRS v9.0"}

# Slot states
curl http://localhost:5000/slots/ENTRY
# Expected: 4 slots all EMPTY on cold start

# MQTT connectivity
mosquitto_pub -h localhost -p 1883 -t iprs/test -m hello

# Gate command test
curl -X POST http://localhost:5000/gate/open \
     -H "X-Api-Key: IPRS-2026-SECURE" \
     -H "Content-Type: application/json"
# Expected: {"status": "ok", "action": "GATE_OPEN"}
```

---

## 🌐 Network Configuration

| Service | IP | Port | Protocol |
|---|---|---|---|
| Flask Server | `<server-ip>` | 5000 | HTTP + WebSocket |
| MQTT Broker (TCP) | `<server-ip>` | 1883 | MQTT — firmware & vision |
| MQTT Broker (WS) | `<server-ip>` | 1884 | WebSocket — browsers |
| ESP32-CAM | DHCP-assigned | 8080 | HTTP (ESPAsyncWebServer) |
| ESP32-WROOM | DHCP-assigned | 8081 | HTTP (Arduino WebServer) |

---

## 📡 MQTT Topic Map

| Topic | Publisher | Subscriber | Payload |
|---|---|---|---|
| `iprs/gate/ENTRY/cmd` | Flask Server | WROOM | `OPEN` or `CLOSE` |
| `iprs/gate/EXIT/cmd` | Flask Server | WROOM | `OPEN` or `CLOSE` |
| `iprs/wroom/ENTRY/status` | WROOM | Flask Server | `{gate, heap, uptime, rssi}` |
| `iprs/ir/ENTRY` | WROOM | Flask Server | `{trigger: true}` |
| `iprs/slot/ENTRY/state` | WROOM | Flask Server | `["EMPTY","OCC","EMPTY","EMPTY"]` |
| `iprs/slot/ENTRY/reserve` | Flask Server | WROOM | `{slot, action: RESERVE\|CANCEL, plate}` |
| `iprs/vision/plate` | Vision Pipeline | Flask Server | `{plate, slot, conf, action, ts}` |
| `iprs/vision/status` | Vision Pipeline | Flask Server | `{online, queue_depth}` |

---

## 📖 API Reference

### Authentication
All write endpoints require the `X-Api-Key` header:
```
X-Api-Key: IPRS-2026-SECURE
```

### Key Endpoints

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/status` | Server health check |
| `GET` | `/slots/ENTRY` | Current slot states for entry lane |
| `POST` | `/upload/ENTRY` | Upload JPEG for OCR (multipart or raw body) |
| `POST` | `/upload/EXIT` | Upload JPEG for exit plate matching |
| `POST` | `/gate/open` | Manually open entry gate |
| `POST` | `/gate/close` | Manually close entry gate |
| `GET` | `/records` | All vehicle entry/exit records |
| `POST` | `/blacklist` | Add plate to blacklist |
| `DELETE` | `/blacklist/<plate>` | Remove plate from blacklist |
| `POST` | `/booking` | Create slot reservation |
| `POST` | `/booking/cancel` | Cancel reservation |
| `GET` | `/stats` | Occupancy and revenue statistics |
| `GET` | `/serial/stream` | SSE stream of ESP32 serial logs |

---

## 🔑 Default Credentials

> ⚠️ **Change all credentials before any production or public deployment.**

| Service | Username | Password / Key |
|---|---|---|
| Owner Dashboard | `admin` or `owner` | `12345678` |
| OTA Firmware Update | — | `iprs2026ota` |
| API Key (`X-Api-Key`) | — | `IPRS-2026-SECURE` |
| MQTT Broker | anonymous | none (development) |

---

## 🐛 Troubleshooting

| Symptom | Root Cause | Fix |
|---|---|---|
| WROOM won't connect to WiFi | Wrong `WIFI_SSID` / `WIFI_PASSWORD` in `#define` | Re-flash with corrected credentials |
| CAM upload fails (flashing) | IO0 not grounded during upload | Ground IO0 to GND; retry at 115200 baud |
| WS pill stays RED on dashboard | Flask server not running | Run `python iprs_server_v9.py`, verify port 5000 |
| MQTT not connecting | Mosquitto broker not running | `sudo systemctl start mosquitto` |
| OCR returns empty string | Tesseract not installed | `sudo apt install tesseract-ocr` |
| Vision pipeline shows no frames | Wrong CAM IP in CONFIG | Verify `CAM_STREAM_URL` matches CAM's DHCP IP |
| Serial monitor garbage | Wrong baud rate | Set Serial Monitor to **115200** |
| OTA update fails | Wrong OTA password | Password is `iprs2026ota` |
| Low heap warning on CAM | PSRAM not detected | Check `psramFound() == true` in serial log |
| Gate servo not moving | Wrong MQTT topic | Publish to `iprs/gate/ENTRY/cmd` with payload `OPEN` |
| Double gate open | `_safe_gate_open()` bypassed | All gate opens must route through `_safe_gate_open()` |
| Dashboard shows UNDETECTED | Plate too dark / small in frame | Improve lighting; check CLAHE clip setting in `ocr_module.py` |

---

## 🔮 Roadmap

- [ ] **Multi-lane support** — N-lane expansion via `LANE` `#define` + server routing
- [ ] **Cloud deployment** — Docker container for Flask server on AWS / GCP / Azure
- [ ] **Mobile app** — Flutter app for wallet, booking, and live slot view
- [ ] **CRNN ANPR** — Replace Tesseract with fine-tuned CRNN for degraded plate accuracy
- [ ] **PostgreSQL migration** — Scale beyond SQLite for multi-server deployments
- [ ] **Payment gateway** — Razorpay / UPI integration for real monetary transactions
- [ ] **RTSP CCTV** — High-quality camera support in vision pipeline
- [ ] **Analytics dashboard** — Occupancy trends, revenue forecasting, peak hour analysis
- [ ] **Solar + UPS** — Off-grid gate controller with LiPo battery backup
- [ ] **RTO/Vahan API** — Real-time plate lookup against national vehicle registry

---

## 📋 Version History

| Version | Component | Key Changes |
|---|---|---|
| v17.2 | WROOM Firmware | Boot false-trigger fix; 12s IR rate limit; `String→char[]` refactor; RESERVED LED blink |
| v9.1 | CAM Firmware | ESPAsyncWebServer; all `delay()` removed; no memory leak; WDT=8s |
| v9.0 | Flask Server | Gate cooldown; duplicate record fix; booking/exit/cancel APIs; SSE serial stream |
| v3.1 | OCR Module | PSM 6 added; CLAHE clip=4.0; bilateral filter variant; BH series regex |
| v10.0 | Dashboards | SmartCoin wallet; blockchain receipt chain; MQTT WS live updates; QR token |

---

## 📚 Glossary

| Term | Meaning |
|---|---|
| **IPRS** | Intelligent Parking & Recognition System |
| **ANPR** | Automatic Number Plate Recognition |
| **WROOM** | ESP32-WROOM-32 — gate controller MCU |
| **CAM** | ESP32-CAM AI Thinker with OV2640 sensor |
| **MQTT** | Message Queuing Telemetry Transport — lightweight IoT protocol |
| **SSE** | Server-Sent Events — unidirectional server→browser stream |
| **YOLO** | You Only Look Once — real-time object detection neural network |
| **ONNX** | Open Neural Network Exchange — portable ML model format |
| **LEDC** | LED Control — ESP32 hardware PWM peripheral |
| **OTA** | Over-The-Air — wireless firmware update mechanism |
| **WAL** | Write-Ahead Logging — SQLite concurrent read/write mode |
| **CLAHE** | Contrast Limited Adaptive Histogram Equalization — image enhancement |
| **NMS** | Non-Maximum Suppression — removes duplicate YOLO detection boxes |
| **IST** | Indian Standard Time — UTC+5:30, used for all timestamps |

---

## 🤝 Contributing

Contributions are welcome! Please follow these steps:

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/your-feature-name`
3. Commit your changes: `git commit -m 'feat: add your feature'`
4. Push to the branch: `git push origin feature/your-feature-name`
5. Open a Pull Request

Please ensure your code follows the existing style and all component-level tests pass before submitting.

---

## 📄 License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.

---

<div align="center">

**Built with ❤️ on ESP32 · Python · Flask · YOLOv8**

*IPRS — Automating parking, one plate at a time.*

</div>

"""
Project  : IPRS — Intelligent Parking & Recognition System
File     : iprs_server_v9.py
Purpose  : Production Flask backend — WebSocket hub, REST API, SQLite DB,
           MQTT bridge, dashboard hosting, OCR upload handler
Version  : v9.0.0
Hardware : Server PC / Raspberry Pi 4  |  Python 3.11+
Modified : 2026-03-21

Endpoints served:
  WS   ws://0.0.0.0:5000/ws              <- ESP32 boards (WROOM + CAM)
  WS   ws://0.0.0.0:5000/ws/dashboard    <- Browser dashboards
  GET  /                                  -> owner_dashboard.html
  GET  /customer                          -> customer_dashboard.html
  GET  /status                            -> server health JSON
  GET  /slots/<lane>                      -> slot state array
  GET  /records                           -> vehicle records (last 200)
  GET  /stats                             -> aggregated KPIs
  POST /gate/<action>                     -> forward gate cmd to WROOM
  POST /upload/<lane>                     -> JPEG upload + OCR + DB write
  GET  /heartbeat/wroom/<lane>            -> WROOM heartbeat ack
  GET  /heartbeat/cam/<lane>              -> CAM heartbeat ack
  POST /serial/log                        -> buffered serial lines from boards
  GET  /serial/stream                     -> SSE stream for serial monitor
  POST /trigger/cam/<lane>               -> HTTP fallback IR trigger
  POST /booking                           -> customer booking registration
  GET  /blacklist                         -> list blacklisted plates
  POST /blacklist                         -> add/remove plate
  GET  /uploads/<filename>               -> serve uploaded plate images

Run:
  python iprs_server_v9.py
  python iprs_server_v9.py --port 5001   (if 5000 is busy)
  python iprs_server_v9.py --debug       (verbose logging)

Dependencies:
  pip install flask flask-sock paho-mqtt requests --break-system-packages
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import sqlite3
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

from flask import Flask, Response, abort, jsonify, request, send_file
from flask_sock import Sock

# =====================================================================
#  CONFIG
# =====================================================================
DB_PATH          = Path("iprs_v9.db")
UPLOAD_DIR       = Path("uploads")
STATIC_DIR       = Path(__file__).parent   # dashboards live next to this script
import os as _os
API_KEY          = _os.environ.get("IPRS_API_KEY", "IPRS-2026-SECURE")  # override via env in production
MQTT_BROKER      = "10.121.81.78"
MQTT_PORT        = 1883
TOTAL_SLOTS      = 4
IST_OFFSET       = 19800                   # UTC + 5:30 seconds
MAX_SERIAL_LINES = 500                     # rolling serial buffer size
BOARD_TIMEOUT_S  = 90                      # mark board offline after this

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("IPRS-Server")

# =====================================================================
#  FLASK APP
# =====================================================================
app  = Flask(__name__, static_folder=None)
sock = Sock(app)
UPLOAD_DIR.mkdir(exist_ok=True)

# CORS — allow browsers served from any origin (file://, different host, etc.)
@app.after_request
def add_cors(response: Response) -> Response:
    response.headers["Access-Control-Allow-Origin"]  = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Api-Key"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response

@app.route("/", defaults={"path": ""}, methods=["OPTIONS"])
@app.route("/<path:path>", methods=["OPTIONS"])
def options_handler(path):
    return Response(status=204)

# =====================================================================
#  DATABASE
# =====================================================================
def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db() -> None:
    with get_db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS vehicle_records (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            plate          TEXT    NOT NULL DEFAULT 'UNDETECTED',
            lane           TEXT    NOT NULL DEFAULT 'ENTRY',
            slot_number    INTEGER,
            entry_time     TEXT,
            exit_time      TEXT,
            duration_min   INTEGER,
            payment_amt    REAL    DEFAULT 0,
            ocr_confidence REAL    DEFAULT 0,
            yolo_conf      REAL    DEFAULT 0,
            status         TEXT    DEFAULT 'parked',
            flagged        INTEGER DEFAULT 0,
            image_url      TEXT,
            token          TEXT,
            created_at     TEXT    DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS slot_state (
            lane    TEXT    NOT NULL,
            slot_n  INTEGER NOT NULL,
            state   TEXT    NOT NULL DEFAULT 'EMPTY',
            plate   TEXT,
            updated_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (lane, slot_n)
        );
        CREATE TABLE IF NOT EXISTS board_status (
            board_id   TEXT PRIMARY KEY,
            lane       TEXT,
            ip         TEXT,
            firmware   TEXT,
            wifi_rssi  INTEGER,
            free_heap  INTEGER,
            uptime_sec INTEGER,
            gate_open  INTEGER DEFAULT 0,
            last_seen  TEXT,
            online     INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS blacklist (
            plate     TEXT PRIMARY KEY,
            added_at  TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS bookings (
            token      TEXT PRIMARY KEY,
            plate      TEXT,
            slot       TEXT,
            from_iso   TEXT,
            until_iso  TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        INSERT OR IGNORE INTO slot_state (lane, slot_n, state) VALUES
            ('ENTRY',1,'EMPTY'),('ENTRY',2,'EMPTY'),
            ('ENTRY',3,'EMPTY'),('ENTRY',4,'EMPTY'),
            ('EXIT',1,'EMPTY'),('EXIT',2,'EMPTY'),
            ('EXIT',3,'EMPTY'),('EXIT',4,'EMPTY');
        CREATE INDEX IF NOT EXISTS idx_vr_entry  ON vehicle_records(entry_time);
        CREATE INDEX IF NOT EXISTS idx_vr_status ON vehicle_records(status);
        CREATE INDEX IF NOT EXISTS idx_vr_plate  ON vehicle_records(plate);
        """)
    log.info("[DB] Initialised -> %s", DB_PATH.resolve())


# IST timezone singleton — avoids repeated timedelta construction
_IST = timezone(timedelta(seconds=IST_OFFSET))


def ist_now() -> str:
    """Return current IST timestamp as 'YYYY-MM-DD HH:MM:SS'. Never raises."""
    return datetime.now(_IST).strftime("%Y-%m-%d %H:%M:%S")

# =====================================================================
#  WEBSOCKET HUB
# =====================================================================
_board_clients: set = set()
_dash_clients:  set = set()
_clients_lock        = threading.Lock()

# ── Gate open cooldown tracker ─────────────────────────────────────────
# ROOT CAUSE FIX: Two independent paths both call _broadcast_boards GATE_OPEN:
#   Path A: POST /upload/<lane>  → server-side OCR → GATE_OPEN  (immediate, ~0.5s)
#   Path B: MQTT iprs/vision/plate → _handle_vision_plate() → GATE_OPEN (~2–5s later)
#
# When Path A fires and then Path B fires 3s later, the WROOM receives two
# GATE_OPEN commands. If the second arrives AFTER the WROOM's GATE_CMD_DEBOUNCE
# window (was 2000ms, now 5000ms), the gate opens a second time for no reason.
#
# Fix: a simple per-lane timestamp dict. _safe_gate_open() checks the time since
# the last GATE_OPEN for that lane. If < GATE_OPEN_COOLDOWN_S, it skips the command.
# All GATE_OPEN broadcasts must go through _safe_gate_open() — never call
# _broadcast_boards({"type":"GATE_OPEN",...}) directly from upload or vision paths.
#
# 15s cooldown: > SERVO_HOLD_MS(3s) + max pipeline latency (~6s) + safety margin.
GATE_OPEN_COOLDOWN_S: float = 15.0
_last_gate_open: dict[str, float] = {}   # lane → monotonic time of last GATE_OPEN
_gate_open_lock = threading.Lock()


def _safe_gate_open(lane: str, reason: str = "") -> bool:
    """
    Broadcast GATE_OPEN only if cooldown has expired for this lane.
    Returns True if command was sent, False if suppressed.
    """
    lane = lane.upper()
    now  = time.monotonic()
    with _gate_open_lock:
        last = _last_gate_open.get(lane, 0.0)
        if (now - last) < GATE_OPEN_COOLDOWN_S:
            log.warning(
                "[GATE] Suppressed duplicate GATE_OPEN lane=%s reason=%s "
                "(%.1fs since last open — cooldown=%.0fs)",
                lane, reason, now - last, GATE_OPEN_COOLDOWN_S,
            )
            return False
        _last_gate_open[lane] = now
    _broadcast_boards({"type": "GATE_OPEN", "lane": lane})
    log.info("[GATE] GATE_OPEN sent lane=%s reason=%s", lane, reason)
    return True


def _broadcast_boards(payload: dict) -> None:
    msg = json.dumps(payload)
    with _clients_lock:
        dead = set()
        for ws in _board_clients:
            try:
                ws.send(msg)
            except Exception:
                dead.add(ws)
        _board_clients.difference_update(dead)


def _broadcast_dash(payload: dict) -> None:
    msg = json.dumps(payload)
    with _clients_lock:
        dead = set()
        for ws in _dash_clients:
            try:
                ws.send(msg)
            except Exception:
                dead.add(ws)
        _dash_clients.difference_update(dead)


@sock.route("/ws")
def ws_board(ws):
    """WebSocket for ESP32 boards."""
    with _clients_lock:
        _board_clients.add(ws)
        count = len(_board_clients)
    log.info("[WS] Board connected  total=%d", count)
    try:
        while True:
            raw = ws.receive(timeout=60)
            if raw is None:
                break
            _handle_board_message(ws, raw)
    except Exception:
        pass
    finally:
        with _clients_lock:
            _board_clients.discard(ws)
            count = len(_board_clients)
        log.info("[WS] Board disconnected  total=%d", count)


@sock.route("/ws/dashboard")
def ws_dashboard(ws):
    """WebSocket for browser dashboards."""
    with _clients_lock:
        _dash_clients.add(ws)
        count = len(_dash_clients)
    log.info("[WS] Dashboard connected  total=%d", count)
    try:
        _send_snapshot(ws)
    except Exception:
        pass
    try:
        while True:
            raw = ws.receive(timeout=90)
            if raw is None:
                break
    except Exception:
        pass
    finally:
        with _clients_lock:
            _dash_clients.discard(ws)
            count = len(_dash_clients)
        log.info("[WS] Dashboard disconnected  total=%d", count)


def _send_snapshot(ws) -> None:
    with get_db() as conn:
        slots = {}
        for row in conn.execute(
            "SELECT lane, slot_n, state FROM slot_state ORDER BY lane, slot_n"
        ):
            slots.setdefault(row["lane"], []).append(row["state"])
        boards = {r["board_id"]: dict(r) for r in conn.execute(
            "SELECT * FROM board_status"
        ).fetchall()}
        records = conn.execute(
            "SELECT * FROM vehicle_records ORDER BY id DESC LIMIT 100"
        ).fetchall()
    ws.send(json.dumps({
        "type":    "SNAPSHOT",
        "slots":   slots,
        "boards":  boards,
        "records": [_rec(r) for r in records],
    }))


def _handle_board_message(ws, raw: str) -> None:
    try:
        msg = json.loads(raw)
    except Exception:
        return
    t       = msg.get("type", "")
    bid     = msg.get("boardId", "UNKNOWN")
    lane    = msg.get("lane", "ENTRY")

    if t == "HELLO":
        _upsert_board(bid, lane, msg, online=True)
        _broadcast_dash({"type": "BOARD_STATUS", "boardId": bid, "online": True,
                         "ip": msg.get("ip",""), "wifiRssi": msg.get("rssi"),
                         "firmware": msg.get("firmware","")})

    elif t == "SLOT_UPDATE":
        statuses = msg.get("slotStatus", [])
        _write_slots(lane, statuses)
        _broadcast_dash({"type": "SLOT_UPDATE", "lane": lane,
                         "slotStatus": statuses,
                         "slotsLeft": msg.get("slotsLeft", 0),
                         "parkingFull": msg.get("parkingFull", False)})

    elif t == "IR_TRIGGER":
        log.info("[IR] %s  lane=%s", bid, lane)
        _broadcast_dash({"type": "IR_TRIGGER", "boardId": bid, "lane": lane, "ts": ist_now()})
        if lane.upper() == "EXIT":
            # EXIT sequence: gate opens FIRST → vehicle passes → camera captures
            # The CAM is mounted facing the gate exit side, so capture AFTER gate opens.
            sent = _safe_gate_open("EXIT", reason="exit-ir-trigger")
            if sent:
                # Schedule a delayed camera trigger so the vehicle is fully through
                # before the camera fires (~2.5s after gate opens = vehicle clears gate)
                def _delayed_exit_cam():
                    import time as _t
                    _t.sleep(2.5)
                    _broadcast_boards({"type": "TRIGGER", "lane": "EXIT"})
                    log.info("[IR] EXIT cam trigger fired (delayed 2.5s after gate open)")
                import threading as _th
                _th.Thread(target=_delayed_exit_cam, daemon=True).start()
            log.info("[IR] EXIT gate open sent=%s", sent)
        else:
            # ENTRY sequence: camera triggers first → OCR → gate opens via upload handler
            _broadcast_boards({"type": "TRIGGER", "lane": lane})

    elif t == "GATE_EVENT":
        _broadcast_dash({"type": "GATE_EVENT", "boardId": bid,
                         "lane": lane, "event": msg.get("event",""), "ts": ist_now()})

    elif t == "LOG":
        line = msg.get("line", "")
        _push_serial(bid, line)
        _broadcast_dash({"type": "SERIAL", "boardId": bid, "line": line, "ts": ist_now()})


def _upsert_board(board_id: str, lane: str, data: dict, online: bool = True) -> None:
    """
    Upsert board status into DB. Safe to call from both WS and HTTP request contexts.
    data must contain 'ip' key when called from WS handler (no request context available).
    """
    with get_db() as conn:
        conn.execute("""
            INSERT INTO board_status
                (board_id, lane, ip, firmware, wifi_rssi, free_heap,
                 uptime_sec, gate_open, last_seen, online)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(board_id) DO UPDATE SET
                lane=excluded.lane, ip=excluded.ip, firmware=excluded.firmware,
                wifi_rssi=excluded.wifi_rssi, free_heap=excluded.free_heap,
                uptime_sec=excluded.uptime_sec, gate_open=excluded.gate_open,
                last_seen=excluded.last_seen, online=excluded.online
        """, (
            board_id, lane,
            # FIX: never fall back to request.headers — crashes outside request context.
            # Callers must pass ip explicitly in data dict.
            data.get("ip", ""),
            data.get("firmware") or data.get("fw", ""),
            data.get("rssi") or data.get("wifiRssi") or data.get("wifi_rssi"),
            data.get("freeHeap") or data.get("heap") or data.get("free_heap"),
            data.get("uptimeSec") or data.get("uptime") or data.get("uptime_sec"),
            1 if (data.get("gateOpen") or data.get("gate") == "open") else 0,
            ist_now(), 1 if online else 0,
        ))


def _write_slots(lane: str, statuses: list) -> None:
    with get_db() as conn:
        for i, state in enumerate(statuses, 1):
            conn.execute(
                "UPDATE slot_state SET state=?, updated_at=? WHERE lane=? AND slot_n=?",
                (state, ist_now(), lane.upper(), i)
            )

# =====================================================================
#  SERIAL BUFFER + SSE
# =====================================================================
_serial_buf: deque[dict] = deque(maxlen=MAX_SERIAL_LINES)   # O(1) append & eviction
_serial_lock             = threading.Lock()
_sse_queues: list[queue.Queue] = []


def _push_serial(board_id: str, line: str) -> None:
    entry = {"boardId": board_id, "line": line, "ts": ist_now()}
    with _serial_lock:
        _serial_buf.append(entry)          # deque evicts oldest automatically
        for q in _sse_queues:
            try:
                q.put_nowait(entry)
            except Exception:
                pass

# =====================================================================
#  AUTH
# =====================================================================
def require_api_key(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        key = request.headers.get("X-Api-Key") or request.args.get("api_key","")
        if key != API_KEY:
            return jsonify({"error": "unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated

# =====================================================================
#  DASHBOARD HOSTING
# =====================================================================
def _serve_html(filename: str) -> Response:
    for base in [STATIC_DIR, Path(".")]:
        p = base / filename
        if p.exists():
            return send_file(str(p))
    return Response(
        f"<h1>404 — {filename} not found</h1>"
        f"<p>Place <b>{filename}</b> in the same directory as iprs_server_v9.py</p>",
        mimetype="text/html", status=404
    )


@app.route("/favicon.ico")
def favicon():
    return Response(status=204)


@app.route("/static/<path:filename>")
def serve_static(filename: str):
    """
    Serve offline-cached JS libraries (chart.min.js, qrcode.min.js, html2canvas.min.js).
    Run model_setup.py first to populate the static/ directory.
    Dashboards call /static/<lib> with an onerror fallback to CDN, so this
    endpoint is only hit when the browser is served from the Flask server.
    """
    import os as _osp
    static_dir = Path(__file__).parent / "static"
    safe = (static_dir / filename).resolve()
    # Path traversal guard
    if _osp.path.commonpath([safe, static_dir.resolve()]) != str(static_dir.resolve()):
        abort(404)
    if not safe.exists():
        abort(404)
    # Determine mime type
    ext = safe.suffix.lower()
    mime = "application/javascript" if ext == ".js" else "text/plain"
    return send_file(str(safe), mimetype=mime, max_age=86400)   # cache 24h


@app.route("/")
def serve_owner():
    return _serve_html("owner_dashboard.html")


@app.route("/customer")
def serve_customer():
    return _serve_html("customer_dashboard.html")

# =====================================================================
#  STATUS
# =====================================================================
@app.route("/status")
def status():
    with get_db() as conn:
        total  = conn.execute("SELECT COUNT(*) FROM vehicle_records").fetchone()[0]
        parked = conn.execute(
            "SELECT COUNT(*) FROM vehicle_records WHERE status='parked'"
        ).fetchone()[0]
    return jsonify({
        "server":        "IPRS v9.0",
        "ok":            True,
        "uptime_sec":    int(time.time() - _start_time),
        "board_clients": len(_board_clients),
        "dash_clients":  len(_dash_clients),
        "records_total": total,
        "parked_now":    parked,
        "ts":            ist_now(),
    })

# =====================================================================
#  SLOTS
# =====================================================================
@app.route("/slots/<lane>")
def get_slots(lane: str):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT slot_n, state, plate FROM slot_state WHERE lane=? ORDER BY slot_n",
            (lane.upper(),)
        ).fetchall()
    left = sum(1 for r in rows if r["state"] == "EMPTY")
    return jsonify({
        "lane":       lane.upper(),
        "slots":      [{"slot": r["slot_n"], "state": r["state"], "plate": r["plate"]} for r in rows],
        "slotsLeft":  left,
        "full":       left == 0,
    })

# =====================================================================
#  RECORDS
# =====================================================================
def _rec(row) -> dict:
    d = dict(row)
    # Compute overstay: vehicle parked > 4 hours and still 'parked'
    overstay = False
    if d.get("status") == "parked" and d.get("entry_time"):
        try:
            entry_dt = datetime.strptime(d["entry_time"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=_IST)
            now_ist  = datetime.now(_IST)
            overstay = (now_ist - entry_dt).total_seconds() > 4 * 3600
        except Exception:
            pass
    return {
        "id":            d.get("id"),
        "plateText":     d.get("plate", ""),
        "lane":          d.get("lane", "ENTRY"),
        "slotNumber":    d.get("slot_number"),
        "entryTime":     d.get("entry_time", ""),
        "exitTime":      d.get("exit_time") or "---",
        "duration":      f"{d.get('duration_min',0)}m" if d.get("duration_min") else "–",
        "paymentAmount": d.get("payment_amt", 0),
        "ocrConfidence": d.get("ocr_confidence", 0),
        "status":        d.get("status", "parked"),   # parked | reserved | exited | cancelled
        "flagged":       bool(d.get("flagged", 0)),
        "overstay":      overstay,
        "plateImageUrl": d.get("image_url", ""),
        "token":         d.get("token", ""),
    }


@app.route("/records")
def get_records():
    try:
        limit = min(int(request.args.get("limit", 200)), 1000)
    except (ValueError, TypeError):
        return jsonify({"error": "limit must be an integer"}), 400
    with get_db() as conn:
        bl   = {r[0] for r in conn.execute("SELECT plate FROM blacklist").fetchall()}
        rows = conn.execute(
            "SELECT * FROM vehicle_records ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    result = []
    for r in rows:
        d = _rec(r)
        d["flagged"] = d["flagged"] or (d["plateText"].upper() in bl)
        result.append(d)
    return jsonify({"records": result, "count": len(result)})

# =====================================================================
#  STATS
# =====================================================================
@app.route("/stats")
def get_stats():
    today = datetime.now(_IST).strftime("%Y-%m-%d")
    with get_db() as conn:
        rev = conn.execute(
            "SELECT COALESCE(SUM(payment_amt),0) FROM vehicle_records "
            "WHERE status='exited' AND entry_time LIKE ?", (f"{today}%",)
        ).fetchone()[0]
        entries = conn.execute(
            "SELECT COUNT(*) FROM vehicle_records WHERE entry_time LIKE ?",
            (f"{today}%",)
        ).fetchone()[0]
        parked = conn.execute(
            "SELECT COUNT(*) FROM vehicle_records WHERE status='parked'"
        ).fetchone()[0]
        flagged = conn.execute(
            "SELECT COUNT(*) FROM vehicle_records "
            "WHERE flagged=1 OR plate IN (SELECT plate FROM blacklist)"
        ).fetchone()[0]
        avg_dur = conn.execute(
            "SELECT AVG(duration_min) FROM vehicle_records "
            "WHERE status='exited' AND entry_time LIKE ?", (f"{today}%",)
        ).fetchone()[0] or 0
    return jsonify({
        "revenue_today":  round(float(rev), 2),
        "entries_today":  entries,
        "parked_now":     parked,
        "flagged":        flagged,
        "avg_duration":   round(float(avg_dur)),
        "ts":             ist_now(),
    })

# =====================================================================
#  GATE CONTROL
# =====================================================================
@app.route("/gate/<action>", methods=["POST"])
@require_api_key
def gate_cmd(action: str):
    if action not in ("open", "close"):
        return jsonify({"error": "invalid action"}), 400
    data = request.get_json(silent=True) or {}
    lane = data.get("lane", "ENTRY").upper()

    if action == "open":
        # FIX: was _broadcast_boards({"type":"GATE_OPEN",...}) directly.
        # That bypassed the 15s per-lane cooldown in _safe_gate_open(), so
        # a double-click on the dashboard or a WS+MQTT race sent two GATE_OPEN
        # commands to the WROOM within the debounce window → gate re-opened.
        # Now routed through the same cooldown guard used by upload + MQTT paths.
        sent = _safe_gate_open(lane, reason="dashboard-manual")
        status = "ok" if sent else "suppressed-cooldown"
        log.info("[GATE] GATE_OPEN dashboard lane=%s sent=%s", lane, sent)
        return jsonify({"status": status, "action": "GATE_OPEN", "lane": lane})
    else:
        _broadcast_boards({"type": "GATE_CLOSE", "lane": lane})
        log.info("[GATE] GATE_CLOSE  lane=%s", lane)
        return jsonify({"status": "ok", "action": "GATE_CLOSE", "lane": lane})

# =====================================================================
#  IMAGE UPLOAD + OCR
# =====================================================================
@app.route("/upload/<lane>", methods=["POST"])
def upload_image(lane: str):
    if request.headers.get("X-Api-Key","") != API_KEY:
        return jsonify({"error": "unauthorized"}), 401

    # ── Extract JPEG from multipart body ─────────────────────────────────
    # Flask's request.files parses multipart/form-data.
    # If for any reason it doesn't populate (e.g. ESP32 sends slightly
    # non-standard multipart), we manually strip the boundary from the raw body.
    jpeg = b""
    if request.files:
        f = request.files.get("image") or next(iter(request.files.values()), None)
        if f:
            jpeg = f.read()

    if not jpeg:
        # Manual JPEG extraction: find SOI (FF D8) and EOI (FF D9) markers.
        # This is reliable regardless of multipart formatting variations.
        raw = request.get_data()
        soi = raw.find(b"\xff\xd8")
        eoi = raw.rfind(b"\xff\xd9")
        if soi != -1 and eoi != -1 and eoi > soi:
            jpeg = raw[soi:eoi + 2]
        else:
            log.warning("[UPLOAD] No JPEG markers found in body (len=%d)", len(raw))

    if not jpeg:
        log.warning("[UPLOAD] Empty body from lane=%s", lane)
        return jsonify({"error": "no image"}), 400

    log.info("[UPLOAD] lane=%s jpeg=%d bytes", lane, len(jpeg))

    # Persist image
    ts_str   = datetime.now(_IST).strftime("%Y%m%d_%H%M%S_%f")
    img_name = f"{lane.upper()}_{ts_str}.jpg"
    (UPLOAD_DIR / img_name).write_bytes(jpeg)
    image_url = f"/uploads/{img_name}"

    # ── OCR — shared for both ENTRY and EXIT lanes ────────────────────────
    plate, conf = "", 0.0
    try:
        from ocr_module import ocr_plate_from_bytes
        for crop in _plate_crops(jpeg):
            p, c = ocr_plate_from_bytes(crop)
            if c > conf:
                plate, conf = p, c
            if plate and conf >= 40.0:
                break   # good enough — stop
    except Exception as exc:
        log.warning("[OCR] %s", exc)

    log.info("[OCR] lane=%s plate=%s conf=%.1f", lane, plate or "UNDETECTED", conf)

    # ══════════════════════════════════════════════════════════════════════
    #  EXIT lane — gate already opened by IR handler.
    #  Find the matching parked/reserved record, mark exited, free slot.
    #  Do NOT create a new entry record and do NOT open gate again.
    # ══════════════════════════════════════════════════════════════════════
    if lane.upper() == "EXIT":
        exit_ts  = ist_now()
        rec_id   = None
        slot_num = None
        dur_min  = 0

        with get_db() as conn:
            # Try plate match first; fall back to most-recent parked row
            row = None
            if plate:
                row = conn.execute(
                    "SELECT id, slot_number, entry_time FROM vehicle_records "
                    "WHERE plate=? AND status IN ('parked','reserved') "
                    "ORDER BY id DESC LIMIT 1",
                    (plate.upper(),)
                ).fetchone()
            if not row:
                row = conn.execute(
                    "SELECT id, slot_number, entry_time FROM vehicle_records "
                    "WHERE status IN ('parked','reserved') "
                    "ORDER BY id DESC LIMIT 1"
                ).fetchone()

            if row:
                rec_id   = row["id"]
                slot_num = row["slot_number"]
                if row["entry_time"]:
                    try:
                        entry_dt = datetime.strptime(
                            row["entry_time"], "%Y-%m-%d %H:%M:%S"
                        ).replace(tzinfo=_IST)
                        dur_min = max(0, int(
                            (datetime.now(_IST) - entry_dt).total_seconds() / 60
                        ))
                    except Exception:
                        pass
                conn.execute(
                    "UPDATE vehicle_records SET status='exited', exit_time=?, "
                    "duration_min=?, image_url=?, ocr_confidence=? WHERE id=?",
                    (exit_ts, dur_min, image_url, round(conf, 1), rec_id)
                )
                if slot_num:
                    # Free slot in DB — use ENTRY lane because physical slots
                    # are managed on the ENTRY-side wroom
                    conn.execute(
                        "UPDATE slot_state SET state='EMPTY', plate=NULL, updated_at=? "
                        "WHERE slot_n=?",
                        (exit_ts, slot_num)
                    )

        log.info("[EXIT] rec_id=%s plate=%s slot=%s dur=%dm",
                 rec_id, plate or "UNDETECTED", slot_num, dur_min)

        # Notify WROOM + MQTT so physical LED goes green immediately
        if slot_num:
            _broadcast_boards({
                "type":  "SLOT_SET",
                "slot":  slot_num,
                "state": "EMPTY",
                "plate": "",
                "lane":  "ENTRY",
            })
            _mqtt_publish(
                "iprs/slot/ENTRY/reserve",
                json.dumps({"slot": slot_num, "action": "CANCEL", "plate": ""},
                           separators=(",", ":")),
            )

        _push_slot_broadcast("ENTRY")
        _push_slot_broadcast("EXIT")
        _broadcast_dash({
            "type": "EXIT_UPDATE", "id": rec_id,
            "plate": plate or "UNDETECTED", "lane": "EXIT",
            "slot": slot_num, "confidence": round(conf, 1),
            "imageUrl": image_url, "durationMin": dur_min, "ts": exit_ts,
        })
        return jsonify({
            "status": "ok", "action": "EXIT",
            "plate": plate, "confidence": round(conf, 1),
            "slot": slot_num, "id": rec_id, "durationMin": dur_min,
        }), 202

    # ══════════════════════════════════════════════════════════════════════
    #  ENTRY lane — assign slot, create record, open gate (OCR → gate)
    # ══════════════════════════════════════════════════════════════════════
    flagged = False
    if plate:
        with get_db() as conn:
            flagged = bool(conn.execute(
                "SELECT 1 FROM blacklist WHERE plate=?", (plate.upper(),)
            ).fetchone())

    slot_num = _first_empty_slot(lane)
    entry_ts = ist_now()

    with get_db() as conn:
        cur = conn.execute("""
            INSERT INTO vehicle_records
                (plate, lane, slot_number, entry_time, ocr_confidence, flagged, image_url, status)
            VALUES (?,?,?,?,?,?,?,'parked')
        """, (plate or "UNDETECTED", lane.upper(), slot_num, entry_ts,
              round(conf, 1), 1 if flagged else 0, image_url))
        rec_id = cur.lastrowid
        if slot_num:
            conn.execute(
                "UPDATE slot_state SET state='OCCUPIED', plate=?, updated_at=? "
                "WHERE lane=? AND slot_n=?",
                (plate, entry_ts, lane.upper(), slot_num)
            )

    _push_slot_broadcast(lane)
    _broadcast_dash({
        "type": "NEW_ENTRY", "id": rec_id, "plate": plate or "UNDETECTED",
        "lane": lane.upper(), "slot": slot_num, "confidence": round(conf, 1),
        "flagged": flagged, "imageUrl": image_url, "ts": entry_ts,
    })
    if flagged and plate:
        _broadcast_dash({"type": "BLACKLIST_ALERT", "plate": plate,
                         "lane": lane, "ts": entry_ts})

    # Gate opens for any non-blacklisted vehicle, even if OCR returned UNDETECTED.
    # Routed through _safe_gate_open() — enforces 15s per-lane cooldown to
    # prevent double-open from WS + MQTT race on vision pipeline confirmation.
    if not flagged:
        _safe_gate_open(lane, reason=f"upload ocr={plate or 'UNDETECTED'}")

    return jsonify({"status": "ok", "plate": plate, "confidence": round(conf, 1),
                    "slot": slot_num, "id": rec_id, "flagged": flagged}), 202


def _first_empty_slot(lane: str):
    with get_db() as conn:
        row = conn.execute(
            "SELECT slot_n FROM slot_state WHERE lane=? AND state='EMPTY' ORDER BY slot_n LIMIT 1",
            (lane.upper(),)
        ).fetchone()
    return row["slot_n"] if row else None


def _plate_crops(jpeg_bytes: bytes) -> list:
    """
    Return JPEG byte-strings to try for OCR, best candidates first.

    Since the ESP32-CAM is positioned IN FRONT of the gate (facing the
    approaching vehicle), the number plate is roughly centred or in the
    lower-centre of the frame. We search:
      1. Contour-detected plate-aspect rectangles (most accurate)
      2. Centre-bottom strip  (50-100% height, 20-80% width — plate sweet spot)
      3. Bottom 50% of frame
      4. Bottom 75% of frame  (plate higher or camera tilted up)
      5. Centre horizontal band  (30-70% height) for eye-level cameras
      6. Full frame             (last resort)
    """
    try:
        import cv2 as _cv2
        import numpy as _np

        arr = _np.frombuffer(jpeg_bytes, dtype=_np.uint8)
        img = _cv2.imdecode(arr, _cv2.IMREAD_COLOR)
        if img is None:
            return [jpeg_bytes]

        h, w = img.shape[:2]
        crops = []

        # ── 1. Contour detection with pre-sharpening ─────────────────────
        gray  = _cv2.cvtColor(img, _cv2.COLOR_BGR2GRAY)
        clahe = _cv2.createCLAHE(clipLimit=4.0, tileGridSize=(8, 8))
        eq    = clahe.apply(gray)
        # Sharpen before edge detection — helps blurry/motion plates
        sharpen_k = _np.array([[-1,-1,-1],[-1,9,-1],[-1,-1,-1]], dtype=_np.float32)
        sharpened = _cv2.filter2D(eq, -1, sharpen_k)
        sharpened = _np.clip(sharpened, 0, 255).astype(_np.uint8)
        blur  = _cv2.bilateralFilter(sharpened, 9, 75, 75)
        edges = _cv2.Canny(blur, 20, 100)   # lower thresholds catch faint edges
        kern  = _cv2.getStructuringElement(_cv2.MORPH_RECT, (5, 2))  # wider for plate shape
        edges = _cv2.dilate(edges, kern, iterations=2)
        cnts, _ = _cv2.findContours(edges, _cv2.RETR_EXTERNAL,
                                     _cv2.CHAIN_APPROX_SIMPLE)
        candidates = []
        for c in cnts:
            x, y, cw, ch = _cv2.boundingRect(c)
            if cw < 40 or ch < 10:
                continue
            ratio = cw / max(ch, 1)
            # Indian plates: standard ~4.5:1, BH series ~3.5:1
            if 2.0 <= ratio <= 7.0 and (cw * ch) >= (w * h * 0.005):
                candidates.append((cw * ch, x, y, cw, ch))
        candidates.sort(reverse=True)
        for _, x, y, cw, ch in candidates[:4]:   # top 4 candidates
            pad = 12
            crop = img[max(0, y-pad):min(h, y+ch+pad),
                       max(0, x-pad):min(w, x+cw+pad)]
            if crop.size == 0:
                continue
            ok, buf = _cv2.imencode(".jpg", crop, [_cv2.IMWRITE_JPEG_QUALITY, 98])
            if ok:
                crops.append(bytes(buf))

        # ── 2. Fixed crops — multiple fractions for camera angle variation ─
        # (fracs are top-boundary of crop — lower frac = more of the frame)
        for top_frac, left_frac, right_frac in [
            (0.50, 0.10, 0.90),   # centre-bottom (plate straight ahead)
            (0.60, 0.00, 1.00),   # bottom 40%   (low camera)
            (0.30, 0.00, 1.00),   # bottom 70%   (high camera)
            (0.25, 0.15, 0.85),   # wide centre band
        ]:
            region = img[int(h * top_frac):h,
                         int(w * left_frac):int(w * right_frac)]
            if region.size == 0:
                continue
            ok, buf = _cv2.imencode(".jpg", region, [_cv2.IMWRITE_JPEG_QUALITY, 95])
            if ok:
                crops.append(bytes(buf))

        crops.append(jpeg_bytes)   # full frame always last
        return crops

    except Exception as exc:
        log.debug("[CROP] %s", exc)
        return [jpeg_bytes]


def _push_slot_broadcast(lane: str) -> None:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT state FROM slot_state WHERE lane=? ORDER BY slot_n",
            (lane.upper(),)
        ).fetchall()
    statuses  = [r["state"] for r in rows]
    slots_left = statuses.count("EMPTY")
    _broadcast_dash({
        "type": "SLOT_UPDATE", "lane": lane.upper(),
        "slotStatus": statuses, "slotsLeft": slots_left,
        "parkingFull": slots_left == 0,
    })

# =====================================================================
#  SERVE UPLOADS
# =====================================================================
@app.route("/uploads/<filename>")
def serve_upload(filename: str):
    """
    Serve a saved plate JPEG.
    Path traversal guard: resolve both paths and confirm containment.
    Works on Linux, macOS, and Windows (uses os.path.commonpath).
    """
    try:
        import os as _osp
        safe        = (UPLOAD_DIR / filename).resolve()
        upload_root = UPLOAD_DIR.resolve()
        # commonpath check is cross-platform and avoids the str+"/") edge case
        if _osp.path.commonpath([safe, upload_root]) != str(upload_root):
            abort(404)
    except Exception:
        abort(404)
    if not safe.exists():
        abort(404)
    return send_file(str(safe), mimetype="image/jpeg",
                     max_age=300)   # 5 min browser cache — images are immutable


@app.route("/status/cam")
def status_cam():
    """
    Returns the last-known IP of the CAM board so the dashboard can
    construct the correct capture/stream URL without hardcoding a DHCP IP.
    Dashboard JS calls this via fallbackFn() in BOARD_HTTP config.
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT ip, last_seen FROM board_status "
            "WHERE board_id LIKE 'IPRS-CAM-%' ORDER BY last_seen DESC LIMIT 1"
        ).fetchone()
    if row and row["ip"]:
        return jsonify({"ip": row["ip"], "last_seen": row["last_seen"], "ok": True})
    return jsonify({"ip": None, "ok": False, "error": "CAM not yet seen"}), 404

# =====================================================================
#  HEARTBEATS
# =====================================================================
@app.route("/heartbeat/wroom/<lane>")
def heartbeat_wroom(lane: str):
    bid = request.headers.get("X-Board-Id", f"IPRS-WROOM-{lane.upper()}")
    ip  = request.headers.get("X-Ip", request.remote_addr)
    rssi = request.headers.get("X-Rssi")
    uptime = request.headers.get("X-Uptime", "0")
    _upsert_board(bid, lane.upper(), {
        "ip":       ip,
        "firmware": request.headers.get("X-Firmware", ""),
        "uptime":   uptime,
        "rssi":     int(rssi) if rssi and rssi.lstrip("-").isdigit() else None,
        "freeHeap": request.headers.get("X-Heap"),
    }, online=True)
    _broadcast_dash({"type": "BOARD_STATUS", "boardId": bid, "online": True,
                     "ip": ip, "wifiRssi": rssi, "uptimeSec": uptime})
    log.debug("[HB] WROOM %s  ip=%s", bid, ip)
    return jsonify({"status": "ok", "ts": ist_now()}), 200


@app.route("/heartbeat/cam/<lane>")
def heartbeat_cam(lane: str):
    bid = request.headers.get("X-Board-Id", f"IPRS-CAM-{lane.upper()}")
    ip  = request.headers.get("X-Ip", request.remote_addr)
    rssi = request.headers.get("X-Rssi")
    uptime = request.headers.get("X-Uptime", "0")
    _upsert_board(bid, lane.upper(), {
        "ip":       ip,
        "firmware": request.headers.get("X-Firmware", ""),
        "uptime":   uptime,
        "rssi":     int(rssi) if rssi and rssi.lstrip("-").isdigit() else None,
    }, online=True)
    _broadcast_dash({"type": "BOARD_STATUS", "boardId": bid, "online": True,
                     "ip": ip, "wifiRssi": rssi, "uptimeSec": uptime})
    log.debug("[HB] CAM %s  ip=%s", bid, ip)
    return jsonify({"status": "ok", "ts": ist_now()}), 200

# =====================================================================
#  SERIAL LOG + SSE
# =====================================================================
@app.route("/serial/log", methods=["POST"])
def serial_log():
    data  = request.get_json(silent=True) or {}
    bid   = data.get("boardId","UNKNOWN")
    lines = data.get("lines",[])
    for line in lines:
        _push_serial(bid, str(line))
        _broadcast_dash({"type": "SERIAL", "boardId": bid,
                         "line": str(line), "ts": ist_now()})
    return jsonify({"status": "ok", "accepted": len(lines)}), 200


@app.route("/serial/stream")
def serial_stream():
    q: queue.Queue = queue.Queue(maxsize=200)
    with _serial_lock:
        _sse_queues.append(q)
        for entry in _serial_buf[-50:]:
            try:
                q.put_nowait(entry)
            except Exception:
                pass

    def generate():
        try:
            while True:
                try:
                    entry = q.get(timeout=30)
                    yield f"data: {json.dumps(entry)}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        except GeneratorExit:
            pass
        finally:
            with _serial_lock:
                try:
                    _sse_queues.remove(q)
                except ValueError:
                    pass

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})

# =====================================================================
#  IR TRIGGER (HTTP fallback when WROOM WS is down)
# =====================================================================
@app.route("/trigger/cam/<lane>", methods=["POST"])
def trigger_cam(lane: str):
    lane_upper = lane.upper()
    log.info("[TRIG] HTTP fallback lane=%s", lane_upper)
    if lane_upper == "EXIT":
        # EXIT: open gate first, then trigger CAM with a delay (same as WS path)
        sent = _safe_gate_open("EXIT", reason="http-fallback-exit-ir")
        if sent:
            def _delayed_exit_cam():
                import time as _t
                _t.sleep(2.5)
                _broadcast_boards({"type": "TRIGGER", "lane": "EXIT"})
                log.info("[TRIG] EXIT cam trigger fired (HTTP fallback delayed)")
            import threading as _th
            _th.Thread(target=_delayed_exit_cam, daemon=True).start()
    else:
        _broadcast_boards({"type": "TRIGGER", "lane": lane_upper})
    return jsonify({"status": "ok", "lane": lane_upper}), 202

# =====================================================================
#  BOOKING
# =====================================================================
@app.route("/booking", methods=["POST"])
def booking():
    """
    Customer pre-booking: reserves a slot, inserts a vehicle_record (status='reserved'),
    updates slot_state to RESERVED, and broadcasts both SLOT_UPDATE + NEW_ENTRY to
    the owner dashboard so it immediately reflects the reservation.

    Payload from customer dashboard:
        { token, plate, slotNum (int 1-4), from (ISO), expiry/until (ISO) }
    """
    data  = request.get_json(silent=True) or {}
    token = data.get("token", str(uuid.uuid4()))
    plate = (data.get("plate","") or "").strip().upper()
    lane  = data.get("lane", "ENTRY").upper()

    # BUG 2 FIX: accept explicit slotNum (int) that customer now sends.
    # Fall back to parsing the legacy string "01"/"02"/"03"/"04" → int.
    raw_slot = data.get("slotNum") or data.get("slot", "")
    try:
        slot_num = int(raw_slot)
    except (ValueError, TypeError):
        slot_num = None
    if not slot_num or not (1 <= slot_num <= 4):
        slot_num = None

    from_iso  = data.get("from") or data.get("fromISO")
    until_iso = data.get("expiry") or data.get("until") or data.get("untilISO")
    entry_ts  = ist_now()

    with get_db() as conn:
        # ── bookings table (unchanged) ──────────────────────────────────
        conn.execute(
            "INSERT OR REPLACE INTO bookings (token, plate, slot, from_iso, until_iso) "
            "VALUES (?,?,?,?,?)",
            (token, plate, str(slot_num or ""), from_iso, until_iso)
        )

        # ── vehicle_records — insert pre-booking record ─────────────────
        # status='reserved' so owner sees it immediately in Records table
        cur = conn.execute("""
            INSERT INTO vehicle_records
                (plate, lane, slot_number, entry_time, ocr_confidence,
                 flagged, status, token)
            VALUES (?,?,?,?,0,0,'reserved',?)
        """, (plate or "CUSTOMER-BOOK", lane, slot_num, entry_ts, token))
        rec_id = cur.lastrowid

        # ── slot_state — mark slot RESERVED ────────────────────────────
        if slot_num:
            conn.execute(
                "UPDATE slot_state SET state='RESERVED', plate=?, updated_at=? "
                "WHERE lane=? AND slot_n=?",
                (plate, entry_ts, lane, slot_num)
            )

    log.info("[BOOK] plate=%s slot=%s lane=%s rec_id=%s token=%s",
             plate, slot_num, lane, rec_id, token[:12])

    # ── BUG 1 FIX: broadcast to owner dashboard ─────────────────────────
    _push_slot_broadcast(lane)
    _broadcast_dash({
        "type":       "NEW_ENTRY",
        "id":         rec_id,
        "plate":      plate or "CUSTOMER-BOOK",
        "lane":       lane,
        "slot":       slot_num,
        "confidence": 0,
        "flagged":    False,
        "imageUrl":   "",
        "ts":         entry_ts,
        "status":     "reserved",
    })

    # ── NEW: Push reservation to WROOM so physical LEDs blink immediately ─
    # Dual-path delivery (WS primary, MQTT fallback) for reliability.
    if slot_num:
        # WebSocket path — instant if WROOM is connected
        _broadcast_boards({
            "type":  "SLOT_SET",
            "slot":  slot_num,
            "state": "RESERVED",
            "plate": plate or "",
            "lane":  lane,
        })
        # MQTT path — survives WS disconnections
        _mqtt_publish(
            f"iprs/slot/{lane}/reserve",
            json.dumps({"slot": slot_num, "action": "RESERVE",
                        "plate": plate or ""}, separators=(",", ":")),
        )
        log.info("[BOOK] RESERVE pushed to WROOM slot=%d lane=%s", slot_num, lane)

    return jsonify({"status": "ok", "token": token, "recId": rec_id}), 201


@app.route("/booking/exit", methods=["POST"])
def booking_exit():
    """
    Called by customer dashboard on Exit & Pay.
    Marks vehicle_record as exited, frees the slot, broadcasts update to owner.
    Payload: { token, paymentAmt (float) }
    """
    data       = request.get_json(silent=True) or {}
    token      = data.get("token", "")
    payment    = float(data.get("paymentAmt", 0) or 0)
    exit_ts    = ist_now()

    with get_db() as conn:
        row = conn.execute(
            "SELECT id, lane, slot_number, entry_time FROM vehicle_records "
            "WHERE token=? ORDER BY id DESC LIMIT 1",
            (token,)
        ).fetchone()
        if not row:
            return jsonify({"error": "booking not found"}), 404

        rec_id   = row["id"]
        lane     = row["lane"] or "ENTRY"
        slot_num = row["slot_number"]

        # Compute duration
        dur_min = 0
        if row["entry_time"]:
            try:
                entry_dt = datetime.strptime(row["entry_time"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=_IST)
                exit_dt  = datetime.now(_IST)
                dur_min  = max(0, int((exit_dt - entry_dt).total_seconds() / 60))
            except Exception:
                pass

        conn.execute(
            "UPDATE vehicle_records SET status='exited', exit_time=?, duration_min=?, "
            "payment_amt=? WHERE id=?",
            (exit_ts, dur_min, round(payment, 2), rec_id)
        )
        if slot_num:
            conn.execute(
                "UPDATE slot_state SET state='EMPTY', plate=NULL, updated_at=? "
                "WHERE lane=? AND slot_n=?",
                (exit_ts, lane, slot_num)
            )

    log.info("[BOOK-EXIT] token=%s slot=%s dur=%dm payment=%.2f",
             token[:12], slot_num, dur_min, payment)
    _push_slot_broadcast(lane)
    # FIX: Broadcast SLOT_SET to WROOM so physical LEDs change from RESERVED→EMPTY
    # Without this, the LED stays amber/red even after the customer has paid and exited.
    if slot_num:
        _broadcast_boards({
            "type":  "SLOT_SET",
            "slot":  slot_num,
            "state": "EMPTY",
            "plate": "",
            "lane":  lane,
        })
        _mqtt_publish(
            f"iprs/slot/{lane}/reserve",
            json.dumps({"slot": slot_num, "action": "CANCEL", "plate": ""},
                       separators=(",", ":")),
        )
        log.info("[BOOK-EXIT] WROOM notified slot=%d lane=%s EMPTY", slot_num, lane)
    _broadcast_dash({
        "type": "SLOT_UPDATE",
        "lane": lane,
        "ts":   exit_ts,
    })
    return jsonify({"status": "ok", "durationMin": dur_min}), 200


@app.route("/booking/cancel", methods=["POST"])
def booking_cancel():
    """
    Called by customer dashboard on Cancel Booking.
    Removes the reserved status from slot and vehicle_record.
    Payload: { token }
    """
    data  = request.get_json(silent=True) or {}
    token = data.get("token", "")

    with get_db() as conn:
        row = conn.execute(
            "SELECT id, lane, slot_number FROM vehicle_records "
            "WHERE token=? AND status='reserved' ORDER BY id DESC LIMIT 1",
            (token,)
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE vehicle_records SET status='cancelled' WHERE id=?",
                (row["id"],)
            )
            lane     = row["lane"] or "ENTRY"
            slot_num = row["slot_number"]
            if slot_num:
                conn.execute(
                    "UPDATE slot_state SET state='EMPTY', plate=NULL, updated_at=? "
                    "WHERE lane=? AND slot_n=?",
                    (ist_now(), lane, slot_num)
                )
            _push_slot_broadcast(lane)
            # Push CANCEL to WROOM so LED returns to green
            _broadcast_boards({
                "type":  "SLOT_SET",
                "slot":  slot_num,
                "state": "EMPTY",
                "plate": "",
                "lane":  lane,
            })
            _mqtt_publish(
                f"iprs/slot/{lane}/reserve",
                json.dumps({"slot": slot_num, "action": "CANCEL", "plate": ""},
                           separators=(",", ":")),
            )
            log.info("[BOOK-CANCEL] token=%s slot=%s WROOM notified", token[:12], slot_num)
        else:
            lane = "ENTRY"

    _broadcast_dash({"type": "SLOT_UPDATE", "lane": lane, "ts": ist_now()})
    return jsonify({"status": "ok"}), 200

# =====================================================================
#  BLACKLIST
# =====================================================================
@app.route("/blacklist", methods=["GET"])
def get_blacklist():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT plate, added_at FROM blacklist ORDER BY added_at DESC"
        ).fetchall()
    return jsonify({"plates": [dict(r) for r in rows]})


@app.route("/blacklist", methods=["POST"])
@require_api_key
def update_blacklist():
    data   = request.get_json(silent=True) or {}
    plate  = (data.get("plate","") or "").strip().upper()
    action = data.get("action","add")
    if not plate or len(plate) < 3:
        return jsonify({"error": "invalid plate"}), 400
    with get_db() as conn:
        if action == "add":
            conn.execute("INSERT OR IGNORE INTO blacklist (plate) VALUES (?)", (plate,))
            conn.execute(
                "UPDATE vehicle_records SET flagged=1 WHERE plate=? AND status='parked'",
                (plate,)
            )
        elif action == "remove":
            conn.execute("DELETE FROM blacklist WHERE plate=?", (plate,))
        else:
            return jsonify({"error": "invalid action"}), 400
    _broadcast_dash({"type": "BLACKLIST_UPDATE", "plate": plate, "action": action})
    log.info("[BL] %s: %s", action, plate)
    return jsonify({"status": "ok", "plate": plate, "action": action})

# =====================================================================
#  MQTT BRIDGE
# =====================================================================
_mqtt_client = None


def _mqtt_publish(topic: str, payload: str) -> None:
    if _mqtt_client:
        try:
            _mqtt_client.publish(topic, payload, qos=1)
        except Exception:
            pass


def _init_mqtt() -> None:
    global _mqtt_client
    try:
        import paho.mqtt.client as mqtt

        def on_connect(client, userdata, flags, rc):
            if rc == 0:
                log.info("[MQTT] Connected to %s:%d", MQTT_BROKER, MQTT_PORT)
                client.subscribe("iprs/#")
            else:
                log.warning("[MQTT] Failed rc=%d", rc)

        def on_message(client, userdata, msg):
            topic   = msg.topic
            payload = msg.payload.decode("utf-8", errors="replace")
            if "vision/plate" in topic:
                try:
                    d = json.loads(payload)
                    plate = d.get("plate","")
                    conf  = float(d.get("conf", 0))
                    slot  = int(d.get("slot", 1))
                    lane  = d.get("action","ENTRY")
                    if plate:
                        _handle_vision_plate(plate, conf, slot, lane)
                except Exception as exc:
                    log.debug("[MQTT] parse error: %s", exc)

        c = mqtt.Client(client_id="iprs-server-v9")
        c.on_connect = on_connect
        c.on_message = on_message
        c.connect_async(MQTT_BROKER, MQTT_PORT, keepalive=60)
        c.loop_start()
        _mqtt_client = c
    except ImportError:
        log.warning("[MQTT] paho-mqtt not installed")
    except Exception as exc:
        log.warning("[MQTT] Init failed: %s", exc)


def _handle_vision_plate(plate: str, conf: float, slot: int, lane: str) -> None:
    """
    Called when the vision pipeline confirms a plate via MQTT.

    ROOT CAUSE FIX — duplicate DB records / missing image on dashboard:
    The upload path (/upload/<lane>) already writes a vehicle_record with
    the saved JPEG image URL.  If the vision pipeline later confirms the
    same vehicle via MQTT, the original code did a blind INSERT → every
    vehicle produced TWO records:
      row 1 (from upload) : correct image_url, possibly weak OCR plate
      row 2 (from MQTT)   : no image_url (NULL), better OCR plate

    The dashboard's image thumbnail showed N/A for the MQTT row because
    image_url was NULL, even though the JPEG was on disk.

    Fix: look for a recent upload record (same lane, parked, last 30 s).
    If found → UPDATE it with the confirmed plate + confidence from YOLO.
    If not found → INSERT (pipeline-only detection with no prior upload).
    Either way we broadcast a single NEW_ENTRY that includes the image URL.
    """
    entry_ts = ist_now()
    flagged  = False
    with get_db() as conn:
        flagged = bool(conn.execute(
            "SELECT 1 FROM blacklist WHERE plate=?", (plate.upper(),)
        ).fetchone())

        # ── Try to UPDATE the recent upload record rather than inserting ──
        # Match: same lane + status='parked' + entry_time in last 30 seconds.
        # 30 s is safely larger than the upload→MQTT pipeline latency (~5 s)
        # and safely smaller than a re-entry gap (>60 s by gate cooldown).
        recent_cutoff = (
            datetime.now(_IST) - timedelta(seconds=30)
        ).strftime("%Y-%m-%d %H:%M:%S")

        existing = conn.execute(
            "SELECT id, image_url, slot_number FROM vehicle_records "
            "WHERE lane=? AND entry_time >= ? AND status='parked' "
            "ORDER BY id DESC LIMIT 1",
            (lane.upper(), recent_cutoff),
        ).fetchone()

        if existing:
            rec_id    = existing["id"]
            image_url = existing["image_url"] or ""
            # Use existing slot if we don't have a better assignment
            use_slot  = slot if slot else (existing["slot_number"] or slot)
            conn.execute(
                "UPDATE vehicle_records "
                "SET plate=?, ocr_confidence=?, flagged=?, slot_number=? "
                "WHERE id=?",
                (plate, round(conf * 100, 1), 1 if flagged else 0,
                 use_slot, rec_id),
            )
            log.info("[MQTT-OCR] Updated existing record id=%d plate=%s conf=%.1f%%",
                     rec_id, plate, conf * 100)
        else:
            # No recent upload record — pipeline-only detection (upload may have
            # failed or CAM trigger path was skipped). Insert a new record.
            cur = conn.execute("""
                INSERT INTO vehicle_records
                    (plate, lane, slot_number, entry_time, ocr_confidence, flagged, status)
                VALUES (?,?,?,?,?,?,'parked')
            """, (plate, lane, slot, entry_ts, round(conf * 100, 1), 1 if flagged else 0))
            rec_id    = cur.lastrowid
            image_url = ""
            log.info("[MQTT-OCR] No recent upload — inserted new record id=%d plate=%s",
                     rec_id, plate)

        conn.execute(
            "UPDATE slot_state SET state='OCCUPIED', plate=?, updated_at=? "
            "WHERE lane=? AND slot_n=?",
            (plate, entry_ts, lane.upper(), slot),
        )

    _push_slot_broadcast(lane)
    _broadcast_dash({
        "type":       "NEW_ENTRY",
        "id":         rec_id,
        "plate":      plate,
        "lane":       lane,
        "slot":       slot,
        "confidence": round(conf * 100, 1),
        "flagged":    flagged,
        "imageUrl":   image_url,   # now correctly populated from upload record
        "ts":         entry_ts,
    })
    if flagged:
        _broadcast_dash({"type": "BLACKLIST_ALERT", "plate": plate,
                         "lane": lane, "ts": entry_ts})
    else:
        _safe_gate_open(lane, reason=f"mqtt-vision plate={plate}")

    log.info("[MQTT-OCR] plate=%s conf=%.1f%% slot=%d flagged=%s imageUrl=%s",
             plate, conf * 100, slot, flagged, image_url or "(none)")

# =====================================================================
#  BOARD WATCHDOG — mark boards offline after BOARD_TIMEOUT_S
# =====================================================================
def _board_watchdog() -> None:
    while True:
        time.sleep(30)
        try:
            cutoff = (datetime.now(_IST) - timedelta(seconds=BOARD_TIMEOUT_S)
                      ).strftime("%Y-%m-%d %H:%M:%S")
            with get_db() as conn:
                went_offline = conn.execute(
                    "SELECT board_id FROM board_status WHERE online=1 AND last_seen<?",
                    (cutoff,)
                ).fetchall()
                if went_offline:
                    conn.execute(
                        "UPDATE board_status SET online=0 WHERE last_seen<?", (cutoff,)
                    )
                    for row in went_offline:
                        log.warning("[WDG] Board offline: %s", row["board_id"])
                        _broadcast_dash({"type": "BOARD_STATUS",
                                         "boardId": row["board_id"], "online": False})
        except Exception as exc:
            log.debug("[WDG] %s", exc)

# =====================================================================
#  ENTRYPOINT
# =====================================================================
_start_time = time.time()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="IPRS Server v9.0")
    parser.add_argument("--port",  type=int, default=5000)
    parser.add_argument("--host",  default="0.0.0.0")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    init_db()
    _init_mqtt()

    threading.Thread(target=_board_watchdog, daemon=True, name="watchdog").start()

    log.info("=" * 58)
    log.info("  IPRS Server v9.0  starting on http://%s:%d", args.host, args.port)
    log.info("  Owner dashboard  →  http://localhost:%d/",          args.port)
    log.info("  Customer dash    →  http://localhost:%d/customer",   args.port)
    log.info("  Status check     →  http://localhost:%d/status",     args.port)
    log.info("  Board WS         →  ws://0.0.0.0:%d/ws",            args.port)
    log.info("  Dashboard WS     →  ws://0.0.0.0:%d/ws/dashboard",  args.port)
    log.info("=" * 58)

    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)

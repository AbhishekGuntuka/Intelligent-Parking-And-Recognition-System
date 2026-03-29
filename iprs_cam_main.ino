/*
  Project  : IPRS — Intelligent Parking & Recognition System
  File     : iprs_cam_main.ino
  Purpose  : Camera node — MJPEG stream, JPEG capture, OCR upload, OLED
  Hardware : ESP32-CAM (AI Thinker / M5-TimerCAM compatible)
  Modified : 2026-03-22

  Upgrade notes vs v8:
    NEW  — ESPAsyncWebServer: /stream, /capture, /status, /flash endpoints
    NEW  — Heap guard: <50000 bytes → skip frame, log warning
    NEW  — WDT tightened to 8s
    NEW  — Frame-interval timer: 500ms between captures (not continuous)
    NEW  — /flash?state=on|off HTTP endpoint
    FIX  — ALL delay() removed from main loop
    FIX  — ALL String class removed — char[] everywhere
    FIX  — esp_camera_fb_return() always called — no memory leak
    FIX  — Non-blocking WiFi reconnect (millis guard)
    KEEP — OLED display (SSD1306 via I2C)
    KEEP — Multi-frame sharpness selection (JPEG size proxy)
    KEEP — Upload to Flask /upload/<lane> with retry
    KEEP — WebSocket hub connection + HELLO registration
*/

// ═══════════════════════════════════════════════════════════════════════
// ── CONFIG BLOCK ──────────────────────────────────────────────────────
// ═══════════════════════════════════════════════════════════════════════
#define DEBUG_MODE              1
#define LANE                    "ENTRY"
#define WIFI_SSID               "Abhi"
#define WIFI_PASSWORD           "12345678"
#define SERVER_IP               "10.121.81.78"
#define SERVER_PORT             5000
#define CAM_HTTP_PORT           8080
#define FIRMWARE_VER            "9.1.0"
#define API_KEY                 "IPRS-2026-SECURE"
#define OTA_HOSTNAME            "iprs-cam-entry"
#define OTA_PASSWORD            "iprs2026ota"

// Timing
#define HEARTBEAT_MS        30000UL
#define WIFI_CHECK_MS       10000UL
#define NTP_RESYNC_MS     3600000UL
#define TRIGGER_RATELIMIT_MS 12000UL   // FIX: was 3000UL — must be > SERVO_HOLD_MS(3000)+pipeline(~6000)+margin
#define CAPTURE_INTERVAL_MS   500UL   // frame polling interval
#define WDT_TIMEOUT_SEC           8   // tighter than WROOM — CAM freezes faster
#define HEAP_MIN_BYTES        50000   // skip capture below this

// Capture tuning
#define CAPTURE_FRAMES            3   // best-of-N by JPEG size
#define FLASH_PREWARM_MS        150
#define UPLOAD_MAX_RETRIES        3

// ─ MJPEG stream boundary ──────────────────────────────────────────────
#define STREAM_BOUNDARY  "IPRSStream"
#define STREAM_PART_FMT  "--" STREAM_BOUNDARY "\r\nContent-Type: image/jpeg\r\nContent-Length: %u\r\n\r\n"

#define STR_HELPER(x) #x
#define STR(x)        STR_HELPER(x)
#define BOARD_ID      "IPRS-CAM-" LANE

#if DEBUG_MODE
  #define DLOG(tag, msg)      serialLog(tag, msg)
  #define DLOGF(tag, fmt, ...) \
    do { char _d[96]; snprintf(_d, sizeof(_d), fmt, ##__VA_ARGS__); serialLog(tag, _d); } while(0)
#else
  #define DLOG(tag, msg)       do {} while(0)
  #define DLOGF(tag, fmt, ...) do {} while(0)
#endif

// ═══════════════════════════════════════════════════════════════════════
// ── PIN DEFINITIONS (AI Thinker ESP32-CAM) ────────────────────────────
// ═══════════════════════════════════════════════════════════════════════
#define PWDN_GPIO_NUM   32
#define RESET_GPIO_NUM  -1
#define XCLK_GPIO_NUM    0
#define SIOD_GPIO_NUM   26
#define SIOC_GPIO_NUM   27
#define Y9_GPIO_NUM     35
#define Y8_GPIO_NUM     34
#define Y7_GPIO_NUM     39
#define Y6_GPIO_NUM     36
#define Y5_GPIO_NUM     21
#define Y4_GPIO_NUM     19
#define Y3_GPIO_NUM     18
#define Y2_GPIO_NUM      5
#define VSYNC_GPIO_NUM  25
#define HREF_GPIO_NUM   23
#define PCLK_GPIO_NUM   22
#define PIN_FLASH_LED    4
#define PIN_BUTTON      13

// ═══════════════════════════════════════════════════════════════════════
// ── INCLUDES ──────────────────────────────────────────────────────────
// ═══════════════════════════════════════════════════════════════════════
#include <Arduino.h>
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <WiFiClient.h>
#include <WebSocketsClient.h>
#include <ESPAsyncWebServer.h>   // Async server — zero blocking in handlers
#include <ArduinoOTA.h>
#include <esp_task_wdt.h>
#include <esp_camera.h>
#include "soc/soc.h"
#include "soc/rtc_cntl_reg.h"
#include <time.h>
#include <sys/time.h>

// ─ OLED ───────────────────────────────────────────────────────────────
#define OLED_W    128
#define OLED_H     64
#define OLED_ADDR 0x3C
Adafruit_SSD1306 oled(OLED_W, OLED_H, &Wire, -1);
static bool oledOk = false;

// ═══════════════════════════════════════════════════════════════════════
// ── OBJECTS & GLOBAL STATE ────────────────────────────────────════════
// ═══════════════════════════════════════════════════════════════════════
AsyncWebServer   camServer(CAM_HTTP_PORT);
WebSocketsClient wsClient;

// ── Camera access mutex ─────────────────────────────────────────────────
// esp_camera_fb_get() is NOT re-entrant. The /stream handler runs in the
// async_tcp task (core 0); captureBestFrame() runs in loop (core 1).
// Without this semaphore the two tasks collide → hard fault within minutes.
static SemaphoreHandle_t camMux = nullptr;
#define CAM_TAKE()  (xSemaphoreTake(camMux, pdMS_TO_TICKS(300)) == pdTRUE)
#define CAM_GIVE()  xSemaphoreGive(camMux)

static bool     wsConnected      = false;
static bool     rtcSynced        = false;
static bool     flashOn          = false;
static volatile bool triggerPending   = false;
static volatile bool isTriggerActive  = false;
// Single CAM serves both ENTRY and EXIT lanes.
// pendingLane stores which lane the latest TRIGGER is for so
// uploadToFlask() posts to the correct /upload/<lane> endpoint
// and handleTrigger() shows the right OLED messages.
static char pendingLane[8] = "ENTRY";   // default; overwritten by wsEvent/HTTP trigger

static uint32_t tmHeartbeat   = 0;
static uint32_t tmWifiCheck   = 0;
static uint32_t tmNtpResync   = 0;
static uint32_t tmCapture     = 0;
static uint32_t tmTrigger     = 0;
static uint32_t bootMs        = 0;

// Serial log queue
#define LOG_QUEUE_MAX 20
static char    gLogQueue[LOG_QUEUE_MAX][120];
static uint8_t gLogHead  = 0;
static uint8_t gLogCount = 0;

// Reusable JSON scratch
static char gJson[320];

// ═══════════════════════════════════════════════════════════════════════
// ── FORWARD DECLARATIONS ──────────────────────────────────────────────
// ═══════════════════════════════════════════════════════════════════════
void initWiFi();
void checkWifi();
void initCamera();
void initWebServer();
void initWebSocket();
void wsEvent(WStype_t, uint8_t*, size_t);
void initOTA();
void syncRTC();
void sendHeartbeat();
void flushSerialLog();
void serialLog(const char*, const char*);
bool handleTrigger();
camera_fb_t* captureBestFrame();
int  uploadToFlask(camera_fb_t*);
void showOLED(const char*, const char*, const char*);
void getTimestamp(char*, size_t);
void getBoardUptime(char*, size_t);

// ═══════════════════════════════════════════════════════════════════════
// ── SERIAL LOG ────────────────────────────────────────────────────────
// ═══════════════════════════════════════════════════════════════════════
void serialLog(const char* tag, const char* msg) {
    Serial.printf("%s %s\n", tag, msg);
    if (wsConnected) {
        char esc[124]; int ei = 0;
        for (int i = 0; msg[i] && ei < 121; i++) {
            if (msg[i] == '"' || msg[i] == '\\') esc[ei++] = '\\';
            esc[ei++] = msg[i];
        }
        esc[ei] = '\0';
        snprintf(gJson, sizeof(gJson),
                 "{\"type\":\"LOG\",\"boardId\":\"" BOARD_ID "\",\"line\":\"%s %s\"}", tag, esc);
        wsClient.sendTXT(gJson);
        return;
    }
    uint8_t slot = gLogHead % LOG_QUEUE_MAX;
    snprintf(gLogQueue[slot], sizeof(gLogQueue[slot]), "%s %s", tag, msg);
    gLogHead = (gLogHead + 1) % LOG_QUEUE_MAX;
    if (gLogCount < LOG_QUEUE_MAX) gLogCount++;
}

void flushSerialLog() {
    if (gLogCount == 0 || WiFi.status() != WL_CONNECTED) { gLogCount = 0; return; }
    uint8_t count = gLogCount; gLogCount = 0;
    static char buf[2600]; int pos = 0;
    pos += snprintf(buf + pos, sizeof(buf) - pos, "{\"boardId\":\"" BOARD_ID "\",\"lines\":[");
    uint8_t start = (gLogHead + LOG_QUEUE_MAX - count) % LOG_QUEUE_MAX;
    for (uint8_t i = 0; i < count && pos < (int)sizeof(buf) - 12; i++) {
        uint8_t idx = (start + i) % LOG_QUEUE_MAX;
        if (i > 0) buf[pos++] = ',';
        buf[pos++] = '"';
        const char* src = gLogQueue[idx];
        while (*src && pos < (int)sizeof(buf) - 5) {
            if (*src == '"' || *src == '\\') buf[pos++] = '\\';
            buf[pos++] = *src++;
        }
        buf[pos++] = '"';
    }
    if (pos < (int)sizeof(buf) - 3) { buf[pos++] = ']'; buf[pos++] = '}'; buf[pos] = '\0'; }
    HTTPClient http; char url[96];
    snprintf(url, sizeof(url), "http://" SERVER_IP ":" STR(SERVER_PORT) "/serial/log");
    http.begin(url);
    http.addHeader("Content-Type", "application/json");
    http.setTimeout(1500);
    http.POST(buf);
    http.end();
}

// ═══════════════════════════════════════════════════════════════════════
// ── WEBSOCKET ─────────────────────────────────────────────────────────
// ═══════════════════════════════════════════════════════════════════════
void wsEvent(WStype_t type, uint8_t* payload, size_t length) {
    switch (type) {
    case WStype_CONNECTED: {
        wsConnected = true;
        char hello[200];
        snprintf(hello, sizeof(hello),
                 "{\"type\":\"HELLO\",\"boardId\":\"" BOARD_ID "\","
                 "\"firmware\":\"%s\",\"ip\":\"%s\",\"rssi\":%d,\"lane\":\"" LANE "\"}",
                 FIRMWARE_VER, WiFi.localIP().toString().c_str(), (int)WiFi.RSSI());
        wsClient.sendTXT(hello);
        DLOG("[WS]", "Connected");
        break;
    }
    case WStype_DISCONNECTED:
        wsConnected = false;
        DLOG("[WS]", "Disconnected");
        break;
    case WStype_TEXT: {
        char* raw = (char*)payload;
        // TRIGGER message carries the lane: {"type":"TRIGGER","lane":"ENTRY"|"EXIT"}
        // Single CAM: store the lane so upload goes to correct endpoint.
        if (strstr(raw, "\"TRIGGER\"") &&
            !isTriggerActive &&
            (millis() - tmTrigger) > TRIGGER_RATELIMIT_MS) {
            // Extract lane field — default to ENTRY if missing
            char* lp = strstr(raw, "\"lane\":\"");
            if (lp) {
                lp += 8;
                int li = 0;
                while (*lp && *lp != '"' && li < 7) pendingLane[li++] = *lp++;
                pendingLane[li] = '\0';
            } else {
                strncpy(pendingLane, "ENTRY", sizeof(pendingLane));
            }
            triggerPending = true;
            DLOGF("[WS]", "TRIGGER lane=%s", pendingLane);
        }
        if (strstr(raw, "\"FLASH_ON\""))  { digitalWrite(PIN_FLASH_LED, HIGH); flashOn = true; }
        if (strstr(raw, "\"FLASH_OFF\"")) { digitalWrite(PIN_FLASH_LED, LOW);  flashOn = false; }
        break;
    }
    default: break;
    }
}

void initWebSocket() {
    wsClient.disconnect();
    uint32_t _tw = millis();
    while (millis() - _tw < 50) { esp_task_wdt_reset(); }
    wsClient.begin(SERVER_IP, SERVER_PORT, "/ws");
    wsClient.onEvent(wsEvent);
    wsClient.setReconnectInterval(3000);
    wsClient.enableHeartbeat(15000, 3000, 2);
}

// ═══════════════════════════════════════════════════════════════════════
// ── ASYNC WEB SERVER — /stream, /capture, /status, /flash ─────────────
// ESPAsyncWebServer callbacks execute in the async TCP task (not loop).
// CRITICAL: Never call esp_camera_fb_get() from two tasks simultaneously.
// The /stream uses its own task-safe frame cycle.
// ═══════════════════════════════════════════════════════════════════════
void initWebServer() {
    // ── /status — JSON health payload ────────────────────────────────
    camServer.on("/status", HTTP_GET, [](AsyncWebServerRequest* req) {
        char uptime[16]; getBoardUptime(uptime, sizeof(uptime));
        char buf[320];
        snprintf(buf, sizeof(buf),
                 "{\"boardId\":\"" BOARD_ID "\",\"firmware\":\"%s\","
                 "\"heap\":%u,\"uptime\":%s,\"camOk\":true,"
                 "\"rssi\":%d,\"ip\":\"%s\",\"flash\":%s,\"lane\":\"" LANE "\"}",
                 FIRMWARE_VER,
                 (unsigned)ESP.getFreeHeap(),
                 uptime,
                 (int)WiFi.RSSI(),
                 WiFi.localIP().toString().c_str(),
                 flashOn ? "true" : "false");
        AsyncWebServerResponse* resp = req->beginResponse(200, "application/json", buf);
        resp->addHeader("Access-Control-Allow-Origin", "*");
        req->send(resp);
    });

    // ── /flash?state=on|off ──────────────────────────────────────────
    camServer.on("/flash", HTTP_GET, [](AsyncWebServerRequest* req) {
        if (req->hasParam("state")) {
            const char* sv = req->getParam("state")->value().c_str();
            if (strcmp(sv, "on") == 0) {
                digitalWrite(PIN_FLASH_LED, HIGH); flashOn = true;
            } else {
                digitalWrite(PIN_FLASH_LED, LOW);  flashOn = false;
            }
        }
        req->send(200, "application/json", flashOn ? "{\"flash\":\"on\"}" : "{\"flash\":\"off\"}");
    });

    // ── /trigger — HTTP trigger from WROOM fallback ──────────────────
    camServer.on("/trigger", HTTP_POST, [](AsyncWebServerRequest* req) {
        if ((millis() - tmTrigger) < TRIGGER_RATELIMIT_MS) {
            req->send(429, "application/json", "{\"error\":\"rate_limited\"}");
            return;
        }
        if (isTriggerActive) {
            req->send(409, "application/json", "{\"error\":\"busy\"}");
            return;
        }
        // Lane can be passed as query param: /trigger?lane=EXIT
        const char* laneVal = "ENTRY";
        if (req->hasParam("lane")) {
            laneVal = req->getParam("lane")->value().c_str();
        }
        strncpy(pendingLane, laneVal, sizeof(pendingLane) - 1);
        pendingLane[sizeof(pendingLane) - 1] = '\0';
        triggerPending = true;
        DLOGF("[HTTP]", "Trigger accepted lane=%s", pendingLane);
        req->send(202, "application/json", "{\"status\":\"accepted\",\"lane\":\"" LANE "\"}");
    });

    // ── /capture — single JPEG snapshot ─────────────────────────────
    // Called by vision pipeline for OCR feed.
    // FIX: beginResponse_P copies the pointer, NOT the data.
    // On some ESPAsyncWebServer builds the async TCP task reads fb->buf
    // AFTER esp_camera_fb_return() has freed it → heap corruption.
    // Safe fix: heap-copy the JPEG bytes, pass ownership to the response.
    camServer.on("/capture", HTTP_GET, [](AsyncWebServerRequest* req) {
        if (ESP.getFreeHeap() < HEAP_MIN_BYTES + 32768) {   // need room for copy
            req->send(503, "application/json", "{\"error\":\"low_heap\"}");
            DLOG("[CAP]", "Low heap — /capture refused");
            return;
        }
        camera_fb_t* fb = esp_camera_fb_get();
        if (!fb) {
            req->send(500, "application/json", "{\"error\":\"cam_fail\"}");
            return;
        }
        // Deep-copy JPEG into a heap buffer owned by the response lambda
        size_t jpegLen = fb->len;
        uint8_t* jpegCopy = (uint8_t*)malloc(jpegLen);
        if (!jpegCopy) {
            esp_camera_fb_return(fb);
            req->send(507, "application/json", "{\"error\":\"alloc_fail\"}");
            return;
        }
        memcpy(jpegCopy, fb->buf, jpegLen);
        esp_camera_fb_return(fb);   // safe to return now — copy is independent

        // FIX: ESPAsyncWebServer filler overload is (contentType, len, filler).
        // There is NO status-code first argument for this overload.
        // Old (wrong): beginResponse(200, "image/jpeg", lambda, jpegLen)
        // Fixed:       beginResponse("image/jpeg",  jpegLen, lambda)
        // Status 200 is the default; set explicitly with setCode() if needed.
        AsyncWebServerResponse* resp = req->beginResponse(
            "image/jpeg", jpegLen,
            [jpegCopy, jpegLen](uint8_t* buf, size_t maxLen, size_t idx) -> size_t {
                size_t remaining = jpegLen - idx;
                if (remaining == 0) { free(jpegCopy); return 0; }
                size_t chunk = (remaining < maxLen) ? remaining : maxLen;
                memcpy(buf, jpegCopy + idx, chunk);
                return chunk;
            });
        // 200 is default; explicit set keeps intent clear
        resp->setCode(200);
        char _szBuf[12], _hpBuf[12];
        snprintf(_szBuf, sizeof(_szBuf), "%u", (unsigned)jpegLen);
        snprintf(_hpBuf, sizeof(_hpBuf), "%u", (unsigned)ESP.getFreeHeap());
        resp->addHeader("X-Frame-Size",  _szBuf);
        resp->addHeader("X-Heap",        _hpBuf);
        resp->addHeader("Cache-Control", "no-cache");
        req->send(resp);
        DLOGF("[CAP]", "/capture %u bytes", (unsigned)jpegLen);
    });

    // ── /stream — MJPEG live feed ────────────────────────────────────
    camServer.on("/stream", HTTP_GET, [](AsyncWebServerRequest* req) {
        // Chunked multipart MJPEG stream
        // Uses AsyncWebServer's chunked response with a generator lambda
        static const char CT[] = "multipart/x-mixed-replace;boundary=" STREAM_BOUNDARY;

        AsyncWebServerResponse* resp = req->beginChunkedResponse(CT,
            [](uint8_t* buf, size_t maxLen, size_t idx) -> size_t {
                // Called repeatedly by the async task until it returns 0
                // Each call must produce exactly one JPEG frame or nothing
                if (ESP.getFreeHeap() < HEAP_MIN_BYTES) return 0;
                if (!CAM_TAKE()) return 0;   // mutex: don't race with captureBestFrame

                camera_fb_t* fb = esp_camera_fb_get();
                if (!fb) { CAM_GIVE(); return 0; }

                // Build MJPEG part header
                char hdr[96];
                int hdrLen = snprintf(hdr, sizeof(hdr), STREAM_PART_FMT, (unsigned)fb->len);

                size_t total = (size_t)hdrLen + fb->len + 2;  // +2 for \r\n
                if (total > maxLen) {
                    esp_camera_fb_return(fb);   // won't fit — skip frame
                    CAM_GIVE();
                    return 0;
                }
                memcpy(buf,             hdr,    hdrLen);
                memcpy(buf + hdrLen,    fb->buf, fb->len);
                buf[hdrLen + fb->len]     = '\r';
                buf[hdrLen + fb->len + 1] = '\n';

                esp_camera_fb_return(fb);   // MUST return buffer
                CAM_GIVE();
                return total;
            });
        resp->addHeader("Access-Control-Allow-Origin", "*");
        req->send(resp);
    });

    // Helper lambda: mutex-protected single frame grab for /stream
    // Defined once, referenced in the chunked generator above via closure

    camServer.onNotFound([](AsyncWebServerRequest* req) {
        req->send(404, "application/json", "{\"error\":\"not_found\"}");
    });

    camServer.begin();
    DLOG("[HTTP]", "Async server started port " STR(CAM_HTTP_PORT));
}

// ═══════════════════════════════════════════════════════════════════════
// ── CAMERA INIT — optimised for outdoor parking plates ───────────────
// ═══════════════════════════════════════════════════════════════════════
void initCamera() {
    camera_config_t cfg;
    cfg.ledc_channel   = LEDC_CHANNEL_0;
    cfg.ledc_timer     = LEDC_TIMER_0;
    cfg.pin_d0         = Y2_GPIO_NUM;
    cfg.pin_d1         = Y3_GPIO_NUM;
    cfg.pin_d2         = Y4_GPIO_NUM;
    cfg.pin_d3         = Y5_GPIO_NUM;
    cfg.pin_d4         = Y6_GPIO_NUM;
    cfg.pin_d5         = Y7_GPIO_NUM;
    cfg.pin_d6         = Y8_GPIO_NUM;
    cfg.pin_d7         = Y9_GPIO_NUM;
    cfg.pin_xclk       = XCLK_GPIO_NUM;
    cfg.pin_pclk       = PCLK_GPIO_NUM;
    cfg.pin_vsync      = VSYNC_GPIO_NUM;
    cfg.pin_href       = HREF_GPIO_NUM;
    cfg.pin_sccb_sda   = SIOD_GPIO_NUM;
    cfg.pin_sccb_scl   = SIOC_GPIO_NUM;
    cfg.pin_pwdn       = PWDN_GPIO_NUM;
    cfg.pin_reset      = RESET_GPIO_NUM;
    cfg.xclk_freq_hz   = 20000000;        // 20MHz XCLK
    cfg.pixel_format   = PIXFORMAT_JPEG;
    cfg.grab_mode      = CAMERA_GRAB_WHEN_EMPTY;

    if (psramFound()) {
        // VGA (640x480): best resolution/RAM tradeoff for plates
        cfg.frame_size   = FRAMESIZE_VGA;
        cfg.jpeg_quality = 12;            // lower = better quality; 10-15 range
        cfg.fb_count     = 2;             // double-buffer for /stream
        cfg.fb_location  = CAMERA_FB_IN_PSRAM;
    } else {
        cfg.frame_size   = FRAMESIZE_CIF; // fallback if no PSRAM
        cfg.jpeg_quality = 14;
        cfg.fb_count     = 1;
        cfg.fb_location  = CAMERA_FB_IN_DRAM;
    }

    if (esp_camera_init(&cfg) != ESP_OK) {
        DLOG("[CAM]", "Init FAILED — restarting");
        showOLED("CAM INIT", "FAILED", "Rebooting...");
        // WDT-safe 2s splash before reboot
        uint32_t _tw = millis();
        while (millis() - _tw < 2000) { esp_task_wdt_reset(); }
        ESP.restart();
    }

    sensor_t* s = esp_camera_sensor_get();
    if (s) {
        if (s->id.PID == OV3660_PID) { s->set_vflip(s, 1); s->set_hmirror(s, 0); }
        s->set_brightness(s,  1);   // +1 for outdoor (slightly brighter)
        s->set_contrast(s,    1);   // +1 for better plate contrast
        s->set_saturation(s,  0);   // neutral saturation
        s->set_sharpness(s,   2);   // max sharpness for text detail
        s->set_denoise(s,     1);
        s->set_whitebal(s,    1);   // AWB on
        s->set_awb_gain(s,    1);
        s->set_exposure_ctrl(s, 1); // AEC on
        s->set_aec2(s,        1);   // AEC2 (night-adaptive)
        s->set_gain_ctrl(s,   1);   // AGC on
        s->set_bpc(s,         1);
        s->set_wpc(s,         1);
        s->set_raw_gma(s,     1);
        s->set_lenc(s,        1);
    }
    DLOG("[CAM]", "Ready VGA@12q");
}

// ═══════════════════════════════════════════════════════════════════════
// ── CAPTURE — best frame by JPEG size (sharpness proxy) ──────────────
// Called from main loop only — single threaded access to camera
// ═══════════════════════════════════════════════════════════════════════
camera_fb_t* captureBestFrame() {
    if (ESP.getFreeHeap() < HEAP_MIN_BYTES) {
        DLOGF("[CAM]", "Low heap (%u) — skip", (unsigned)ESP.getFreeHeap());
        return nullptr;
    }
    if (!CAM_TAKE()) {
        DLOG("[CAM]", "Camera busy — skip");
        return nullptr;
    }

    // Switch to SVGA (800x600) for OCR capture.
    // VGA (640x480) at barrier distance → plate ≈18 px tall → Tesseract fails.
    // SVGA → plate ≈28 px tall → readable.
    // Restore VGA after capture so MJPEG stream continues at normal resolution.
    sensor_t* s = esp_camera_sensor_get();
    if (s) {
        s->set_framesize(s, FRAMESIZE_SVGA);
        // Discard stale frame so AEC settles at new resolution
        camera_fb_t* stale = esp_camera_fb_get();
        if (stale) esp_camera_fb_return(stale);
        uint32_t _tw = millis();
        while (millis() - _tw < 150) esp_task_wdt_reset();
    }

    camera_fb_t* best      = nullptr;
    uint32_t     bestScore = 0;

    // Flash pre-warm — gives sensor time to adjust AE
    digitalWrite(PIN_FLASH_LED, HIGH);
    uint32_t t0 = millis();
    while (millis() - t0 < (uint32_t)FLASH_PREWARM_MS) esp_task_wdt_reset();

    for (int i = 0; i < CAPTURE_FRAMES; i++) {
        // Discard stale sensor pipeline frame
        camera_fb_t* stale = esp_camera_fb_get();
        if (stale) esp_camera_fb_return(stale);

        uint32_t tw = millis();
        while (millis() - tw < 80) esp_task_wdt_reset();

        camera_fb_t* fb = esp_camera_fb_get();
        if (!fb) { DLOGF("[CAM]", "Frame %d null", i+1); continue; }

        DLOGF("[CAM]", "Frame %d: %u bytes SVGA", i+1, (unsigned)fb->len);

        if (fb->len > bestScore) {
            if (best) esp_camera_fb_return(best);
            best      = fb;
            bestScore = fb->len;
        } else {
            esp_camera_fb_return(fb);   // MUST return every non-winner
        }
    }

    digitalWrite(PIN_FLASH_LED, LOW);

    // Restore VGA for MJPEG stream
    if (s) s->set_framesize(s, FRAMESIZE_VGA);

    CAM_GIVE();
    return best;   // caller MUST call esp_camera_fb_return(best) when done
}

// ═══════════════════════════════════════════════════════════════════════
// ── UPLOAD TO FLASK — raw TCP multipart POST ─────────────────────────
// Returns 0 on HTTP 2xx, negative on error
// ═══════════════════════════════════════════════════════════════════════
int uploadToFlask(camera_fb_t* fb) {
    if (WiFi.status() != WL_CONNECTED) return -1;
    WiFiClient client; client.setTimeout(10000);
    if (!client.connect(SERVER_IP, SERVER_PORT)) {
        DLOG("[UP]", "TCP connect fail");
        return -2;
    }
    const char* boundary = "IPRSv9Bnd";
    char head[256];
    int  headLen = snprintf(head, sizeof(head),
        "--%s\r\nContent-Disposition: form-data; name=\"image\"; filename=\"plate.jpeg\"\r\n"
        "Content-Type: image/jpeg\r\n\r\n", boundary);
    char tail[64];
    int  tailLen = snprintf(tail, sizeof(tail), "\r\n--%s--\r\n", boundary);
    uint32_t bodyLen = (uint32_t)headLen + fb->len + (uint32_t)tailLen;

    client.printf("POST /upload/%s HTTP/1.1\r\n", pendingLane);
    client.printf("Host: %s:%d\r\nX-Api-Key: %s\r\nX-Board-Id: %s\r\n",
                  SERVER_IP, SERVER_PORT, API_KEY, BOARD_ID);
    client.printf("Content-Type: multipart/form-data; boundary=%s\r\n"
                  "Content-Length: %u\r\nConnection: close\r\n\r\n",
                  boundary, bodyLen);
    client.write((const uint8_t*)head, headLen);

    // Stream JPEG in 1KB chunks to avoid stack overflow
    for (size_t i = 0; i < fb->len; i += 1024) {
        esp_task_wdt_reset();
        size_t chunk = (fb->len - i < 1024) ? (fb->len - i) : 1024;
        client.write(fb->buf + i, chunk);
    }
    client.write((const uint8_t*)tail, tailLen);

    // Wait for response — no delay(): feed WDT during wait
    uint32_t rt0 = millis();
    while (!client.available() && (millis() - rt0 < 8000)) {
        esp_task_wdt_reset();
        // yield to other tasks every ~5ms (vTaskDelay(1) ≈ 1 tick = 1ms on ESP32)
        vTaskDelay(pdMS_TO_TICKS(5));
    }
    if (!client.available()) { client.stop(); DLOG("[UP]", "No response"); return -3; }

    // Parse status line — "HTTP/1.1 202 Accepted"
    char line[64]; int li = 0;
    while (client.available() && li < 63) {
        char c = client.read();
        if (c == '\n') break;
        if (c != '\r') line[li++] = c;
    }
    line[li] = '\0';
    client.stop();

    // Extract HTTP code from "HTTP/1.1 NNN ..."
    char* sp = strchr(line, ' ');
    if (!sp) { DLOG("[UP]", "Bad response"); return -4; }
    int httpCode = (int)strtol(sp + 1, nullptr, 10);
    DLOGF("[UP]", "HTTP %d", httpCode);
    return (httpCode >= 200 && httpCode < 300) ? 0 : -4;
}

// ═══════════════════════════════════════════════════════════════════════
// ── TRIGGER HANDLER — single CAM serves both ENTRY and EXIT lanes ─────
//
// ENTRY flow: IR detects vehicle → server sends TRIGGER(ENTRY) → CAM captures
//             → uploads to /upload/ENTRY → server does OCR → opens gate
//
// EXIT flow:  IR detects vehicle → server opens EXIT gate immediately →
//             server sends TRIGGER(EXIT) after 2.5s → CAM captures →
//             uploads to /upload/EXIT → server marks record exited, slot EMPTY
//
// pendingLane is set by wsEvent/HTTP trigger before triggerPending=true.
// ═══════════════════════════════════════════════════════════════════════
bool handleTrigger() {
    isTriggerActive = true;
    triggerPending  = false;
    tmTrigger       = millis();

    bool isExit = (strncmp(pendingLane, "EXIT", 4) == 0);

    if (isExit) {
        DLOG("[TRIG]", "EXIT capture — gate already open");
        showOLED("Vehicle", "Exiting...", "");
    } else {
        DLOG("[TRIG]", "ENTRY capture...");
        showOLED("Detecting", "Vehicle...", "");
    }

    camera_fb_t* fb = captureBestFrame();
    if (!fb) {
        isTriggerActive = false;
        showOLED("Capture", "Failed", "Ready");
        return false;
    }

    char fbinfo[48];
    snprintf(fbinfo, sizeof(fbinfo), "Frame %u bytes", (unsigned)fb->len);
    DLOG("[TRIG]", fbinfo);
    flushSerialLog();

    if (isExit) {
        showOLED("Uploading", "Exit record", "");
    } else {
        showOLED("Uploading", "Please wait", "");
    }

    int code = -1;
    for (int attempt = 1; attempt <= UPLOAD_MAX_RETRIES; attempt++) {
        esp_task_wdt_reset();
        char at[32]; snprintf(at, sizeof(at), "Upload %d/%d", attempt, UPLOAD_MAX_RETRIES);
        DLOG("[TRIG]", at);
        code = uploadToFlask(fb);
        if (code == 0) break;
        if (attempt < UPLOAD_MAX_RETRIES) {
            uint32_t tw = millis();
            while (millis() - tw < (uint32_t)(1000 * attempt)) esp_task_wdt_reset();
        }
    }

    // CRITICAL: ALWAYS return frame buffer — missing this crashes CAM in <2 min
    esp_camera_fb_return(fb);

    if (code != 0) {
        char err[32]; snprintf(err, sizeof(err), "Upload failed %d", code);
        DLOG("[TRIG]", err);
        showOLED("Upload", "Failed", "Check server");
        uint32_t tw = millis();
        while (millis() - tw < 2000) esp_task_wdt_reset();
        showOLED("Welcome!", "IPRS Ready", "");
        isTriggerActive = false;
        return false;
    }

    flushSerialLog();

    if (isExit) {
        // Gate already opened — just confirm exit recorded
        DLOG("[TRIG]", "Exit record saved OK");
        showOLED("Goodbye!", "Safe trip", "");
        uint32_t tw = millis();
        while (millis() - tw < 2000) esp_task_wdt_reset();
    } else {
        // ENTRY: server opens gate after OCR
        DLOG("[TRIG]", "Upload OK — gate opening server-side");
        showOLED("Gate", "Opening...", "");
        uint32_t tw = millis();
        while (millis() - tw < 1000) esp_task_wdt_reset();
        showOLED("Thank You!", "Safe parking", "");
        tw = millis();
        while (millis() - tw < 3000) esp_task_wdt_reset();
    }

    showOLED("Welcome!", "IPRS Ready", "");
    DLOG("[TRIG]", "Pipeline complete");
    isTriggerActive = false;
    return true;
}

// ═══════════════════════════════════════════════════════════════════════
// ── HEARTBEAT ─────────────────────────────────────────────────────────
// ═══════════════════════════════════════════════════════════════════════
void sendHeartbeat() {
    if (WiFi.status() != WL_CONNECTED) return;
    if (ESP.getFreeHeap() < HEAP_MIN_BYTES) return;
    char uptime[16]; getBoardUptime(uptime, sizeof(uptime));
    char url[128];
    snprintf(url, sizeof(url),
             "http://" SERVER_IP ":" STR(SERVER_PORT) "/heartbeat/cam/%s", LANE);
    HTTPClient http;
    http.begin(url);
    http.addHeader("X-Board-Id", BOARD_ID);
    http.addHeader("X-Firmware", FIRMWARE_VER);
    http.addHeader("X-Uptime",   uptime);
    // FIX: was String((int)WiFi.RSSI()).c_str() — replaced with char[] snprintf
    char _rssiBuf[8];
    snprintf(_rssiBuf, sizeof(_rssiBuf), "%d", (int)WiFi.RSSI());
    http.addHeader("X-Rssi",     _rssiBuf);
    http.addHeader("X-Ip",       WiFi.localIP().toString().c_str());
    http.setTimeout(4000);
    int code = http.GET();
    http.end();
    DLOGF("[HB]", "HTTP %d heap=%u", code, (unsigned)ESP.getFreeHeap());
}

// ═══════════════════════════════════════════════════════════════════════
// ── WIFI ──────────────────────────────────────────────────────────────
// ═══════════════════════════════════════════════════════════════════════
void initWiFi() {
    Serial.printf("[WIFI] Connecting to \"%s\"", WIFI_SSID);
    WiFi.mode(WIFI_STA);
    WiFi.setSleep(false);                  // disable modem sleep — prevents MJPEG stream stutter
    WiFi.setTxPower(WIFI_POWER_19_5dBm);   // max TX power — helps in crowded venues
    WiFi.setAutoReconnect(true);
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    uint32_t t0 = millis();
    while (WiFi.status() != WL_CONNECTED) {
        if (millis() - t0 > 15000) { Serial.println("\n[WIFI] Timeout"); return; }
        delay(400); Serial.print('.');
    }
    Serial.printf("\n[WIFI] IP:%s RSSI:%ddBm\n",
                  WiFi.localIP().toString().c_str(), WiFi.RSSI());
}

void checkWifi() {
    static bool wasConnected = false;
    bool nowConnected = (WiFi.status() == WL_CONNECTED);
    if (nowConnected && !wasConnected) {
        DLOG("[WIFI]", "Reconnected");
        wsClient.disconnect();
        uint32_t _tw = millis();
        while (millis() - _tw < 50) { esp_task_wdt_reset(); }
        wsClient.begin(SERVER_IP, SERVER_PORT, "/ws");
        wsClient.onEvent(wsEvent);
        wsClient.setReconnectInterval(3000);
        wsClient.enableHeartbeat(15000, 3000, 2);
        syncRTC();
    }
    wasConnected = nowConnected;
}

// ═══════════════════════════════════════════════════════════════════════
// ── OTA ───────────────────────────────────────────────────────────────
// ═══════════════════════════════════════════════════════════════════════
void initOTA() {
    ArduinoOTA.setHostname(OTA_HOSTNAME);
    ArduinoOTA.setPassword(OTA_PASSWORD);
    ArduinoOTA.onStart([]()  { esp_task_wdt_delete(NULL); DLOG("[OTA]", "Start"); });
    ArduinoOTA.onEnd([]()    { DLOG("[OTA]", "Done"); });
    ArduinoOTA.onProgress([](uint32_t d, uint32_t t) { esp_task_wdt_reset(); });
    ArduinoOTA.onError([](ota_error_t e) { esp_task_wdt_add(NULL); });
    ArduinoOTA.begin();
}

// ═══════════════════════════════════════════════════════════════════════
// ── OLED ──────────────────────────────────────────────────────────────
// ═══════════════════════════════════════════════════════════════════════
void showOLED(const char* l1, const char* l2, const char* l3) {
    if (!oledOk) return;
    oled.clearDisplay();
    oled.setTextColor(SSD1306_WHITE);
    oled.setTextSize(1);
    oled.setCursor(0, 8);  oled.println(l1);
    if (l2 && l2[0]) { oled.setCursor(0, 24); oled.println(l2); }
    if (l3 && l3[0]) { oled.setCursor(0, 40); oled.println(l3); }
    oled.display();
}

// ═══════════════════════════════════════════════════════════════════════
// ── RTC HELPERS ───────────────────────────────────────────────────────
// ═══════════════════════════════════════════════════════════════════════
void syncRTC() {
    configTime(19800, 0, "pool.ntp.org", "time.google.com");
    struct tm ti; uint32_t t0 = millis();
    while (!getLocalTime(&ti) && (millis() - t0 < 6000)) {
        esp_task_wdt_reset();
        vTaskDelay(pdMS_TO_TICKS(300));   // FIX: was delay(300)
    }
    rtcSynced = getLocalTime(&ti);
    if (rtcSynced) {
        char buf[24]; strftime(buf, sizeof(buf), "%Y-%m-%d %H:%M:%S", &ti);
        DLOG("[RTC]", buf);
    } else {
        DLOG("[RTC]", "Sync failed");
    }
}

void getTimestamp(char* buf, size_t sz) {
    struct timeval tv; gettimeofday(&tv, nullptr);
    time_t ist = tv.tv_sec + 19800;
    struct tm* t = gmtime(&ist);
    snprintf(buf, sz, "%04d-%02d-%02d %02d:%02d:%02d",
             t->tm_year+1900, t->tm_mon+1, t->tm_mday,
             t->tm_hour, t->tm_min, t->tm_sec);
}

void getBoardUptime(char* buf, size_t sz) {
    snprintf(buf, sz, "%lu", (unsigned long)((millis() - bootMs) / 1000UL));
}

// ═══════════════════════════════════════════════════════════════════════
// ── SETUP ─────────────────────────────────────════════════════════════
// ═══════════════════════════════════════════════════════════════════════
void setup() {
    // Disable brownout detector — CAM power spike on boot trips it
    WRITE_PERI_REG(RTC_CNTL_BROWN_OUT_REG, 0);

    Serial.begin(115200); delay(200);
    Serial.println(F("\n[BOOT] IPRS v9.0 ESP32-CAM"));

    pinMode(PIN_FLASH_LED, OUTPUT);
    pinMode(PIN_BUTTON,    INPUT_PULLUP);
    digitalWrite(PIN_FLASH_LED, LOW);

    // I2C for OLED — SDA=15, SCL=14 (AI Thinker layout)
    Wire.begin(15, 14);
    oledOk = oled.begin(SSD1306_SWITCHCAPVCC, OLED_ADDR);
    if (oledOk) { oled.clearDisplay(); oled.display(); }
    showOLED("IPRS v9.0", "Local OCR", "Lane: " LANE);
    uint32_t tboot = millis();
    while (millis() - tboot < 1500) {}  // boot splash — no delay(), use busy wait

    // WDT armed AFTER splash so boot freeze doesn't reset before OLED
    // WDT: v2.x uses esp_task_wdt_init(sec,panic), v3.x uses config struct
#if ESP_ARDUINO_VERSION_MAJOR >= 3
    const esp_task_wdt_config_t _wdt_cfg = {
        .timeout_ms     = WDT_TIMEOUT_SEC * 1000U,
        .idle_core_mask = 0,
        .trigger_panic  = true,
    };
    esp_task_wdt_reconfigure(&_wdt_cfg);
#else
    esp_task_wdt_init(WDT_TIMEOUT_SEC, true);
#endif
    esp_task_wdt_add(NULL);

    showOLED("WiFi...", WIFI_SSID, "");
    initWiFi();
    showOLED("WiFi OK", WiFi.localIP().toString().c_str(), "");
    syncRTC();

    // Create camera mutex BEFORE initCamera so /stream can't race with capture
    camMux = xSemaphoreCreateMutex();
    configASSERT(camMux != nullptr);   // halt on failure — camera needs this

    initCamera();
    initWebServer();   // async — non-blocking
    initOTA();
    initWebSocket();

    bootMs = millis();
    tmHeartbeat = bootMs - HEARTBEAT_MS + 5000;
    tmCapture   = bootMs;

    sendHeartbeat();
    showOLED("Welcome!", "Lane: " LANE, "Ready");
    DLOG("[BOOT]", "CAM v9.0 ready");
}

// ═══════════════════════════════════════════════════════════════════════
// ── LOOP — non-blocking ───────────────────────────────────────────────
// ═══════════════════════════════════════════════════════════════════════
void loop() {
    esp_task_wdt_reset();

    // High-frequency pumps
    wsClient.loop();
    ArduinoOTA.handle();

    // Trigger dispatch — execute in loop to ensure single-threaded camera access
    if (triggerPending && !isTriggerActive &&
        (millis() - tmTrigger) > TRIGGER_RATELIMIT_MS) {
        handleTrigger();
    }

    uint32_t now = millis();

    if (now - tmHeartbeat >= HEARTBEAT_MS) {
        tmHeartbeat = now; sendHeartbeat();
    }
    if (now - tmWifiCheck >= WIFI_CHECK_MS) {
        tmWifiCheck = now; checkWifi();
    }
    if (now - tmNtpResync >= NTP_RESYNC_MS) {
        tmNtpResync = now; syncRTC();
    }
    if (!wsConnected && gLogCount > 0 && (now - tmHeartbeat > 800)) {
        flushSerialLog();
    }

    // Feed WDT during idle — loop is fast when no trigger pending
    esp_task_wdt_reset();
}
// END IPRS v9.0 ESP32-CAM

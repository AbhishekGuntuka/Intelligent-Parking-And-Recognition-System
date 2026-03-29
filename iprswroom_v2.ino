/*
  IPRS v17.3 — iprswroom_v2.ino
  Gate controller: WiFi, MQTT, FreeRTOS servo, OTA, reserved-slot LED blink
  Hardware: ESP32-WROOM-32   Modified: 2026-03-28

  ═══════════════════════════════════════════════════════════════════════
  CHANGES vs ORIGINAL UPLOADED FILE (v17.2)
  ═══════════════════════════════════════════════════════════════════════

  BUG-FIX-1 — COMPILE ERROR (missing terminating " character):
    wsEvent() TEXT handler, GATE_CLOSE strstr call had a stray extra "
    at end of the string literal, making the compiler see an unterminated
    string. Fixed by removing the extra ".
      Error was: missing terminating " character  (line ~593)

  BUG-FIX-2 — EXIT GATE NEVER OPENED:
    handleIR() only polled PIN_IR_ENTRY. PIN_IR_EXIT was wired and
    defined but NEVER read in code — so the exit gate never opened.
    Fix: handleIR() now polls BOTH sensors independently:
      • ENTRY IR → IR_TRIGGER lane=ENTRY (unchanged flow)
      • EXIT  IR → IR_TRIGGER lane=EXIT  (NEW)
    Each has its own static rate-limit timer so they don't block each other.
    On EXIT IR: server opens EXIT gate immediately, waits 2.5s, then
    sends TRIGGER(EXIT) to the single ESP32-CAM to capture the plate.

  ═══════════════════════════════════════════════════════════════════════
  Carried forward from v17.2:
    FIX-1  Boot false-trigger: gEntryIRPrev seeded from real sensor state.
    FIX-2  gSlotIRPrev seeded from real slot sensors at boot.
    FIX-3  TRIGGER_RATELIMIT_MS = 12 000 ms (> full servo+pipeline cycle).
    FIX-4  String().c_str() eliminated — char gBoardIP[] everywhere.
    NEW-1  Reserved-slot LED blink engine (tickLEDs, millis-only, zero heap).
    NEW-2  MQTT RESERVE / CANCEL commands update physical LEDs.
    NEW-3  /status response includes "ip" field.
*/

// ═══ CONFIG ═══════════════════════════════════════════════════════════
#define DEBUG_MODE              1
#define LANE                "ENTRY"        // "ENTRY" or "EXIT"
#define WIFI_SSID           "Abhi"
#define WIFI_PASSWORD       "12345678"
#define SERVER_IP           "10.121.81.78"
#define SERVER_PORT         5000
#define MQTT_BROKER         "10.121.81.78"
#define MQTT_PORT           1883
#define MQTT_USER           ""
#define MQTT_PASS           ""
#define OTA_HOSTNAME        "iprs-wroom-entry"
#define OTA_PASSWORD        "iprs2026ota"
#define WROOM_HTTP_PORT     8081
#define TOTAL_SLOTS         4
#define API_KEY             "IPRS-2026-SECURE"
#define FIRMWARE_VER        "17.2.0"

// Timing (ms, compared with millis())
#define HEARTBEAT_MS          30000UL
#define MQTT_RECONNECT_MS      5000UL
#define WIFI_CHECK_MS         10000UL
#define NTP_RESYNC_MS       3600000UL
#define SLOT_PUBLISH_MS        2000UL
#define TRIGGER_RATELIMIT_MS  12000UL   // > SERVO_HOLD(3s)+pipeline(~6s)+margin
#define GATE_CMD_DEBOUNCE_MS   5000UL
#define WDT_TIMEOUT_SEC           10
#define SERVO_HOLD_MS          3000
#define SERVO_STEP_DELAY_MS      15
#define HEAP_MIN_BYTES        60000
#define BLINK_HALF_MS          500UL    // 1 Hz blink half-period

// LED PWM
#define LED_PWM_FREQ   1000
#define LED_PWM_RES       8
#define LEDC_CH_GRN_S3    0  // GPIO4
#define LEDC_CH_GRN_S4    1  // GPIO16
#define LEDC_CH_GRN_WIFI  2  // GPIO17
#define LEDC_CH_GRN_PWR   3  // GPIO5
#define LEDC_CH_RED_S1    4  // GPIO21
#define LEDC_CH_RED_S2    5  // GPIO22
#define LEDC_CH_RED_S3    6  // GPIO23
#define LEDC_CH_RED_S4    7  // GPIO13

// LED duty levels (0-255)
#define LED_FULL       200   // solid on
#define LED_WIFI_ON    180   // WiFi indicator
#define LED_PWR_ON     255   // power LED
#define LED_RES_RED     40   // dim red during RESERVED (amber appearance)
// RESERVED blink phases are 200 (on) and 0 (off) — handled in tickLEDs

// ═══ PINS ═════════════════════════════════════════════════════════════
#define PIN_IR_ENTRY    14
#define PIN_IR_EXIT     27
#define PIN_IR_SLOT1    26   // DAC2 — INPUT_PULLUP safe
#define PIN_IR_SLOT2    25   // DAC1 — INPUT_PULLUP safe
#define PIN_IR_SLOT3    33   // input-only GPIO
#define PIN_IR_SLOT4    32   // input-only GPIO
#define PIN_SERVO       18
// Green LEDs — slots 1+2 on boot-strapping pins (digitalWrite only)
#define PIN_LED_GRN_SLOT1  15   // ⚠ boot-strap — digitalWrite only
#define PIN_LED_GRN_SLOT2   2   // ⚠ boot-strap — digitalWrite only
#define PIN_LED_GRN_SLOT3   4
#define PIN_LED_GRN_SLOT4  16
#define PIN_LED_GRN_WIFI   17
#define PIN_LED_GRN_POWER   5
// Red LEDs — all LEDC-capable
#define PIN_LED_RED_SLOT1  21
#define PIN_LED_RED_SLOT2  22
#define PIN_LED_RED_SLOT3  23
#define PIN_LED_RED_SLOT4  13
#define PIN_INDICATOR      19   // red LED + buzzer (pulse on gate event)
#define SERVO_CLOSED_DEG    0
#define SERVO_OPEN_DEG     90

#define STR_HELPER(x) #x
#define STR(x)        STR_HELPER(x)
#define BOARD_ID      "IPRS-WROOM-" LANE

#if DEBUG_MODE
  #define DLOG(t,m)       serialLog(t,m)
  #define DLOGF(t,f,...)  do{char _d[96];snprintf(_d,sizeof(_d),f,##__VA_ARGS__);serialLog(t,_d);}while(0)
#else
  #define DLOG(t,m)       do{}while(0)
  #define DLOGF(t,f,...)  do{}while(0)
#endif

// ═══ INCLUDES ══════════════════════════════════════════════════════════
#include <Arduino.h>
#include <WiFi.h>
#include <WebServer.h>
#include <HTTPClient.h>
#include <WebSocketsClient.h>
#include <PubSubClient.h>
#include <ESP32Servo.h>
#include <ArduinoOTA.h>
#include <esp_task_wdt.h>
#include <time.h>
#include <sys/time.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"

// ═══ STATE MACHINE ════════════════════════════════════════════════════
typedef enum : uint8_t { SLOT_EMPTY=0, SLOT_RESERVED=1, SLOT_OCCUPIED=2, SLOT_RELEASED=3 } SlotState;
static const char* SLOT_STR[] = { "EMPTY","RESERVED","OCCUPIED","RELEASED" };

typedef struct {
    SlotState state;
    char      plate[16];
    uint32_t  changedAt;
    char      ts[22];
} ParkingSlot;

static ParkingSlot gSlots[TOTAL_SLOTS+1]; // 1-indexed
static bool        gFull  = false;
static int         gFree  = TOTAL_SLOTS;

static void slotTrans(uint8_t idx, SlotState ns, const char* plate) {
    if (idx<1||idx>TOTAL_SLOTS) return;
    ParkingSlot* s=&gSlots[idx];
    if (s->state==ns) return;
    SlotState os=s->state; s->state=ns; s->changedAt=millis();
    if (plate&&plate[0]) { strncpy(s->plate,plate,15); s->plate[15]='\0'; }
    struct timeval tv; gettimeofday(&tv,nullptr);
    time_t ist=tv.tv_sec+19800; struct tm* t=gmtime(&ist);
    snprintf(s->ts,sizeof(s->ts),"%04d-%02d-%02d %02d:%02d:%02d",
             t->tm_year+1900,t->tm_mon+1,t->tm_mday,t->tm_hour,t->tm_min,t->tm_sec);
    DLOGF("[SLOT]","#%u %s->%s plate=%s",idx,SLOT_STR[os],SLOT_STR[ns],s->plate);
}

// ═══ CRITICAL SECTION ═════════════════════════════════════════════════
static portMUX_TYPE gMux = portMUX_INITIALIZER_UNLOCKED;
#define CS_ENTER() portENTER_CRITICAL(&gMux)
#define CS_EXIT()  portEXIT_CRITICAL(&gMux)

// ═══ GLOBALS ══════════════════════════════════════════════════════════
static WebServer        wroomServer(WROOM_HTTP_PORT);
static WebSocketsClient wsClient;
static WiFiClient       mqttWifi;
static PubSubClient     mqttClient(mqttWifi);
static Servo            gateServo;

volatile bool vOpen=false, vClose=false, vGateIsOpen=false, vServoActive=false;
static bool   wsConn=false, rtcOk=false;

static uint32_t tmHB=0,tmMQTT=0,tmWifi=0,tmNTP=0,tmSlot=0,tmTrig=0,tmGate=0,bootMs=0;

#define LQ 20
static char    logQ[LQ][120]; static uint8_t logHead=0,logCnt=0;

static char topicSlot[56],topicGate[56],topicStat[56],topicIR[56];
static char gJ[320];          // reusable JSON scratch
static char gIP[20];          // board IP cache (no String)

// ═══ HELPERS ══════════════════════════════════════════════════════════
#define STR_HELPER(x) #x
#define STR(x) STR_HELPER(x)

static char _sb[128];
const char* slotsJson() {
    int p=0; _sb[p++]='[';
    for(int i=1;i<=TOTAL_SLOTS;i++){
        if(i>1)_sb[p++]=','; _sb[p++]='"';
        const char*s=SLOT_STR[gSlots[i].state];
        while(*s&&p<(int)sizeof(_sb)-4)_sb[p++]=*s++;
        _sb[p++]='"';
    }
    _sb[p++]=']'; _sb[p]='\0'; return _sb;
}

void getTS(char*b,size_t sz){
    struct timeval tv; gettimeofday(&tv,nullptr);
    time_t ist=tv.tv_sec+19800; struct tm*t=gmtime(&ist);
    snprintf(b,sz,"%04d-%02d-%02d %02d:%02d:%02d",
             t->tm_year+1900,t->tm_mon+1,t->tm_mday,t->tm_hour,t->tm_min,t->tm_sec);
}
void getUptime(char*b,size_t sz){snprintf(b,sz,"%lu",(unsigned long)((millis()-bootMs)/1000UL));}

// ═══ SERIAL LOG ════════════════════════════════════════════════════════
void serialLog(const char*tag,const char*msg){
    Serial.printf("%s %s\n",tag,msg);
    if(wsConn){
        char esc[124]; int ei=0;
        for(int i=0;msg[i]&&ei<121;i++){if(msg[i]=='"'||msg[i]=='\\')esc[ei++]='\\';esc[ei++]=msg[i];}
        esc[ei]='\0';
        snprintf(gJ,sizeof(gJ),"{\"type\":\"LOG\",\"boardId\":\"" BOARD_ID "\",\"line\":\"%s %s\"}",tag,esc);
        wsClient.sendTXT(gJ); return;
    }
    uint8_t sl=logHead%LQ; snprintf(logQ[sl],sizeof(logQ[sl]),"%s %s",tag,msg);
    logHead=(logHead+1)%LQ; if(logCnt<LQ)logCnt++;
}

void flushLog(){
    if(!logCnt||WiFi.status()!=WL_CONNECTED){logCnt=0;return;}
    uint8_t cnt=logCnt; logCnt=0;
    static char buf[2600]; int p=0;
    p+=snprintf(buf+p,sizeof(buf)-p,"{\"boardId\":\"" BOARD_ID "\",\"lines\":[");
    uint8_t st=(logHead+LQ-cnt)%LQ;
    for(uint8_t i=0;i<cnt&&p<(int)sizeof(buf)-12;i++){
        uint8_t idx=(st+i)%LQ; if(i>0)buf[p++]=','; buf[p++]='"';
        const char*src=logQ[idx];
        while(*src&&p<(int)sizeof(buf)-5){if(*src=='"'||*src=='\\')buf[p++]='\\';buf[p++]=*src++;}
        buf[p++]='"';
    }
    if(p<(int)sizeof(buf)-3){buf[p++]=']';buf[p++]='}';buf[p]='\0';}
    HTTPClient h; char url[96];
    snprintf(url,sizeof(url),"http://" SERVER_IP ":" STR(SERVER_PORT) "/serial/log");
    h.begin(url); h.addHeader("Content-Type","application/json"); h.setTimeout(1500); h.POST(buf); h.end();
}

// ═══ MQTT ══════════════════════════════════════════════════════════════
void mqttPub(const char*t,const char*p){if(mqttClient.connected())mqttClient.publish(t,p,false);}

void mqttCB(char*topic,byte*payload,unsigned int len){
    if(!len||len>127)return;
    char msg[128]; memcpy(msg,payload,len); msg[len]='\0';

    // Gate commands
    if(strstr(topic,"/cmd")){
        if(strncmp(msg,"OPEN", 4)==0){CS_ENTER();vOpen =true;CS_EXIT();DLOG("[MQTT]","OPEN rx");}
        if(strncmp(msg,"CLOSE",5)==0){CS_ENTER();vClose=true;CS_EXIT();DLOG("[MQTT]","CLOSE rx");}
    }
    // Vision pipeline plate confirmation
    if(strstr(topic,"vision/plate")){
        char*sp=strstr(msg,"\"slot\":"),*ap=strstr(msg,"\"action\":\"");
        if(!sp||!ap)return;
        int sn=(int)strtol(sp+7,nullptr,10); bool isE=(strstr(ap,"ENTRY")!=nullptr);
        char pl[16]={}; char*pp=strstr(msg,"\"plate\":\"");
        if(pp){pp+=9;int i=0;while(*pp&&*pp!='"'&&i<15)pl[i++]=*pp++;}
        if(sn>=1&&sn<=TOTAL_SLOTS){
            slotTrans(sn,isE?SLOT_OCCUPIED:SLOT_RELEASED,pl);
            int f=0; for(int i=1;i<=TOTAL_SLOTS;i++)if(gSlots[i].state==SLOT_EMPTY)f++;
            gFree=f; gFull=(f==0);
        }
    }
    // Reservation commands from server: {"slot":N,"action":"RESERVE"|"CANCEL","plate":"..."}
    if(strstr(topic,"/reserve")){
        char*sp=strstr(msg,"\"slot\":"),*ap=strstr(msg,"\"action\":\"");
        if(!sp||!ap)return;
        int sn=(int)strtol(sp+7,nullptr,10);
        bool isR=strstr(ap,"RESERVE")!=nullptr, isC=strstr(ap,"CANCEL")!=nullptr;
        char pl[16]={}; char*pp=strstr(msg,"\"plate\":\"");
        if(pp){pp+=9;int i=0;while(*pp&&*pp!='"'&&i<15)pl[i++]=*pp++;}
        if(sn>=1&&sn<=TOTAL_SLOTS){
            if(isR)slotTrans(sn,SLOT_RESERVED,pl);
            if(isC&&gSlots[sn].state==SLOT_RESERVED)slotTrans(sn,SLOT_EMPTY,"");
            int f=0; for(int i=1;i<=TOTAL_SLOTS;i++)if(gSlots[i].state==SLOT_EMPTY)f++;
            gFree=f; gFull=(f==0);
        }
    }
    // Always refresh LEDs and push update after any MQTT slot change
    extern void updateLEDs(); extern void sendSlotUpdate();
    updateLEDs(); sendSlotUpdate();
}

void checkMQTT(){
    if(mqttClient.connected()||WiFi.status()!=WL_CONNECTED)return;
    char cid[36]; snprintf(cid,sizeof(cid),"iprs-wroom-%s-%04lX",LANE,millis()&0xFFFF);
    bool ok=(MQTT_USER[0])?mqttClient.connect(cid,MQTT_USER,MQTT_PASS):mqttClient.connect(cid);
    if(ok){
        DLOG("[MQTT]","Connected");
        mqttClient.subscribe(topicGate);
        mqttClient.subscribe("iprs/vision/plate");
        char topicRes[60]; snprintf(topicRes,sizeof(topicRes),"iprs/slot/%s/reserve",LANE);
        mqttClient.subscribe(topicRes);
        snprintf(gJ,sizeof(gJ),"{\"board\":\"" BOARD_ID "\",\"fw\":\"%s\",\"ip\":\"%s\",\"online\":true}",FIRMWARE_VER,gIP);
        mqttPub(topicStat,gJ);
    }else DLOGF("[MQTT]","Failed rc=%d",mqttClient.state());
}

void initMQTT(){
    snprintf(topicSlot,sizeof(topicSlot),"iprs/slot/%s/state",LANE);
    snprintf(topicGate,sizeof(topicGate),"iprs/gate/%s/cmd",  LANE);
    snprintf(topicStat,sizeof(topicStat),"iprs/wroom/%s/status",LANE);
    snprintf(topicIR,  sizeof(topicIR),  "iprs/ir/%s",        LANE);
    mqttClient.setServer(MQTT_BROKER,MQTT_PORT);
    mqttClient.setCallback(mqttCB);
    mqttClient.setKeepAlive(30); mqttClient.setSocketTimeout(5);
    checkMQTT();
}

// ═══ WIFI ══════════════════════════════════════════════════════════════
void initWiFi(){
    Serial.printf("[WIFI] Connecting to \"%s\"",WIFI_SSID);
    WiFi.mode(WIFI_STA); WiFi.setAutoReconnect(true); WiFi.begin(WIFI_SSID,WIFI_PASSWORD);
    uint32_t t0=millis();
    while(WiFi.status()!=WL_CONNECTED){
        if(millis()-t0>15000){Serial.println("\n[WIFI] Timeout");return;}
        delay(400); Serial.print('.');
    }
    snprintf(gIP,sizeof(gIP),"%s",WiFi.localIP().toString().c_str());
    Serial.printf("\n[WIFI] IP:%s RSSI:%ddBm\n",gIP,WiFi.RSSI());
}

void checkWifi(){
    static bool was=false; bool now=(WiFi.status()==WL_CONNECTED);
    if(!now&&was)DLOG("[WIFI]","Dropped");
    if(now&&!was){
        snprintf(gIP,sizeof(gIP),"%s",WiFi.localIP().toString().c_str());
        DLOGF("[WIFI]","Back %s",gIP);
        wsClient.disconnect();
        uint32_t tw=millis(); while(millis()-tw<80)esp_task_wdt_reset();
        wsClient.begin(SERVER_IP,SERVER_PORT,"/ws"); wsClient.onEvent(wsEvent);
        wsClient.setReconnectInterval(3000); wsClient.enableHeartbeat(15000,3000,2);
    }
    was=now;
}

// Forward declaration needed by checkWifi
void wsEvent(WStype_t,uint8_t*,size_t);

// ═══ WDT & OTA ═════════════════════════════════════════════════════════
void setupWDT(){
#if ESP_ARDUINO_VERSION_MAJOR>=3
    const esp_task_wdt_config_t c={.timeout_ms=WDT_TIMEOUT_SEC*1000U,.idle_core_mask=0,.trigger_panic=true};
    esp_task_wdt_reconfigure(&c);
#else
    esp_task_wdt_init(WDT_TIMEOUT_SEC,true);
#endif
    esp_task_wdt_add(NULL);
}

void initOTA(){
    ArduinoOTA.setHostname(OTA_HOSTNAME); ArduinoOTA.setPassword(OTA_PASSWORD);
    ArduinoOTA.onStart(  [](){esp_task_wdt_delete(NULL);DLOG("[OTA]","Start");});
    ArduinoOTA.onEnd(    [](){DLOG("[OTA]","Done");});
    ArduinoOTA.onProgress([](uint32_t,uint32_t){esp_task_wdt_reset();});
    ArduinoOTA.onError(  [](ota_error_t e){char s[24];snprintf(s,sizeof(s),"Err %u",(unsigned)e);DLOG("[OTA]",s);esp_task_wdt_add(NULL);});
    ArduinoOTA.begin();
}

// ═══ SENSORS ═══════════════════════════════════════════════════════════
static inline bool irEntry(){return digitalRead(PIN_IR_ENTRY)==LOW;}
static inline bool irSlot(uint8_t s){
    static const uint8_t P[4]={PIN_IR_SLOT1,PIN_IR_SLOT2,PIN_IR_SLOT3,PIN_IR_SLOT4};
    return(s>=1&&s<=4)&&digitalRead(P[s-1])==LOW;
}
static bool    gEntryPrev=false;  // seeded in setup()
static uint8_t gSlotPrev =0xFF;   // seeded in setup()

void pollSlotIR(){
    uint8_t mask=0;
    for(uint8_t i=1;i<=TOTAL_SLOTS;i++)if(irSlot(i))mask|=(1u<<(i-1));
    if(mask==gSlotPrev)return;
    gSlotPrev=mask;
    int f=0;
    for(uint8_t i=1;i<=TOTAL_SLOTS;i++){
        bool occ=(mask>>(i-1))&0x01;
        if(occ){slotTrans(i,SLOT_OCCUPIED,"");}
        else{
            if(gSlots[i].state==SLOT_OCCUPIED||gSlots[i].state==SLOT_RELEASED)slotTrans(i,SLOT_EMPTY,"");
            if(gSlots[i].state==SLOT_EMPTY)f++;
        }
    }
    gFree=f; gFull=(f==0);
    extern void updateLEDs(); extern void sendSlotUpdate();
    updateLEDs(); sendSlotUpdate();
}

void handleIR(){
    // ── ENTRY IR ──────────────────────────────────────────────────────────
    bool nowEntry=irEntry(), risingEntry=(nowEntry&&!gEntryPrev);
    gEntryPrev=nowEntry;
    if(risingEntry){
        uint32_t t=millis();
        if((t-tmTrig)<TRIGGER_RATELIMIT_MS){DLOG("[IR]","ENTRY rate-limited");}
        else{
            tmTrig=t; DLOG("[IR]","Vehicle at ENTRY");
            char ts[22]; getTS(ts,sizeof(ts));
            if(wsConn){
                snprintf(gJ,sizeof(gJ),"{\"type\":\"IR_TRIGGER\",\"boardId\":\"" BOARD_ID "\","
                         "\"lane\":\"ENTRY\",\"slotsLeft\":%d,\"parkingFull\":%s,\"ts\":\"%s\"}",
                         gFree,gFull?"true":"false",ts);
                wsClient.sendTXT(gJ);
            }else{
                HTTPClient h; char url[128];
                snprintf(url,sizeof(url),"http://" SERVER_IP ":" STR(SERVER_PORT) "/trigger/cam/ENTRY");
                h.begin(url); h.addHeader("Content-Type","application/json");
                snprintf(gJ,sizeof(gJ),"{\"boardId\":\"" BOARD_ID "\",\"lane\":\"ENTRY\",\"slotsLeft\":%d}",gFree);
                h.POST(gJ); h.end();
            }
            mqttPub(topicIR,"{\"trigger\":true,\"lane\":\"ENTRY\"}");
        }
    }

    // ── EXIT IR ───────────────────────────────────────────────────────────
    // FIX: EXIT IR was never polled — gate never opened on vehicle exit.
    // EXIT sequence: IR triggers → server opens EXIT gate immediately →
    // delayed 2.5s → server sends TRIGGER to EXIT cam → cam captures plate.
    static bool gExitIRPrev=false;
    bool nowExit=(digitalRead(PIN_IR_EXIT)==LOW), risingExit=(nowExit&&!gExitIRPrev);
    gExitIRPrev=nowExit;
    if(risingExit){
        // Use a separate rate-limit timer for exit so ENTRY cooldown doesn't block
        static uint32_t tmExitTrig=0;
        uint32_t t=millis();
        if((t-tmExitTrig)<TRIGGER_RATELIMIT_MS){DLOG("[IR]","EXIT rate-limited");}
        else{
            tmExitTrig=t; DLOG("[IR]","Vehicle at EXIT");
            char ts[22]; getTS(ts,sizeof(ts));
            // Send IR_TRIGGER with lane=EXIT — server will open EXIT gate + delayed cam trigger
            if(wsConn){
                snprintf(gJ,sizeof(gJ),"{\"type\":\"IR_TRIGGER\",\"boardId\":\"" BOARD_ID "\","
                         "\"lane\":\"EXIT\",\"slotsLeft\":%d,\"parkingFull\":%s,\"ts\":\"%s\"}",
                         gFree,gFull?"true":"false",ts);
                wsClient.sendTXT(gJ);
            }else{
                // HTTP fallback: hit the trigger endpoint which also fires gate open
                HTTPClient h; char url[128];
                snprintf(url,sizeof(url),"http://" SERVER_IP ":" STR(SERVER_PORT) "/trigger/cam/EXIT");
                h.begin(url); h.addHeader("Content-Type","application/json");
                snprintf(gJ,sizeof(gJ),"{\"boardId\":\"" BOARD_ID "\",\"lane\":\"EXIT\",\"slotsLeft\":%d}",gFree);
                h.POST(gJ); h.end();
            }
            char topicExitIR[56];
            snprintf(topicExitIR,sizeof(topicExitIR),"iprs/ir/EXIT");
            mqttPub(topicExitIR,"{\"trigger\":true,\"lane\":\"EXIT\"}");
        }
    }
}

// ═══ SLOT UPDATE ════════════════════════════════════════════════════════
void sendSlotUpdate(){
    if(!wsConn)return;
    snprintf(gJ,sizeof(gJ),"{\"type\":\"SLOT_UPDATE\",\"boardId\":\"" BOARD_ID "\","
             "\"lane\":\"" LANE "\",\"slotStatus\":%s,\"slotsLeft\":%d,\"parkingFull\":%s}",
             slotsJson(),gFree,gFull?"true":"false");
    wsClient.sendTXT(gJ); mqttPub(topicSlot,slotsJson());
}

// ═══ LED CONTROL ════════════════════════════════════════════════════════
// LEDC pins (indices 0-7)
static const uint8_t LP[8]={PIN_LED_GRN_SLOT3,PIN_LED_GRN_SLOT4,PIN_LED_GRN_WIFI,PIN_LED_GRN_POWER,
                              PIN_LED_RED_SLOT1,PIN_LED_RED_SLOT2,PIN_LED_RED_SLOT3,PIN_LED_RED_SLOT4};
static const uint8_t LC[8]={LEDC_CH_GRN_S3,LEDC_CH_GRN_S4,LEDC_CH_GRN_WIFI,LEDC_CH_GRN_PWR,
                              LEDC_CH_RED_S1,LEDC_CH_RED_S2,LEDC_CH_RED_S3,LEDC_CH_RED_S4};
// Boot-strapping pin indices: slot1=GPIO15, slot2=GPIO2
static const uint8_t LD[2]={PIN_LED_GRN_SLOT1,PIN_LED_GRN_SLOT2};

static inline void lw(uint8_t idx,uint8_t duty){
#if ESP_ARDUINO_VERSION_MAJOR>=3
    ledcWrite(LP[idx],duty);
#else
    ledcWrite(LC[idx],duty);
#endif
}

// Blink engine state
static uint8_t  gBlinkMask=0;
static bool     gBlinkOn  =false;
static uint32_t tmBlink   =0;

void initLEDs(){
    for(uint8_t i=0;i<2;i++){pinMode(LD[i],OUTPUT);digitalWrite(LD[i],LOW);}
#if ESP_ARDUINO_VERSION_MAJOR>=3
    for(uint8_t i=0;i<8;i++)ledcAttach(LP[i],LED_PWM_FREQ,LED_PWM_RES);
#else
    for(uint8_t i=0;i<8;i++){ledcSetup(LC[i],LED_PWM_FREQ,LED_PWM_RES);ledcAttachPin(LP[i],LC[i]);}
#endif
}

/*
 * LED patterns per slot state:
 *   EMPTY    → Green SOLID(200)   Red OFF(0)
 *   OCCUPIED → Green OFF(0)       Red SOLID(200)
 *   RESERVED → Green BLINK 1Hz   Red DIM(40)   ← new NEW-1
 *   RELEASED → same as EMPTY (transient)
 *
 * Slots 1+2: boot-strapping GPIO → plain digitalWrite for green.
 * Slots 3+4: LEDC PWM for green; LEDC for red (all 4 slots).
 */
void updateLEDs(){
    gBlinkMask=0;
    for(uint8_t i=0;i<TOTAL_SLOTS;i++){
        SlotState st=gSlots[i+1].state;
        bool occ=(st==SLOT_OCCUPIED), res=(st==SLOT_RESERVED);
        if(res)gBlinkMask|=(1u<<i);
        // Green LED
        if(i<2){
            // boot-strapping pin — digitalWrite
            digitalWrite(LD[i],occ||res?LOW:HIGH);  // OFF for occ/res, ON for empty
            // NOTE: for RESERVED, tickLEDs will toggle this pin at 1Hz
        }else{
            uint8_t gi=i-2;  // LEDC index 0 or 1
            lw(gi, occ?0 : (res?0:LED_FULL));  // start in OFF phase for RESERVED
        }
        // Red LED (LEDC indices 4-7 for all 4 slots)
        lw(4+i, occ?LED_FULL : (res?LED_RES_RED:0));
    }
    lw(2,(WiFi.status()==WL_CONNECTED)?LED_WIFI_ON:0);  // WiFi indicator
    lw(3,LED_PWR_ON);                                    // Power — always on
}

/*
 * tickLEDs() — call every loop() cycle.
 * Toggles green LEDs for RESERVED slots at BLINK_HALF_MS rate.
 * Zero heap: uses static bitmask + millis() compare only.
 */
void tickLEDs(){
    if(!gBlinkMask)return;
    uint32_t now=millis();
    if((now-tmBlink)<BLINK_HALF_MS)return;
    tmBlink=now; gBlinkOn=!gBlinkOn;
    for(uint8_t i=0;i<TOTAL_SLOTS;i++){
        if(!((gBlinkMask>>i)&0x01))continue;
        if(i<2){ digitalWrite(LD[i],gBlinkOn?HIGH:LOW); }
        else   { lw(i-2,gBlinkOn?LED_FULL:0); }
    }
}

void pulseIndicator(){
    digitalWrite(PIN_INDICATOR,HIGH);
    vTaskDelay(pdMS_TO_TICKS(100));
    digitalWrite(PIN_INDICATOR,LOW);
}

// ═══ SERVO / GATE ═══════════════════════════════════════════════════════
void servoTask(void*param){
    int tgt=(int)(intptr_t)param;
    int cur=(tgt==SERVO_OPEN_DEG)?SERVO_CLOSED_DEG:SERVO_OPEN_DEG;
    int step=(tgt>cur)?1:-1;
    while(cur!=tgt){cur+=step;gateServo.write(cur);vTaskDelay(pdMS_TO_TICKS(SERVO_STEP_DELAY_MS));}
    if(tgt==SERVO_OPEN_DEG){
        CS_ENTER();vGateIsOpen=true;CS_EXIT();
        uint32_t hs=millis();
        while(millis()-hs<(uint32_t)SERVO_HOLD_MS){esp_task_wdt_reset();vTaskDelay(pdMS_TO_TICKS(200));}
        int p=SERVO_OPEN_DEG;
        while(p>SERVO_CLOSED_DEG){p--;gateServo.write(p);vTaskDelay(pdMS_TO_TICKS(SERVO_STEP_DELAY_MS));}
        CS_ENTER();vGateIsOpen=false;CS_EXIT();
    }else{CS_ENTER();vGateIsOpen=false;CS_EXIT();}
    CS_ENTER();vServoActive=false;CS_EXIT();
    vTaskDelete(NULL);
}

void openGate(){
    bool run; CS_ENTER();run=vServoActive;CS_EXIT();
    if(run){DLOG("[SERVO]","Busy");return;}
    uint32_t n=millis();
    if((n-tmGate)<GATE_CMD_DEBOUNCE_MS){DLOG("[GATE]","Debounce");return;}
    tmGate=n; CS_ENTER();vServoActive=true;CS_EXIT();
    xTaskCreatePinnedToCore(servoTask,"srvOpen",4096,(void*)(intptr_t)SERVO_OPEN_DEG,1,NULL,0);
    updateLEDs(); sendSlotUpdate();
    snprintf(gJ,sizeof(gJ),"{\"type\":\"GATE_EVENT\",\"boardId\":\"" BOARD_ID "\","
             "\"lane\":\"" LANE "\",\"event\":\"GATE_OPENED\"}");
    if(wsConn)wsClient.sendTXT(gJ);
    mqttPub(topicStat,"{\"gate\":\"open\"}");
    pulseIndicator(); DLOG("[GATE]","Opening");
}

void closeGate(){
    bool run; CS_ENTER();run=vServoActive;CS_EXIT();
    if(run){DLOG("[SERVO]","Busy");return;}
    tmGate=0; CS_ENTER();vServoActive=true;CS_EXIT();
    xTaskCreatePinnedToCore(servoTask,"srvClose",4096,(void*)(intptr_t)SERVO_CLOSED_DEG,1,NULL,0);
    updateLEDs(); mqttPub(topicStat,"{\"gate\":\"closed\"}"); pulseIndicator(); DLOG("[GATE]","Closing");
}

// ═══ WEBSOCKET ══════════════════════════════════════════════════════════
void wsEvent(WStype_t type,uint8_t*payload,size_t len){
    switch(type){
    case WStype_CONNECTED:{
        wsConn=true;
        char h[220];
        snprintf(h,sizeof(h),"{\"type\":\"HELLO\",\"boardId\":\"" BOARD_ID "\","
                 "\"firmware\":\"%s\",\"ip\":\"%s\",\"rssi\":%d,\"lane\":\"" LANE "\"}",
                 FIRMWARE_VER,gIP,(int)WiFi.RSSI());
        wsClient.sendTXT(h); DLOG("[WS]","Connected"); break;
    }
    case WStype_DISCONNECTED: wsConn=false; DLOG("[WS]","Disconnected"); break;
    case WStype_TEXT:{
        char*r=(char*)payload;
        if     (strstr(r,"\"GATE_OPEN\"")) {CS_ENTER();vOpen =true;CS_EXIT();}
        else if(strstr(r,"\"GATE_CLOSE\"")){CS_ENTER();vClose=true;CS_EXIT();}  // FIX: removed extra stray " that caused compile error
        else if(strstr(r,"\"SLOT_SET\"")){ // {"type":"SLOT_SET","slot":2,"state":"RESERVED","plate":"MH12"}
            char*sp=strstr(r,"\"slot\":"),*stp=strstr(r,"\"state\":\"");
            if(sp&&stp){
                int sn=(int)strtol(sp+7,nullptr,10); stp+=9;
                SlotState ns=SLOT_EMPTY;
                if(strncmp(stp,"OCCUPIED",8)==0)ns=SLOT_OCCUPIED;
                if(strncmp(stp,"RESERVED",8)==0)ns=SLOT_RESERVED;
                if(strncmp(stp,"RELEASED",8)==0)ns=SLOT_RELEASED;
                char pl[16]={}; char*pp=strstr(r,"\"plate\":\"");
                if(pp){pp+=9;int i=0;while(*pp&&*pp!='"'&&i<15)pl[i++]=*pp++;}
                slotTrans(sn,ns,pl); sendSlotUpdate(); updateLEDs();
            }
        }
        break;
    }
    default:break;
    }
}

void initWS(){
    wsClient.disconnect();
    uint32_t tw=millis(); while(millis()-tw<50)esp_task_wdt_reset();
    wsClient.begin(SERVER_IP,SERVER_PORT,"/ws"); wsClient.onEvent(wsEvent);
    wsClient.setReconnectInterval(3000); wsClient.enableHeartbeat(15000,3000,2);
}

// ═══ HTTP SERVER ════════════════════════════════════════════════════════
void initHTTP(){
    const char*hk[]={"X-Api-Key","X-Board-Id","Authorization"};
    wroomServer.collectHeaders(hk,3);
    wroomServer.on("/gate/open", HTTP_POST,[](){ 
        if(!wroomServer.hasHeader("X-Api-Key")||strcmp(wroomServer.header("X-Api-Key").c_str(),API_KEY)!=0){
            wroomServer.send(401,"application/json","{\"error\":\"unauthorized\"}");return;}
        CS_ENTER();vOpen=true;CS_EXIT();
        wroomServer.send(200,"application/json","{\"status\":\"ok\",\"action\":\"GATE_OPEN\"}"); });
    wroomServer.on("/gate/close",HTTP_POST,[](){ 
        if(!wroomServer.hasHeader("X-Api-Key")||strcmp(wroomServer.header("X-Api-Key").c_str(),API_KEY)!=0){
            wroomServer.send(401,"application/json","{\"error\":\"unauthorized\"}");return;}
        CS_ENTER();vClose=true;CS_EXIT();
        wroomServer.send(200,"application/json","{\"status\":\"ok\",\"action\":\"GATE_CLOSE\"}"); });
    wroomServer.on("/status",HTTP_GET,[](){
        bool go;CS_ENTER();go=vGateIsOpen;CS_EXIT();
        char up[16];getUptime(up,sizeof(up));
        char buf[420];
        snprintf(buf,sizeof(buf),
                 "{\"boardId\":\"" BOARD_ID "\",\"lane\":\"" LANE "\","
                 "\"gateOpen\":%s,\"slotsLeft\":%d,\"parkingFull\":%s,"
                 "\"slotStatus\":%s,\"firmware\":\"%s\",\"uptime\":%s,"
                 "\"heap\":%u,\"rssi\":%d,\"ip\":\"%s\","
                 "\"mqttUp\":%s,\"wsUp\":%s}",
                 go?"true":"false",gFree,gFull?"true":"false",slotsJson(),
                 FIRMWARE_VER,up,(unsigned)ESP.getFreeHeap(),(int)WiFi.RSSI(),gIP,
                 mqttClient.connected()?"true":"false",wsConn?"true":"false");
        wroomServer.send(200,"application/json",buf); });
    wroomServer.onNotFound([](){wroomServer.send(404,"application/json","{\"error\":\"not_found\"}");});
    wroomServer.begin(); DLOG("[HTTP]","WROOM server port " STR(WROOM_HTTP_PORT));
}

// ═══ HEARTBEAT ══════════════════════════════════════════════════════════
void sendHB(){
    if(WiFi.status()!=WL_CONNECTED)return;
    if(ESP.getFreeHeap()<HEAP_MIN_BYTES){DLOG("[HB]","Low heap");return;}
    char up[16];getUptime(up,sizeof(up));
    char rs[8];snprintf(rs,sizeof(rs),"%d",(int)WiFi.RSSI());
    char url[128];snprintf(url,sizeof(url),"http://" SERVER_IP ":" STR(SERVER_PORT) "/heartbeat/wroom/%s",LANE);
    HTTPClient h; h.begin(url);
    h.addHeader("X-Board-Id",BOARD_ID); h.addHeader("X-Firmware",FIRMWARE_VER);
    h.addHeader("X-Uptime",up); h.addHeader("X-Rssi",rs); h.addHeader("X-Ip",gIP);
    h.setTimeout(4000); int code=h.GET(); h.end();
    DLOGF("[HB]","HTTP %d heap=%u",code,(unsigned)ESP.getFreeHeap());
    snprintf(gJ,sizeof(gJ),"{\"uptime\":%s,\"rssi\":%s,\"heap\":%u,\"gate\":%s}",
             up,rs,(unsigned)ESP.getFreeHeap(),vGateIsOpen?"true":"false");
    mqttPub(topicStat,gJ);
}

// ═══ RTC ════════════════════════════════════════════════════════════════
void syncRTC(){
    configTime(19800,0,"pool.ntp.org","time.google.com");
    struct tm ti; uint32_t t0=millis();
    while(!getLocalTime(&ti)&&(millis()-t0<6000)){esp_task_wdt_reset();vTaskDelay(pdMS_TO_TICKS(300));}
    rtcOk=getLocalTime(&ti);
    if(rtcOk){char b[24];strftime(b,sizeof(b),"%Y-%m-%d %H:%M:%S",&ti);DLOG("[RTC]",b);}
    else DLOG("[RTC]","NTP fail — offline clock");
}

// ═══ SETUP ══════════════════════════════════════════════════════════════
void setup(){
    Serial.begin(115200); delay(200);
    Serial.println(F("\n[BOOT] IPRS v17.2 WROOM"));

    // IR pins — configure BEFORE reading initial state (FIX-1 / FIX-2)
    pinMode(PIN_IR_ENTRY,INPUT_PULLUP); pinMode(PIN_IR_EXIT,INPUT_PULLUP);
    pinMode(PIN_IR_SLOT1,INPUT_PULLUP); pinMode(PIN_IR_SLOT2,INPUT_PULLUP);
    pinMode(PIN_IR_SLOT3,INPUT_PULLUP); pinMode(PIN_IR_SLOT4,INPUT_PULLUP);
    pinMode(PIN_INDICATOR,OUTPUT); digitalWrite(PIN_INDICATOR,LOW);

    initLEDs(); updateLEDs();
    gateServo.attach(PIN_SERVO); gateServo.write(SERVO_CLOSED_DEG); delay(400);

    initWiFi(); syncRTC(); setupWDT();
    initHTTP(); initWS(); initMQTT(); initOTA();

    for(int i=1;i<=TOTAL_SLOTS;i++){memset(&gSlots[i],0,sizeof(ParkingSlot));gSlots[i].state=SLOT_EMPTY;}

    bootMs=millis();
    tmHB  =bootMs-HEARTBEAT_MS  +5000;
    tmSlot=bootMs-SLOT_PUBLISH_MS+2000;

    // ── FIX-1: Seed entry edge detector from real hardware ──────────────
    // Without this a blocked beam at boot fires a false RISING edge → gate opens.
    gEntryPrev=irEntry();

    // ── FIX-2: Seed slot IR mask and prime slot state from hardware ──────
    gSlotPrev=0;
    int f=TOTAL_SLOTS;
    for(uint8_t i=1;i<=TOTAL_SLOTS;i++){
        if(irSlot(i)){gSlotPrev|=(1u<<(i-1));slotTrans(i,SLOT_OCCUPIED,"");f--;}
    }
    gFree=f; gFull=(f==0);
    updateLEDs();  // reflect boot-time occupied slots immediately

    sendHB(); sendSlotUpdate();
    DLOGF("[BOOT]","v17.2 ready beam=%s slotMask=0x%02X free=%d",
          gEntryPrev?"BLOCKED":"clear",gSlotPrev,gFree);
}

// ═══ LOOP ═══════════════════════════════════════════════════════════════
void loop(){
    esp_task_wdt_reset();

    wsClient.loop();
    wroomServer.handleClient();
    mqttClient.loop();
    ArduinoOTA.handle();

    tickLEDs();   // 1 Hz blink engine for RESERVED slots — cost ≈ 1 µs when idle

    bool doOpen=false,doClose=false;
    CS_ENTER();
    if(vOpen) {doOpen =true;vOpen =false;}
    if(vClose){doClose=true;vClose=false;}
    CS_EXIT();
    if(doOpen) openGate();
    if(doClose)closeGate();

    handleIR();
    pollSlotIR();

    uint32_t now=millis();
    if(now-tmHB  >=HEARTBEAT_MS)    {tmHB  =now;sendHB();}
    if(now-tmMQTT>=MQTT_RECONNECT_MS){tmMQTT=now;checkMQTT();}
    if(now-tmWifi>=WIFI_CHECK_MS)   {tmWifi=now;checkWifi();}
    if(now-tmNTP >=NTP_RESYNC_MS)   {tmNTP =now;syncRTC();}
    if(now-tmSlot>=SLOT_PUBLISH_MS) {tmSlot=now;sendSlotUpdate();}
    if(!wsConn&&logCnt>0&&(now-tmHB>800))flushLog();
}
// END IPRS v17.2

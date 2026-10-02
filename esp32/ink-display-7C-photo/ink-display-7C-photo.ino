#include <WiFi.h>
#include <WebServer.h>
#include <HTTPClient.h>
#include <Preferences.h>
#include <SPI.h>
#include <time.h>
#include "esp_heap_caps.h"
#include "esp_system.h"
#include "esp_sleep.h"

#include <GxEPD2_4C.h>
#include <HardwareSerial.h>
#include "esp_wifi.h"
#include "esp_bt.h"

#include "driver/gpio.h"
#include "driver/rtc_io.h"

#include "battery_icons.h"

// =======================
//  调试开关（需要串口时改成 1）
// =======================
#define DEBUG_LOG 0

HardwareSerial DebugSerial(0);

#if DEBUG_LOG
  #define DBG_BEGIN()    DebugSerial.begin(115200)
  #define DBG_PRINT(x)   DebugSerial.print(x)
  #define DBG_PRINTLN(x) DebugSerial.println(x)
#else
  #define DBG_BEGIN()
  #define DBG_PRINT(x)
  #define DBG_PRINTLN(x)
#endif

#ifndef LED_BUILTIN
#define LED_BUILTIN 2
#endif

// =======================
//  恢复出厂设置：上电时按下 GPIO38 -> 清 NVS 中的 WiFi/配置，并进入 AP 配网
// =======================
#define PIN_FACTORY_RESET 38
#define FACTORY_RESET_ACTIVE_LOW 1
static const uint32_t FACTORY_RESET_SAMPLE_DELAY_MS = 5;

// =======================
//  AP 配置页保底：进入 AP 后 5 分钟没保存配置 -> 睡到“下一个刷新点”
// =======================
static const uint32_t AP_TIMEOUT_MS = 5UL * 60UL * 1000UL; // 5 分钟

// =======================
//  墨水屏参数 & 引脚
// =======================
// 逻辑分辨率：竖屏 480x800
static const int EPD_WIDTH  = 800;
static const int EPD_HEIGHT = 480;
static const int FB_WIDTH   = 480;
static const int FB_HEIGHT  = 800;

// SPI引脚 (重映射: BUSY=9, RST=12, DC=11, CS=10, SCK=14, MOSI=13)
#define PIN_EPD_BUSY 9
#define PIN_EPD_RST  12
#define PIN_EPD_DC   11
#define PIN_EPD_CS   10
#define PIN_EPD_SCLK 14
#define PIN_EPD_DIN  13

// =======================
//  电池电量检测 (1S LiPo 3.7V 8000mAh, 100k+100k 1:2 分压到 ADC)
// =======================
// 分压前电池电压: 4.20V (满) -> 3.00V (空)
// 分压后 ADC 端电压: 2.10V -> 1.50V
// ADC 引脚: GPIO6 (ADC1_CH5 on ESP32-S3, ADC2 在 WiFi 占用时不可用)
#define PIN_BATTERY_ADC   6
#define BATTERY_ADC_ATTEN ADC_11db          // ESP32-S3 上对应 0-3.3V 量程 (Arduino core 3.x 改名为 ADC_11db)
#define BATTERY_DIVIDER_RATIO 2.0f         // V_adc/V_bat = 100k/200k = 0.5 -> V_bat = V_adc * 2
#define BATTERY_ADC_SAMPLES 16

// 校准因子 — 补偿 ADC eFuse 误差 + 电阻公差
// 校准步骤:
//   1. 烧固件, 串口 log 看 [BAT] adcMv=XXXX (这是 ADC 引脚实测 mV, 已扣分压)
//   2. 用万用表实测电池两端 V_actual
//   3. factor = V_actual / (adcMv × 2.0)   ← 直接基于原始 adcMv 算, 不受上一次的 cal 影响
//   4. 改这个常量, 重刷
// 例: adcMv=2048, 万用表=4.126V → factor = 4.126 / (2048×2.0/1000) = 4.126 / 4.096 = 1.0073
#define BATTERY_CALIBRATION_FACTOR 1.0150f  // ← 改这个值校准

// LiPo 放电曲线 -> 11 段离散等级 (0,10,20,...,100)
// 阈值数组是分压前 (电池端) 电压 mV，从高到低排列
static const uint16_t BATTERY_LEVEL_MV[11] = {
  4125,  // 100% (>= 4.125V)
  3950,  //  90% (>= 3.95V)
  3850,  //  80% (>= 3.85V)
  3780,  //  70% (>= 3.78V)
  3720,  //  60% (>= 3.72V)
  3660,  //  50% (>= 3.66V)
  3600,  //  40% (>= 3.60V)
  3550,  //  30% (>= 3.55V)
  3450,  //  20% (>= 3.45V)
  3300,  //  10% (>= 3.30V)
  3000,  //   0% (<  3.30V; 3.00V 为截止/关机电压)
};
static const uint8_t  BATTERY_LEVEL_PCT[11] = { 100, 90, 80, 70, 60, 50, 40, 30, 20, 10, 0 };

// 屏幕: GDEM075F52 (480x800 4色 panel, GDEM 系列 50ms reset)
// 4 色 panel class + GDEM 驱动。如果换用其它屏幕，请自行修改此处
GxEPD2_4C<
  GxEPD2_750c_GDEM075F52,
  GxEPD2_750c_GDEM075F52::HEIGHT / 4
> display(
  GxEPD2_750c_GDEM075F52(
    PIN_EPD_CS,
    PIN_EPD_DC,
    PIN_EPD_RST,
    PIN_EPD_BUSY
  )
);

// =======================
//  静态每日相册 BIN 路径前缀，建议修改，防止隐私泄露。需同步修改 config.py 中的 DOWNLOAD_KEY。
// =======================
#define DAILY_PHOTO_PATH_PREFIX "/static/inktime/CHANGEME_32_HEX_DOWNLOAD_KEY/photo_"
#define DAILY_PHOTO_COUNT       10   // 0..9

// =======================
//  配置存储 / WiFi / WebServer
// =======================
Preferences prefs;
WebServer  server(80);

struct Config {
  String  wifi_ssid;
  String  wifi_pass;
  String  backend_hostport;
  int32_t tz_offset_hours;   // legacy: 静态时区偏移,只在 tz_label 为空时使用
  String  tz_label;          // 选自 TZ_TABLE[i].label,带 DST 规则
  uint8_t refresh_hour;
  bool    rotate180;
  bool    valid;
};

const char*  DEFAULT_HOSTPORT = "";
const int32_t DEFAULT_TZ      = 8;
const uint8_t DEFAULT_HOUR    = 8;

Config g_cfg;
uint8_t* framebuffer = nullptr;

// =======================
//  时区表 (北美优先,含 DST 规则)
//  偏移量:东正西负,以秒计(与 configTime 的 gmtOffset_sec 同向)
//  DST 规则:美加 2007 年后统一(3 月第 2 个周日 -> 11 月第 1 个周日)
//  没有 DST 的地区 hasDst=false, dstOffsetSec 等于 stdOffsetSec
// =======================
struct TzInfo {
  const char* iana;           // IANA 时区名(用于 IP 检测匹配)
  const char* label;          // 下拉框里显示的完整标签(也是 NVS 里存的 key)
  int         stdOffsetSec;   // 标准时间偏移 (UTC+0 为 0)
  int         dstOffsetSec;   // 夏令时偏移
  bool        hasDst;         // 是否观察 DST
  const char* posix;          // POSIX TZ 字符串(参考,运行时不用)
};
static const TzInfo TZ_TABLE[] = {
  // 美东
  {"America/Halifax",     "America/Halifax (AST/ADT, PEI/NS/NB)",   -14400, -10800, true,  "AST4ADT,M3.2.0,M11.1.0"},
  {"America/St_Johns",    "America/St_Johns (NST/NDT, NL)",         -12600,  -9000, true,  "NST3:30NDT,M3.2.0,M11.1.0"},
  {"America/New_York",    "America/New_York (EST/EDT)",             -18000, -14400, true,  "EST5EDT,M3.2.0,M11.1.0"},
  {"America/Toronto",     "America/Toronto (EST/EDT, ON)",          -18000, -14400, true,  "EST5EDT,M3.2.0,M11.1.0"},
  // 美中
  {"America/Chicago",     "America/Chicago (CST/CDT)",              -21600, -18000, true,  "CST6CDT,M3.2.0,M11.1.0"},
  {"America/Winnipeg",    "America/Winnipeg (CST/CDT, MB)",         -21600, -18000, true,  "CST6CDT,M3.2.0,M11.1.0"},
  // 美山
  {"America/Denver",      "America/Denver (MST/MDT)",               -25200, -21600, true,  "MST7MDT,M3.2.0,M11.1.0"},
  {"America/Phoenix",     "America/Phoenix (MST, no DST, AZ)",      -25200, -25200, false, "MST7"},
  // 美西
  {"America/Los_Angeles", "America/Los_Angeles (PST/PDT)",          -28800, -25200, true,  "PST8PDT,M3.2.0,M11.1.0"},
  {"America/Vancouver",   "America/Vancouver (PST/PDT, BC)",        -28800, -25200, true,  "PST8PDT,M3.2.0,M11.1.0"},
  // 北美其它
  {"America/Anchorage",   "America/Anchorage (AKST/AKDT)",          -32400, -28800, true,  "AKST9AKDT,M3.2.0,M11.1.0"},
  {"Pacific/Honolulu",    "Pacific/Honolulu (HST, no DST)",         -36000, -36000, false, "HST10"},
  // 兜底
  {"UTC",                 "UTC",                                         0,      0,  false, "UTC0"},
  {"Asia/Shanghai",       "Asia/Shanghai (CST, no DST, 中国)",       28800,  28800, false, "CST-8"},
};
static const size_t TZ_TABLE_LEN = sizeof(TZ_TABLE) / sizeof(TZ_TABLE[0]);

// 按 label 查 TzInfo(线性查表,只有十几条,够用)
static const TzInfo* lookupTz(const String &label) {
  if (label.isEmpty()) return nullptr;
  for (size_t i = 0; i < TZ_TABLE_LEN; ++i) {
    if (label.equals(TZ_TABLE[i].label)) return &TZ_TABLE[i];
  }
  return nullptr;
}

// 按 IANA 名查 TzInfo(用于 IP 检测回填)
static const TzInfo* lookupTzByIana(const String &iana) {
  if (iana.isEmpty()) return nullptr;
  for (size_t i = 0; i < TZ_TABLE_LEN; ++i) {
    if (iana.equals(TZ_TABLE[i].iana)) return &TZ_TABLE[i];
  }
  return nullptr;
}

// 北美 DST 判断:3 月第 2 个周日(含) -> 11 月第 1 个周日(不含)
// 精度到"天",2 AM 边界的 1 小时误差对每日 8 AM 唤醒画屏可忽略
static bool isNorthAmericaDST(time_t nowUtc) {
  if (nowUtc <= 0) return false;
  struct tm utc;
  gmtime_r(&nowUtc, &utc);
  int year = utc.tm_year + 1900;
  if (year < 2007) return false;  // 2007 年前规则不同,不考虑

  // 算 3 月第 2 个周日 和 11 月第 1 个 周日 的"年中第几天"
  auto dayOfYear = [](int y, int m, int d) {
    static const int cum[] = {0,31,59,90,120,151,181,212,243,273,304,334};
    int doy = cum[m-1] + d;
    if (m > 2 && ((y%4==0 && y%100!=0) || y%400==0)) doy++;
    return doy;
  };
  auto weekdayOn1st = [](int y, int m) {
    // Zeller-like: 求某月 1 日是星期几 (0=Sun..6=Sat)
    static const int t[] = {0,3,2,5,0,3,5,1,4,6,2,4};
    int yy = y - (m < 3);
    int w = (yy + yy/4 - yy/100 + yy/400 + t[m-1] + 1) % 7;
    return w;  // 0=Sun
  };
  int firstSunMar = (7 - weekdayOn1st(year, 3)) % 7 + 1;  // 1..7
  int firstSunNov = (7 - weekdayOn1st(year, 11)) % 7 + 1;
  int dstStart = dayOfYear(year, 3, firstSunMar + 7);  // 2nd Sunday of March
  int dstEnd   = dayOfYear(year, 11, firstSunNov);     // 1st Sunday of November

  int doy = dayOfYear(year, utc.tm_mon + 1, utc.tm_mday);
  return (doy >= dstStart) && (doy < dstEnd);
}

// 计算当前应使用的 UTC 偏移(秒,东正)
// 优先用 tz_label(DST 自动),否则用老字段 tz_offset_hours
static int computeUtcOffsetSec(const Config &cfg) {
  const TzInfo* tz = lookupTz(cfg.tz_label);
  if (tz) {
    if (!tz->hasDst) return tz->stdOffsetSec;
    time_t nowUtc = time(nullptr);
    return isNorthAmericaDST(nowUtc) ? tz->dstOffsetSec : tz->stdOffsetSec;
  }
  return (int)cfg.tz_offset_hours * 3600;
}

// 把当前时区应用到 lwIP/SNTP 层;setup() 早期 + 任何 getLocalTime 前都要调一次
static void applyTimezone(const Config &cfg) {
  int offsetSec = computeUtcOffsetSec(cfg);
  configTime(offsetSec, 0, "pool.ntp.org", "time.nist.gov", "ntp.aliyun.com");
#if DEBUG_LOG
  DBG_PRINT("[TIME] tz="); DBG_PRINT(cfg.tz_label.isEmpty() ? String("(legacy)") : cfg.tz_label);
  DBG_PRINT(" offsetSec="); DBG_PRINTLN(offsetSec);
#endif
}

static void releaseAllGpioHoldsAtBoot() {
  gpio_deep_sleep_hold_dis();
  for (int gpio = 0; gpio <= 48; ++gpio) {
    gpio_num_t gn = (gpio_num_t)gpio;
    if (!GPIO_IS_VALID_GPIO(gn)) continue;
    gpio_hold_dis(gn);
    if (rtc_gpio_is_valid_gpio(gn)) rtc_gpio_hold_dis(gn);
  }
}

static void clearConfigNVS() {
#if DEBUG_LOG
  DBG_PRINTLN("[NVS] clearConfigNVS()");
#endif
  prefs.begin("dashcfg", false);
  prefs.clear();
  prefs.end();
}

static bool isFactoryResetRequestedAtBoot() {
  pinMode(PIN_FACTORY_RESET, INPUT_PULLUP);
  delay(FACTORY_RESET_SAMPLE_DELAY_MS);
#if FACTORY_RESET_ACTIVE_LOW
  return (digitalRead(PIN_FACTORY_RESET) == LOW);
#else
  return (digitalRead(PIN_FACTORY_RESET) == HIGH);
#endif
}

static void saveLastTimeEpoch(time_t epoch) {
  prefs.begin("dashcfg", false);
  prefs.putULong("last_epoch", (uint32_t)epoch);
  prefs.end();
#if DEBUG_LOG
  DBG_PRINT("[TIME] save last_epoch="); DBG_PRINTLN((uint32_t)epoch);
#endif
}

static bool loadLastTimeEpoch(time_t &epochOut) {
  prefs.begin("dashcfg", true);
  uint32_t v = prefs.getULong("last_epoch", 0);
  prefs.end();
  if (v == 0) return false;
  epochOut = (time_t)v;
  return true;
}

static uint32_t minutesToNextRefreshFromLastEpoch(const Config &cfg) {
  time_t lastEpoch;
  if (!loadLastTimeEpoch(lastEpoch)) {
    return 1440;
  }

  struct tm t;
  localtime_r(&lastEpoch, &t);

  int curMinOfDay = t.tm_hour * 60 + t.tm_min;
  int targetMin   = (int)cfg.refresh_hour * 60;
  int deltaMin;

  if (curMinOfDay < targetMin) deltaMin = targetMin - curMinOfDay;
  else                         deltaMin = 24 * 60 - (curMinOfDay - targetMin);

  if (deltaMin < 1) deltaMin = 24 * 60;
  if (deltaMin > 1440) deltaMin = 1440;
  return (uint32_t)deltaMin;
}

// =======================
//  配置读写
// =======================
void loadConfig(Config &cfg) {
  prefs.begin("dashcfg", true); // read-only
  cfg.wifi_ssid        = prefs.getString("ssid", "");
  cfg.wifi_pass        = prefs.getString("pass", "");
  cfg.backend_hostport = prefs.getString("hostport", DEFAULT_HOSTPORT);
  cfg.tz_offset_hours  = prefs.getInt("tz", DEFAULT_TZ);
  cfg.tz_label         = prefs.getString("tz_label", "");
  cfg.refresh_hour     = (uint8_t)prefs.getUChar("hour", DEFAULT_HOUR);
  cfg.rotate180        = prefs.getBool("rot180", false);
  prefs.end();

  cfg.valid = (cfg.wifi_ssid.length() > 0);

#if DEBUG_LOG
  DBG_PRINTLN("---- loadConfig ----");
  DBG_PRINT("[CFG] ssid="); DBG_PRINTLN(cfg.wifi_ssid);
  DBG_PRINT("[CFG] hostport="); DBG_PRINTLN(cfg.backend_hostport);
  DBG_PRINT("[CFG] tz_offset_hours="); DBG_PRINTLN(cfg.tz_offset_hours);
  DBG_PRINT("[CFG] tz_label="); DBG_PRINTLN(cfg.tz_label);
  DBG_PRINT("[CFG] refresh_hour="); DBG_PRINTLN((int)cfg.refresh_hour);
  DBG_PRINT("[CFG] rotate180="); DBG_PRINTLN(cfg.rotate180 ? "true" : "false");
  DBG_PRINT("[CFG] valid="); DBG_PRINTLN(cfg.valid ? "true" : "false");
#endif
}

void saveConfig(const Config &cfg) {
  prefs.begin("dashcfg", false);
  prefs.putString("ssid", cfg.wifi_ssid);
  prefs.putString("pass", cfg.wifi_pass);
  prefs.putString("hostport", cfg.backend_hostport);
  prefs.putInt("tz", cfg.tz_offset_hours);
  prefs.putString("tz_label", cfg.tz_label);
  prefs.putUChar("hour", cfg.refresh_hour);
  prefs.putBool("rot180", cfg.rotate180);
  prefs.end();

#if DEBUG_LOG
  DBG_PRINTLN("[CFG] saved");
#endif
}

// =======================
//  HTML 工具
// =======================
String htmlEscape(const String &s) {
  String out;
  out.reserve(s.length());
  for (size_t i = 0; i < s.length(); ++i) {
    char c = s[i];
    if      (c == '&')  out += F("&amp;");
    else if (c == '<')  out += F("&lt;");
    else if (c == '>')  out += F("&gt;");
    else if (c == '"')  out += F("&quot;");
    else                out += c;
  }
  return out;
}

static void wifiHardResetForPortal() {
#if DEBUG_LOG
  DBG_PRINTLN("[WIFI] wifiHardResetForPortal()");
#endif
  WiFi.scanDelete();
  WiFi.disconnect(true, true);
  WiFi.mode(WIFI_OFF);
  delay(200);

  WiFi.mode(WIFI_AP_STA);

  WiFi.setSleep(false);
  esp_wifi_set_ps(WIFI_PS_NONE);

  WiFi.scanDelete();
  delay(50);
}

String buildConfigPage() {
  WiFi.scanDelete();
  delay(30);

  int n = WiFi.scanNetworks(/*async=*/false, /*hidden=*/true);

#if DEBUG_LOG
  DBG_PRINT("[CFG] scanNetworks n="); DBG_PRINTLN(n);
#endif

  String curSsid  = g_cfg.wifi_ssid;
  String host     = htmlEscape(g_cfg.backend_hostport);
  int32_t tz      = g_cfg.tz_offset_hours;
  if (tz < -12 || tz > 14) tz = DEFAULT_TZ;
  String curTzLbl = g_cfg.tz_label;
  uint8_t hour    = g_cfg.refresh_hour;
  if (hour > 23) hour = DEFAULT_HOUR;
  bool rot180     = g_cfg.rotate180;

  String html;
  html.reserve(8192);

  html += F("<!DOCTYPE html><html><head><meta charset='utf-8'>");
  html += F("<meta name='viewport' content='width=device-width,initial-scale=1'>");
  html += F("<title>InkTime 设置</title></head><body>");
  html += F("<h2>InkTime 设置</h2>");
  html += F("<form method='POST' action='/save'>");

  html += F("WiFi SSID:<br>");
  html += F("<select id='ssid_select' style='width: 288px;' onchange=\"document.getElementById('ssid_input').value=this.value;\">");
  html += F("<option value=''>（手动输入或选择）</option>");
  if (n > 0) {
    for (int i = 0; i < n; ++i) {
      String s = WiFi.SSID(i);
      if (s.length() == 0) continue;
      String esc = htmlEscape(s);
      html += F("<option value='");
      html += esc;
      html += F("'");
      if (s == curSsid) html += F(" selected");
      html += F(">");
      html += esc;
      html += F("</option>");
    }
  }
  html += F("</select><br>");
  html += F("<input id='ssid_input' name='ssid' style='width: 280px;' value='");
  html += htmlEscape(curSsid);
  html += F("'><br><br>");

  html += F("密码:<br><input name='pass' type='password' style='width: 280px;'><br><br>");

  html += F("服务器 (host:port):<br><input name='hostport' size='40' value='");
  html += host;
  html += F("'><br><br>");

  html += F("每日刷新时间（0-23 点整）：<br><select name='hour'>");
  for (int h = 0; h < 24; ++h) {
    html += "<option value='";
    html += String(h);
    html += "'";
    if (h == hour) html += " selected";
    html += ">";
    html += String(h);
    html += F(" 点</option>");
  }
  html += F("</select><br><br>");

  html += F("时区（含 DST 自动切换）:<br>");
  html += F("<select name='tz_label' id='tz_label' style='width: 360px;'>");
  html += F("<option value=''>(不选,用下方手动偏移量)</option>");
  for (size_t i = 0; i < TZ_TABLE_LEN; ++i) {
    String lbl = String(TZ_TABLE[i].label);
    String esc = htmlEscape(lbl);
    html += "<option value='";
    html += esc;
    html += "'";
    if (lbl == curTzLbl) html += F(" selected");
    html += ">";
    html += esc;
    html += F("</option>");
  }
  html += F("</select>");
  html += F(" <button type='button' onclick='detectTz()'>根据 IP 自动检测</button><br>");
  html += F("<span id='tz_status' style='color:#666;font-size:12px;'></span><br>");
  html += F("<script>");
  html += F("function detectTz(){");
  html += F("var s=document.getElementById('tz_status');");
  html += F("s.textContent='检测中…';");
  html += F("fetch('/detect-tz').then(function(r){return r.text();}).then(function(t){");
  html += F("if(t==='FAIL'||t===''){s.textContent='检测失败,请手动选';s.style.color='#c00';return;}");
  html += F("var sel=document.getElementById('tz_label');");
  html += F("var hit=false;");
  html += F("for(var i=0;i<sel.options.length;i++){");
  html += F("if(sel.options[i].value===t){sel.selectedIndex=i;hit=true;break;}");
  html += F("}");
  html += F("if(hit){s.textContent='已自动选择: '+t;s.style.color='#080';}");
  html += F("else{s.textContent='检测到 '+t+' 但不在列表,请手动选';s.style.color='#c00';}");
  html += F("});}");
  html += F("</script><br>");

  html += F("手动偏移量 (仅当上面未选时区时生效, UTC+?):<br>");
  html += F("<select name='tz'>");
  for (int t = -12; t <= 14; ++t) {
    html += "<option value='";
    html += String(t);
    html += "'";
    if (t == tz) html += " selected";
    html += ">";
    if (t >= 0) html += "+";
    html += String(t);
    html += F("</option>");
  }
  html += F("</select><br><br>");

  html += F("<label><input type='checkbox' name='rot180' value='1'");
  if (rot180) html += F(" checked");
  html += F("> 画面旋转 180°</label><br><br>");

  if (n <= 0) {
    html += F("<p style='color:#c00'>未扫描到 WiFi，可直接在上方输入框手动填写 SSID。</p>");
  }

  html += F("<input type='submit' value='保存并重启'>");
  html += F("</form></body></html>");

  return html;
}

// =======================
//  WebServer 处理
// =======================
void handleRoot() {
#if DEBUG_LOG
  DBG_PRINTLN("[HTTP] GET /");
#endif
  server.send(200, "text/html; charset=utf-8", buildConfigPage());
}

void handleSave() {
#if DEBUG_LOG
  DBG_PRINTLN("[HTTP] POST /save");
#endif
  String ssid     = server.arg("ssid");
  String pass     = server.arg("pass");
  String host     = server.arg("hostport");
  String hourStr  = server.arg("hour");
  String tzLabel  = server.arg("tz_label");
  String tzLegacy = server.arg("tz");            // 老字段,只在用户没选新时区时回退
  bool rot180Req  = (server.arg("rot180") == "1");

  ssid.trim();
  host.trim();
  tzLabel.trim();

  Config newCfg = g_cfg;

  if (ssid.length() > 0) newCfg.wifi_ssid = ssid;
  if (pass.length() > 0) newCfg.wifi_pass = pass;

  newCfg.backend_hostport = host;

  // tz_label 优先:必须在 TZ_TABLE 里才接受(防止用户瞎填)
  if (!tzLabel.isEmpty() && lookupTz(tzLabel) != nullptr) {
    newCfg.tz_label = tzLabel;
  } else {
    newCfg.tz_label = "";
  }
  // 保留老字段(向后兼容;若 tz_label 非空则运行时忽略)
  int32_t tz = tzLegacy.toInt();
  if (tz < -12) tz = -12;
  if (tz > 14)  tz = 14;
  newCfg.tz_offset_hours = tz;

  int hour = hourStr.toInt();
  if (hour < 0)  hour = 0;
  if (hour > 23) hour = 23;
  newCfg.refresh_hour = (uint8_t)hour;

  newCfg.rotate180 = rot180Req;
  newCfg.valid     = (newCfg.wifi_ssid.length() > 0);

  saveConfig(newCfg);

  server.send(
    200,
    "text/html; charset=utf-8",
    F("<html><body><h3>保存成功，设备即将重启...</h3></body></html>")
  );

  delay(800);
  ESP.restart();
}

// =======================
//  IP 地理检测: GET ip-api.com/json/ -> 解析 timezone 字段
//  返回匹配的 TzInfo label,失败返回空字符串
// =======================
static String fetchTzFromIP() {
  if (WiFi.status() != WL_CONNECTED) return "";

  HTTPClient http;
  http.begin("http://ip-api.com/json/?fields=status,timezone");
  // 超时 4s:别在配网页上卡太久
  http.setTimeout(4000);
  int code = http.GET();
  if (code != HTTP_CODE_OK) {
#if DEBUG_LOG
    DBG_PRINT("[TZ] ip-api code="); DBG_PRINTLN(code);
#endif
    http.end();
    return "";
  }
  String body = http.getString();
  http.end();

  // 简单字符串解析,避开拉一个 ArduinoJson 依赖
  // 期望响应: {"status":"success","timezone":"America/Halifax"}
  // status != success 时(内网 IP / 限流)也放弃
  if (body.indexOf("\"status\":\"success\"") < 0) {
#if DEBUG_LOG
    DBG_PRINT("[TZ] ip-api not success: "); DBG_PRINTLN(body);
#endif
    return "";
  }
  int tIdx = body.indexOf("\"timezone\":\"");
  if (tIdx < 0) return "";
  tIdx += 12;
  int tEnd = body.indexOf("\"", tIdx);
  if (tEnd < 0 || tEnd <= tIdx) return "";
  String iana = body.substring(tIdx, tEnd);
  iana.trim();

  const TzInfo* tz = lookupTzByIana(iana);
  if (!tz) {
#if DEBUG_LOG
    DBG_PRINT("[TZ] no table match for iana="); DBG_PRINTLN(iana);
#endif
    return "";
  }
#if DEBUG_LOG
  DBG_PRINT("[TZ] IP detected "); DBG_PRINT(iana);
  DBG_PRINT(" -> "); DBG_PRINTLN(tz->label);
#endif
  return String(tz->label);
}

void handleDetectTz() {
#if DEBUG_LOG
  DBG_PRINTLN("[HTTP] GET /detect-tz");
#endif
  String label = fetchTzFromIP();
  if (label.isEmpty()) {
    server.send(200, "text/plain; charset=utf-8", "FAIL");
    return;
  }
  server.send(200, "text/plain; charset=utf-8", label);
}

// =======================
//  Deep Sleep 前
// =======================
void prepareDeepSleepDomains() {
  esp_sleep_pd_config(ESP_PD_DOMAIN_RTC_PERIPH,    ESP_PD_OPTION_OFF);
#if SOC_PM_SUPPORT_RTC_SLOW_MEM_PD
  // 原 ESP32 (original) 才有这个 power domain, ESP32-S3 没有
  esp_sleep_pd_config(ESP_PD_DOMAIN_RTC_SLOW_MEM,  ESP_PD_OPTION_OFF);
#endif
#if SOC_PM_SUPPORT_RTC_FAST_MEM_PD
  // 同上, ESP32-S3 上 SOC_PM_SUPPORT_RTC_FAST_MEM_PD 未定义
  esp_sleep_pd_config(ESP_PD_DOMAIN_RTC_FAST_MEM,  ESP_PD_OPTION_OFF);
#endif
}

// =======================
//  关闭墨水屏相关引脚，提升续航表现
//
//  包含引脚:
//    - EPD 6 根 (BUSY/RST/DC/CS/SCK/MOSI)
//    - 电池 ADC (GPIO6):  100k+100k 分压网络常开, ESP32 ADC 输入缓冲在 sleep 时有几 µA 漏电
//    - 出厂复位 (GPIO38): boot 后设过 INPUT_PULLUP, sleep 时需放掉
// =======================
static void powerDownEPD() {
  const int epdPins[] = {
    PIN_EPD_BUSY, PIN_EPD_RST, PIN_EPD_DC, PIN_EPD_CS, PIN_EPD_SCLK, PIN_EPD_DIN,
    PIN_BATTERY_ADC,    // GPIO6
    PIN_FACTORY_RESET,  // GPIO38
  };
  for (size_t i = 0; i < sizeof(epdPins)/sizeof(epdPins[0]); ++i) {
    int p = epdPins[i];
    pinMode(p, INPUT);
    pinMode(p, INPUT_PULLDOWN);
  }
}

static void deepSleepHoldOnlyEpdPins() {
  const int epdPins[] = {
    PIN_EPD_BUSY, PIN_EPD_RST, PIN_EPD_DC, PIN_EPD_CS, PIN_EPD_SCLK, PIN_EPD_DIN,
    PIN_BATTERY_ADC,    // GPIO6  - ADC 输入漏电
    PIN_FACTORY_RESET,  // GPIO38 - 上拉漏电
  };
  for (size_t i = 0; i < sizeof(epdPins)/sizeof(epdPins[0]); ++i) {
    gpio_num_t gn = (gpio_num_t)epdPins[i];
    if (!GPIO_IS_VALID_GPIO(gn)) continue;

    gpio_set_direction(gn, GPIO_MODE_INPUT);
    gpio_pulldown_en(gn);
    gpio_pullup_dis(gn);
    gpio_hold_en(gn);

    if (rtc_gpio_is_valid_gpio(gn)) rtc_gpio_isolate(gn);
  }
  gpio_deep_sleep_hold_en();
}

// =======================
//  Deep Sleep
// =======================
void goDeepSleepMinutes(uint32_t minutes) {
  if (minutes < 1)    minutes = 1;
  if (minutes > 1440) minutes = 1440;

#if DEBUG_LOG
  DBG_PRINT("[SLEEP] minutes="); DBG_PRINTLN((int)minutes);
#endif

  uint64_t us = (uint64_t)minutes * 60ULL * 1000000ULL;

  if (framebuffer) {
    heap_caps_free(framebuffer);
    framebuffer = nullptr;
  }

  powerDownEPD();

  WiFi.disconnect(true, true);
  WiFi.mode(WIFI_OFF);
  esp_wifi_stop();

#if defined(CONFIG_BT_ENABLED)
  esp_bt_controller_disable();
#endif

  deepSleepHoldOnlyEpdPins();

  prepareDeepSleepDomains();
  esp_sleep_enable_timer_wakeup(us);

#if DEBUG_LOG
  DBG_PRINTLN("[SLEEP] go deep sleep");
#endif
  esp_deep_sleep_start();
}

// =======================
//  启动 AP 配置模式
// =======================
void startConfigPortal() {
#if DEBUG_LOG
  DBG_PRINTLN("[CFG] enter startConfigPortal()");
#endif

  wifiHardResetForPortal();

  String apSsid     = "InkTime-" + String((uint32_t)ESP.getEfuseMac(), HEX).substring(4);
  const char* apPwd = "12345678";

  bool apOk = WiFi.softAP(apSsid.c_str(), apPwd);

#if DEBUG_LOG
  DBG_PRINT("[CFG] softAP result = "); DBG_PRINTLN(apOk ? "OK" : "FAIL");
  DBG_PRINT("[CFG] AP SSID = "); DBG_PRINTLN(apSsid);
  DBG_PRINT("[CFG] AP IP   = "); DBG_PRINTLN(WiFi.softAPIP());
#endif

  server.on("/", HTTP_GET, handleRoot);
  server.on("/save", HTTP_POST, handleSave);
  server.on("/detect-tz", HTTP_GET, handleDetectTz);
  server.begin();

  uint32_t enterMs = millis();

  for (;;) {
    server.handleClient();

    if (millis() - enterMs > AP_TIMEOUT_MS) {
#if DEBUG_LOG
      DBG_PRINTLN("[AP] timeout: no config saved");
#endif
      uint32_t mins = minutesToNextRefreshFromLastEpoch(g_cfg);
#if DEBUG_LOG
      DBG_PRINT("[AP] sleep to next refresh, minutes="); DBG_PRINTLN((int)mins);
#endif
      delay(50);
      goDeepSleepMinutes(mins);
    }

    delay(10);
  }
}

// =======================
//  WiFi 连接
// =======================
bool connectWiFi(const Config &cfg, uint32_t timeout_ms = 15000) {
#if DEBUG_LOG
  DBG_PRINTLN("[WIFI] connectWiFi()");
  DBG_PRINT("[WIFI] target ssid="); DBG_PRINTLN(cfg.wifi_ssid);
#endif

  if (cfg.wifi_ssid.isEmpty()) return false;

  WiFi.disconnect(true, true);
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(false);  // 显式关闭:不让 WiFi 栈在背后耗电重连,失败走 AP 配网

  WiFi.setSleep(true);
  WiFi.setTxPower(WIFI_POWER_8_5dBm);
  WiFi.begin(cfg.wifi_ssid.c_str(), cfg.wifi_pass.c_str());

  uint32_t start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < timeout_ms) {
    delay(200);
#if DEBUG_LOG
    DBG_PRINT(".");
#endif
  }
#if DEBUG_LOG
  DBG_PRINTLN();
#endif

  bool ok = (WiFi.status() == WL_CONNECTED);

#if DEBUG_LOG
  if (ok) {
    DBG_PRINTLN("[WIFI] connected");
    DBG_PRINT("[WIFI] IP="); DBG_PRINTLN(WiFi.localIP());
  } else {
    DBG_PRINTLN("[WIFI] connect FAILED");
  }
#endif

  return ok;
}

// =======================
//  NTP 同步时间
// =======================
bool syncTime(const Config &cfg, struct tm &outLocal) {
#if DEBUG_LOG
  DBG_PRINTLN("[TIME] syncTime start");
#endif
  applyTimezone(cfg);  // 用 tz_label(DST 自动)或老 tz_offset_hours 设置 offset

  for (int i = 0; i < 30; ++i) {
    if (getLocalTime(&outLocal)) {
#if DEBUG_LOG
      char buf[64];
      strftime(buf, sizeof(buf), "%Y-%m-%d %H:%M:%S", &outLocal);
      DBG_PRINT("[TIME] OK: "); DBG_PRINTLN(buf);
#endif
      time_t nowEpoch = time(nullptr);
      if (nowEpoch > 0) saveLastTimeEpoch(nowEpoch);
      return true;
    }
    delay(500);
  }
#if DEBUG_LOG
  DBG_PRINTLN("[TIME] syncTime FAILED");
#endif
  return false;
}

// =======================
//  下载每日相册 BIN
// =======================
bool downloadDailyPhotoBin(const Config &cfg) {
  size_t target = (size_t)FB_WIDTH * FB_HEIGHT; // 384000 bytes

  if (!framebuffer) {
#if DEBUG_LOG
    DBG_PRINT("[FB] malloc framebuffer size="); DBG_PRINTLN((int)target);
#endif
    framebuffer = (uint8_t*)heap_caps_malloc(
      target,
      MALLOC_CAP_8BIT | MALLOC_CAP_SPIRAM
    );
    if (!framebuffer) {
#if DEBUG_LOG
      DBG_PRINTLN("[FB] malloc PSRAM failed, try internal RAM");
#endif
      framebuffer = (uint8_t*)heap_caps_malloc(target, MALLOC_CAP_8BIT);
    }
  }
  if (!framebuffer) {
#if DEBUG_LOG
    DBG_PRINTLN("[FB] framebuffer malloc FAILED");
#endif
    return false;
  }

  if (cfg.backend_hostport.length() == 0) {
#if DEBUG_LOG
    DBG_PRINTLN("[HTTP] hostport empty, skip download");
#endif
    return false;
  }

  int idx = random(0, DAILY_PHOTO_COUNT);

  String url;
  String hp = cfg.backend_hostport;
  hp.trim();

  if (hp.startsWith("http://") || hp.startsWith("https://")) {
    url = hp + String(DAILY_PHOTO_PATH_PREFIX) + String(idx) + ".bin";
  } else {
    url  = "http://" + hp;
    url += String(DAILY_PHOTO_PATH_PREFIX);
    url += String(idx);
    url += ".bin";
  }

#if DEBUG_LOG
  DBG_PRINT("[HTTP] GET "); DBG_PRINTLN(url);
#endif

  HTTPClient http;
  http.begin(url);
  int code = http.GET();
  if (code != HTTP_CODE_OK) {
#if DEBUG_LOG
    DBG_PRINT("[HTTP] code="); DBG_PRINTLN(code);
#endif
    http.end();
    return false;
  }

  int len = http.getSize();
#if DEBUG_LOG
  DBG_PRINT("[HTTP] content-length="); DBG_PRINTLN(len);
#endif

  WiFiClient *stream = http.getStreamPtr();
  size_t total = 0;

  const uint32_t DOWNLOAD_TIMEOUT_MS = 60 * 1000;
  uint32_t start_ms = millis();

  while (http.connected() && (len > 0 || len == -1) && total < target) {
    if (millis() - start_ms > DOWNLOAD_TIMEOUT_MS) {
#if DEBUG_LOG
      DBG_PRINTLN("[HTTP] download timeout");
#endif
      http.end();
      return false;
    }

    size_t avail = stream->available();
    if (avail) {
      size_t toRead = avail;
      if (toRead > target - total) toRead = target - total;
      int r = stream->read(framebuffer + total, toRead);
      if (r > 0) {
        total += r;
        if (len > 0) len -= r;
      }
    } else {
      delay(1);
    }
  }

  http.end();

#if DEBUG_LOG
  DBG_PRINT("[HTTP] total read="); DBG_PRINTLN((int)total);
#endif

  if (total != target) {
#if DEBUG_LOG
    DBG_PRINT("[HTTP] size mismatch, expect=");
    DBG_PRINT((int)target);
    DBG_PRINT(" got=");
    DBG_PRINTLN((int)total);
#endif
    return false;
  }

  return true;
}

// =======================
//  电池电量检测
// =======================
static int readBatteryMilliVolts() {
  // 取 N 次采样，去掉最大/最小各 3 个，余下取平均
  int buf[BATTERY_ADC_SAMPLES];
  for (int i = 0; i < BATTERY_ADC_SAMPLES; ++i) {
    buf[i] = analogReadMilliVolts(PIN_BATTERY_ADC);
    delay(2);
  }
  // sort
  for (int i = 0; i < BATTERY_ADC_SAMPLES - 1; ++i) {
    for (int j = i + 1; j < BATTERY_ADC_SAMPLES; ++j) {
      if (buf[j] < buf[i]) {
        int t = buf[i]; buf[i] = buf[j]; buf[j] = t;
      }
    }
  }
  // drop top 3 and bottom 3, average middle 10
  int trim = 3;
  long sum = 0;
  int count = 0;
  for (int i = trim; i < BATTERY_ADC_SAMPLES - trim; ++i) {
    sum += buf[i];
    count++;
  }
  int avgAdcMv = (count > 0) ? (int)(sum / count) : 0;
  // 还原为电池端电压
  int batMv = (int)(avgAdcMv * BATTERY_DIVIDER_RATIO * BATTERY_CALIBRATION_FACTOR);
#if DEBUG_LOG
  DBG_PRINT("[BAT] adcMv="); DBG_PRINT(avgAdcMv);
  DBG_PRINT(" batMv="); DBG_PRINT(batMv);
  DBG_PRINT(" (cal="); DBG_PRINTLN(BATTERY_CALIBRATION_FACTOR);
#endif
  return batMv;
}

static uint8_t readBatteryLevelPercent() {
  int batMv = readBatteryMilliVolts();
  // batMv 已是分压前电池电压
  for (uint8_t i = 0; i < 11; ++i) {
    if (batMv >= BATTERY_LEVEL_MV[i]) {
      return BATTERY_LEVEL_PCT[i];
    }
  }
  return 0;
}

// 在 framebuffer 中电池图标位置绘制白色背景 + 黑色图标
static void overlayBatteryIconInFrameBuffer(uint8_t percent) {
  if (!framebuffer) return;

  const uint8_t* icon = battery_icon_for_level(percent);
  if (!icon) return;

  const int ix = BATTERY_ICON_X;
  const int iy = BATTERY_ICON_Y;

  // 先把整个图标区域刷成白色 (idx=1)，避免污染下方/旁边残留像素
  for (int y = 0; y < BATTERY_ICON_H; ++y) {
    int fy = iy + y;
    if (fy < 0 || fy >= FB_HEIGHT) continue;
    for (int x = 0; x < BATTERY_ICON_W; ++x) {
      int fx = ix + x;
      if (fx < 0 || fx >= FB_WIDTH) continue;
      framebuffer[fy * FB_WIDTH + fx] = 1;  // WHITE
    }
  }

  // 把图标非白像素写回 (白像素跳过，保持上面写的白色)
  for (int y = 0; y < BATTERY_ICON_H; ++y) {
    int fy = iy + y;
    if (fy < 0 || fy >= FB_HEIGHT) continue;
    for (int x = 0; x < BATTERY_ICON_W; ++x) {
      int fx = ix + x;
      if (fx < 0 || fx >= FB_WIDTH) continue;
      uint8_t c = pgm_read_byte(&icon[y * BATTERY_ICON_W + x]);
      if (c == 1) continue;            // 透明/白，保持白底
      framebuffer[fy * FB_WIDTH + fx] = c;
    }
  }
}

// =======================
//  墨水屏显示
// =======================
void initDisplay(const Config &cfg) {
#if DEBUG_LOG
  DBG_PRINTLN("[EPD] initDisplay");
#endif
  SPI.end();
  SPI.begin(PIN_EPD_SCLK, -1 /*MISO*/, PIN_EPD_DIN, PIN_EPD_CS);

  display.init(0, true, 50, false);  // 50ms reset (GDEM 4色 panel 必须)

  if (cfg.rotate180) display.setRotation(3);
  else              display.setRotation(1);
}

void drawFromFramebuffer(const Config &cfg) {
  (void)cfg;

  // 先读取电池电量并把图标 overlay 到 framebuffer
  uint8_t batPct = 0;
  if (framebuffer) {
    batPct = readBatteryLevelPercent();
#if DEBUG_LOG
    DBG_PRINT("[BAT] level="); DBG_PRINTLN((int)batPct);
#endif
    overlayBatteryIconInFrameBuffer(batPct);
  }

  display.setFullWindow();
  int w = display.width();   // 480
  int h = display.height();  // 800

#if DEBUG_LOG
  DBG_PRINT("[EPD] logical w="); DBG_PRINT(w);
  DBG_PRINT(" h="); DBG_PRINTLN(h);
#endif

  display.firstPage();
  do {
    for (int y = 0; y < FB_HEIGHT && y < h; ++y) {
      for (int x = 0; x < FB_WIDTH && x < w; ++x) {
        uint8_t c = framebuffer[y * FB_WIDTH + x];
        uint16_t col;
        switch (c) {
          case 0: col = GxEPD_BLACK;  break;
          case 1: col = GxEPD_WHITE;  break;
          case 2: col = GxEPD_RED;    break;
          case 3: col = GxEPD_YELLOW; break;
          default: col = GxEPD_WHITE; break;
        }
        display.drawPixel(x, y, col);
      }
    }
  } while (display.nextPage());

  display.hibernate();
}

// =======================
//  睡到下一个唤醒点
// =======================
void sleepUntilNextSchedule(const Config &cfg, bool hasTime, const struct tm &now) {
  if (!hasTime) {
    goDeepSleepMinutes(1440);
    return;
  }

  int curMinOfDay = now.tm_hour * 60 + now.tm_min;
  int targetMin   = (int)cfg.refresh_hour * 60;
  int delta;

  if (curMinOfDay < targetMin) delta = targetMin - curMinOfDay;
  else                         delta = 24 * 60 - (curMinOfDay - targetMin);

  if (delta < 1) delta = 24 * 60;

#if DEBUG_LOG
  DBG_PRINT("[SLEEP] nowMin="); DBG_PRINT(curMinOfDay);
  DBG_PRINT(" targetMin="); DBG_PRINT(targetMin);
  DBG_PRINT(" delta="); DBG_PRINTLN(delta);
#endif

  goDeepSleepMinutes((uint32_t)delta);
}

// =======================
//  setup / loop
// =======================
void setup() {
  releaseAllGpioHoldsAtBoot();

  setCpuFrequencyMhz(80);
  pinMode(LED_BUILTIN, OUTPUT);
  digitalWrite(LED_BUILTIN, LOW);

  // 初始化电池 ADC (GPIO6 / ADC1_CH5, 0-3.3V 量程)
  analogSetPinAttenuation(PIN_BATTERY_ADC, BATTERY_ADC_ATTEN);
  analogReadMilliVolts(PIN_BATTERY_ADC);  // 丢一次读数，让 ADC 通道稳定

  DBG_BEGIN();
  delay(200);

#if DEBUG_LOG
  DBG_PRINTLN();
  DBG_PRINTLN("===== ESP32-S3 InkTime Daily Photo boot (GDEM075F52 4C) =====");
#endif

  if (isFactoryResetRequestedAtBoot()) {
#if DEBUG_LOG
  DBG_PRINTLN("[BOOT] GPIO38 LOW at boot -> clear NVS + reset WiFi driver");
#endif
  clearConfigNVS();

  WiFi.disconnect(true, true);
  WiFi.mode(WIFI_OFF);
  esp_wifi_stop();
  delay(200);
}

  randomSeed(esp_random());

  loadConfig(g_cfg);

  if (!g_cfg.valid) {
#if DEBUG_LOG
    DBG_PRINTLN("[BOOT] no valid config -> AP portal");
#endif
    startConfigPortal();
  }

#if DEBUG_LOG
  DBG_PRINTLN("[BOOT] have config -> connect WiFi");
#endif
  if (!connectWiFi(g_cfg)) {
#if DEBUG_LOG
    DBG_PRINTLN("[BOOT] connect failed -> AP portal");
#endif
    startConfigPortal();
  }

  struct tm timeinfo;
  // 先把 timezone 装好,后面 getLocalTime 才能返回正确本地时间
  applyTimezone(g_cfg);

  // 快速路径: RTC 在 deep sleep 后保留时间,如果还"年"就跳过 NTP 等待
  // 正常每日唤醒场景每次可省 5-15s 联网时间
  bool hasTime = false;
  {
    if (getLocalTime(&timeinfo, 0)) {
      time_t nowEpoch = mktime(&timeinfo);
      if (nowEpoch > 1700000000) {  // 2023-11-14 之后:RTC 还活着
        hasTime = true;
        saveLastTimeEpoch(nowEpoch);
#if DEBUG_LOG
        char tBuf[32];
        strftime(tBuf, sizeof(tBuf), "%Y-%m-%d %H:%M:%S", &timeinfo);
        DBG_PRINT("[TIME] RTC OK, skip NTP: "); DBG_PRINTLN(tBuf);
#endif
      }
    }
  }
  if (!hasTime) {
    // RTC 丢时间(首次开机 / RTC 掉电) -> 老路径走 NTP
    hasTime = syncTime(g_cfg, timeinfo);
  }

  bool ok = downloadDailyPhotoBin(g_cfg);
  if (ok) {
    initDisplay(g_cfg);
    drawFromFramebuffer(g_cfg);
  } else {
#if DEBUG_LOG
    DBG_PRINTLN("[BOOT] downloadDailyPhotoBin FAILED");
#endif
  }

  if (!hasTime) {
    struct tm tmp;
    if (syncTime(g_cfg, tmp)) sleepUntilNextSchedule(g_cfg, true, tmp);
    else                      sleepUntilNextSchedule(g_cfg, false, timeinfo);
  } else {
    sleepUntilNextSchedule(g_cfg, true, timeinfo);
  }
}

void loop() {
}
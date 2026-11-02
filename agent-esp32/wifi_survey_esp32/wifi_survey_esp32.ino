/*
 * wifi_survey_esp32.ino - Wi-Fi Survey scanner node for an ESP32.
 *
 * Sits in one place, scans 2.4 GHz every INTERVAL_MS and POSTs one reading
 * to /api/v1/readings in the survey schema:
 *   {"node_id","ts","position":{"x","y","floor"},"samples":[{"bssid","ssid",
 *    "band","channel","freq_mhz","rssi"}]}
 *
 * First boot, or a failed join, raises a captive portal at the SoftAP
 * gateway address that the sketch prints on the serial console. It
 * asks for the Wi-Fi network, server URL, node token, node name and
 * position. Nothing secret is compiled in: pair on the server first, then
 * paste the node token into the portal.
 *
 * The radio is 2.4 GHz only, so a 5 GHz-only network is invisible and a
 * mixed one shows up through its 2.4 GHz BSSID. RSSI is the radio's own
 * dBm, which makes this the most repeatable of the node types.
 */

#include <WiFi.h>
#include <HTTPClient.h>
#include <Preferences.h>
#include <WebServer.h>
#include <DNSServer.h>
#include <time.h>

static const char *AP_SSID = "WiFi-Survey-Setup";
static const char *NTP1 = "pool.ntp.org";
static const char *NTP2 = "time.cloudflare.com";
static const unsigned long INTERVAL_MS = 30000;
static const unsigned long WIFI_TIMEOUT_MS = 20000;
static const long EPOCH_PLAUSIBLE = 1700000000L;  // 2023-11-14: NTP has run

Preferences prefs;
WebServer portal(80);
DNSServer dns;
bool portalRunning = false;

String cfgSsid, cfgPass, cfgServer, cfgToken, cfgNodeId;
float cfgX = 0.0f, cfgY = 0.0f;
int cfgFloor = 0;

// ---------------------------------------------------------------- helpers

String jsonEscape(const String &in) {
  String out;
  for (size_t i = 0; i < in.length(); i++) {
    char c = in[i];
    switch (c) {
      case '"':  out += "\\\""; break;
      case '\\': out += "\\\\"; break;
      case '\n': out += "\\n";  break;
      case '\r': out += "\\r";  break;
      case '\t': out += "\\t";  break;
      default:
        if ((unsigned char)c < 0x20) {
          char buf[7];
          snprintf(buf, sizeof(buf), "\\u%04x", (unsigned char)c);
          out += buf;
        } else {
          out += c;
        }
    }
  }
  return out;
}

const char *bandFor(uint32_t f) { return f >= 5925 ? "6" : (f >= 4900 ? "5" : "2.4"); }

int channelFor(uint32_t f) {
  if (f == 2484) return 14;
  if (f >= 2412 && f < 2484) return (int)((f - 2407) / 5);
  if (f >= 4900 && f < 5925) return (int)((f - 5000) / 5);
  if (f >= 5925) return (int)((f - 5950) / 5);
  return 0;
}

bool loadConfig() {
  prefs.begin("wifi-survey", true);
  cfgSsid = prefs.getString("ssid", "");
  cfgPass = prefs.getString("pass", "");
  cfgServer = prefs.getString("server", "");
  cfgToken = prefs.getString("token", "");
  cfgNodeId = prefs.getString("node", "");
  cfgX = prefs.getFloat("x", 0.0f);
  cfgY = prefs.getFloat("y", 0.0f);
  cfgFloor = prefs.getInt("floor", 0);
  prefs.end();
  while (cfgServer.endsWith("/")) cfgServer.remove(cfgServer.length() - 1);
  return cfgSsid.length() && cfgServer.length() && cfgToken.length() && cfgNodeId.length();
}

void saveConfig() {
  prefs.begin("wifi-survey", false);
  prefs.putString("ssid", cfgSsid);
  prefs.putString("pass", cfgPass);
  prefs.putString("server", cfgServer);
  prefs.putString("token", cfgToken);
  prefs.putString("node", cfgNodeId);
  prefs.putFloat("x", cfgX);
  prefs.putFloat("y", cfgY);
  prefs.putInt("floor", cfgFloor);
  prefs.end();
}

// ---------------------------------------------------------------- portal

void portalRoot() {
  static const char PAGE[] PROGMEM =
    "<!doctype html><meta charset=utf-8><meta name=viewport "
    "content='width=device-width,initial-scale=1'><title>Wi-Fi Survey node</title>"
    "<style>body{font:15px system-ui;margin:0;padding:22px;background:#0b1016;color:#dbe6f2}"
    "h1{font-size:17px;text-transform:uppercase;letter-spacing:.08em}"
    "label{display:block;margin:12px 0 4px;color:#8ea3b8;font-size:12px;text-transform:uppercase}"
    "input{width:100%;box-sizing:border-box;padding:9px;border-radius:6px;border:1px solid #22303f;"
    "background:#121a24;color:#dbe6f2;font:inherit}button{margin-top:18px;width:100%;padding:11px;"
    "border:0;border-radius:6px;background:#2ee6a6;color:#06120d;font-weight:700}p{color:#8ea3b8;"
    "font-size:12px;line-height:1.5}</style><h1>Wi-Fi Survey node</h1>"
    "<p>Pair this node on the server first (<code>wifi-survey pair</code>), then paste the "
    "node token here. The ESP32 sees 2.4 GHz only.</p><form method=POST action=/save>"
    "<label>Wi-Fi SSID</label><input name=ssid required>"
    "<label>Wi-Fi password</label><input name=pass type=password>"
    "<label>Server URL</label><input name=server placeholder='http://survey.local:8800' required>"
    "<label>Node token</label><input name=token required>"
    "<label>Node name</label><input name=node placeholder='esp32-shed' required>"
    "<label>Position x (m)</label><input name=x value='0'>"
    "<label>Position y (m)</label><input name=y value='0'>"
    "<label>Floor index</label><input name=floor value='0'>"
    "<button type=submit>Save and restart</button></form>";
  portal.send_P(200, "text/html; charset=utf-8", PAGE);
}

void portalSave() {
  cfgSsid = portal.arg("ssid");
  cfgPass = portal.arg("pass");
  cfgServer = portal.arg("server");
  cfgToken = portal.arg("token");
  cfgNodeId = portal.arg("node");
  cfgX = portal.arg("x").toFloat();
  cfgY = portal.arg("y").toFloat();
  cfgFloor = portal.arg("floor").toInt();
  while (cfgServer.endsWith("/")) cfgServer.remove(cfgServer.length() - 1);
  saveConfig();
  portal.send(200, "text/html; charset=utf-8",
              "<p style='font-family:system-ui;padding:20px'>Saved. Rebooting...</p>");
  delay(1200);
  ESP.restart();
}

void startPortal() {
  WiFi.mode(WIFI_AP);
  WiFi.softAP(AP_SSID);
  dns.start(53, "*", WiFi.softAPIP());
  portal.on("/", portalRoot);
  portal.on("/save", HTTP_POST, portalSave);
  portal.onNotFound(portalRoot);  // captive-portal probes land here
  portal.begin();
  portalRunning = true;
  Serial.printf("portal: join %s, open http://%s/\n", AP_SSID,
                WiFi.softAPIP().toString().c_str());
}

// ---------------------------------------------------------------- radio

bool connectWifi() {
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);  // fresher scan results without modem sleep
  WiFi.begin(cfgSsid.c_str(), cfgPass.c_str());
  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < WIFI_TIMEOUT_MS) {
    delay(250);
    Serial.print('.');
  }
  Serial.println();
  if (WiFi.status() != WL_CONNECTED) return false;
  Serial.printf("joined %s as %s\n", cfgSsid.c_str(), WiFi.localIP().toString().c_str());
  configTime(0, 0, NTP1, NTP2);
  return true;
}

bool clockReady() { return time(nullptr) > EPOCH_PLAUSIBLE; }

String isoNow() {
  time_t now = time(nullptr);
  struct tm t;
  gmtime_r(&now, &t);
  char buf[24];
  strftime(buf, sizeof(buf), "%Y-%m-%dT%H:%M:%SZ", &t);
  return String(buf);
}

String buildPayload() {
  int n = WiFi.scanNetworks(false, true);  // blocking, include hidden
  if (n <= 0) return "";
  String b;
  b.reserve(96 + n * 96);
  b += "{\"node_id\":\"" + jsonEscape(cfgNodeId) + "\",\"ts\":\"" + isoNow() +
       "\",\"position\":{\"x\":" + String(cfgX, 2) + ",\"y\":" + String(cfgY, 2) +
       ",\"floor\":" + String(cfgFloor) + "},\"samples\":[";
  int written = 0;
  for (int i = 0; i < n; i++) {
    String bssid = WiFi.BSSIDstr(i);
    uint32_t freq = (uint32_t)WiFi.frequency(i);
    if (bssid.length() != 17 || freq == 0) continue;
    bssid.toLowerCase();
    if (written++) b += ",";
    b += "{\"bssid\":\"" + bssid + "\",\"ssid\":";
    String ssid = WiFi.SSID(i);
    b += ssid.length() ? ("\"" + jsonEscape(ssid) + "\"") : String("null");
    b += ",\"band\":\"" + String(bandFor(freq)) + "\",\"channel\":" +
         String(channelFor(freq)) + ",\"freq_mhz\":" + String(freq) +
         ",\"rssi\":" + String(WiFi.RSSI(i)) + "}";
  }
  WiFi.scanDelete();
  if (written == 0) return "";
  return b + "]}";
}

int postReading(const String &body) {
  HTTPClient http;
  http.setTimeout(15000);
  if (!http.begin(cfgServer + "/api/v1/readings")) {
    Serial.println("http.begin failed (bad URL?)");
    return -1;
  }
  http.addHeader("Content-Type", "application/json");
  http.addHeader("Authorization", "Bearer " + cfgToken);
  http.addHeader("User-Agent", "wifi-survey-esp32/1.0");
  int code = http.POST(body);
  if (code > 0) {
    Serial.printf("POST %d %s\n", code, http.getString().substring(0, 160).c_str());
  } else {
    Serial.printf("POST failed: %s\n", http.errorToString(code).c_str());
  }
  http.end();
  if (code == 401) cfgToken = "";  // revoked: repair through the portal
  return code;
}

// ---------------------------------------------------------------- lifecycle

void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println("\nwifi-survey esp32 node");
  if (!loadConfig()) {
    Serial.println("no/incomplete config: captive portal");
    startPortal();
    return;
  }
  if (!connectWifi()) {
    Serial.println("cannot join the configured network: portal for repair");
    startPortal();
    return;
  }
  if (!clockReady()) {
    Serial.println("waiting for NTP (readings need a real clock)");
    unsigned long start = millis();
    while (!clockReady() && millis() - start < 15000) delay(500);
    if (!clockReady()) Serial.println("no NTP: timestamps would say 1970");
  }
  Serial.printf("node %s -> %s, floor %d at (%.2f, %.2f)\n", cfgNodeId.c_str(),
                cfgServer.c_str(), cfgFloor, cfgX, cfgY);
}

void loop() {
  if (portalRunning) {
    dns.processNextRequest();
    portal.handleClient();
    return;
  }
  if (WiFi.status() != WL_CONNECTED) {
    if (!connectWifi()) {
      delay(5000);
      return;
    }
  }
  if (!clockReady()) {  // never post a 1970 timestamp
    delay(5000);
    return;
  }
  if (cfgToken.length() == 0) {
    Serial.println("token rejected earlier: portal for repair");
    startPortal();
    return;
  }
  String payload = buildPayload();
  if (payload.length() == 0) {
    Serial.println("scan returned nothing");
  } else {
    postReading(payload);
  }
  delay(INTERVAL_MS);
}

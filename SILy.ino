/*
SILy
Copyright (C) 2024-2025 Pierre Gaufillet

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.
*/

#include <ArduinoOTA.h>
#include <ESPAsyncWebServer.h>
#include <LittleFS.h>
#include <Preferences.h>
#include <WiFi.h>

#include "loramapmgr.h"
#include "loramgr.h"
#include "silypreferences.h"

#define SILY_VERSION "0.1.0"
#define SILY_NAME "SILy"
#define SILY_COPYRIGHT "Pierre Gaufillet 2024-2025"
#define SILY_LICENSE "GNU GPLv3"

#define BOARD_LED 37
#define LED_ON HIGH
#define LED_OFF LOW

AsyncWebServer server(80);
SILyPreferences *silyPrefs;
LoraMgr &loraMgr = LoraMgr::getInstance();
LoraMapMgr &loraMapMgr = LoraMapMgr::getInstance();

void setup() {
  pinMode(BOARD_LED, OUTPUT);
  digitalWrite(BOARD_LED, LED_ON);

  Serial.begin(115200);
  sleep(1);
  Serial.println("[INFO] Starting board...");

  Serial.println("[INFO] Starting NVS...");
  silyPrefs = new SILyPreferences();

  Serial.println(silyPrefs->generateJson());
  Serial.println("[INFO] NVS started");

  Serial.print("[INFO] Starting LittleFS...");
  if (!LittleFS.begin(false)) {
    Serial.println(" Unable to mount LittleFS filesystem.");
    return;
  } else {
    Serial.println("ok");
  }

  Serial.println("[INFO] Setting up WiFi...");
  String hostname = silyPrefs->get("general", "hostname");
  String ssid = silyPrefs->get("wifi", "ssid");
  String password = silyPrefs->get("wifi", "password");

  WiFi.setHostname(hostname.c_str());
  Serial.print("[INFO] Hostname: ");
  Serial.println(WiFi.getHostname());
  if (silyPrefs->get("wifi", "mode") == "Access Point") {
    WiFi.mode(WIFI_AP);
    WiFi.softAP(ssid, password);
    Serial.print("[INFO] Broadcasting Wifi AP ");
    Serial.println(WiFi.softAPSSID());
    Serial.print("[INFO] AP IP: ");
    Serial.println(WiFi.softAPIP());
  } else {
    WiFi.begin(ssid, password);
    while (WiFi.status() != WL_CONNECTED) {
      Serial.printf("[INFO] Connecting to WiFi %s...\n", ssid.c_str());
      delay(1000);
    }

    Serial.print("[INFO] IP: ");
    Serial.println(WiFi.localIP());
  }
  Serial.println("[INFO] WiFi started");

  Serial.println("[INFO] Setting up Web server...");
  server.onNotFound([](AsyncWebServerRequest *request) { request->send(404); });
  Serial.println("[INFO] Error handler...ok");

  server.on("/config", HTTP_GET, [](AsyncWebServerRequest *request) {
    String json = silyPrefs->generateJson();
    Serial.print("[DEBUG] Sending: ");
    Serial.println(json);
    request->send(200, "application/json", silyPrefs->generateJson());
  });
  Serial.println("[INFO] GET config handler...ok");

  server.on("/config/reset", HTTP_POST, [](AsyncWebServerRequest *request) {
    Serial.println("[DEBUG] Reset config");
    silyPrefs->reset();
    request->send(200);
  });
  Serial.println("[INFO] POST config reset handler...ok");

  // Beware here: data is NOT NULL terminated. It can therefore not be cast
  // directly into char*
  server.on(
      "/config", HTTP_POST,
      [](AsyncWebServerRequest *request) { request->send(200); }, NULL,
      [](AsyncWebServerRequest *request, uint8_t *data, size_t len,
         size_t index, size_t total) {
        String json = String((const char *)data, len);
        Serial.print("[DEBUG] Receiving: ");
        Serial.println(json);
        silyPrefs->parseJson(json);
      });
  Serial.println("[INFO] POST config handler...ok");

  server.on("/app", HTTP_GET, [](AsyncWebServerRequest *request) {
    String json = "{\"version\":\"" SILY_VERSION "\",\"name\":\"" SILY_NAME
                  "\",\"copyright\":\"" SILY_COPYRIGHT
                  "\",\"license\":\"" SILY_LICENSE "\"}";
    Serial.print("[DEBUG] Sending: ");
    Serial.println(json);
    request->send(200, "application/json", json);
  });
  Serial.println("[INFO] GET app handler...ok");

  server.serveStatic("/", LittleFS, "/").setDefaultFile("/index.html");
  Serial.println("[INFO] Static files handler...ok");

  server.begin();
  Serial.println("[INFO] Web server started");

  // Hostname defaults to esp3232-[MAC]
  ArduinoOTA.setHostname(hostname.c_str());
  ArduinoOTA
      .onStart([]() {
        String type;
        if (ArduinoOTA.getCommand() == U_FLASH) {
          type = "sketch";
        } else { // U_SPIFFS (also works for LittleFS)
          type = "filesystem";
          LittleFS.end();
        }

        Serial.print("[INFO] Start updating " + type);
      })
      .onEnd([]() { Serial.println("\n[INFO] Updating done"); })
      .onProgress(
          [](unsigned int progress, unsigned int total) { Serial.printf("."); })
      .onError([](ota_error_t error) {
        Serial.printf("[ERROR] %u: ", error);
        if (error == OTA_AUTH_ERROR) {
          Serial.println("Auth Failed");
        } else if (error == OTA_BEGIN_ERROR) {
          Serial.println("Begin Failed");
        } else if (error == OTA_CONNECT_ERROR) {
          Serial.println("Connect Failed");
        } else if (error == OTA_RECEIVE_ERROR) {
          Serial.println("Receive Failed");
        } else if (error == OTA_END_ERROR) {
          Serial.println("End Failed");
        }
      });

  // OTA starts lately due to LoRaMesher initialization duration (time between
  // setup and loop is too long). It causes problems when flashing the
  // filesystem and immediately after the program.
  // TODO: start OTA in a separate task instead of loop
  ArduinoOTA.begin();
  Serial.println("[INFO] OTA started");

  loraMgr.setConfig(silyPrefs);
  loraMgr.start();

  Serial.println("[INFO] Lora mesh started");

  if (silyPrefs->get("general", "role") == "Node") {
    loraMapMgr.setPeriod(10000);
    loraMapMgr.startNode();

  } else if (silyPrefs->get("general", "role") == "Gateway") {
    loraMapMgr.startGateway();
  }
  Serial.println("[INFO] Lora map started");

  Serial.println("[INFO] Board started");
}

void loop() {
  delay(150);
  ArduinoOTA.handle();
}

/*
SILy
Copyright (C) 2024 Pierre Gaufillet

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

#include <Preferences.h>
#include <WiFi.h>
#include <ESPAsyncWebServer.h>
#include <LittleFS.h>

#include "SILyPreferences.h"

#define BOARD_LED 37
#define LED_ON HIGH
#define LED_OFF LOW

AsyncWebServer server(80);

SILyPreferences *silyPrefs;

void setup()
{
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
    if (!LittleFS.begin(false))
    {
        Serial.println(" Unable to mount LittleFS filesystem.");
        return;
    }
    else
    {
        Serial.println("ok");
    }

    Serial.println("[INFO] Setting up WiFi...");
    String hostname = silyPrefs->get("general", "hostname");
    String ssid = silyPrefs->get("wifi", "ssid");
    String password = silyPrefs->get("wifi", "password");

    WiFi.setHostname(hostname.c_str());
    Serial.print("[INFO] Hostname: ");
    Serial.println(WiFi.getHostname());
    if (silyPrefs->get("wifi", "mode") == "Access Point")
    {
        WiFi.mode(WIFI_AP);
        WiFi.softAP(ssid, password);
        Serial.print("[INFO] Broadcasting Wifi AP ");
        Serial.println(WiFi.softAPSSID());
        Serial.print("[INFO] AP IP: ");
        Serial.println(WiFi.softAPIP());
    }
    else
    {
        WiFi.begin(ssid, password);
        while (WiFi.status() != WL_CONNECTED)
        {
            Serial.printf("[INFO] Connecting to WiFi %s...\n", ssid.c_str());
            delay(1000);
        }

        Serial.print("[INFO] IP: ");
        Serial.println(WiFi.localIP());
    }
    Serial.println("[INFO] WiFi started");

    Serial.println("[INFO] Setting up Web server...");
    server.onNotFound([](AsyncWebServerRequest *request)
                      { request->send(404); });
    Serial.println("[INFO] Error handler...ok");

    server.on("/config", HTTP_GET, [](AsyncWebServerRequest *request)
              { String json = silyPrefs->generateJson();
                Serial.print("[DEBUG] Sending: ");
                Serial.println(json);
                request->send(200, "application/json", silyPrefs->generateJson()); });
    Serial.println("[INFO] GET config handler...ok");

    // Beware here: data is NOT NULL terminated. It can therefore not be cast directly into char*
    server.on("/config", HTTP_POST, [](AsyncWebServerRequest *request)
              { request->send(200); }, NULL, [](AsyncWebServerRequest *request, uint8_t *data, size_t len, size_t index, size_t total)
              { 
                String json = String((const char *) data, len);
                Serial.print("[DEBUG] Receiving: ");
                Serial.println(json);
                silyPrefs->parseJson(json); });
    Serial.println("[INFO] POST config handler...ok");

    server.serveStatic("/", LittleFS, "/").setDefaultFile("/index.html");
    Serial.println("[INFO] Static files handler...ok");

    server.begin();
    Serial.println("[INFO] Web server started");

    Serial.println("[INFO] Board started");
}

void loop()
{
    sleep(5);
    // Serial.println("[DEBUG] I'm alive!");
}

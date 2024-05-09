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
    Serial.begin(115200);
    sleep(1);
    Serial.println("Setting up board...");

    pinMode(BOARD_LED, OUTPUT);
    digitalWrite(BOARD_LED, LED_ON);

    silyPrefs = new SILyPreferences();

    if (!LittleFS.begin(false))
    {
        Serial.println("[ERROR] Unable to mount LittleFS filesystem.");
        return;
    }

    WiFi.setHostname(silyPrefs->get("general", "hostname"));
    if (silyPrefs->get("wifi", "mode") == "Access Point")
    {
        Serial.println("Creating Wifi AP");
        WiFi.mode(WIFI_AP);
        WiFi.softAP(silyPrefs->get("wifi", "ssid"), silyPrefs->get("wifi", "password"));
        Serial.print("AP Created with IP Gateway ");
        Serial.println(WiFi.softAPIP());
    }
    else
    {
        WiFi.begin(silyPrefs->get("wifi", "ssid"), silyPrefs->get("wifi", "password"));
        while (WiFi.status() != WL_CONNECTED)
        {
            Serial.println("Connecting to WiFi...");
            delay(1000);
        }

        Serial.print("IP: ");
        Serial.println(WiFi.localIP());
    }

    server.onNotFound([](AsyncWebServerRequest *request)
                      { request->send(404); });

    server.on("/config", HTTP_GET, [](AsyncWebServerRequest *request)
              { request->send(200, "application/json", silyPrefs->generateJson()); });

    server.on("/config", HTTP_POST, [](AsyncWebServerRequest *request)
              {
            String message;
            if (request->hasParam("jsonConfig", true))
            {
                silyPrefs->parseJson(request->getParam("jsonConfig", true)->value().c_str());
            }
            request->send(200); });

    server.serveStatic("/", LittleFS, "/").setDefaultFile("/index.html");

    server.begin();
}

void loop()
{
    sleep(1);
}

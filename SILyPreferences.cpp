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

#include <Preferences.h>
#include <nvs_flash.h>
#include <ArduinoJson.h>
#include "SILyPreferences.h"

SILyPreferences::SILyPreferences()
{
    prefs = new Preferences();

    // Initialize if not already done
    prefs->begin("wifi", true);
    bool has_been_initalized = prefs->isKey("password");
    prefs->end();

    if (!has_been_initalized)
    {
        Serial.print("[INFO] Initializing NVS preferences...");
        reset();
        Serial.println("ok");
    }
    // Check NVS health (enough free entries for a complete reconfiguration).
    // If unsufficient, save configuration in RAM, erase and restore
    size_t free_entries = prefs->freeEntries();
    if (free_entries < 15)
    {
        Serial.print("[INFO] NVS nearly full: cleaning up...");

        String json = generateJson();

        nvs_flash_erase();
        nvs_flash_init();

        parseJson(json);
        Serial.println("ok");
    }
}

String SILyPreferences::get(const char *ns, const char *key)
{
    prefs->begin(ns, true);
    String ret = prefs->getString(key);
    prefs->end();

    return ret;
}

void SILyPreferences::put(const char *ns, const char *key, const char *value)
{
    prefs->begin(ns);
    prefs->putString(key, value);
    prefs->end();
}

String SILyPreferences::generateJson()
{
    String json = "[";

    prefs->begin("general", true);
    json += "{\"namespace\":\"general\",\"settings\":{";
    json += "\"hostname\":\"";
    json += prefs->getString("hostname");
    json += "\",";
    json += "\"role\":\"";
    json += prefs->getString("role");
    json += "\"}}";
    prefs->end();

    json += ",";

    json += "{\"namespace\":\"router\",\"settings\":{";
    prefs->begin("router", true);
    json += "\"serveraddress\":\"";
    json += prefs->getString("serveraddress");
    json += "\",";
    json += "\"serverport\":\"";
    json += prefs->getString("serverport");
    json += "\"}}";
    prefs->end();

    json += ",";

    json += "{\"namespace\":\"node\",\"settings\":{";
    prefs->begin("node", true);
    json += "\"loraaddress\":\"";
    json += prefs->getString("loraaddress");
    json += "\"}}";
    prefs->end();

    json += ",";

    json += "{\"namespace\":\"lora\",\"settings\":{";
    prefs->begin("lora", true);
    json += "\"frequency\":\"";
    json += prefs->getString("frequency");
    json += "\",";
    json += "\"spreadfactor\":\"";
    json += prefs->getString("spreadfactor");
    json += "\",";
    json += "\"bandwidth\":\"";
    json += prefs->getString("bandwidth");
    json += "\",";
    json += "\"coderate\":\"";
    json += prefs->getString("coderate");
    json += "\",";
    json += "\"txpower\":\"";
    json += prefs->getString("txpower");
    json += "\",";
    json += "\"password\":\"";
    json += prefs->getString("password");
    json += "\"}}";
    prefs->end();

    json += ",";

    json += "{\"namespace\":\"wifi\",\"settings\":{";
    prefs->begin("wifi", true);
    json += "\"mode\":\"";
    json += prefs->getString("mode");
    json += "\",";
    json += "\"ssid\":\"";
    json += prefs->getString("ssid");
    json += "\",";
    json += "\"password\":\"";
    json += prefs->getString("password");
    json += "\"}}";
    prefs->end();

    json += "]";

    return json;
}

bool SILyPreferences::parseJson(String json)
{
    StaticJsonDocument<1536> doc;
    DeserializationError error = deserializeJson(doc, json);

    /*     Serial.println("[DEBUG] Config json received");
        Serial.printf("[DEBUG] body: \"%s\"\n", json.c_str());
        if (error)
        {
            Serial.print("deserializeJson() failed: ");
            Serial.println(error.c_str());
            return false;
        }
     */

    JsonArray array = doc.as<JsonArray>();
    for (JsonVariant v : array)
    {
        prefs->begin(v["namespace"]);
        // Serial.print("[DEBUG] Namespace ");
        // Serial.println(v["namespace"].as<String>());
        for (JsonPair w : v["settings"].as<JsonObject>())
        {
            String ret = prefs->getString(w.key().c_str());
            if (ret != w.value().as<String>())
            {
                Serial.print("[DEBUG]  ");
                Serial.print(w.key().c_str());
                Serial.print("=");
                Serial.println(w.value().as<String>());
                prefs->putString(w.key().c_str(), w.value().as<String>());
            }
        }
        prefs->end();
    }

    return true;
}

void SILyPreferences::reset()
{
    // Default values
    prefs->begin("general");
    prefs->putString("hostname", "sily1");
    prefs->putString("role", "Node");
    prefs->end();

    prefs->begin("router");
    prefs->putString("serveraddress", "");
    prefs->putString("serverport", "");
    prefs->end();

    prefs->begin("node");
    prefs->putString("loraaddress", "");
    prefs->end();

    prefs->begin("lora");
    prefs->putString("frequency", "869.5");
    prefs->putString("spreadfactor", "7");
    prefs->putString("bandwidth", "125");
    prefs->putString("coderate", "4:5");
    prefs->putString("txpower", "22");
    prefs->putString("password", "");
    prefs->end();

    prefs->begin("wifi");
    prefs->putString("mode", "Access Point");
    prefs->putString("ssid", "Sily1");
    prefs->putString("password", "sily2025"); // no password or >= 8 chars
    prefs->end();
}
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
        init();
    }
    // Check NVS health (enough free entries for a complete reconfiguration).
    // If unsufficient, save configuration in RAM, erase and restore
    size_t free_entries = prefs->freeEntries();
    if (free_entries < 15)
    {
        const char *json = generateJson();

        nvs_flash_erase();
        nvs_flash_init();

        parseJson(json);
    }
}

const char *SILyPreferences::get(const char *ns, const char *key)
{
    prefs->begin(ns, true);
    String ret = prefs->getString(key);
    prefs->end();

    return ret.c_str();
}

void SILyPreferences::put(const char *ns, const char *key, const char *value)
{
    prefs->begin(ns);
    prefs->putString(key, value);
    prefs->end();
}

const char *SILyPreferences::generateJson()
{
    String json = "[";

    prefs->begin("general", true);
    json += "{\"namespace\":\"general\", {";
    json += "\"hostname\":\"";
    json += prefs->getString("hostname");
    json += "\",";
    json += "\"role\":\"";
    json += prefs->getString("role");
    json += "\"}}";
    prefs->end();

    json += ",";

    json = "{\"namespace\":\"router\", {";
    prefs->begin("router", true);
    json += "\"serveraddress\":\"";
    json += prefs->getString("serveraddress");
    json += "\",";
    json += "\"serverport\":\"";
    json += prefs->getString("serverport");
    json += "\"}}";
    prefs->end();

    json += ",";

    json = "{\"namespace\":\"node\", {";
    prefs->begin("node", true);
    json += "\"loraaddress\":\"";
    json += prefs->getString("loraaddress");
    json += "\"}}";
    prefs->end();

    json += ",";

    json = "{\"namespace\":\"lora\", {";
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

    json = "{\"namespace\":\"wifi\", {";
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

    return json.c_str();
}

bool SILyPreferences::parseJson(const char *json)
{
    StaticJsonDocument<1536> doc;
    DeserializationError error = deserializeJson(doc, json);

    if (error)
    {
        Serial.print("deserializeJson() failed: ");
        Serial.println(error.c_str());
        return false;
    }

    JsonArray array = doc.as<JsonArray>();
    for (JsonVariant v : array)
    {
        prefs->begin(v["namespace"]);
        for (JsonPair w : v["settings"].as<JsonObject>())
        {
            prefs->putString(w.key().c_str(), w.value().as<String>());
        }
        prefs->end();
    }

    return true;
}

bool SILyPreferences::init()
{
    // Default values
    prefs->begin("general");
    prefs->putString("hostname", "sily1");
    prefs->putString("role", "node");
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
    prefs->putString("ssid", "sily1");
    prefs->putString("password", "sily1");
    prefs->end();
}
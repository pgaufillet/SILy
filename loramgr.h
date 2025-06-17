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

#ifndef LORAMGR_H
#define LORAMGR_H

#include "freertos/FreeRTOS.h"
#include "freertos/portmacro.h"
#include "silypreferences.h"
#include <LoraMesher.h>

#define MAX_PACKET_TYPES 256

using PacketCallback = void (*)(uint8_t *payload, size_t payloadSize,
                                uint16_t dst, uint16_t src, void *context);

struct PacketProcessor {
  PacketCallback callback = nullptr;
  void *context = nullptr;
};

class LoraMgr {
public:
  LoraMesher &radio = LoraMesher::getInstance();

  static LoraMgr &getInstance() {
    static LoraMgr singleton;
    return singleton;
  };

  void setConfig(SILyPreferences *silyPrefs);
  void start();

  void registerPacketProcessor(uint8_t packet_type, PacketCallback callback,
                               void *context);
  void unregisterPacketProcessor(uint8_t packet_type);

  void createPacketAndSend(uint16_t dst, uint8_t packetType, uint8_t *payload,
                           uint8_t payloadSize);

private:
  LoraMesher::LoraMesherConfig config;
  TaskHandle_t receiveLoRaMessage_Handle = NULL;
  portMUX_TYPE lock = portMUX_INITIALIZER_UNLOCKED;
  PacketProcessor processors[MAX_PACKET_TYPES] = {};

  LoraMgr();
  LoraMgr(const LoraMgr &) = delete;
  LoraMgr &operator=(const LoraMgr &) = delete;
  static void processReceivedPackets(void *parameters);
};

#endif // LORAMGR_H

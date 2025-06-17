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

#ifndef LORAMAPMGR_H
#define LORAMAPMGR_H

#include "loramgr.h"
#include "meshlinktable.h"
#include "silypreferences.h"
#include <LoraMesher.h>

#define MESH_LINK_TABLE_CAPACITY 256

class LoraMapMgr {
public:
  void setPeriod(unsigned int period);
  void startNode();
  void startGateway();
  void stop();

  static LoraMapMgr &getInstance() {
    static LoraMapMgr singleton;
    return singleton;
  };

private:
  LoraMgr &loramgr = LoraMgr::getInstance();
  uint32_t period = -1; // in ms
  TimerHandle_t periodicTimer;
  MeshLinkTable *links = nullptr;

  LoraMapMgr();
  LoraMapMgr(const LoraMapMgr &) = delete;
  LoraMapMgr &operator=(const LoraMapMgr &) = delete;
  static void sendHeartbeat(TimerHandle_t xTimer);
  static void receiveHeartbeat(uint8_t *payload, size_t payloadSize,
                               uint16_t dst, uint16_t src, void *context);
};

#endif // LORAMAPMGR_H

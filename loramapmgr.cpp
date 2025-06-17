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

#include "loramapmgr.h"

#define HEARTBEAT_PACKET_TYPE 0x02

struct __attribute__((packed)) Neighbor {
  uint16_t address;
  int8_t snr;
};

struct __attribute__((packed)) HeartbeatPayloadHeader {
  uint8_t batteryLevel;
  uint8_t silyMode;
  uint8_t numNeighbors;
};

LoraMapMgr::LoraMapMgr() {
  links = new MeshLinkTable(MESH_LINK_TABLE_CAPACITY);
}

void LoraMapMgr::setPeriod(unsigned int a_period) {
  period = a_period;
  stop();
}

void LoraMapMgr::startNode() {
  if (period < 0) {
    return;
  }

  periodicTimer = xTimerCreate("HeartbeatTimer",      // name
                               pdMS_TO_TICKS(period), // period (ms)
                               pdTRUE,                // auto-reload
                               this,                  // ID
                               sendHeartbeat          // callback
  );
  xTimerStart(periodicTimer, 0);
}

void LoraMapMgr::startGateway() {
  loramgr.registerPacketProcessor(HEARTBEAT_PACKET_TYPE, receiveHeartbeat,
                                  this);
}

void LoraMapMgr::stop() {
  if (periodicTimer != NULL) {
    xTimerStop(periodicTimer, 0);
  }
  loramgr.unregisterPacketProcessor(HEARTBEAT_PACKET_TYPE);
}

void LoraMapMgr::sendHeartbeat(TimerHandle_t xTimer) {
  void *ptr = pvTimerGetTimerID(xTimer);
  LoraMapMgr *object = static_cast<LoraMapMgr *>(ptr);

  Serial.println("[INFO] Heartbeat");

  RouteNode *closestGateway = object->loramgr.radio.getClosestGateway();
  if (closestGateway != nullptr) {
    std::vector<Neighbor> neighbors;

    LM_LinkedList<RouteNode> *routingTableList =
        object->loramgr.radio.routingTableListCopy();

    Serial.printf("[DEBUG] Routing table size: %d\n",
                  routingTableList->getLength());

    for (size_t i = 0; i < routingTableList->getLength(); i++) {

      RouteNode *route = (*routingTableList)[i];
      NetworkNode *node = &(route->networkNode);
      Serial.printf("[DEBUG] Node %04X, Metric %d\n", node->address,
                    node->metric);
      if (node->metric == 1) { // direct neighbors only
        neighbors.push_back(
            Neighbor{.address = node->address, .snr = route->receivedSNR});
      }
    }
    delete routingTableList;

    uint8_t numNeighbors = neighbors.size();
    uint8_t payloadSize =
        sizeof(HeartbeatPayloadHeader) + numNeighbors * sizeof(Neighbor);

    uint8_t *payload = (uint8_t *)malloc(payloadSize);
    if (!payload)
      return; // allocation failure

    HeartbeatPayloadHeader header = {.batteryLevel = 0, // to be completed
                                     .silyMode = 0,     // to be completed
                                     .numNeighbors = numNeighbors};

    memcpy(payload, &header, sizeof(header));
    memcpy(payload + sizeof(header), neighbors.data(),
           numNeighbors * sizeof(Neighbor));

    Serial.print("[DEBUG] Heartbeat: ");
    for (size_t i = 0; i < payloadSize; i++) {
      Serial.printf("%02X ", payload[i]);
    }
    Serial.println();

    object->loramgr.createPacketAndSend(closestGateway->networkNode.address,
                                        HEARTBEAT_PACKET_TYPE, payload,
                                        payloadSize);

    free(payload);
  }
}

void LoraMapMgr::receiveHeartbeat(uint8_t *payload, size_t payloadSize,
                                  uint16_t dst, uint16_t src, void *context) {
  LoraMapMgr *self = static_cast<LoraMapMgr *>(context);
  if (payloadSize >= sizeof(HeartbeatPayloadHeader)) {
    HeartbeatPayloadHeader *header =
        reinterpret_cast<HeartbeatPayloadHeader *>(payload);
    Serial.printf("[DEBUG] Heartbeat from %02X, batt: %d, "
                  "mode: %d, neighbors: %d\n",
                  src, header->batteryLevel, header->silyMode,
                  header->numNeighbors);

    if (header->numNeighbors > 0) {
      size_t expectedSize = sizeof(HeartbeatPayloadHeader) +
                            header->numNeighbors * sizeof(Neighbor);
      if (payloadSize == expectedSize) {
        Neighbor *neighbors =
            reinterpret_cast<Neighbor *>(payload + sizeof(*header));
        for (size_t i = 0; i < header->numNeighbors; i++) {
          Serial.printf("[DEBUG] Neighbor %04X, SNR: %d\n",
                        neighbors[i].address, neighbors[i].snr);
          self->links->addOrUpdate(src, neighbors[i].address, neighbors[i].snr);
        }
      } else {
        Serial.printf("[ERROR] Invalid payload size: expected %zu, got %zu\n",
                      expectedSize, payloadSize);
      }
    }
  } else {
    Serial.println("[ERROR] Invalid heartbeat packet size");
  }
}
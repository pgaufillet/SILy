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

#include "loramgr.h"

#define RADIO_SCLK_PIN 5
#define RADIO_MISO_PIN 3
#define RADIO_MOSI_PIN 6
#define RADIO_CS_PIN 7
#define RADIO_DIO1_PIN 33
#define RADIO_BUSY_PIN 34
#define RADIO_RST_PIN 8

SPIClass silySPI(HSPI);

void LoraMgr::setConfig(SILyPreferences *silyPrefs) {
  config.module = LoraMesher::LoraModules::SX1262_MOD;
  config.loraCs = RADIO_CS_PIN;
  config.loraIrq = RADIO_DIO1_PIN;
  config.loraIo1 = RADIO_BUSY_PIN;
  config.loraRst = RADIO_RST_PIN;
  silySPI.begin(RADIO_SCLK_PIN, RADIO_MISO_PIN, RADIO_MOSI_PIN, RADIO_CS_PIN);
  config.spi = &silySPI;
  config.freq = silyPrefs->get("lora", "frequency").toFloat();
  config.bw = silyPrefs->get("lora", "bandwidth").toFloat();
  config.sf = silyPrefs->get("lora", "spreadfactor").toInt();
  String coding_rate = silyPrefs->get("lora", "coderate");
  config.cr = coding_rate.substring(coding_rate.length() - 1).toInt();
  config.power = silyPrefs->get("lora", "txpower").toInt();

  if (silyPrefs->get("general", "role") == "Gateway") {
    radio.addRole(ROLE_GATEWAY);
  }

  radio.begin(config);
}

void LoraMgr::start() {
  // Start the LoRaMesher
  int res = xTaskCreate(processReceivedPackets, "", 4096, this, 2,
                        &receiveLoRaMessage_Handle);

  if (res != pdPASS) {
    Serial.printf("[ERROR] Receive Task creation error: %d\n", res);
  }
  radio.setReceiveAppDataTaskHandle(receiveLoRaMessage_Handle);
  radio.start();
}

LoraMgr::LoraMgr() {}

void LoraMgr::registerPacketProcessor(uint8_t packet_type,
                                      PacketCallback callback, void *context) {
  taskENTER_CRITICAL(&lock);
  processors[packet_type].callback = callback;
  processors[packet_type].context = context;
  taskEXIT_CRITICAL(&lock);
}

void LoraMgr::unregisterPacketProcessor(uint8_t packet_type) {
  taskENTER_CRITICAL(&lock);
  processors[packet_type].callback = nullptr;
  processors[packet_type].context = nullptr;
  taskEXIT_CRITICAL(&lock);
}

void LoraMgr::createPacketAndSend(uint16_t dst, uint8_t packetType,
                                  uint8_t *payload, uint8_t payloadSize) {
  uint8_t *full_payload = (uint8_t *)malloc(payloadSize + 1);
  if (!full_payload)
    return; // allocation failure

  full_payload[0] = packetType;
  memcpy(full_payload + 1, payload, payloadSize);
  radio.createPacketAndSend(dst, full_payload, payloadSize + 1);
  free(full_payload);
}

void LoraMgr::processReceivedPackets(void *parameters) {
  LoraMgr *singleton = static_cast<LoraMgr *>(parameters);
  for (;;) {
    // Wait for the notification of processReceivedPackets and enter blocking
    ulTaskNotifyTake(pdPASS, portMAX_DELAY);
    // Get the receivedAppPackets and get all the elements
    while (singleton->radio.getReceivedQueueSize() > 0) {
      // Get the first element inside the Received User Packets FiFo
      AppPacket<uint8_t> *packet = singleton->radio.getNextAppPacket<uint8_t>();

      // Serial.printf("[DEBUG] From %04X (%d): ", packet->src,
      //               packet->payloadSize);

      // for (size_t i = 0; i < packet->getPayloadLength(); i++) {
      //   Serial.printf("%02X ", packet->payload[i]);
      // }

      // Serial.println();

      size_t payloadLength = packet->getPayloadLength();
      if (payloadLength > 0) {

        PacketCallback cb;
        void *ctx;

        taskENTER_CRITICAL(&(singleton->lock));
        cb = singleton->processors[packet->payload[0]].callback;
        ctx = singleton->processors[packet->payload[0]].context;
        taskEXIT_CRITICAL(&(singleton->lock));

        // Serial.printf("[DEBUG] Processing packet type %02X from %04X\n",
        //               packet->payload[0], packet->src);
        if (cb != nullptr) {
          cb(packet->payload + 1, payloadLength - 1, packet->dst, packet->src,
             ctx);
        }

        // Then delete the packet
        singleton->radio.deletePacket(packet);
      }
    }
  }
}

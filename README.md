# SILy

Meshed LoRa network for SportIdent SRR Orienteering stations based on Lilygo T3S3

The communication path is:

**SI BSF8-SRR** --SRR--> **SI SRR module** --UART--> **Lilygo T3S3 node** --LoraMesh--> **Lilygo T3S3 gateway** --WiFi+TCP--> **MeOS**.

## Hardware

* SportIdent SRR BSF8-SRR-DB and SRR modules: <https://www.sportident.com> and <https://www.sportident.com/images/PDF/1_si_base_products/8_si-radio/SRR-Kit/SPORTident_SRR_en.pdf>.
* Lilygo T3S3: <https://github.com/Xinyuan-LilyGO/LilyGo-LoRa-Series>.

## Software

* MeOS: <http://www.melin.nu/meos/en/> and <https://github.com/melinsoftware/meos>.
* ESP32 Arduino documentation: <https://espressif-docs.readthedocs-hosted.com/projects/arduino-esp32/en/latest/>.
* Lilygo T3S3: <https://github.com/Xinyuan-LilyGO/LilyGo-LoRa-Series>.
* LoraMesher: <https://github.com/LoRaMesher/LoRaMesher>.

## Configuration guide

TBC

## Node administration

TBC

## Architecture

SILy nodes can be of 2 types:

* **Node**: general case. Nodes read punches and send them to the gateway.
* **Gateway**: only one gateway in the network, receiving punches from nodes and
relaying them to MeOS.

Nodes and gateway share the same software.

## Technical notes

TBC

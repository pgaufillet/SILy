"""Board support: the only hardware-specific code.

- `board.t3s3`: LilyGo T3S3 wiring (SX1262 on SPI);
- `board.platform`: the ESP32 platform of the mesh (NVS storage, RTC
  retained memory, hardware random source);
- `board.sx1262`: the SX1262 radio adapter (continuous reception, transmit,
  carrier sense), with its pure bookkeeping in `board.sense`.

Imports neither the application (``sily``) nor the mesh (``mesh``): the mesh
contracts are implemented by duck typing, and ``sily.radio`` binds the radio
adapter to the mesh's radio events.
"""

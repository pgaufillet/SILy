# SILy firmware: the upstream LILYGO_T3_S3 frozen modules plus the asyncio
# LoRa modem (lora.AsyncSX1262).
include("$(PORT_DIR)/boards/LILYGO_T3_S3/manifest.py")
require("lora-async")

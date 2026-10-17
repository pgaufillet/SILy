"""LilyGo T3S3 (ESP32-S3 + SX1262), with the SILy firmware (frozen lora-async)."""

from lora import AsyncSX1262
from machine import SPI, Pin

# SX1262 SPI clock (the chip accepts up to 16 MHz).
_SPI_BAUDRATE = 8_000_000


def make_modem(lora_cfg=None):
    """Reset the SX1262 and return it as an asyncio modem (lora.AsyncSX1262).

    The driver checks the chip status after the reset and raises RuntimeError
    if the radio does not answer. lora_cfg is the micropython-lib LoRa
    configuration dict; None keeps the driver defaults.
    """
    p = Pin.board
    spi = SPI(
        1,
        baudrate=_SPI_BAUDRATE,
        polarity=0,
        phase=0,
        sck=p.LORA_SCK,
        mosi=p.LORA_MOSI,
        miso=p.LORA_MISO,
    )
    return AsyncSX1262(
        spi=spi,
        cs=p.LORA_CS,
        busy=p.LORA_BUSY,
        dio1=p.LORA_DIO1,
        dio2_rf_sw=True,
        dio3_tcxo_millivolts=1800,
        reset=p.LORA_RST,
        lora_cfg=lora_cfg,
    )

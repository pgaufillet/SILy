"""LilyGo T3S3 (ESP32-S3 + SX1262), with the SILy firmware (frozen lora-async).

Wiring of the board and of the box around it: docs/hardware.md. Pins come
from the firmware's pin names (``Pin.board``), but for the SRR receiver's
UART, which is not on the board.
"""

from lora import AsyncSX1262
from machine import ADC, I2C, SPI, UART, Pin

# SX1262 SPI clock (the chip accepts up to 16 MHz).
_SPI_BAUDRATE = 8_000_000

# Battery: VBAT through a 100 kOhm / 100 kOhm divider on VBAT_SENSE (GPIO1,
# ADC1, usable while WiFi runs), read with 11 dB attenuation (up to about
# 3.1 V at the pin, 6.2 V of battery).
_VBAT_DIVIDER = 2
_VBAT_READS = 8

# SSD1306 OLED display, 128 x 64 pixels, on I2C0.
OLED_ADDRESS = 0x3C
OLED_WIDTH = 128
OLED_HEIGHT = 64
_I2C_FREQ = 400_000

# SRR receiver: UART1 on the QWIIC socket (GND, 3V3, GPIO43, GPIO44), 38400
# baud 8N1 as on the receiver.
SRR_UART = 1
SRR_TX = 43  # to the receiver's RX
SRR_RX = 44  # from the receiver's TX
SRR_BAUDRATE = 38400


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


def make_led():
    """Return the user LED (GPIO37, active high) as an output Pin, off."""
    return Pin(Pin.board.LED, Pin.OUT, value=0)


def battery_millivolts(reads=_VBAT_READS):
    """Return the battery voltage in millivolts (int), the mean of reads ADC reads.

    Calibrated reading (``read_uv``), accurate to a few per cent with the
    divider's tolerance. Without a battery, on USB, it reads the charger's
    output.
    """
    adc = ADC(Pin.board.VBAT_SENSE, atten=ADC.ATTN_11DB)
    total = 0
    for _ in range(reads):
        total += adc.read_uv()
    return (total * _VBAT_DIVIDER + 500 * reads) // (1000 * reads)


def make_i2c():
    """Return the I2C bus of the OLED display (SDA GPIO18, SCL GPIO17)."""
    p = Pin.board
    return I2C(0, scl=p.OLED_SCL, sda=p.OLED_SDA, freq=_I2C_FREQ)


def make_oled(i2c=None):
    """Return the SSD1306 display (``ssd1306.SSD1306_I2C``: initialised,
    cleared and on), None when no device answers at `OLED_ADDRESS`.

    i2c: its bus (None: `make_i2c`).
    """
    import ssd1306  # frozen in the firmware

    if i2c is None:
        i2c = make_i2c()
    if OLED_ADDRESS not in i2c.scan():
        return None
    return ssd1306.SSD1306_I2C(OLED_WIDTH, OLED_HEIGHT, i2c, addr=OLED_ADDRESS)


def make_srr_uart():
    """Open the SRR receiver's UART (UART1, 38400 baud 8N1) and return it."""
    return UART(
        SRR_UART,
        baudrate=SRR_BAUDRATE,
        bits=8,
        parity=None,
        stop=1,
        tx=SRR_TX,
        rx=SRR_RX,
    )

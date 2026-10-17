"""ESP32 platform of the mesh: NVS storage, RTC retained memory, hardware RNG.

Implements the contracts documented in ``mesh.platform`` (MESH_DESIGN §1,
§6, §14) without importing the mesh (duck typing).

- `NvsStorage`: one ``esp32.NVS`` namespace. ESP-IDF writes a value when it
  is set; ``commit`` is the point after which the caller may rely on it.
- `RtcRetained`: ``machine.RTC().memory()``, the RTC slow memory that
  MicroPython keeps across soft resets, ``machine.reset()`` and the watchdog
  (``RTC_NOINIT_ATTR``), lost on power loss. 2048 bytes on the ESP32-S3
  (``MICROPY_HW_RTC_USER_MEM_MAX``, measured on the T3S3).
- `HardwareRandom`: ``os.urandom``, the ESP32 hardware random number
  generator (true random while WiFi or Bluetooth runs, pseudo-random
  otherwise, per ESP-IDF; enough for backoff and first sequence numbers).
"""

import os

import esp32
from machine import RTC

RTC_MEMORY_SIZE = 2048

_NOT_FOUND = -4354  # ESP_ERR_NVS_NOT_FOUND
_INVALID_LENGTH = -4364  # ESP_ERR_NVS_INVALID_LENGTH
_MAX_VALUE = 4000  # longest blob read (an NVS blob of one page)


class NvsStorage:
    """Persistent key -> bytes store on the NVS namespace namespace."""

    def __init__(self, namespace="mesh", buffer_size=64):
        self._nvs = esp32.NVS(namespace)
        self._buf = bytearray(buffer_size)

    def get(self, key):
        buf = self._buf
        while True:
            try:
                n = self._nvs.get_blob(key, buf)
            except OSError as e:
                if e.errno == _NOT_FOUND:
                    return None
                if e.errno == _INVALID_LENGTH and len(buf) < _MAX_VALUE:
                    buf = bytearray(min(2 * len(buf), _MAX_VALUE))
                    continue
                raise
            return bytes(buf[:n])

    def set(self, key, value):
        self._nvs.set_blob(key, value)

    def erase(self, key):
        try:
            self._nvs.erase_key(key)
        except OSError as e:
            if e.errno != _NOT_FOUND:
                raise

    def commit(self):
        self._nvs.commit()


class RtcRetained:
    """Retained memory on the RTC user memory, size bytes from its start.

    ``data`` is restored from what was last saved (zeros after a power loss
    or if nothing was saved); ``save`` copies the whole of it back.
    """

    def __init__(self, size=RTC_MEMORY_SIZE):
        if size > RTC_MEMORY_SIZE:
            raise ValueError("RTC memory too small")
        self._rtc = RTC()
        self.data = bytearray(size)
        saved = self._rtc.memory()
        n = min(len(saved), size)
        self.data[:n] = saved[:n]

    def save(self):
        self._rtc.memory(self.data)


class HardwareRandom:
    """Random source on ``os.urandom``."""

    def getrandbits(self, bits):
        if bits < 1 or bits > 32:
            raise ValueError("bits out of range")
        return int.from_bytes(os.urandom(4), "little") & ((1 << bits) - 1)

    def randbytes(self, n):
        return os.urandom(n)

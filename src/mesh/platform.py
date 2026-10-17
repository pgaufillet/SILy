"""Platform contract: storage, retained memory, random source (MESH_DESIGN §1).

Injected into the mesh, so that it runs on the MicroPython unix port. All
calls are synchronous and short. There is no clock object: time is supplied
by the runner with each call to the core (`mesh.core`).

The classes below document the interfaces; implementations need not inherit
from them. The ESP32 platform comes with the board adapter, host fakes with
the simulator.

Uses (§6, §14): the frame counter and class 2 sender sequences go to storage
(NVS), the replay table and the duty-cycle window to retained memory (RTC
memory).
"""


class Storage:
    """Persistent key -> bytes store for one namespace (ESP32: ``esp32.NVS``).

    Keys are str of at most 15 characters. A written value is read back at
    once, but survives a reset or a power loss only after ``commit``: the
    caller decides when the flash is written. A namespace isolates its keys
    from the other namespaces of the same storage.
    """

    def get(self, key):
        """Return the value of key (bytes), or None if absent."""
        raise NotImplementedError

    def set(self, key, value):
        """Set key to value (bytes-like), durable after commit."""
        raise NotImplementedError

    def erase(self, key):
        """Remove key (no error if absent), durable after commit."""
        raise NotImplementedError

    def commit(self):
        """Make every previous set and erase durable."""
        raise NotImplementedError


class Retained:
    """Memory kept across resets, lost on power loss (ESP32: RTC memory).

    ``data`` is a bytearray of fixed size, worked on in place. ``save()``
    copies it to the retained memory: after a reset (soft reset, watchdog)
    ``data`` holds what was last saved; after a power loss it holds zeros.
    Users mark their content as valid themselves (e.g. a magic or a check).
    """

    data = None

    def save(self):
        """Keep the current content of data across resets."""
        raise NotImplementedError


class Random:
    """Random source (ESP32: hardware RNG; host: seeded generator)."""

    def getrandbits(self, bits):
        """Return an int of bits random bits, 1 <= bits <= 32."""
        raise NotImplementedError

    def randbytes(self, n):
        """Return n random bytes."""
        raise NotImplementedError


def randbelow(rng, n):
    """Return a uniform int in [0, n) from rng, 1 <= n <= 2**32 (rejection)."""
    if n < 1 or n > 1 << 32:
        raise ValueError("n out of range")
    bits = 0
    while (1 << bits) < n:
        bits += 1
    if bits == 0:
        return 0
    while True:
        r = rng.getrandbits(bits)
        if r < n:
            return r

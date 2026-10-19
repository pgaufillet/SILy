"""Platform fakes: storage with power loss, retained memory, random source.

They implement the contracts of `mesh.platform`; their extra methods simulate
what happens to a board: ``reset`` (soft reset, watchdog), ``power_loss`` and,
for the flash, a full erase.
"""

from mesh.platform import Retained, Storage

_ERASED = object()


class FakeFlash:
    """Persistent store shared by the namespaces of one board."""

    def __init__(self):
        self.committed = {}  # (namespace, key) -> bytes
        self._namespaces = []
        self.commits = 0

    def namespace(self, name):
        """Return the `FakeStorage` of namespace name."""
        storage = FakeStorage(self, name)
        self._namespaces.append(storage)
        return storage

    def drop_uncommitted(self):
        """Lose every uncommitted write: what a reset or power loss does.

        ESP-IDF guarantees durability only after commit, so any reset is
        treated as losing uncommitted writes.
        """
        for storage in self._namespaces:
            storage.pending = {}

    def full_erase(self):
        """Erase everything (``esptool erase_flash``)."""
        self.committed = {}
        self.drop_uncommitted()


class FakeStorage(Storage):
    """One namespace of a `FakeFlash`."""

    def __init__(self, flash, name):
        self.flash = flash
        self.name = name
        self.pending = {}  # key -> bytes or _ERASED

    def _check(self, key):
        if not isinstance(key, str) or not 0 < len(key) <= 15:
            raise ValueError("bad key")

    def get(self, key):
        self._check(key)
        value = self.pending.get(key)
        if value is _ERASED:
            return None
        if value is not None:
            return value
        return self.flash.committed.get((self.name, key))

    def set(self, key, value):
        self._check(key)
        self.pending[key] = bytes(value)

    def erase(self, key):
        self._check(key)
        self.pending[key] = _ERASED

    def commit(self):
        committed = self.flash.committed
        for key, value in self.pending.items():
            if value is _ERASED:
                committed.pop((self.name, key), None)
            else:
                committed[(self.name, key)] = value
        self.pending = {}
        self.flash.commits += 1


class FakeRetained(Retained):
    """Retained memory of size bytes (ESP32 RTC user memory: 2048)."""

    def __init__(self, size):
        self.data = bytearray(size)
        self._saved = bytearray(size)  # copied in place: saved on every frame

    def save(self):
        self._saved[:] = self.data

    def reset(self):
        """Soft reset: data returns to its last saved content."""
        self.data[:] = self._saved

    def power_loss(self):
        """Power loss: data and saved content become zeros."""
        self._saved[:] = bytes(len(self.data))
        self.data[:] = self._saved


class FakePlatform:
    """The platform objects of one simulated board.

    ``storage`` is the namespace ``namespace`` of ``flash``; ``random`` is the
    given generator (e.g. `sim.prng.XorShift32`).
    """

    def __init__(self, random, retained_size=2048, namespace="mesh"):
        self.flash = FakeFlash()
        self.storage = self.flash.namespace(namespace)
        self.retained = FakeRetained(retained_size)
        self.random = random

    def reset(self):
        """Soft reset or watchdog: retained memory kept, uncommitted lost."""
        self.flash.drop_uncommitted()
        self.retained.reset()

    def power_loss(self):
        """Power loss: retained memory cleared, uncommitted writes lost."""
        self.flash.drop_uncommitted()
        self.retained.power_loss()

"""Start decision at boot: crash count and one-shot skip flag.

The application starts at every boot, from ``main.py``. So that a box
caught in a crash loop, or a board on the bench, gets its REPL back, the
decision rests on two values of a key-value store (``board.platform``'s
`NvsStorage` on the board, namespace `NAMESPACE`; any store with ``get``,
``set``, ``erase``, ``commit``):

- the crash count: one more at each crash (the application then resets
  the board); from `MAX_CRASHES` on, the box boots in safe mode (no
  application, the REPL free); `STABLE_MS` of running clears it, and so
  does a power-on (switching the box off and on retries);
- the skip flag: set from the REPL (``sily.hw.skip_next_boot``, used by
  ``tools/bench.py`` before a hard reset), it makes the next boot return
  to the REPL at once, then clears itself; a power-on clears it too.

Values are written only when they change (flash wear).
"""

NAMESPACE = "sily"
MAX_CRASHES = 3
STABLE_MS = 600000  # 10 min

START = "start"
SKIP = "skip"
SAFE = "safe"

_CRASHES = "crashes"
_SKIP = "skip"


class Guard:
    """Start decision on store (see the module documentation)."""

    def __init__(self, store):
        self.store = store

    @property
    def crashes(self):
        """The crash count since the last stable run or power-on."""
        value = self.store.get(_CRASHES)
        return value[0] if value else 0

    def decide(self, power_on):
        """Return `START`, `SKIP` or `SAFE` for this boot; power_on: it follows one."""
        if power_on:
            self.clear()
        store = self.store
        if store.get(_SKIP) is not None:
            store.erase(_SKIP)
            store.commit()
            return SKIP
        if self.crashes >= MAX_CRASHES:
            return SAFE
        return START

    def crashed(self):
        """Count a crash; return the new count."""
        n = min(self.crashes + 1, 255)
        self.store.set(_CRASHES, bytes((n,)))
        self.store.commit()
        return n

    def stable(self):
        """The application ran `STABLE_MS`: clear the crash count."""
        if self.store.get(_CRASHES) is not None:
            self.store.erase(_CRASHES)
            self.store.commit()

    def skip_next(self):
        """Make the next boot return to the REPL without starting."""
        self.store.set(_SKIP, b"\x01")
        self.store.commit()

    def clear(self):
        """Clear the crash count and the skip flag (power-on, new configuration)."""
        store = self.store
        changed = False
        for key in (_CRASHES, _SKIP):
            if store.get(key) is not None:
                store.erase(key)
                changed = True
        if changed:
            store.commit()

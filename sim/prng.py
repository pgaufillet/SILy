"""Deterministic random generator (xorshift32), independent of `random`.

Implements the `mesh.platform.Random` contract. The same seed gives the same
sequence on any port and any run; the unix port's `random` module is not used
because its seeding and algorithm are build options.
"""

_MASK = 0xFFFFFFFF


def mix32(x):
    """Return a well-mixed 32-bit value of x (MurmurHash3 finaliser)."""
    x &= _MASK
    x ^= x >> 16
    x = (x * 0x85EBCA6B) & _MASK
    x ^= x >> 13
    x = (x * 0xC2B2AE35) & _MASK
    x ^= x >> 16
    return x


class XorShift32:
    """Marsaglia's xorshift32 seeded through mix32; seed: any int."""

    def __init__(self, seed):
        self.seed = seed
        state = mix32(seed ^ 0x9E3779B9)
        self._state = state if state else 0x6D2B79F5

    def next32(self):
        """Return the next 32-bit value."""
        x = self._state
        x ^= (x << 13) & _MASK
        x ^= x >> 17
        x ^= (x << 5) & _MASK
        self._state = x
        return x

    def getrandbits(self, bits):
        if bits < 1 or bits > 32:
            raise ValueError("bits out of range")
        return self.next32() >> (32 - bits)

    def randbytes(self, n):
        out = bytearray(n)
        for i in range(n):
            out[i] = self.next32() >> 24
        return bytes(out)

    def chance(self, p):
        """Return True with probability p (float in [0, 1]); one draw always."""
        return self.next32() < int(p * 4294967296)

    def derive(self, label):
        """Return an independent generator for label (int), from the seed only.

        Giving each node and the medium its own derived generator keeps their
        sequences unchanged when another consumer is added or removed.
        """
        return XorShift32(mix32(mix32(self.seed) + 0x632BE5AB * (label + 1)))

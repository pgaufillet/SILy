"""Multi-node runs in simulated time: reproducibility and neighbour filter.

Beacon cores (carrier sense, random backoff and jitter) run for one simulated
hour on lossy links; every random draw comes from the run's seed.
"""

import unittest

from mesh.radio import NeighbourFilter
from sim.medium import OK, Medium
from sim.node import Node
from sim.prng import XorShift32
from sim.scheduler import Scheduler

from simcores import BeaconCore, source_of

_HOUR = 3600 * 1000
_PERIOD = 10000
_JITTER = 1000
_AIRTIME = 50


def _run(seed, allowed=None, duration=_HOUR):
    """Run 4 beacon nodes; node 2 is filtered to allowed if given."""
    master = XorShift32(seed)
    s = Scheduler()
    m = Medium(s, lambda length: _AIRTIME, master.derive(0), trace=True)
    addresses = (1, 2, 3, 4)
    for a in addresses:
        m.attach(a)
    m.link_both(1, 2, loss=0.1, rssi=-90, snr=3)
    m.link_both(2, 3, loss=0.2, rssi=-100, snr=-2)
    m.link_both(1, 3, loss=0.05)
    m.link(3, 4, loss=0.3)  # asymmetric: 4 hears 3 badly, 3 never hears 4
    cores = {}
    radios = {}
    for a in addresses:
        radio = m.ports[a]
        if a == 2 and allowed is not None:
            radio = NeighbourFilter(radio, allowed, source_of)
        core = BeaconCore(a, master.derive(a), _PERIOD, _JITTER, _AIRTIME)
        Node(s, core, radio).start()
        cores[a] = core
        radios[a] = radio
    s.run(duration, limit=200000)
    return cores, radios, m


def _deliveries(cores):
    return dict((a, [(t, src, seq) for t, src, seq, _, _ in c.received]) for a, c in cores.items())


class Reproducibility(unittest.TestCase):
    def test_same_seed_same_run(self):
        first, _, m1 = _run(3)
        second, _, m2 = _run(3)
        self.assertEqual(_deliveries(first), _deliveries(second))
        self.assertEqual(m1.trace, m2.trace)

    def test_different_seed_different_run(self):
        first, _, _ = _run(3)
        other, _, _ = _run(4)
        self.assertNotEqual(_deliveries(first), _deliveries(other))

    def test_run_is_plausible(self):
        cores, _, m = _run(3)
        for core in cores.values():
            # One frame per period + jitter + airtime (+ rare backoffs).
            self.assertTrue(_HOUR // 11500 < core.sent <= _HOUR // 10050, core.sent)
        outcomes = [t[3] for t in m.trace]
        self.assertTrue(outcomes.count(OK) > outcomes.count("loss") > 0)
        self.assertEqual([r for r in cores[3].received if r[1] == 4], [])
        self.assertTrue(cores[4].received)
        self.assertEqual(m.stats(1).tx_ms, cores[1].sent * _AIRTIME)


class Filter(unittest.TestCase):
    def test_filtered_source_never_delivered(self):
        cores, radios, m = _run(3, allowed=set((1,)))
        sources = set(r[1] for r in cores[2].received)
        self.assertEqual(sources, set((1,)))
        # Node 3 did transmit and node 2's radio did receive it.
        heard_from_3 = [t for t in m.trace if t[1] == 3 and t[2] == 2 and t[3] == OK]
        self.assertTrue(heard_from_3)
        self.assertEqual(radios[2].filtered, len(heard_from_3))
        self.assertTrue(any(r[1] == 2 for r in cores[1].received))  # 2 still sends

    def test_unfiltered_node_hears_both(self):
        cores, _, _ = _run(3)
        self.assertEqual(set(r[1] for r in cores[2].received), set((1, 3)))

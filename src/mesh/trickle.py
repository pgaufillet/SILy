"""Trickle timer (RFC 6206; MESH_DESIGN §9 Trickle and control traffic).

Pure: times in ms are passed in, random draws come from an injected
`mesh.platform.Random`. The timer only decides **when** an advertisement (a
frame holding one DODAG information message, port 9) is sent; the caller
builds and queues it (`mesh.dodag`).

- Intervals I from ``imin`` (32 slots, §9) doubling up to ``imax`` (the
  presence period). In each interval the advertisement is due at a random
  time t in [I/2, I).
- **Suppression** (k = 2): at t the advertisement is skipped if the node
  already sent a frame in the interval (any frame advertises version and
  rank, `transmitted`) or heard `K` frames of its own version from
  neighbours of finite rank (`heard`).
- **Information needed** (reading, see below): after a reset that a
  neighbour can only satisfy with the DODAG information itself (a
  solicitation, a neighbour of an older version, a new version: version and
  parameters travel only in port 9 messages), the next decision counts port 9
  messages only: own (``transmitted(True)``) and heard (``heard(True)``).
- **At Imax** (intervals reached by doubling): nothing is sent, since a
  node's presence frame, sent every presence period, advertises anyway (§9:
  "in steady state Trickle sends nothing"). The root beacon (§16) is not a
  Trickle advertisement: `mesh.dodag` sends it on its own periodic timer,
  which Trickle resets neither postpone nor suppress.
- **Reset** (`reset`): I = Imin and a new interval starts, unless I already
  equals Imin and the advertisement of the current interval is still to come
  (the header and payload are built at transmission, so it carries the new
  state; restarting would only postpone it: RFC 6206 rule 6).

`poll` returns `URGENT` when an advertisement is due (every one follows a
reset: priority 1, §6), 0 otherwise. A stopped timer (``interval`` None: a
detached node) decides nothing; `reset` starts it.

Readings where §9 is silent: what "Imax = the presence period, in steady
state Trickle sends nothing" means (no advertisement at Imax, the root
included: its beacon has its own timer); which frames suppress an advertisement that
answers a need for the DODAG information (port 9 ones only: a neighbour
cannot adopt a version from a frame header, §9); the reset that keeps a
pending advertisement.
"""

from mesh.platform import randbelow

K = 2  # redundancy constant (§9)
IMIN_SLOTS = 32  # Imin in slots (§8, §9)

# poll result.
URGENT = 1  # advertisement after a reset (port 9 priority 1)


class Trickle:
    """Trickle timer of one node (see the module docstring).

    imin_ms, imax_ms: shortest and longest interval (imax below imin is
    raised to imin); random: a `mesh.platform.Random`; k: redundancy
    constant.

    Read: ``interval`` (ms, None when stopped), ``start_at``, ``send_at``,
    ``end_at``, ``count`` (frames of the own version heard in the interval),
    ``info_count`` (port 9 messages of them), ``need_info``; counters
    ``resets`` (triggers), ``sends``, ``suppressed``.
    """

    def __init__(self, imin_ms, imax_ms, random, k=K):
        self.random = random
        self.k = k
        self.imin = 1
        self.imax = 1
        self.configure(imin_ms, imax_ms)
        self.interval = None
        self.start_at = None
        self.send_at = None
        self.end_at = None
        self.first = False  # first interval after a reset
        self.decided = False
        self.count = 0
        self.info_count = 0
        self.sent = False
        self.info_sent = False
        self.need_info = False
        self.resets = 0
        self.sends = 0
        self.suppressed = 0

    def configure(self, imin_ms, imax_ms):
        """Set Imin and Imax (new network-wide parameters, §9); applies from the next interval."""
        if imin_ms < 1:
            raise ValueError("bad Imin")
        self.imin = imin_ms
        self.imax = imax_ms if imax_ms > imin_ms else imin_ms

    @property
    def running(self):
        return self.interval is not None

    def at_imax(self):
        """Return True in an interval of Imax reached by doubling (steady state)."""
        return self.interval is not None and not self.first and self.interval >= self.imax

    # --- Events ----------------------------------------------------------------------

    def reset(self, now, need_info=False):
        """Reset I to Imin (an event of §9), starting the timer if stopped.

        need_info: the event asks neighbours for the DODAG information
        itself (solicitation, older version, new version). Returns True if
        a new interval started, False if the pending advertisement of the
        current Imin interval is kept.
        """
        self.resets += 1
        if need_info:
            self.need_info = True
        if self.interval == self.imin and self.first and not self.decided:
            return False
        self._begin(now, self.imin)
        self.first = True
        return True

    def stop(self):
        """Stop the timer (a node without route advertises nothing)."""
        self.interval = None
        self.start_at = self.send_at = self.end_at = None
        self.need_info = False

    def heard(self, info=False):
        """A frame of the own version from a neighbour of finite rank (info False).

        info True: one DODAG information message of the own version, in a
        frame already counted (the caller reports the frame, then its port 9
        message).
        """
        if info:
            self.info_count += 1
        else:
            self.count += 1

    def transmitted(self, info=False):
        """The node sent a frame; info: it held a DODAG information message."""
        self.sent = True
        if info:
            self.info_sent = True

    # --- Time --------------------------------------------------------------------------

    def poll(self, now):
        """Advance to now; return `URGENT` if an advertisement is due, else 0.

        A late poll catches up interval by interval, one advertisement at most.
        """
        due = 0
        while self.interval is not None:
            if not self.decided and now >= self.send_at:
                self.decided = True
                if not due:
                    due = self._decide()
            if now < self.end_at:
                break
            nxt = self.interval * 2
            self._begin(self.end_at, nxt if nxt < self.imax else self.imax)
            self.first = False
        return due

    def timer(self):
        """Return the time of the next decision or interval end, or None when stopped."""
        if self.interval is None:
            return None
        return self.end_at if self.decided else self.send_at

    def _begin(self, start, interval):
        self.interval = interval
        self.start_at = start
        half = interval // 2
        self.send_at = start + half + randbelow(self.random, interval - half)
        self.end_at = start + interval
        self.decided = False
        self.count = 0
        self.info_count = 0
        self.sent = False
        self.info_sent = False

    def _decide(self):
        if self.need_info:
            self.need_info = False
            if self.info_sent or self.info_count >= self.k:
                self.suppressed += 1
                return 0
        elif self.at_imax():
            return 0
        elif self.sent or self.count >= self.k:
            self.suppressed += 1
            return 0
        self.sends += 1
        return URGENT

"""Field sniffer: one console line per frame the radio hears (`run` board only).

Runs on a board configured as a box (``/config.json``, ``make
configure``): the radio is set up as the application sets it, from the
configuration's regional profile, SF and power (``sily.config``,
``sily.radio.lora_config``, the adapter of ``sily.hw``,
``board.sx1262.Sx1262``); it receives continuously and never transmits
(the adapter's ``transmit`` is never called). The configuration's key is
neither used nor printed: the frames are opened on the host
(``tools/capture.py``).

**The REPL.** ``make sniff`` (``tools/sniff.py``) runs `run` through
``mpremote exec``, as ``make box-check`` runs its check: mpremote's
Ctrl-C at connection stops the application (``SILY STOP``: journal
closed, radio in standby), then its soft reset gives a fresh interpreter
in which ``main.py`` does not run (no application) and ``boot.py`` keeps
the hardware watchdog fed by its timer (``sily.watchdog``). `run`
returns at the next Ctrl-C, that of the second mpremote ``make sniff``
starts once its own is interrupted: the radio is put to sleep and the
board stays at the REPL; `stop`, run by that second mpremote (``resume``:
the same interpreter), prints the end line. Nothing touches the start
guard (``sily.guard``: no skip flag, no crash counted): the board's next
reset (``mpremote reset``, the power switch) starts the application; the
end line tells the guard's state (``crashes``, ``skip``) as a check.

**Lines** (`FORMAT`), fields separated by one space:

- ``SNIFF {...}`` first, a JSON object: ``fmt`` (`FORMAT`), the box's
  ``name``, ``address``, ``role`` and key ``fingerprint`` (not the key),
  the radio settings (``profile`` identifier, its ``pname`` and ``hz``,
  ``sf``, ``bw`` Hz, ``cr`` (4/cr), ``preamble`` symbols, ``sync`` word,
  ``power`` dBm), ``t`` the board's ms ticks and ``utc`` its RTC (ms
  since the Unix epoch, ``sily.utc``; before 2025 when never set) once
  the radio receives, ``tp`` the ticks' period (they wrap: ``t`` modulo
  ``tp``);
- ``F T UTC RSSI SNR HEX``: a frame with a valid CRC; T and UTC as
  ``t``, ``utc`` when the adapter reports it (its RxDone serviced: the
  end of the frame on air, plus the loop's delay, expected under a ms on
  this idle loop), RSSI dBm, SNR dB (quarter-dB steps), HEX the frame's bytes;
- ``C T UTC RSSI SNR``: a frame whose CRC failed (its bytes not read: a
  collision, a weak frame);
- ``END {...}`` (`stop`): ``frames`` and ``crc`` lines printed, the
  adapter's counters (``board.sx1262.Sx1262.stats``: ``hdr_err`` header
  errors, ``preambles``, ``headers``, ``errors``, ``rx_restarts``,
  ``noise`` floor dBm) and the start guard's ``crashes`` and ``skip``.
  Missing when the board was reset or unplugged before.

Per frame: one ticks read, one RTC read, the bytes in hex and one print,
in the adapter's reception callback (about a ms on the board, estimated:
below the shortest frame's time on air, so the next frame is serviced in
time). Nothing is written to flash.
"""

import asyncio
import json
from binascii import hexlify
from time import ticks_add, ticks_ms

from mesh import profile as profiles
from sily import config
from sily.guard import NAMESPACE, Guard
from sily.radio import PREAMBLE, SYNC_WORD, lora_config

CONFIG_PATH = "/config.json"
FORMAT = 1

SETTINGS = "SNIFF"
FRAME = "F"
CRC = "C"
END = "END"
ERROR = "SNIFF ERROR"

current = None  # the Sniffer of the last run (`stop`)


def ticks_period():
    """Return the period of ``time.ticks_ms`` on this port (2**30 on the board)."""
    return ticks_add(0, -1) + 1


def settings(cfg, regional, t, utc_ms, period):
    """Return the fields of the ``SNIFF`` line of configuration cfg (``sily.config``)
    and its profile object, regional, at ticks t and RTC utc_ms."""
    return {
        "fmt": FORMAT,
        "name": cfg["name"],
        "address": cfg["address"],
        "role": cfg["role"],
        "fingerprint": cfg["fingerprint"],
        "profile": regional.ident,
        "pname": regional.name,
        "hz": regional.frequency_hz,
        "sf": cfg["sf"],
        "bw": regional.bandwidth_hz,
        "cr": regional.coding_rate,
        "preamble": PREAMBLE,
        "sync": SYNC_WORD,
        "power": cfg["tx_power"],
        "t": t,
        "utc": utc_ms,
        "tp": period,
    }


def frame_line(t, utc_ms, rssi, snr, frame):
    """Return the ``F`` line of a frame received at ticks t, RTC utc_ms."""
    return "%s %d %d %d %s %s" % (FRAME, t, utc_ms, rssi, snr, hexlify(frame).decode())


def crc_line(t, utc_ms, rssi, snr):
    """Return the ``C`` line of a frame whose CRC failed."""
    return "%s %d %d %d %s" % (CRC, t, utc_ms, rssi, snr)


class Sniffer:
    """Prints a line per frame adapter reports (``board.sx1262.Sx1262`` API).

    clock: the ms ticks (``time.ticks_ms``); utc_ms: the RTC (``sily.hw``);
    write(line): the console. ``frames``, ``crc``: the lines printed;
    ``hw``: the hardware bundle of the run (`stop` reads its start guard).
    """

    def __init__(self, adapter, clock, utc_ms, write, hw=None):
        self.adapter = adapter
        self.hw = hw
        self.clock = clock
        self.utc_ms = utc_ms
        self.write = write
        self.frames = 0
        self.crc = 0
        adapter.on_receive = self._received
        adapter.on_crc_error = self._crc_error
        adapter.on_transmitted = None  # it never transmits

    def _received(self, frame, rssi, snr):
        self.frames += 1
        self.write(frame_line(self.clock(), self.utc_ms(), rssi, snr, frame))

    def _crc_error(self, rssi, snr):
        self.crc += 1
        self.write(crc_line(self.clock(), self.utc_ms(), rssi, snr))

    def close(self):
        """Put the radio to sleep (idempotent)."""
        self.adapter.modem.sleep()

    def end(self, store):
        """Return the fields of the ``END`` line; store: the start guard's."""
        stats = self.adapter.stats()
        out = {"frames": self.frames, "crc": self.crc}
        for name in ("hdr_err", "preambles", "headers", "errors", "rx_restarts", "noise"):
            if name in stats:
                out[name] = stats[name]
        g = Guard(store)
        out["crashes"] = g.crashes
        out["skip"] = 1 if g.skipping else 0
        return out


def _read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def run(hw=None, path=CONFIG_PATH, write=print):
    """Sniff until Ctrl-C (module documentation); return the `Sniffer`.

    hw: the hardware bundle (``sily.hw`` when None); write(line): the
    console. Without a valid configuration, one ``SNIFF ERROR field:
    problem`` line per problem and None: the radio is not touched.
    """
    global current
    if hw is None:
        from sily import hw
    text = _read(path)
    if text is None:
        write("%s file: %s missing (make configure)" % (ERROR, path))
        return None
    cfg, errors = config.load(text)
    if errors:
        for e in errors:
            write("%s %s" % (ERROR, e))
        return None
    regional = profiles.get(cfg["profile"])
    adapter = hw.adapter(lora_config(regional, cfg["sf"], cfg["tx_power"]))
    adapter.start()  # continuous reception, then the noise floor
    s = current = Sniffer(adapter, ticks_ms, hw.utc_ms, write, hw)
    fields = settings(cfg, regional, ticks_ms(), hw.utc_ms(), ticks_period())
    write("%s %s" % (SETTINGS, json.dumps(fields)))
    asyncio.new_event_loop()
    try:
        asyncio.run(adapter.run())
    except KeyboardInterrupt:
        pass
    finally:
        asyncio.new_event_loop()
        s.close()
    return s


def stop(write=print):
    """Put the radio of the last run to sleep and print its ``END`` line
    (none without a run in this interpreter)."""
    s = current
    if s is None:
        write("%s no run in this interpreter" % ERROR)
        return
    s.close()
    write("%s %s" % (END, json.dumps(s.end(s.hw.storage(NAMESPACE)))))

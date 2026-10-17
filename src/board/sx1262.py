"""SX1262 radio adapter on micropython-lib's lora.AsyncSX1262 (MESH_DESIGN §1, §4, §8).

Hardware side of the mesh radio contract (``mesh.radio``), without importing
the mesh: it reports through three callbacks, which the application binds to
the contract's events (``sily.radio``):

- ``on_receive(frame, rssi, snr)``: a frame with a valid LoRa CRC; frame is a
  bytearray, rssi in dBm (int), snr in dB (float, quarter-dB steps);
- ``on_crc_error(rssi, snr)``: a frame with a bad CRC;
- ``on_transmitted(duration_ms)``: the frame given to `transmit` is on air
  no more; duration_ms is measured from SetTx to the TxDone IRQ, rounded up.

The radio receives continuously (single LoRa channel, nodes always listen)
and leaves reception only to transmit. The driver is used through its
low-level API (``prepare_send``, ``start_send``, ``start_recv``) and a few
raw commands; its receive coroutines are not used.

**IRQs.** The driver enables RxDone, TxDone, Timeout and CrcErr only; the
adapter adds PreambleDetected, HeaderValid and HeaderErr to the IRQ mask and
to DIO1, as the spike added CadDone (``spike/cad.py``). An asyncio task waits
on DIO1 and services the IRQ status: preamble and header IRQs open a
`board.sense.Reception` window (a frame is being received), RxDone and
HeaderErr close it.

**Carrier sense** (`busy`, §8): busy while transmitting, while a frame is
being received (PreambleDetected or HeaderValid without RxDone; this also
covers frames below the noise floor) or when the instantaneous RSSI
(GetRssiInst) is above the noise floor + 10 dB. The noise floor is the median
of RSSI samples taken at start (`start`) and refreshed every second while
nothing is received (`board.sense.NoiseFloor`). CAD is not used (spike: poor
detection).

**Watchdogs.** A transmission without TxDone after twice its time on air
plus 100 ms is aborted and reported (``tx_timeouts``) with its elapsed time,
so that the mesh core is never left waiting; a radio found outside reception
while idle is put back into it (``rx_restarts``).

Counters (``stats()``) and CPU time of the radio paths (``cpu``, µs: count,
total, maximum) serve the bench measurements (§18).
"""

import asyncio
from time import sleep_ms, ticks_add, ticks_diff, ticks_ms, ticks_us

from micropython import const

from board.sense import NoiseFloor, Reception

# SX126x commands and IRQ bits (Semtech SX1261/2 datasheet §13).
_CMD_CFG_DIO_IRQ = const(0x08)
_CMD_GET_RX_BUFFER_STATUS = const(0x13)
_CMD_GET_PACKET_STATUS = const(0x14)
_CMD_GET_RSSI_INST = const(0x15)
_CMD_READ_BUFFER = const(0x1E)

_IRQ_TX_DONE = const(0x001)
_IRQ_RX_DONE = const(0x002)
_IRQ_PREAMBLE = const(0x004)
_IRQ_HEADER_VALID = const(0x010)
_IRQ_HEADER_ERR = const(0x020)
_IRQ_CRC_ERR = const(0x040)
_IRQ_TIMEOUT = const(0x200)
_IRQ_MASK = const(0x277)  # all of the above, on DIO1 too

_MODE_RX = const(5)

_HOUSEKEEPING_MS = const(100)  # watchdogs and missed IRQs
_NOISE_EVERY = const(10)  # housekeeping rounds per noise floor sample (1 s)
_RX_CHECK_EVERY = const(50)  # housekeeping rounds per reception check (5 s)
_CALIBRATION_SAMPLES = const(32)


def _meter():
    return [0, 0, 0]  # count, total µs, max µs


def _account(meter, t0):
    dt = ticks_diff(ticks_us(), t0)
    meter[0] += 1
    meter[1] += dt
    if dt > meter[2]:
        meter[2] = dt


class Sx1262:
    """Mesh radio adapter on a configured lora.AsyncSX1262 modem.

    modem: built and configured (frequency, SF, bandwidth, coding rate,
    preamble, sync word, power: ``board.t3s3.make_modem``), in standby.
    Call `start` once, then run `run` as an asyncio task.
    """

    def __init__(self, modem):
        self.modem = modem
        self.on_receive = None
        self.on_crc_error = None
        self.on_transmitted = None
        t_sym_us = modem._get_t_sym_us()
        # PreambleDetected to HeaderValid: the rest of the preamble, the sync
        # word and 4.25 symbols, the 8 header symbols, plus a margin of 4.
        preamble_ms = (modem._preamble_len + 17) * t_sym_us // 1000 + 1
        frame_ms = modem.get_time_on_air_us(255) // 1000 + 1
        self.reception = Reception(preamble_ms, frame_ms)
        self.noise = NoiseFloor()
        self.frame_ms = frame_ms
        # Counters.
        self.rx_frames = 0
        self.crc_errors = 0
        self.header_errors = 0
        self.preambles = 0
        self.headers = 0
        self.tx_frames = 0
        self.tx_timeouts = 0
        self.rx_restarts = 0
        self.busy_tx = 0
        self.busy_rx = 0
        self.busy_rssi = 0
        self.senses = 0
        self.last_rssi = None  # of the last frame received
        self.last_snr = None
        self.last_sense_rssi = None
        self.last_tx_us = 0  # measured duration of the last transmission
        self.last_tx_len = 0
        self.tx_us_total = 0  # measured durations of the mesh's transmissions
        self.tx_toa_us_total = 0  # their time on air computed by the driver
        self.cpu = {
            "rx": _meter(),
            "irq": _meter(),
            "sense": _meter(),
            "tx": _meter(),
            "tx_done": _meter(),
        }
        # State.
        self._flag = asyncio.ThreadSafeFlag()
        self._irq_ms = 0
        self._irq_us = 0
        self._tx = False
        self._raw = False
        self._tx_us = 0
        self._tx_toa_us = 0
        self._tx_deadline = 0
        self._rounds = 0
        self._frame = None
        self.errors = 0  # exceptions of the radio paths (SPI, chip status)
        self.last_error = None
        modem.set_irq_callback(self._isr)
        modem._cmd(">BHHHH", _CMD_CFG_DIO_IRQ, _IRQ_MASK, _IRQ_MASK, 0, 0)
        try:
            modem.calibrate_image()  # 863-870 MHz band (reset value: 902-928 MHz)
        except ValueError:
            pass  # no image calibration known for this frequency
        modem._clear_irq()

    def _isr(self):
        # From the driver's DIO1 handler (soft IRQ): timestamp, wake the task.
        self._irq_ms = ticks_ms()
        self._irq_us = ticks_us()
        self._flag.set()

    # --- Control -----------------------------------------------------------------

    def start(self):
        """Start continuous reception and measure the noise floor (about 70 ms)."""
        self._receive()
        samples = []
        for _ in range(_CALIBRATION_SAMPLES):
            sleep_ms(2)
            samples.append(self.rssi_inst())
        self.noise.calibrate(samples)

    async def run(self):
        """Service the radio IRQs for ever (asyncio task)."""
        asyncio.create_task(self._housekeeping())
        flag = self._flag
        while True:
            await flag.wait()
            self._guarded(self._service)

    def _guarded(self, fn):
        # A radio error must not end the task: count it, keep the last one.
        try:
            return fn()
        except Exception as e:  # SPI, chip status, driver
            self.errors += 1
            self.last_error = repr(e)
            return None

    def rssi_inst(self):
        """Return the instantaneous RSSI (dBm), in reception."""
        return -self.modem._cmd("B", _CMD_GET_RSSI_INST, n_read=2)[1] // 2

    # --- Contract ----------------------------------------------------------------

    def transmit(self, frame):
        """Leave reception and transmit frame; ``on_transmitted`` follows."""
        t0 = ticks_us()
        m = self.modem
        self.reception.end()
        self._tx = True
        self.last_tx_len = len(frame)
        self._tx_toa_us = m.get_time_on_air_us(len(frame))
        try:
            m.prepare_send(frame)
            self._tx_us = ticks_us()
            m.start_send()
        except Exception as e:  # SPI, chip status: report the frame as not sent
            self.errors += 1
            self.last_error = repr(e)
            self._tx = False
            raw = self._raw
            self._raw = False
            self._guarded(self._receive)
            if not raw and self.on_transmitted is not None:
                self.on_transmitted(0)
            return
        self._tx_deadline = ticks_add(ticks_ms(), 2 * self._tx_toa_us // 1000 + 100)
        _account(self.cpu["tx"], t0)

    def busy(self):
        """Carrier sense (§8): return True if the channel is busy."""
        t0 = ticks_us()
        result = self._sense()
        _account(self.cpu["sense"], t0)
        return result

    def _sense(self):
        self.senses += 1
        if self._tx:
            self.busy_tx += 1
            return True
        self._guarded(self._service)  # IRQs raised but not serviced yet
        if self.reception.active(ticks_ms()):
            self.busy_rx += 1
            return True
        rssi = self._guarded(self.rssi_inst)
        self.last_sense_rssi = rssi
        if rssi is None or self.noise.busy(rssi):
            self.busy_rssi += 1
            return True
        return False

    # --- Debug ---------------------------------------------------------------------

    async def transmit_raw(self, frame, cut_ms=None):
        """Transmit frame outside the mesh (no ``on_transmitted``); bench only.

        cut_ms: abort the transmission after cut_ms, so that receivers get a
        truncated frame (a CRC error). Returns the measured duration in µs, or
        None if cut or timed out. The mesh core senses the channel busy
        meanwhile, so it never transmits over it.
        """
        if self._tx:
            return None
        self._raw = True
        self.transmit(frame)
        if cut_ms is not None:
            await asyncio.sleep_ms(cut_ms)
            if self._tx:
                self._abort()
                return None
        while self._tx:
            await asyncio.sleep_ms(5)
        return self.last_tx_us

    # --- Internals -----------------------------------------------------------------

    def _receive(self):
        m = self.modem
        m._tx = False
        m.start_recv(None, True)

    def _abort(self):
        self.modem.standby()
        self._receive()
        self._tx = False
        self._raw = False

    def _service(self):
        m = self.modem
        while True:
            flags = m._get_irq() & _IRQ_MASK
            if not flags:
                return
            t0 = ticks_us()
            if self._tx:
                m._clear_irq(flags)
                if flags & _IRQ_TX_DONE:
                    self._tx_done(t0)
                continue
            if flags & _IRQ_RX_DONE:
                self._rx_done(flags)
                m._clear_irq(flags)
                self.reception.end()
                _account(self.cpu["rx"], t0)
                if flags & _IRQ_CRC_ERR:
                    if self.on_crc_error is not None:
                        self.on_crc_error(self.last_rssi, self.last_snr)
                elif self.on_receive is not None:
                    self.on_receive(self._frame, self.last_rssi, self.last_snr)
                self._frame = None
                continue
            m._clear_irq(flags)
            if flags & _IRQ_HEADER_ERR:
                self.header_errors += 1
                self.reception.end()
            elif flags & _IRQ_HEADER_VALID:
                self.headers += 1
                self.reception.header(self._irq_ms)
            elif flags & _IRQ_PREAMBLE:
                self.preambles += 1
                self.reception.preamble(self._irq_ms)
            if flags & _IRQ_TIMEOUT:  # not in continuous reception: resume it
                self.rx_restarts += 1
                self._receive()
            _account(self.cpu["irq"], t0)

    def _rx_done(self, flags):
        m = self.modem
        status = m._cmd("B", _CMD_GET_PACKET_STATUS, n_read=4)
        self.last_rssi = -status[1] // 2
        snr = status[2]
        self.last_snr = ((snr ^ 0x80) - 0x80) / 4
        self._frame = None
        if flags & _IRQ_CRC_ERR:
            self.crc_errors += 1
            return
        res = m._cmd("B", _CMD_GET_RX_BUFFER_STATUS, n_read=3)
        n = res[1]
        ptr = res[2]
        frame = bytearray(n)
        m._cmd("BB", _CMD_READ_BUFFER, ptr, n_read=1, read_buf=frame)
        self._frame = frame
        self.rx_frames += 1

    def _tx_done(self, t0):
        irq_us = self._irq_us
        if ticks_diff(irq_us, self._tx_us) <= 0:  # IRQ not seen (polled)
            irq_us = t0
        self.last_tx_us = ticks_diff(irq_us, self._tx_us)
        self.tx_frames += 1
        self._receive()
        self._tx = False
        raw = self._raw
        self._raw = False
        _account(self.cpu["tx_done"], t0)
        if not raw:
            self._report(self.last_tx_us)

    def _report(self, duration_us):
        self.tx_us_total += duration_us
        self.tx_toa_us_total += self._tx_toa_us
        if self.on_transmitted is not None:
            self.on_transmitted((duration_us + 999) // 1000)

    async def _housekeeping(self):
        while True:
            await asyncio.sleep_ms(_HOUSEKEEPING_MS)
            self._guarded(self._service)
            self._guarded(self._watch)

    def _watch(self):
        now = ticks_ms()
        if self._tx:
            if ticks_diff(now, self._tx_deadline) >= 0:
                self.tx_timeouts += 1
                elapsed = ticks_diff(ticks_us(), self._tx_us)
                raw = self._raw
                self._abort()
                self.last_tx_us = elapsed
                if not raw:
                    self._report(elapsed)
            return
        self._rounds += 1
        if self.reception.active(now):
            return
        if self._rounds % _NOISE_EVERY == 0:
            self.noise.add(self.rssi_inst())
        if self._rounds % _RX_CHECK_EVERY == 0:
            if self.modem._get_status()[0] != _MODE_RX:
                self.rx_restarts += 1
                self._receive()

    def stats(self):
        """Return the counters and the noise floor as a dict."""
        return {
            "rx": self.rx_frames,
            "crc": self.crc_errors,
            "hdr_err": self.header_errors,
            "preambles": self.preambles,
            "headers": self.headers,
            "stale": self.reception.stale,
            "tx": self.tx_frames,
            "tx_timeouts": self.tx_timeouts,
            "rx_restarts": self.rx_restarts,
            "errors": self.errors,
            "last_error": self.last_error,
            "senses": self.senses,
            "busy_tx": self.busy_tx,
            "busy_rx": self.busy_rx,
            "busy_rssi": self.busy_rssi,
            "noise": self.noise.floor,
            "noise_rejected": self.noise.rejected,
            "last_rssi": self.last_rssi,
            "last_snr": self.last_snr,
            "last_tx_us": self.last_tx_us,
            "last_tx_len": self.last_tx_len,
            "tx_us_total": self.tx_us_total,
            "tx_toa_us_total": self.tx_toa_us_total,
            "cpu": self.cpu,
        }

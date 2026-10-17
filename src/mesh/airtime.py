"""LoRa time on air and demodulation limits (MESH_DESIGN §4, §5).

Time on air follows the Semtech SX1261/2 datasheet (§6.1.4) for spreading
factors 7 to 12, as the airtime budget of MESH_DESIGN §16 does: preamble +
4.25 symbols, then 8 symbols plus the payload symbols, rounded up to whole
coding-rate blocks. Low data rate optimisation is on when a symbol lasts
16 ms or more, the rule the micropython-lib driver applies when it configures
the radio, so that the computed and the real time on air agree.

Integer arithmetic only, without big integers in the millisecond path.
Defaults are the v1 LoRa parameters of §4: BW 125 kHz, CR 4/5, explicit
header, CRC on, preamble of 8 symbols.
"""

SF_MIN = 7
SF_MAX = 12

# SX1262 bandwidths in Hz, as the micropython-lib driver counts them.
BANDWIDTHS = (7800, 10400, 15600, 20800, 31250, 41700, 62500, 125000, 250000, 500000)

# Demodulation limits (SNR, tenths of dB) for SF7 to SF12 (§5, Semtech datasheet).
_SNR_LIMITS_X10 = (-75, -100, -125, -150, -175, -200)


def _check_sf(sf):
    if sf < SF_MIN or sf > SF_MAX:
        raise ValueError("sf out of range")


def low_data_rate(sf, bandwidth_hz):
    """Return True if low data rate optimisation is on (symbol >= 16 ms)."""
    # 2**sf / bw >= 16 ms, without big integers.
    return (1 << sf) * 125 >= 2 * bandwidth_hz


def symbols_x4(length, sf, bandwidth_hz=125000, coding_rate=5, preamble=8, crc=True, header=True):
    """Return four times the number of symbols of a LoRa packet.

    length: payload bytes (0 to 255); coding_rate: 5 to 8 for 4/5 to 4/8;
    header: explicit header. The fixed 4.25 symbols make the count a multiple
    of a quarter symbol.
    """
    _check_sf(sf)
    if length < 0 or length > 255:
        raise ValueError("length out of range")
    if coding_rate < 5 or coding_rate > 8:
        raise ValueError("coding rate out of range")
    bits = 8 * length - 4 * sf + 8 + (16 if crc else 0) + (20 if header else 0)
    if bits < 0:
        bits = 0
    per_symbol = 4 * (sf - 2 if low_data_rate(sf, bandwidth_hz) else sf)
    blocks = (bits + per_symbol - 1) // per_symbol
    return 17 + 4 * (preamble + 8 + blocks * coding_rate)


def _quanta(length, sf, bandwidth_hz, coding_rate, preamble, crc, header):
    # Time on air = quanta * 250 / bandwidth ms (a quantum: 1/4 symbol / 2**sf).
    if bandwidth_hz <= 0 or bandwidth_hz % 50:
        raise ValueError("bandwidth must be a positive multiple of 50 Hz")
    return symbols_x4(length, sf, bandwidth_hz, coding_rate, preamble, crc, header) << sf


def time_on_air_ms(
    length, sf, bandwidth_hz=125000, coding_rate=5, preamble=8, crc=True, header=True
):
    """Return the time on air of a packet in ms, rounded up (for accounting)."""
    q = _quanta(length, sf, bandwidth_hz, coding_rate, preamble, crc, header)
    d = bandwidth_hz // 50
    return (q * 5 + d - 1) // d


def time_on_air_us(
    length, sf, bandwidth_hz=125000, coding_rate=5, preamble=8, crc=True, header=True
):
    """Return the time on air of a packet in µs, rounded up (diagnostics)."""
    q = _quanta(length, sf, bandwidth_hz, coding_rate, preamble, crc, header)
    d = bandwidth_hz // 50
    return (q * 5000 + d - 1) // d


def snr_limit_x10(sf):
    """Return the demodulation limit of sf, in tenths of dB (§5 margins)."""
    _check_sf(sf)
    return _SNR_LIMITS_X10[sf - SF_MIN]

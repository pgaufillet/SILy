"""Regional profiles and radio configuration validation (MESH_DESIGN §4).

A profile is data: channel frequency, bandwidth and coding rate (fixed),
maximum power (ERP or EIRP), and access rules (duty cycle and its window,
maximum transmission time, mandatory listen-before-talk). The operator
chooses the profile, the spreading factor, the TX power and the antenna gain;
`validate` refuses settings outside the profile.

v1 ships the EU 869.525 MHz profile and the **custom** profile (identifier
0xFF): frequency, bandwidth, coding rate and duty cycle entered by the
operator, within the radio's capabilities only. Compliance is then the
operator's responsibility; `Profile.custom` lets the UI flag it, and the
identifier travels in the DODAG information message (§9) for the root's map
and alarms (§13).

Powers are integers in hundredths of dB (cdB, cdBm), so that no float is
needed once the antenna gain has been read.
"""

from mesh.airtime import BANDWIDTHS, SF_MAX, SF_MIN

EU_869 = 1  # EU 869.4–869.65 MHz sub-band, 500 mW ERP, 10 % (§4)
CUSTOM = 0xFF

ERP = 0  # power limit measured as effective radiated power (dipole reference)
EIRP = 1  # ... as equivalent isotropic radiated power

# ERP = EIRP - 2.15 dB (gain of a half-wave dipole over isotropic).
DIPOLE_CDB = 215

# SX1262 capabilities, which bound the custom profile (§4).
TX_POWER_MIN = -9  # dBm
TX_POWER_MAX = 22  # dBm
FREQUENCY_MIN = 150000000  # Hz
FREQUENCY_MAX = 960000000  # Hz
CUSTOM_BANDWIDTH_MAX = 125000  # Hz (custom profile: 7.8–125 kHz, §4)

# The mesh allows itself 90 % of the regulatory duty cycle (§4, §6).
DUTY_CYCLE_MARGIN_PERCENT = 90

# Problems reported by `validate`, named after the setting at fault.
SF = "sf"
TX_POWER = "tx_power"
POWER_LIMIT = "power_limit"  # TX power + antenna gain above the profile's ERP/EIRP
ANTENNA_GAIN = "antenna_gain"
FREQUENCY = "frequency"
BANDWIDTH = "bandwidth"
CODING_RATE = "coding_rate"
DUTY_CYCLE = "duty_cycle"


class Profile:
    """Regulatory profile of a region (or the custom one).

    ident: identifier carried in the DODAG information message (0xFF custom);
    frequency_hz, bandwidth_hz: the single channel; coding_rate: 5–8 for
    4/5–4/8; max_power_cdbm, power_kind: power limit (ERP or EIRP), None when
    only the radio bounds the power (custom); duty_cycle_bp: duty-cycle limit
    in hundredths of a percent (1000 = 10 %; 10000 = no limit) over window_s
    seconds; max_tx_ms: maximum transmission time (0: none); lbt: None, or
    (threshold dBm, sensing time µs) when listen-before-talk is mandatory.
    """

    def __init__(
        self,
        ident,
        name,
        frequency_hz,
        bandwidth_hz,
        coding_rate,
        max_power_cdbm,
        power_kind,
        duty_cycle_bp,
        window_s=3600,
        max_tx_ms=0,
        lbt=None,
    ):
        self.ident = ident
        self.name = name
        self.frequency_hz = frequency_hz
        self.bandwidth_hz = bandwidth_hz
        self.coding_rate = coding_rate
        self.max_power_cdbm = max_power_cdbm
        self.power_kind = power_kind
        self.duty_cycle_bp = duty_cycle_bp
        self.window_s = window_s
        self.max_tx_ms = max_tx_ms
        self.lbt = lbt

    @property
    def custom(self):
        """True for the custom profile: compliance is the operator's."""
        return self.ident == CUSTOM

    def budget_ms(self):
        """Transmission time the mesh allows itself per window, in ms (§6)."""
        # window_s * 1000 * bp / 10000 * margin / 100, small integers only.
        return self.window_s * self.duty_cycle_bp * DUTY_CYCLE_MARGIN_PERCENT // 1000

    def window_ms(self):
        """Duty-cycle observation window, in ms."""
        return self.window_s * 1000


# 500 mW = 26.99 dBm, rounded down.
_EU = Profile(EU_869, "EU 869.525 MHz", 869525000, 125000, 5, 2699, ERP, 1000)

PROFILES = {EU_869: _EU}


def get(ident):
    """Return the regional profile ident, or None if unknown (foreign, §9)."""
    return PROFILES.get(ident)


def custom(frequency_hz, bandwidth_hz, coding_rate, duty_cycle_bp):
    """Return a custom profile; `validate` checks its values."""
    return Profile(
        CUSTOM, "custom", frequency_hz, bandwidth_hz, coding_rate, None, None, duty_cycle_bp
    )


def _is_int(value):
    return type(value) is int


def gain_cdb(antenna_gain_dbi):
    """Return an antenna gain in dBi (int or float) as an int in cdB."""
    return int(round(antenna_gain_dbi * 100))


def validate(profile, sf, tx_power_dbm, antenna_gain_dbi=0):
    """Return the problems of a radio configuration, empty if it is valid.

    Checks the spreading factor (7–12, §5), the TX power (SX1262 range, whole
    dBm) and, when the profile bounds the power, TX power + antenna gain
    against it (ERP: minus 2.15 dB). For the custom profile, also checks the
    values entered by the operator against the radio's capabilities. Problems
    are the names above (`SF`, `TX_POWER`...), in that order.
    """
    problems = []
    if not _is_int(sf) or sf < SF_MIN or sf > SF_MAX:
        problems.append(SF)
    if not _is_int(tx_power_dbm) or not TX_POWER_MIN <= tx_power_dbm <= TX_POWER_MAX:
        problems.append(TX_POWER)
    try:
        gain = gain_cdb(antenna_gain_dbi)
    except (TypeError, ValueError, OverflowError):
        problems.append(ANTENNA_GAIN)
        gain = None
    if gain is not None and TX_POWER not in problems and profile.max_power_cdbm is not None:
        power = tx_power_dbm * 100 + gain
        if profile.power_kind == ERP:
            power -= DIPOLE_CDB
        if power > profile.max_power_cdbm:
            problems.append(POWER_LIMIT)
    if profile.custom:
        f = profile.frequency_hz
        if not _is_int(f) or not FREQUENCY_MIN <= f <= FREQUENCY_MAX:
            problems.append(FREQUENCY)
        bw = profile.bandwidth_hz
        if not _is_int(bw) or bw not in BANDWIDTHS or bw > CUSTOM_BANDWIDTH_MAX:
            problems.append(BANDWIDTH)
        cr = profile.coding_rate
        if not _is_int(cr) or not 5 <= cr <= 8:
            problems.append(CODING_RATE)
        dc = profile.duty_cycle_bp
        if not _is_int(dc) or not 1 <= dc <= 10000:
            problems.append(DUTY_CYCLE)
    return problems

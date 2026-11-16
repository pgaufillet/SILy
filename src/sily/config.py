"""Configuration of a box: the board file ``/config.json`` and its host merge.

Runs on the board, on the unix port and on CPython (``tools/box.py``): the
common subset only.

The board file is JSON, written by ``tools/box.py config`` (`board_config`)
and kept by ``make deploy``. `load` checks it whole: every error is reported,
each as ``field: problem`` (nested fields with dots, list items with their
index: ``wifi.known[0].ssid: missing``), unknown keys refused (a typo must
not pass silently), defaults filled in. Fields:

- ``version`` (required): `VERSION`;
- ``role`` (required): ``gateway`` (the root of the mesh, address 0) or
  ``node``;
- ``address`` (required): 0-65534, unique in the network;
- ``name``: at most 16 UTF-8 bytes (node information), default
  ``box<address>``;
- ``key``, ``fingerprint`` (required): the frame key and the key
  fingerprint of the network, 32 and 8 hexadecimal characters, derived on
  the host from the network password (MESH_DESIGN §14: no password on the
  boxes);
- ``network``: the network name (the salt of the derivation; informational
  on the board), default empty;
- ``profile``: the regional profile (1, EU 869.525 MHz, the default);
  ``sf`` (required): 7-12;
- ``tx_power`` (dBm, default 22), ``antenna_gain`` (dBi, default 0):
  checked against the profile's power limit (``mesh.profile.validate``);
- ``presence_s``: presence period of the DODAG the gateway issues (nodes
  adopt it), gateway only, default null (the profile's own at that SF);
- ``allow``: null or the list of addresses heard (neighbour filter, to
  force hops at home), default null (every neighbour);
- ``led``: the user LED shows the state, default true;
- ``display``: ``mode`` ``off``, ``always`` or ``window`` (on for
  ``window_min`` minutes after start);
- ``wifi``: ``ap`` null or ``{"prefix", "password"}`` (an access point
  ``<prefix>-<name>``, WPA2, password of 8 to 63 characters), ``known`` a
  list of ``{"ssid", "password"}``;
- ``journal``: ``minute_s`` (record period), ``sync_s`` (flush period),
  ``max_kb`` (bound of the journal), ``segment_kb`` (one file).

The display, WiFi and journal sections are checked now, used by later
parts of the application.

Host merge (`board_config`): one network file for every box (password,
network name, derivation count, profile, SF, presence period) and one file
per box (the other fields); a box may not override a network field. The key
and fingerprint are derived with ``mesh.kdf``; the result holds no password.
"""

import json

from mesh import profile as profiles

VERSION = 1
ADDRESS_MAX = 65534  # 0xFFFF is the broadcast address
NAME_MAX = 16  # bytes (mesh.reports.NAME_MAX)
SSID_MAX = 32  # bytes
GATEWAY = "gateway"
NODE = "node"

# Fields of the network file: the network-wide settings. A box file may set
# none of them, nor what is derived from them.
NETWORK_FIELDS = ("password", "network", "iterations", "profile", "sf", "presence_s")
_DERIVED = ("version", "key", "fingerprint")

_REQUIRED = "required"  # default of a required field
_HEX = "0123456789abcdefABCDEF"


# --- Checks ------------------------------------------------------------------------
#
# A check takes (value, field, errors), appends "field: problem" to errors for
# each problem and returns the value to keep (defaults filled in).


def _leaf(test):
    """Return the check of test(value) -> problem or None."""

    def check(value, field, errors):
        problem = test(value)
        if problem:
            errors.append(field + ": " + problem)
        return value

    return check


def _int(low, high):
    def test(value):
        if type(value) is not int:
            return "an integer expected"
        if not low <= value <= high:
            return "out of range %d to %d" % (low, high)
        return None

    return _leaf(test)


def _number(low, high):
    def test(value):
        if type(value) is not int and type(value) is not float:
            return "a number expected"
        if not low <= value <= high:
            return "out of range %d to %d" % (low, high)
        return None

    return _leaf(test)


def _text(low, high):
    """A string of low to high UTF-8 bytes."""

    def test(value):
        if type(value) is not str:
            return "a string expected"
        n = len(value.encode())
        if not low <= n <= high:
            return "%d to %d bytes expected" % (low, high)
        return None

    return _leaf(test)


def _hex(size):
    def test(value):
        if type(value) is not str or len(value) != size or any(c not in _HEX for c in value):
            return "%d hexadecimal characters expected" % size
        return None

    return _leaf(test)


def _one_of(choices):
    def test(value):
        if value not in choices:
            return "one of %s expected" % ", ".join(choices)
        return None

    return _leaf(test)


def _bool_test(value):
    return None if type(value) is bool else "true or false expected"


def _version_test(value):
    return None if type(value) is int and value == VERSION else "%d expected" % VERSION


def _profile_test(value):
    if type(value) is not int or value == profiles.CUSTOM or profiles.get(value) is None:
        known = ", ".join("%d (%s)" % (k, p.name) for k, p in profiles.PROFILES.items())
        return "unknown, known: " + known
    return None


def _optional(check):
    def optional(value, field, errors):
        return None if value is None else check(value, field, errors)

    return optional


def _list(item, size):
    def check(value, field, errors):
        if type(value) is not list:
            errors.append(field + ": a list expected")
            return value
        if len(value) > size:
            errors.append(field + ": at most %d items expected" % size)
        return [item(v, "%s[%d]" % (field, i), errors) for i, v in enumerate(value)]

    return check


def _section(schema):
    def check(value, field, errors):
        if type(value) is not dict:
            errors.append(field + ": an object expected")
            return value
        return _fields(schema, value, field + ".", errors)

    return check


def _fields(schema, obj, prefix, errors):
    """Check obj against schema ((key, default, check), ...); return it with defaults.

    A default that is a dict stands for a section: the section's own defaults.
    """
    out = {}
    for key, default, check in schema:
        if key in obj:
            out[key] = check(obj[key], prefix + key, errors)
        elif default is _REQUIRED:
            errors.append(prefix + key + ": missing")
        elif type(default) is dict:
            out[key] = check({}, prefix + key, errors)
        elif type(default) is list:
            out[key] = list(default)
        else:
            out[key] = default
    known = [s[0] for s in schema]
    for key in sorted(obj):
        if key not in known:
            errors.append(prefix + str(key) + ": unknown key")
    return out


def _wpa_password_test(value):
    if type(value) is not str or not 8 <= len(value) <= 63:
        return "8 to 63 characters expected"
    return None


def _known_password_test(value):
    if type(value) is not str or not (len(value) == 0 or 8 <= len(value) <= 63):
        return "empty (open network) or 8 to 63 characters expected"
    return None


_DISPLAY = (
    ("mode", "always", _one_of(("off", "always", "window"))),
    ("window_min", 10, _int(1, 1440)),
)
_AP = (
    ("prefix", _REQUIRED, _text(1, 16)),
    ("password", _REQUIRED, _leaf(_wpa_password_test)),
)
_KNOWN = (
    ("ssid", _REQUIRED, _text(1, SSID_MAX)),
    ("password", _REQUIRED, _leaf(_known_password_test)),
)
_WIFI = (
    ("ap", None, _optional(_section(_AP))),
    ("known", [], _list(_section(_KNOWN), 8)),
)
_JOURNAL = (
    ("minute_s", 60, _int(1, 3600)),
    ("sync_s", 300, _int(1, 3600)),
    ("max_kb", 1024, _int(16, 1536)),
    ("segment_kb", 64, _int(4, 256)),
)
_BOARD = (
    ("version", _REQUIRED, _leaf(_version_test)),
    ("role", _REQUIRED, _one_of((GATEWAY, NODE))),
    ("address", _REQUIRED, _int(0, ADDRESS_MAX)),
    ("name", None, _text(1, NAME_MAX)),  # None: box<address>
    ("key", _REQUIRED, _hex(32)),
    ("fingerprint", _REQUIRED, _hex(8)),
    ("network", "", _text(0, 64)),
    ("profile", profiles.EU_869, _leaf(_profile_test)),
    ("sf", _REQUIRED, _int(profiles.SF_MIN, profiles.SF_MAX)),
    ("tx_power", 22, _int(profiles.TX_POWER_MIN, profiles.TX_POWER_MAX)),
    ("antenna_gain", 0, _number(-10, 30)),
    ("presence_s", None, _optional(_int(1, 65535))),
    ("allow", None, _optional(_list(_int(0, ADDRESS_MAX), 64))),
    ("led", True, _leaf(_bool_test)),
    ("display", {}, _section(_DISPLAY)),
    ("wifi", {}, _section(_WIFI)),
    ("journal", {}, _section(_JOURNAL)),
)
_NETWORK = (
    ("password", _REQUIRED, _text(1, 128)),
    ("network", "", _text(0, 64)),
    ("iterations", None, _int(1, 10000000)),  # None: mesh.kdf.DEFAULT_ITERATIONS
    ("profile", profiles.EU_869, _leaf(_profile_test)),
    ("sf", _REQUIRED, _int(profiles.SF_MIN, profiles.SF_MAX)),
    ("presence_s", None, _optional(_int(1, 65535))),
)


def ap_ssid(prefix, name):
    """Return the SSID of a box's access point: ``<prefix>-<name>``."""
    return prefix + "-" + name


def _is_int(cfg, key):
    return type(cfg.get(key)) is int


def _cross(cfg, errors):
    """Checks across fields, on the fields that passed their own checks."""
    role = cfg.get("role")
    address = cfg.get("address")
    address_ok = _is_int(cfg, "address") and 0 <= address <= ADDRESS_MAX
    if role in (GATEWAY, NODE) and address_ok:
        if role == NODE and address == 0:
            errors.append("address: 0 is the gateway's (role node)")
        if role == GATEWAY and address != 0:
            errors.append("role: a gateway has address 0, not %d" % address)
    if role == NODE and cfg.get("presence_s") is not None:
        errors.append("presence_s: gateway only (nodes adopt the gateway's)")
    if cfg.get("name") is None and address_ok:
        cfg["name"] = "box%d" % address
    profile = profiles.get(cfg.get("profile")) if _is_int(cfg, "profile") else None
    power = cfg.get("tx_power")
    gain = cfg.get("antenna_gain")
    if (
        profile is not None
        and _is_int(cfg, "tx_power")
        and profiles.TX_POWER_MIN <= power <= profiles.TX_POWER_MAX
        and type(gain) in (int, float)
        and profiles.POWER_LIMIT in profiles.validate(profile, profiles.SF_MIN, power, gain)
    ):
        errors.append(
            "tx_power: %d dBm with antenna_gain %s dBi above the limit of %s"
            % (power, gain, profile.name)
        )
    wifi = cfg.get("wifi")
    ap = wifi.get("ap") if type(wifi) is dict else None
    name = cfg.get("name")
    if type(ap) is dict and type(ap.get("prefix")) is str and type(name) is str:
        ssid = ap_ssid(ap["prefix"], name)
        if len(ssid.encode()) > SSID_MAX:
            errors.append("wifi.ap.prefix: SSID %s longer than %d bytes" % (ssid, SSID_MAX))
    journal = cfg.get("journal")
    if type(journal) is dict:
        segment = journal.get("segment_kb")
        bound = journal.get("max_kb")
        if type(segment) is int and type(bound) is int and segment > bound:
            errors.append("journal.segment_kb: above journal.max_kb")


def check(obj):
    """Check a decoded board configuration; return (config, errors).

    config: obj with every default filled in, None when errors is not empty;
    errors: ``field: problem`` strings, all of them.
    """
    if type(obj) is not dict:
        return None, ["file: a JSON object expected"]
    errors = []
    cfg = _fields(_BOARD, obj, "", errors)
    _cross(cfg, errors)
    return (None if errors else cfg), errors


def load(text):
    """Check the text of a board file; return (config, errors) as `check`."""
    try:
        obj = json.loads(text)
    except ValueError as e:
        return None, ["file: not valid JSON (%s)" % e]
    return check(obj)


def board_config(network, box, derive=None):
    """Merge a network file and a box file (decoded); return (config, errors).

    config: the board file (`check`), with the key and fingerprint derived
    from the network's password, name and derivation count by derive
    (``mesh.kdf.derive`` by default) and without the password; None when
    errors is not empty. The gateway's file carries the network's presence
    period, a node's does not.
    """
    from mesh import kdf

    if derive is None:
        derive = kdf.derive
    if type(network) is not dict:
        return None, ["network file: a JSON object expected"]
    if type(box) is not dict:
        return None, ["box file: a JSON object expected"]
    errors = []
    net = _fields(_NETWORK, network, "", errors)
    errors = ["network file: " + e for e in errors]
    for key in sorted(box):
        if key in NETWORK_FIELDS or key in _DERIVED:
            errors.append("%s: set by the network file, not by a box" % key)
    if errors:
        return None, errors
    iterations = net["iterations"]
    if iterations is None:
        iterations = kdf.DEFAULT_ITERATIONS
    keys = derive(net["password"], net["network"], iterations)
    merged = dict(box)
    merged["version"] = VERSION
    merged["key"] = keys.frame_key.hex()
    merged["fingerprint"] = keys.fingerprint.hex()
    merged["network"] = net["network"]
    merged["profile"] = net["profile"]
    merged["sf"] = net["sf"]
    if box.get("role") == GATEWAY and net["presence_s"] is not None:
        merged["presence_s"] = net["presence_s"]
    return check(merged)

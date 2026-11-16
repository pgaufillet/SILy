#!/usr/bin/env python3
"""Configuration of the boxes from the host (CPython 3, mpremote).

    tools/box.py config NETWORK BOX --port PORT [--mpremote PATH]
    tools/box.py check [--network NETWORK] BOX...

Two kinds of JSON files, kept outside the repository (they hold the
network password, and a box may hold WiFi passwords); the templates
``tools/network.example.json`` and ``tools/box.example.json`` show them
(their placeholder passwords, the network's and the access point's, are
refused by ``config`` and ``check``):

- the network file: what every box of the network shares (``password``,
  ``network`` name, ``iterations`` of the key derivation, ``profile``,
  ``sf``, ``presence_s``, ``traffic`` the test traffic);
- one file per box: the rest (``role``, ``address``, ``name``, power,
  LED, display, WiFi, journal; ``sily.config``).

``config`` merges the two (``sily.config.board_config``: every error
reported, a box overriding a network field refused), derives the frame key
and fingerprint, then writes the result to the board's ``/config.json``
with mpremote (its Ctrl-C stops a running application), clears the start
guard (crash count, skip flag) and resets the board, which starts the
application. ``make configure PORT=... NETWORK=... BOX=...`` runs it.

``check`` reads a set of box files (a whole network): each must merge with
the network file or, without one, with `STAND_IN` in its place (a box's
own fields checked all the same: unknown keys, a missing role or address,
a network field set by a box); the addresses and the access points' SSIDs
must be unique and exactly one box must be the gateway. Exit status 1
with one line per problem, ``FILE: field: problem``.
"""

import json
import sys


def _parent(path):
    head = path.rsplit("/", 1)
    return head[0] if len(head) == 2 and head[0] else "."


sys.path.insert(0, _parent(_parent(__file__)) + "/src")
from sily import config  # noqa: E402

# The template's password: a network file must replace it.
EXAMPLE_PASSWORD = "replace with the network passphrase"
# The box template's access point password: a box file must replace it.
EXAMPLE_AP_PASSWORD = "replace with the access point password"

# The network's fields when ``check`` has no network file: a box's own
# fields checked against them, no key derived.
STAND_IN = {"password": "stand-in", "sf": 7}


class _StandInKeys:
    frame_key = bytes(16)
    fingerprint = bytes(4)


def _stand_in_derive(password, name, iterations):
    return _StandInKeys


USAGE = (
    "usage: box.py config NETWORK BOX --port PORT [--mpremote PATH]\n"
    "       box.py check [--network NETWORK] BOX..."
)


def load_json(path):
    """Return (object, errors) of the JSON file path."""
    try:
        with open(path) as f:
            text = f.read()
    except OSError as e:
        return None, ["%s: cannot read (%s)" % (path, e)]
    try:
        return json.loads(text), []
    except ValueError as e:
        return None, ["%s: not valid JSON (%s)" % (path, e)]


def _template(network, network_path):
    if type(network) is dict and network.get("password") == EXAMPLE_PASSWORD:
        return ["%s: password: still the template's" % network_path]
    return []


def _ap_template(box):
    """Return the problem of a decoded box file whose access point password
    is still the template's (unlocated), or none."""
    wifi = box.get("wifi") if type(box) is dict else None
    ap = wifi.get("ap") if type(wifi) is dict else None
    if type(ap) is dict and ap.get("password") == EXAMPLE_AP_PASSWORD:
        return ["wifi.ap.password: still the template's"]
    return []


def _located(errors, network_path, box_path):
    """Prefix each error of a merge with the file it is about (network_path
    None: no network file, every error is the box's)."""
    out = []
    for e in errors:
        if network_path is not None and e.startswith(network_path + ": "):
            out.append(e)
        elif e.startswith("network file: "):
            out.append(network_path + e[len("network file") :])
        else:
            out.append("%s: %s" % (box_path, e))
    return out


def merge(network, box, network_path="network", derive=None):
    """Return (board configuration, errors) of decoded network and box files."""
    errors = _template(network, network_path) + _ap_template(box)
    if errors:
        return None, errors
    return config.board_config(network, box, derive)


def check_boxes(boxes, network=None, network_path="network"):
    """Return the problems of a network's boxes, [(path, decoded box file)].

    Unique addresses and SSIDs, exactly one gateway, no template password;
    each box merged with network (decoded; one key derivation for all), or
    with `STAND_IN` when None.
    """
    errors = []
    addresses = {}
    gateways = []
    for path, box in boxes:
        if type(box) is not dict:
            errors.append("%s: a JSON object expected" % path)
            continue
        errors.extend("%s: %s" % (path, e) for e in _ap_template(box))
        address = box.get("address")
        if type(address) is int:
            addresses.setdefault(address, []).append(path)
        if box.get("role") == config.GATEWAY:
            gateways.append(path)
    for address in sorted(addresses):
        paths = addresses[address]
        if len(paths) > 1:
            errors.append("address %d: in %s" % (address, ", ".join(paths)))
    if len(gateways) != 1:
        errors.append(
            "gateway: %d boxes (%s), exactly one expected"
            % (len(gateways), ", ".join(gateways) if gateways else "none")
        )
    if network is None:
        network, network_path = STAND_IN, None
        derive = _stand_in_derive
    else:
        template = _template(network, network_path)
        if template:
            return errors + template
        cache = {}

        def derive(password, name, iterations):
            k = (password, name, iterations)
            if k not in cache:
                from mesh import kdf

                cache[k] = kdf.derive(password, name, iterations)
            return cache[k]

    ssids = {}
    for path, box in boxes:
        if type(box) is not dict:
            continue
        cfg, problems = config.board_config(network, box, derive)
        for e in _located(problems, network_path, path):
            if e not in errors:  # a network file's problem, once
                errors.append(e)
        ap = None if cfg is None else cfg["wifi"]["ap"]
        if ap is not None:
            ssids.setdefault(config.ap_ssid(ap["prefix"], cfg["name"]), []).append(path)
    for ssid in sorted(ssids):
        paths = ssids[ssid]
        if len(paths) > 1:
            errors.append("SSID %s: in %s" % (ssid, ", ".join(paths)))
    return errors


def push_commands(mpremote, port, local):
    """Return the mpremote command writing local as the board's /config.json.

    Then the start guard cleared and the board reset (the application starts).
    """
    return [
        mpremote,
        "connect",
        port,
        "fs",
        "cp",
        local,
        ":config.json",
        "+",
        "exec",
        "from sily import hw; hw.clear_guard()",
        "+",
        "reset",
    ]


def _config(network_path, box_path, port, mpremote):
    import os
    import subprocess
    import tempfile

    network, errors = load_json(network_path)
    box, more = load_json(box_path)
    errors += more
    if not errors:
        cfg, errors = merge(network, box, network_path)
        errors = _located(errors, network_path, box_path)
    if errors:
        for e in errors:
            print(e)
        return 1
    print(
        "box %s: %s at address %d, SF%d, %d dBm, key fingerprint %s"
        % (
            cfg["name"],
            cfg["role"],
            cfg["address"],
            cfg["sf"],
            cfg["tx_power"],
            cfg["fingerprint"],
        ),
        flush=True,
    )
    with tempfile.TemporaryDirectory() as tmp:
        local = os.path.join(tmp, "config.json")
        with open(local, "w") as f:
            json.dump(cfg, f, indent=1)
            f.write("\n")
        return subprocess.call(push_commands(mpremote, port, local))


def _check(network_path, box_paths):
    errors = []
    network = None
    if network_path is not None:
        network, errors = load_json(network_path)
    boxes = []
    for path in box_paths:
        box, more = load_json(path)
        errors += more
        boxes.append((path, box))
    if not errors:
        errors = check_boxes(boxes, network, network_path)
    for e in errors:
        print(e)
    if errors:
        return 1
    print("%d boxes: OK" % len(boxes))
    return 0


def main(argv):
    import argparse

    parser = argparse.ArgumentParser(usage=USAGE)
    commands = parser.add_subparsers(dest="command", required=True)
    c = commands.add_parser("config")
    c.add_argument("network")
    c.add_argument("box")
    c.add_argument("--port", required=True)
    c.add_argument("--mpremote", default="mpremote")
    k = commands.add_parser("check")
    k.add_argument("--network")
    k.add_argument("boxes", nargs="+")
    args = parser.parse_args(argv[1:])
    if args.command == "config":
        return _config(args.network, args.box, args.port, args.mpremote)
    return _check(args.network, args.boxes)


if __name__ == "__main__":
    sys.exit(main(sys.argv))

#!/usr/bin/env python3
"""Configuration of the boxes from the host (CPython 3, mpremote).

    tools/box.py config NETWORK BOX --port PORT [--mpremote PATH]
    tools/box.py check [--network NETWORK] BOX...

Two kinds of JSON files, kept outside the repository (they hold the
network password, and a box may hold WiFi passwords); the templates
``tools/network.example.json`` and ``tools/box.example.json`` show them:

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
the network file if one is given; the addresses must be unique and
exactly one box must be the gateway. Exit status 1 with one line per
problem, ``FILE: field: problem``.
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


def _located(errors, network_path, box_path):
    """Prefix each error of a merge with the file it is about."""
    out = []
    for e in errors:
        if e.startswith(network_path + ": "):
            out.append(e)
        elif e.startswith("network file: "):
            out.append(network_path + e[len("network file") :])
        else:
            out.append("%s: %s" % (box_path, e))
    return out


def merge(network, box, network_path="network", derive=None):
    """Return (board configuration, errors) of decoded network and box files."""
    errors = _template(network, network_path)
    if errors:
        return None, errors
    return config.board_config(network, box, derive)


def check_boxes(boxes, network=None, network_path="network"):
    """Return the problems of a network's boxes, [(path, decoded box file)].

    Unique addresses, exactly one gateway; with network (decoded), each box
    merged with it (one key derivation for all).
    """
    errors = []
    addresses = {}
    gateways = []
    for path, box in boxes:
        if type(box) is not dict:
            errors.append("%s: a JSON object expected" % path)
            continue
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
    if network is not None:
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

        for path, box in boxes:
            _, problems = config.board_config(network, box, derive)
            for e in _located(problems, network_path, path):
                if e not in errors:  # a network file's problem, once
                    errors.append(e)
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

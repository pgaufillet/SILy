#!/usr/bin/env python3
"""Field capture from a sniffer board (``make sniff``; CPython 3, mpremote).

    tools/sniff.py OUT --port PORT [--mpremote PATH]

Runs ``sily.sniff.run`` on the board at PORT, a board configured as a
box (``make configure``), through ``mpremote exec`` (`run_command`: its
Ctrl-C at connection stops the application, its soft reset leaves it
stopped, ``src/sily/sniff.py``), and writes every line the board prints
into OUT, each prefixed by the host's UTC at its arrival (ms since the
Unix epoch, ``time.time_ns``) and a space (`stamp`), flushed line by line
(OUT replaced). The terminal shows each frame in short (`echo`) and the
other lines whole.

Ctrl-C ends the capture without losing a line: mpremote runs in its own
process group, so that the terminal's Ctrl-C reaches this tool only,
which passes it on (SIGINT) and reads mpremote's output to its end; a
second Ctrl-C kills mpremote. Then a second mpremote (`stop_command`,
``resume``: the same interpreter) stops the board's sniffer by its own
Ctrl-C, puts the radio to sleep and prints the ``END`` line (counters,
start guard), written into OUT too. The board stays at the REPL; its
next reset starts the application. Exit status 0 once the ``SNIFF`` and
``END`` lines are in OUT, 1 otherwise.

``tools/capture.py`` (``make sniff-trace``) turns OUT into a trace for
``tools/traceview.html`` and checks that the frames travel encrypted.
"""

import sys

SETTINGS = "SNIFF "  # sily.sniff's line prefixes
FRAME = "F "
CRC = "C "
END = "END "

RUN_CODE = "from sily import sniff; sniff.run()"
STOP_CODE = "from sily import sniff; sniff.stop()"
STOP_TIMEOUT_S = 30

USAGE = "sniff.py OUT --port PORT [--mpremote PATH]"


def run_command(mpremote, port):
    """Return the mpremote command running the sniffer on the board at port."""
    return [mpremote, "connect", port, "exec", RUN_CODE]


def stop_command(mpremote, port):
    """Return the mpremote command stopping it and printing its ``END`` line."""
    return [mpremote, "connect", port, "resume", "exec", STOP_CODE]


def stamp(host_ms, line):
    """Return line as written into the capture: the host's ms, a space, line."""
    return "%d %s" % (host_ms, line)


def echo(line, count):
    """Return the terminal's text of a board line; count: frames so far (this one included)."""
    if line.startswith(FRAME):
        parts = line.split(" ")
        if len(parts) == 6 and len(parts[5]) >= 10:
            data = parts[5]
            source = int(data[8:10] + data[6:8], 16)
            return "frame %d: %d B from 0x%04X, %s dBm, SNR %s dB" % (
                count,
                len(data) // 2,
                source,
                parts[3],
                parts[4],
            )
        return "frame %d" % count
    if line.startswith(CRC):
        return "CRC error: " + line[len(CRC) :]
    return line


class Writer:
    """Writes the board's lines into out (a file), stamped by clock() (ms),
    echoed by show(text); ``frames``, ``settings``, ``end``: what it saw."""

    def __init__(self, out, clock, show):
        self.out = out
        self.clock = clock
        self.show = show
        self.frames = 0
        self.settings = False
        self.end = False

    def line(self, raw):
        """Take one line of the board (bytes or str, its end of line included)."""
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        line = raw.rstrip("\r\n")
        if not line:
            return
        self.out.write(stamp(self.clock(), line) + "\n")
        self.out.flush()
        if line.startswith(FRAME):
            self.frames += 1
        elif line.startswith(SETTINGS):
            self.settings = True
        elif line.startswith(END):
            self.end = True
        self.show(echo(line, self.frames))

    def pump(self, stream):
        """Take every line of stream (``readline``) until its end."""
        while True:
            raw = stream.readline()
            if not raw:
                return
            self.line(raw)


def _follow(cmd, writer, timeout=None):
    """Run cmd in its own process group, its lines to writer; a Ctrl-C is
    passed on to it (a second one kills it). Return its exit status."""
    import signal
    import subprocess

    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True
    )
    interrupts = []

    def interrupted(signum, frame):
        interrupts.append(signum)
        if len(interrupts) == 1:
            writer.show("stopping (Ctrl-C again: kill mpremote)")
            proc.send_signal(signal.SIGINT)
        else:
            proc.kill()

    previous = signal.signal(signal.SIGINT, interrupted)
    try:
        writer.pump(proc.stdout)
        try:
            return proc.wait(timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            return proc.wait()
    finally:
        signal.signal(signal.SIGINT, previous)


def record(out_path, port, mpremote="mpremote", show=print):
    """Capture into out_path from the board at port until Ctrl-C (module
    documentation); return the exit status."""
    import time

    def clock():
        return time.time_ns() // 1000000

    with open(out_path, "w") as out:
        writer = Writer(out, clock, show)
        show("sniffing on %s into %s; Ctrl-C to stop" % (port, out_path))
        _follow(run_command(mpremote, port), writer)
        show("stopping the board's sniffer")
        _follow(stop_command(mpremote, port), writer, STOP_TIMEOUT_S)
    show("%d frames into %s" % (writer.frames, out_path))
    if not writer.settings:
        show("no SNIFF line: the sniffer did not start (its lines above)")
        return 1
    if not writer.end:
        show("no END line: the board's sniffer may still run (make sniff again, or reset it)")
        return 1
    return 0


def main(argv):
    import argparse

    parser = argparse.ArgumentParser(usage=USAGE)
    parser.add_argument("out")
    parser.add_argument("--port", required=True)
    parser.add_argument("--mpremote", default="mpremote")
    args = parser.parse_args(argv[1:])
    return record(args.out, args.port, args.mpremote)


if __name__ == "__main__":
    sys.exit(main(sys.argv))

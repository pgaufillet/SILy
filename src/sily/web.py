"""The box's web server: one HTML page and the JSON it polls, under asyncio.

Routes (``GET`` only, ``405`` otherwise, ``404`` for other paths): ``/``
and ``/index.html``, the page (`PAGE`, beside this module, streamed from
its file with its ``Content-Length``); ``/status.json``, ``{"box":
status(), "mesh": mesh()}``, ``mesh`` only when it returns a value (the
gateway's view, ``sily.nodes``). The server receives these callables, never
the configuration: the page shows what ``status`` returns and nothing else
(no key, no password). Responses are HTTP/1.0, ``Connection: close``,
``Cache-Control: no-store``; the JSON ends with the connection (its length
is not known in advance).

The query string is ignored but for ``now`` on ``/status.json``: the
phone's time when it sent the poll, ms since the Unix epoch (the page's
``Date.now()``). A valid one (decimal digits only, at most `NOW_DIGITS`,
from 2025-01-01 to before 2100-01-01: ``sily.utc.valid``) goes to the
callable ``clock`` before the JSON is built, then the handler yields; an
absent, malformed or out-of-range one is ignored (the JSON served as
without it), as is any ``now`` without ``clock``.

The server shares the asyncio loop with the mesh, whose root answers frames
within tens of ms: no step of it may hold the loop long. The JSON is never
built whole: `pieces` yields it piece by piece (dicts recursed, each item of
a list of dicts or lists dumped whole by the C encoder, the nodes' generator
consumed one row at a time), written by about `CHUNK` bytes with a
``drain`` between, which lets the other tasks run; ``clock(now)`` (a
journal line at most) and ``status()`` are one block each, each followed by
a yield. Every synchronous
stretch of the server's code is timed: the longest since the start and
since the last minute record (`figures`, `minute`).

Bounds: at most `CONNECTIONS` connections served at once (beyond, closed at
once and counted ``refused``); the request (line and headers, no body read)
at most `REQUEST_MAX` bytes within `READ_MS`, else ``400`` (too long or
malformed) or closed; the whole response within `RESPONSE_MS` (a phone
that stops reading cannot hold a task). Each connection ends in one
counter: ``requests`` (a response sent in full), ``errors`` (a bad request
answered ``400``, a timeout or a reset during a request or its response,
any exception), ``idle`` (closed, or timed out, before its first byte: a
browser's spare connection) or ``refused``.

The application turns any exception escaping a task into a crash and a
reset: no exception leaves a connection's handler (counted, the connection
closed) nor the accept loop (asyncio's, which skips failed accepts; any
other failure of it is counted and the server is then down, `running`).

Upstream asyncio behaviour relied on (MicroPython ``asyncio/stream.py``):
``start_server`` spawns one task per accepted connection; ``write`` tries
the socket at once and keeps the rest; ``drain`` always yields;
``wait_closed`` closes the socket. ``Server.close`` only cancels the accept
task, which closes the listening socket at its next turn: when the loop has
already stopped, the socket is closed when collected, so `start` collects
and retries once when it cannot listen.
"""

import asyncio
import gc
import json
import os
import time

from sily import utc

PORT = 80
HOST = "0.0.0.0"
BACKLOG = 2  # connections waiting for accept
CONNECTIONS = 3  # served at once: page and poll, a second phone, a spare connection
REQUEST_MAX = 1024  # bytes of request line and headers
READ_PART = 256  # bytes asked per read of the request
READ_MS = 2000  # the whole request within this
RESPONSE_MS = 10000  # the whole response within this
CHUNK = 512  # bytes written between two drains
NOW_DIGITS = 13  # of the query's now: ms since the Unix epoch, 2001 to 2286

_DIR = __file__.rsplit("/", 1)[0] if "/" in __file__ else "."
PAGE = _DIR + "/page.html"

_PAGE = 0  # routes besides the status codes
_JSON = 1
_TEXT = {400: "Bad Request", 404: "Not Found", 405: "Method Not Allowed"}


def _gen():
    yield


_GENERATOR = type(_gen())


def _items(items):
    sep = "["
    for item in items:
        yield sep + json.dumps(item)
        sep = ","
    yield "]" if sep == "," else "[]"


def pieces(obj):
    """Yield the JSON text of obj in pieces (str): dicts recursed, each item
    of a list of dicts or lists and of a generator dumped whole (a generator
    as a list), anything else dumped whole."""
    t = type(obj)
    if t is dict:
        sep = "{"
        for key in obj:
            yield sep + json.dumps(key) + ":"
            sep = ","
            yield from pieces(obj[key])
        yield "}" if sep == "," else "{}"
    elif t is _GENERATOR:
        yield from _items(obj)
    elif (t is list or t is tuple) and obj and type(obj[0]) in (dict, list, tuple):
        yield from _items(obj)
    else:
        yield json.dumps(obj)


def _head(code, ctype, length=None):
    head = "HTTP/1.0 %d %s\r\nContent-Type: %s\r\nCache-Control: no-store\r\n" % (
        code,
        _TEXT.get(code, "OK"),
        ctype,
    )
    if code == 405:
        head += "Allow: GET\r\n"
    if length is not None:
        head += "Content-Length: %d\r\n" % length
    return head + "Connection: close\r\n\r\n"


def _parse(data):
    """Return (method, path, query) of a request (bytes up to its blank line;
    query b"" without one), None if malformed."""
    line = bytes(data[: data.find(b"\r\n")]).split()
    if len(line) != 3 or not line[2].startswith(b"HTTP/"):
        return None
    target = line[1].split(b"?", 1)
    return line[0], target[0], target[1] if len(target) == 2 else b""


def _now(query):
    """Return the ms of the first ``now`` of query (bytes), None when absent or
    not valid (module documentation)."""
    for item in query.split(b"&"):
        if item.startswith(b"now="):
            digits = item[4:]
            if 0 < len(digits) <= NOW_DIGITS and digits.isdigit():
                return utc.valid(int(digits))
            return None
    return None


class Web:
    """The server of the page and its JSON (module documentation).

    status: () -> dict, the box (``App.status``); mesh: () -> dict or None
    (``App.mesh``); clock: (ms) -> None, the phone's time of a poll (``App``,
    None: ignored); page: the page's file; host, port: where to listen;
    connections, read_ms, response_ms: the bounds (`CONNECTIONS`, `READ_MS`,
    `RESPONSE_MS`).
    """

    def __init__(
        self,
        status,
        mesh=None,
        clock=None,
        page=PAGE,
        host=HOST,
        port=PORT,
        connections=CONNECTIONS,
        read_ms=READ_MS,
        response_ms=RESPONSE_MS,
    ):
        self._status = status
        self._mesh = mesh
        self._clock = clock
        self.page = page
        self.host = host
        self.port = port
        self.connections = connections
        self.read_ms = read_ms
        self.response_ms = response_ms
        self.server = None  # asyncio's Server while listening
        self.active = 0  # connections being served
        self.requests = 0
        self.errors = 0
        self.idle = 0
        self.refused = 0
        self.sent = 0  # bytes
        self.stretch_us = 0  # longest synchronous stretch since the start
        self.recent_us = 0  # likewise since the last `minute`
        self._t0 = 0  # ticks_us of the current task's last resumption

    @property
    def running(self):
        return self.server is not None

    async def start(self):
        """Listen (OSError when it cannot); the server then runs in its own
        tasks until `stop`."""
        try:
            server = await asyncio.start_server(self._handle, self.host, self.port, BACKLOG)
        except OSError:  # the socket of an earlier run, not closed yet: collect it
            gc.collect()
            server = await asyncio.start_server(self._handle, self.host, self.port, BACKLOG)
        self.server = server
        asyncio.create_task(self._watch(server))

    def stop(self):
        """Stop listening (closed at the accept task's next turn); the
        connections being served end by themselves."""
        server = self.server
        self.server = None
        if server is not None:
            server.close()

    def figures(self):
        """Return the counters (totals since the start) for ``App.status``."""
        return {
            "requests": self.requests,
            "errors": self.errors,
            "idle": self.idle,
            "refused": self.refused,
            "sent": self.sent,
            "stretch_ms": (self.stretch_us + 999) // 1000,
        }

    def minute(self):
        """Return [requests, errors, idle, refused, kB sent, longest stretch
        in ms since the last call (rounded up)] and restart the latter."""
        out = [
            self.requests,
            self.errors,
            self.idle,
            self.refused,
            self.sent // 1024,
            (self.recent_us + 999) // 1000,
        ]
        self.recent_us = 0
        return out

    # --- Timing: one stretch from a task's resumption to its next await ----------------

    def _lap(self):
        d = time.ticks_diff(time.ticks_us(), self._t0)
        if d > self.stretch_us:
            self.stretch_us = d
        if d > self.recent_us:
            self.recent_us = d

    async def _wait(self, aw):
        self._lap()
        try:
            return await aw
        finally:
            self._t0 = time.ticks_us()

    # --- Tasks -----------------------------------------------------------------------

    async def _watch(self, server):
        try:
            await server.task  # ends at `stop`
        except Exception:  # the accept loop failed: no server
            self.errors += 1
        if self.server is server:
            self.server = None

    async def _handle(self, reader, writer):
        self._t0 = time.ticks_us()
        try:
            if self.active >= self.connections:
                self.refused += 1
            else:
                self.active += 1
                try:
                    await self._serve(reader, writer)
                finally:
                    self.active -= 1
        except Exception:  # timeout, reset by the peer, a failing callable
            self.errors += 1
        finally:
            try:
                await writer.wait_closed()  # closes the socket
            except Exception:
                self.errors += 1
            self._lap()

    async def _serve(self, reader, writer):
        data = bytearray()
        try:
            request = await self._wait(asyncio.wait_for_ms(self._read(reader, data), self.read_ms))
        except (asyncio.TimeoutError, OSError, EOFError):
            if data:  # part of a request
                raise
            self.idle += 1
            return
        if request is None:
            route = 400
        elif request[0] != b"GET":
            route = 405
        elif request[1] == b"/" or request[1] == b"/index.html":
            route = _PAGE
        elif request[1] == b"/status.json":
            route = _JSON
        else:
            route = 404
        now = None
        if route == _JSON and self._clock is not None:
            now = _now(request[2])
        await self._wait(asyncio.wait_for_ms(self._respond(writer, route, now), self.response_ms))
        if route == 400:
            self.errors += 1
        else:
            self.requests += 1

    async def _read(self, reader, data):
        """Read the request into data up to its blank line; return `_parse`
        of it, None when longer than `REQUEST_MAX`."""
        self._t0 = time.ticks_us()
        while b"\r\n\r\n" not in data:
            if len(data) >= REQUEST_MAX:
                return None
            part = await self._wait(reader.read(min(READ_PART, REQUEST_MAX - len(data))))
            if not part:
                raise EOFError
            data.extend(part)
        return _parse(data)

    async def _respond(self, writer, route, now=None):
        self._t0 = time.ticks_us()
        if route == _PAGE:
            await self._page(writer)
        elif route == _JSON:
            if now is not None:
                self._clock(now)
                await self._wait(asyncio.sleep_ms(0))
            out = {"box": self._status()}
            mesh = None if self._mesh is None else self._mesh()
            if mesh is not None:
                out["mesh"] = mesh
            await self._wait(asyncio.sleep_ms(0))
            await self._stream(writer, _head(200, "application/json"), pieces(out))
        else:
            body = _TEXT[route] + "\n"
            await self._stream(writer, _head(route, "text/plain", len(body)), (body,))

    async def _page(self, writer):
        size = os.stat(self.page)[6]
        with open(self.page, "rb") as f:
            await self._flush(writer, _head(200, "text/html; charset=utf-8", size).encode())
            buf = bytearray(CHUNK)
            view = memoryview(buf)
            while True:
                n = f.readinto(buf)
                if not n:
                    break
                await self._flush(writer, view[:n])

    async def _stream(self, writer, head, parts):
        """Write head then parts (str), by `CHUNK` characters or a little more."""
        buf = [head]
        size = len(head)
        for part in parts:
            buf.append(part)
            size += len(part)
            if size >= CHUNK:
                await self._flush(writer, "".join(buf).encode())
                buf = []
                size = 0
        if buf:
            await self._flush(writer, "".join(buf).encode())

    async def _flush(self, writer, data):
        writer.write(data)
        self.sent += len(data)
        await self._wait(writer.drain())

"""The box's web server (sily.web): JSON in pieces, routes, bounds, counters; the page.

The JSON pieces and the chunked writes are checked on their own and through
a connection's handler on a fake stream; the routes, bounds and counters
over loopback, the unix port's real sockets in the test's event loop, with
short injected timeouts (no ``getsockname`` there: fixed high ports, the
next one when busy). Every client read has its own limit, so that a broken
bound fails a test instead of hanging it.
"""

import asyncio
import json
import os
import time
import unittest

from test_sily_display import gateway_status, node_status

from sily import web
from sily.web import CHUNK, Web, pieces

FIRST_PORT = 18640
CLIENT_MS = 3000  # a client's limit on any read
_ports = [FIRST_PORT]


def _run(coro):
    asyncio.new_event_loop()
    try:
        return asyncio.run(coro)
    finally:
        asyncio.new_event_loop()


async def _start(server):
    """Start server on the next free port; return the port."""
    for _ in range(50):
        server.port = _ports[0]
        _ports[0] += 1
        try:
            await server.start()
            return server.port
        except OSError:
            continue
    raise OSError("no free port")


async def _read_all(reader):
    """Return (bytes until the server closes, True if it reset the connection)."""
    data = b""
    try:
        while True:
            part = await asyncio.wait_for_ms(reader.read(1024), CLIENT_MS)
            if not part:
                return data, False
            data += part
    except OSError:  # closed with request bytes unread: a reset
        return data, True


async def _get(port, raw):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(raw)
    await writer.drain()
    out = await _read_all(reader)
    await writer.wait_closed()
    return out


def _request(path, method="GET"):
    return b"%s %s HTTP/1.1\r\nHost: 192.168.4.1\r\nAccept: */*\r\n\r\n" % (method, path)


def _split(data):
    """Return (status code, headers dict with lower-case names, body)."""
    head, body = data.split(b"\r\n\r\n", 1)
    lines = head.decode().split("\r\n")
    headers = {}
    for line in lines[1:]:
        name, value = line.split(":", 1)
        headers[name.strip().lower()] = value.strip()
    return int(lines[0].split()[1]), headers, body


def _rows(n):
    return [
        {
            "address": a,
            "name": "box%d" % a,
            "state": "ok",
            "parent": a - 1,
            "battery": 3900,
            "links": [[a - 1, 12, 1, 95], [a + 1, -128, 0, 40]],
            "age_s": 3,
            "flags": 0,
        }
        for a in range(1, n + 1)
    ]


def _mesh(rows, made=None):
    """Return mesh() for rows: its nodes a generator (made counts the rows made)."""

    def nodes():
        for row in rows:
            if made is not None:
                made[0] += 1
            yield row

    return lambda: {"period_s": 30, "beyond": 0, "nodes": nodes()}


def _plain(obj):
    return json.loads(json.dumps(obj))


class Pieces(unittest.TestCase):
    def test_round_trip(self):
        obj = {
            "a": {"b": {"c": [1, 2, 3], "d": {}}, "e": []},
            "lists": [[1, [2, 3]], [], [None]],
            "rows": [{"x": 1, "y": [1, 2]}, {"z": None}],
            "text": 'quote " and \\ and é',
            "f": 0.75,
            "n": None,
            "t": True,
            "empty": {},
        }
        self.assertEqual(json.loads("".join(pieces(obj))), _plain(obj))
        for value in ([], {}, [1, 2], 5, "x", None):
            self.assertEqual(json.loads("".join(pieces(value))), value)

    def test_generator_as_a_list_one_row_at_a_time(self):
        rows = _rows(3)
        made = [0]
        obj = {"mesh": _mesh(rows, made)(), "none": _mesh([])()["nodes"]}
        it = pieces(obj)
        text = ""
        for piece in it:
            text += piece
            if '"box1"' in piece:
                break
        self.assertEqual(made[0], 1)  # the second row not made yet
        text += "".join(it)
        self.assertEqual(made[0], 3)
        out = json.loads(text)
        self.assertEqual(out["mesh"]["nodes"], _plain(rows))
        self.assertEqual(out["none"], [])

    def test_each_row_one_piece(self):
        rows = _rows(4)
        self.assertEqual(
            list(pieces(rows)),
            ["[" + json.dumps(rows[0])] + ["," + json.dumps(r) for r in rows[1:]] + ["]"],
        )
        self.assertEqual(list(pieces([1, 2])), [json.dumps([1, 2])])  # scalars: whole


class FakeStream:
    """A connection's stream (reader and writer): the request, then end of file."""

    def __init__(self, request, stall_ms=0):
        self.request = request
        self.stall_ms = stall_ms
        self.calls = []
        self.closed = False

    async def read(self, n):
        data = self.request[:n]
        self.request = self.request[n:]
        return data

    def write(self, data):
        self.calls.append(bytes(data))

    async def drain(self):
        self.calls.append(None)
        if self.stall_ms:
            await asyncio.sleep_ms(self.stall_ms)

    async def wait_closed(self):
        self.closed = True


class Handler(unittest.TestCase):
    def test_json_written_by_chunks_with_drains(self):
        box = gateway_status(neighbours=[gateway_status()["neighbours"][0]] * 12)
        rows = _rows(12)
        server = Web(lambda: box, _mesh(rows))
        stream = FakeStream(_request(b"/status.json"))
        _run(server._handle(stream, stream))
        self.assertTrue(stream.closed)
        writes = stream.calls[0::2]
        self.assertEqual(stream.calls[1::2], [None] * len(writes))  # a drain after each write
        self.assertGreaterEqual(len(writes), 6)
        head = web._head(200, "application/json")
        longest = max(len(head), max(len(p) for p in pieces({"box": box, "mesh": _mesh(rows)()})))
        for data in writes:
            self.assertLessEqual(len(data), CHUNK + longest)
        code, headers, body = _split(b"".join(writes))
        self.assertEqual((code, headers["content-type"]), (200, "application/json"))
        expected = {"box": box, "mesh": {"period_s": 30, "beyond": 0, "nodes": rows}}
        self.assertEqual(json.loads(body), _plain(expected))
        self.assertEqual((server.requests, server.errors, server.active), (1, 0, 0))
        self.assertEqual(server.sent, len(b"".join(writes)))

    def test_response_timeout(self):
        # A phone that stops reading: each drain stalls 2 s (two chunks here).
        server = Web(node_status, response_ms=50)
        stream = FakeStream(_request(b"/status.json"), stall_ms=2000)
        t = time.ticks_ms()
        _run(server._handle(stream, stream))
        self.assertLessEqual(time.ticks_diff(time.ticks_ms(), t), 1000)
        self.assertTrue(stream.closed)
        self.assertEqual((server.requests, server.errors, server.active), (0, 1, 0))


class Server(unittest.TestCase):
    def _serve(self, server, client):
        """Run client(port) against server started on a free port; return its result."""

        async def main():
            port = await _start(server)
            try:
                return await client(port)
            finally:
                server.stop()
                await asyncio.sleep_ms(10)

        return _run(main())

    def test_page_with_its_length(self):
        with open(web.PAGE, "rb") as f:
            page = f.read()

        async def client(port):
            out = []
            for path in (b"/", b"/index.html"):
                out.append(await _get(port, _request(path)))
            return out

        server = Web(node_status)
        for data, reset in self._serve(server, client):
            code, headers, body = _split(data)
            self.assertEqual((code, reset), (200, False))
            self.assertEqual(headers["content-type"], "text/html; charset=utf-8")
            self.assertEqual(int(headers["content-length"]), len(page))
            self.assertEqual(headers["cache-control"], "no-store")
            self.assertEqual(body, page)
        self.assertEqual((server.requests, server.errors), (2, 0))

    def test_status_json_on_gateway_and_node(self):
        box = gateway_status()
        rows = _rows(5)

        async def client(port):
            return await _get(port, _request(b"/status.json"))

        data, _ = self._serve(Web(lambda: box, _mesh(rows)), client)
        code, headers, body = _split(data)
        self.assertEqual((code, headers["content-type"]), (200, "application/json"))
        expected = {"box": box, "mesh": {"period_s": 30, "beyond": 0, "nodes": rows}}
        self.assertEqual(json.loads(body), _plain(expected))
        node = node_status()
        data, _ = self._serve(Web(lambda: node, lambda: None), client)
        self.assertEqual(json.loads(_split(data)[2]), {"box": _plain(node)})

    def test_query_string_ignored(self):
        async def client(port):
            return [
                await _get(port, _request(b"/status.json?t=1700000000")),
                await _get(port, _request(b"/?x=1")),
            ]

        (json_data, _), (page_data, _) = self._serve(Web(node_status), client)
        self.assertEqual(json.loads(_split(json_data)[2]), {"box": _plain(node_status())})
        self.assertEqual(_split(page_data)[1]["content-type"], "text/html; charset=utf-8")

    def test_now_goes_to_the_clock_before_the_json(self):
        calls = []

        def status():
            calls.append("status")
            return node_status()

        async def client(port):
            out = []
            for path in (
                b"/status.json?now=1794929400000",
                b"/status.json?x=1&now=1735689600000&now=4102444799999",
                b"/?now=1794929400000",  # the page: ignored
                b"/status.json",
            ):
                out.append(await _get(port, _request(path)))
            return out

        server = Web(status, clock=lambda ms: calls.append(ms))
        out = self._serve(server, client)
        self.assertEqual(calls, [1794929400000, "status", 1735689600000, "status", "status"])
        for data, _ in out[:2] + out[3:]:
            self.assertEqual(json.loads(_split(data)[2]), {"box": _plain(node_status())})
        self.assertEqual((server.requests, server.errors), (4, 0))

    def test_now_not_valid_ignored(self):
        queries = (
            b"now=",
            b"now=abc",
            b"now=+1794929400000",
            b"now=-1794929400000",
            b"now=1794929400000.5",
            b"now=1794929400000x",
            b"now=1735689599999",  # before 2025-01-01
            b"now=4102444800000",  # 2100-01-01
            b"now=0001794929400000",  # too many digits
            b"t=1794929400000",  # another name
            b"now=x&now=1794929400000",  # the first counts
            b"NOW=1794929400000",
        )
        calls = []

        async def client(port):
            out = []
            for query in queries:
                out.append(await _get(port, _request(b"/status.json?" + query)))
            return out

        server = Web(node_status, clock=lambda ms: calls.append(ms))
        out = self._serve(server, client)
        self.assertEqual(calls, [])
        for data, _ in out:
            self.assertEqual(json.loads(_split(data)[2]), {"box": _plain(node_status())})
        self.assertEqual((server.requests, server.errors), (len(queries), 0))
        self.assertEqual(web._now(b"now=1794929400000"), 1794929400000)  # the same parser

    def test_failing_clock_counted_never_raised(self):
        def clock(ms):
            raise ValueError("clock failed")

        raised = []

        async def client(port):
            asyncio.get_event_loop().set_exception_handler(lambda loop, c: raised.append(c))
            out = [await _get(port, _request(b"/status.json?now=1794929400000"))]
            out.append(await _get(port, _request(b"/status.json")))
            await asyncio.sleep_ms(20)
            return out

        server = Web(node_status, clock=clock)
        (first, _), (second, _) = self._serve(server, client)
        self.assertEqual(first, b"")
        self.assertEqual(_split(second)[0], 200)
        self.assertEqual((server.errors, server.requests, server.active), (1, 1, 0))
        self.assertEqual(raised, [])

    def test_not_found_and_not_allowed(self):
        async def client(port):
            return [
                await _get(port, _request(b"/x")),
                await _get(port, _request(b"/favicon.ico")),
                await _get(port, _request(b"/status.json", b"POST")),
            ]

        server = Web(node_status)
        out = self._serve(server, client)
        self.assertEqual([_split(d)[0] for d, _ in out], [404, 404, 405])
        self.assertEqual(_split(out[2][0])[1]["allow"], "GET")
        self.assertEqual((server.requests, server.errors), (3, 0))

    def test_bad_requests(self):
        async def client(port):
            return [
                await _get(port, b"GARBAGE\r\n\r\n"),
                await _get(port, b"GET /" + b"a" * 2000 + b" HTTP/1.1\r\n\r\n"),
            ]

        server = Web(node_status)
        (bad, _), (long, reset) = self._serve(server, client)
        self.assertEqual(_split(bad)[0], 400)
        # Closed with request bytes unread: the 400 may be lost to a reset.
        self.assertTrue(reset or _split(long)[0] == 400)
        self.assertEqual((server.requests, server.errors), (0, 2))

    def test_connection_limit(self):
        async def client(port):
            held = []
            for _ in range(2):
                held.append(await asyncio.open_connection("127.0.0.1", port))
            await asyncio.sleep_ms(50)  # both accepted, waiting for a request
            t = time.ticks_ms()
            third = await _get(port, _request(b"/status.json"))
            elapsed = time.ticks_diff(time.ticks_ms(), t)
            out = []
            for reader, writer in held:
                writer.write(_request(b"/status.json"))
                await writer.drain()
                out.append(await _read_all(reader))
                await writer.wait_closed()
            return third, elapsed, out

        server = Web(node_status, connections=2, read_ms=2000)
        (data, _), elapsed, held = self._serve(server, client)
        self.assertEqual(data, b"")  # closed at once, nothing sent
        self.assertLessEqual(elapsed, 1000)
        self.assertEqual([_split(d)[0] for d, _ in held], [200, 200])
        self.assertEqual((server.refused, server.requests, server.errors), (1, 2, 0))

    def test_silent_and_slow_clients_timed_out(self):
        async def client(port):
            out = []
            for raw in (b"", b"GET /status.json HT"):
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                if raw:
                    writer.write(raw)
                    await writer.drain()
                t = time.ticks_ms()
                data, _ = await _read_all(reader)
                out.append((data, time.ticks_diff(time.ticks_ms(), t)))
                await writer.wait_closed()
            return out

        server = Web(node_status, read_ms=100)
        (silent, t_silent), (slow, t_slow) = self._serve(server, client)
        self.assertEqual((silent, slow), (b"", b""))
        for t in (t_silent, t_slow):
            self.assertGreaterEqual(t, 90)
            self.assertLessEqual(t, 1000)
        # No byte: idle (a browser's spare connection); part of a request: an error.
        self.assertEqual((server.idle, server.errors, server.requests), (1, 1, 0))

    def test_failures_counted_never_raised(self):
        calls = [0]

        def status():
            calls[0] += 1
            if calls[0] == 1:
                raise ValueError("status failed")
            return node_status()

        def rows():
            yield _rows(1)[0]
            raise ValueError("row failed")

        raised = []
        mesh = [None]

        async def client(port):
            asyncio.get_event_loop().set_exception_handler(lambda loop, c: raised.append(c))
            out = [await _get(port, _request(b"/status.json"))]
            mesh[0] = {"period_s": 30, "beyond": 0, "nodes": rows()}
            out.append(await _get(port, _request(b"/status.json")))  # fails mid-stream
            mesh[0] = None
            out.append(await _get(port, _request(b"/status.json")))
            await asyncio.sleep_ms(20)  # a task's exception would reach the handler now
            return out

        server = Web(status, lambda: mesh[0])
        (first, _), (second, _), (third, _) = self._serve(server, client)
        self.assertEqual(first, b"")
        code, _, body = _split(second)
        self.assertEqual(code, 200)
        self.assertRaises(ValueError, json.loads, body)  # cut after the first row
        self.assertEqual(json.loads(_split(third)[2]), {"box": _plain(node_status())})
        self.assertEqual((server.errors, server.requests), (2, 1))
        self.assertEqual(raised, [])
        self.assertEqual(server.active, 0)

    def test_stretch_of_a_slow_status(self):
        def slow():
            t = time.ticks_us()
            while time.ticks_diff(time.ticks_us(), t) < 5000:
                pass
            return node_status()

        server = Web(slow)

        async def client(port):
            await _get(port, _request(b"/status.json"))
            return server.minute(), server.minute()

        first, second = self._serve(server, client)
        self.assertEqual(first[:4], [1, 0, 0, 0])
        self.assertEqual(first[4], server.sent // 1024)
        self.assertGreaterEqual(first[5], 5)
        self.assertEqual(second[5], 0)  # restarted by the first call
        figures = server.figures()
        self.assertGreaterEqual(figures["stretch_ms"], 5)  # since the start: kept
        self.assertEqual((figures["requests"], figures["sent"]), (1, server.sent))

    def test_clock_and_status_are_separate_stretches(self):
        def busy(ms):
            t = time.ticks_us()
            while time.ticks_diff(time.ticks_us(), t) < ms * 1000:
                pass

        def slow():
            busy(8)
            return node_status()

        server = Web(slow, clock=lambda ms: busy(8))

        async def client(port):
            await _get(port, _request(b"/status.json?now=1794929400000"))
            return server.figures()

        figures = self._serve(server, client)
        self.assertGreaterEqual(figures["stretch_ms"], 8)
        self.assertLessEqual(figures["stretch_ms"], 13)  # not both in one

    def test_stop_then_listen_again(self):
        async def client(port):
            again = Web(node_status, port=port)
            server.stop()
            self.assertFalse(server.running)
            await asyncio.sleep_ms(10)  # the accept task closes the socket
            await again.start()
            data, _ = await _get(port, _request(b"/status.json"))
            again.stop()
            await asyncio.sleep_ms(10)
            return data

        server = Web(node_status)
        data = self._serve(server, client)
        self.assertEqual(_split(data)[0], 200)

    def test_busy_port_raises_and_does_not_run(self):
        async def client(port):
            busy = Web(node_status, port=port)
            try:
                await busy.start()
            except OSError:
                return busy.running
            busy.stop()
            return "started"

        server = Web(node_status)
        self.assertIs(self._serve(server, client), False)
        self.assertEqual(server.errors, 0)


class Page(unittest.TestCase):
    def test_page_file(self):
        self.assertTrue(web.PAGE.endswith("/sily/page.html"))
        # Sent whole at each load, by CHUNK bytes from flash: kept lean (no library).
        self.assertLessEqual(os.stat(web.PAGE)[6], 32 * 1024)
        with open(web.PAGE) as f:
            text = f.read()
        self.assertIn("/status.json", text)
        self.assertIn('fetch("/status.json?now=" + Date.now(),', text)  # the phone's time
        self.assertIn('"clock " + local(b.utc)', text)
        self.assertIn('<link rel=icon href="data:,">', text)
        for scheme in ("http://", "https://"):
            self.assertFalse(scheme in text, scheme)
        for attr in ("src=", "href="):
            at = text.find(attr)
            while at >= 0:
                value = text[at + len(attr) :].lstrip("\"'")
                self.assertTrue(value.startswith("data:"), text[at : at + 40])
                at = text.find(attr, at + 1)

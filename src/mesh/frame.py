"""Frame and message codec (MESH_DESIGN §11).

A frame is one LoRa packet of at most 255 B::

    header (9 B, clear, AEAD associated data) | messages (encrypted) | tag (4 B)

Header: byte 0 versions (bits 7–6 protocol version, bit 5 key epoch, bits 4–1
DODAG version, bit 0 reserved: sent 0, ignored); link destination 2 B; link
source 2 B; frame counter 3 B; rank 1 B. Integers are little-endian.

Security boundary (§14, implemented by the security layer): `aad` is the
associated data, `body` the region encrypted in place (plaintext and
ciphertext have the same length), `tag` the 4 bytes the seal writes. The
AEAD nonce starts with the link source and the frame counter exactly as they
appear in the header (bytes `SOURCE_OFFSET` to `COUNTER_OFFSET` + 3). `encode`
leaves the body in clear and the tag zeroed; `decode_messages` expects the
body in clear. Reception order: `decode_header` (length and protocol version,
step 1), then the security layer opens the body, then `decode_messages`.

Messages: control 1 B (bits 7–5 addressing mode, bits 4–3 class, bit 2
routing header present, bit 1 bundle, bit 0 last), port 1 B, length 1 B
(payload length of one element; absent for a last message without bundle,
which extends to the tag), count 1 B (bundle only, at least 2), then per
element: addresses (by mode), sequence 2 B (classes 1 and 2, and
broadcasts), routing header (count n ≤ 7, then n hops of 2 B), payload.
`encode` bundles consecutive messages of the same mode, class, routing-header
presence, port and payload length, and marks the final group *last*.

Errors: decoding raises `FormatError` only, its ``reason`` one of `LENGTH`,
`VERSION`, `TRUNCATED`, `MALFORMED`; encoding raises ValueError for values
out of range or a frame above the limit (a bug of the caller).
"""

from mesh.airtime import time_on_air_us

PROTOCOL_VERSION = 1  # 0 reserved, 2 next incompatible version, 3 escape (§11, MP25)

HEADER_SIZE = 9
TAG_SIZE = 4
OVERHEAD = HEADER_SIZE + TAG_SIZE  # 13
MAX_FRAME = 255  # LoRa payload limit

# Header offsets.
VERSIONS_OFFSET = 0
DEST_OFFSET = 1
SOURCE_OFFSET = 3
COUNTER_OFFSET = 5
RANK_OFFSET = 8

ROOT = 0x0000
BROADCAST = 0xFFFF  # every neighbour (link destination), every node
COUNTER_MAX = 0xFFFFFF
DODAG_VERSION_MAX = 15
RANK_ROOT = 0
RANK_INFINITE = 255  # no route, or poisoned (§9)

# Addressing modes (control bits 7–5).
LINK_LOCAL = 0  # no address
UPWARD = 1  # origin
DOWNWARD = 2  # destination; source-routed
P2P = 3  # origin, then destination
NODE_BROADCAST = 4  # origin
ROOT_BROADCAST = 5  # no address
FIRST_HOP = 6  # upward, origin = link source; rewritten as UPWARD by the first relay
# Mode 7 and class 3 are reserved.

MAX_ROUTE_HOPS = 7  # intermediate hops of a source route (8 hops at most, §10)

# FormatError reasons.
LENGTH = "length"  # frame shorter than header + tag, or above 255 B
VERSION = "version"  # protocol version 0, 2 or escape 3: not supported (counted, MP25)
TRUNCATED = "truncated"  # a message runs past the end of the body
MALFORMED = "malformed"  # reserved mode or class, bad count, misplaced routing header...

_CONTROL_ROUTE = 0x04
_CONTROL_BUNDLE = 0x02
_CONTROL_LAST = 0x01

# Address bytes per mode, and whether the mode carries origin / destination.
_ADDRESS_BYTES = b"\x00\x02\x02\x04\x02\x00\x00"
_HAS_ORIGIN = (False, True, False, True, True, False, False)
_HAS_DEST = (False, False, True, True, False, False, False)


class FormatError(ValueError):
    """A received frame or body that cannot be decoded; ``reason`` says why."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class Header:
    """Clear header of a frame.

    dest: link destination (next hop or `BROADCAST`); source: link source;
    counter: frame counter (24 bits); rank: sender's rank (0 root, 255 none);
    dodag_version: 0–15 (serial arithmetic, §9); epoch: key epoch bit.
    """

    def __init__(self, dest, source, counter, rank, dodag_version=0, epoch=0):
        self.dest = dest
        self.source = source
        self.counter = counter
        self.rank = rank
        self.dodag_version = dodag_version
        self.epoch = epoch

    def __eq__(self, other):
        return (
            isinstance(other, Header)
            and self.dest == other.dest
            and self.source == other.source
            and self.counter == other.counter
            and self.rank == other.rank
            and self.dodag_version == other.dodag_version
            and self.epoch == other.epoch
        )

    def __repr__(self):
        return "Header(dest=0x%04x, source=0x%04x, counter=%d, rank=%d, version=%d, epoch=%d)" % (
            self.dest,
            self.source,
            self.counter,
            self.rank,
            self.dodag_version,
            self.epoch,
        )


class Message:
    """One message (one element of a bundle once decoded).

    mode: addressing mode (`LINK_LOCAL`... `FIRST_HOP`); cls: service class
    0–2; port: 0–255; payload: bytes-like (a memoryview into the received
    buffer once decoded: copy it to keep it beyond the buffer's reuse);
    origin, dest: addresses, set exactly when the mode carries them; seq:
    16-bit sequence, set exactly for classes 1 and 2 and broadcasts; route:
    None, or the tuple of remaining intermediate hops of a source route
    (modes `DOWNWARD` and `P2P` only, at most `MAX_ROUTE_HOPS`; may be empty).
    """

    def __init__(self, mode, cls, port, payload=b"", origin=None, dest=None, seq=None, route=None):
        self.mode = mode
        self.cls = cls
        self.port = port
        self.payload = payload
        self.origin = origin
        self.dest = dest
        self.seq = seq
        self.route = route

    def __eq__(self, other):
        return (
            isinstance(other, Message)
            and self.mode == other.mode
            and self.cls == other.cls
            and self.port == other.port
            and self.origin == other.origin
            and self.dest == other.dest
            and self.seq == other.seq
            and self.route == other.route
            and bytes(self.payload) == bytes(other.payload)
        )

    def __repr__(self):
        return "Message(mode=%d, cls=%d, port=%d, origin=%r, dest=%r, seq=%r, route=%r, %r)" % (
            self.mode,
            self.cls,
            self.port,
            self.origin,
            self.dest,
            self.seq,
            self.route,
            bytes(self.payload),
        )


def has_sequence(mode, cls):
    """Return True if a message of this mode and class carries a sequence."""
    return cls == 1 or cls == 2 or mode == NODE_BROADCAST or mode == ROOT_BROADCAST


def expand_first_hop(msg, link_source):
    """Rewrite a first-hop message (mode 6) as upward (mode 1); return msg.

    The origin is the link source of the frame that carried it (§11
    first-hop compression). Port payloads compressed on the first hop (the
    presence report's parent) are restored by their port's handler.
    """
    if msg.mode == FIRST_HOP:
        msg.mode = UPWARD
        msg.origin = link_source
    return msg


def frame_limit(sf=None, profile=None):
    """Return the largest frame length allowed, in bytes.

    255, or less when profile (`mesh.profile.Profile`) caps the transmission
    time (``max_tx_ms`` > 0) at spreading factor sf: the longest frame whose
    time on air (§4 parameters, preamble 8) does not exceed the cap. Raises
    ValueError if not even an empty frame (header and tag) fits.
    """
    if profile is None or not profile.max_tx_ms:
        return MAX_FRAME
    cap_us = profile.max_tx_ms * 1000
    n = MAX_FRAME
    while n >= OVERHEAD:
        if time_on_air_us(n, sf, profile.bandwidth_hz, profile.coding_rate) <= cap_us:
            return n
        n -= 1
    raise ValueError("maximum transmission time below the smallest frame")


# --- Security boundary -------------------------------------------------------


def aad(buf):
    """Return the associated data of a frame (its clear header)."""
    return memoryview(buf)[:HEADER_SIZE]


def body(buf, length):
    """Return the encrypted region of a frame of length bytes (the messages)."""
    return memoryview(buf)[HEADER_SIZE : length - TAG_SIZE]


def tag(buf, length):
    """Return the tag region of a frame of length bytes."""
    return memoryview(buf)[length - TAG_SIZE : length]


# --- Header ------------------------------------------------------------------


def _check_range(value, high, name):
    if type(value) is not int or value < 0 or value > high:
        raise ValueError(name + " out of range")


def encode_header(buf, header):
    """Write header into buf[0:9]."""
    _check_range(header.dest, 0xFFFF, "dest")
    _check_range(header.source, 0xFFFF, "source")
    _check_range(header.counter, COUNTER_MAX, "counter")
    _check_range(header.rank, 255, "rank")
    _check_range(header.dodag_version, DODAG_VERSION_MAX, "dodag_version")
    _check_range(header.epoch, 1, "epoch")
    buf[0] = PROTOCOL_VERSION << 6 | header.epoch << 5 | header.dodag_version << 1
    buf[1] = header.dest & 0xFF
    buf[2] = header.dest >> 8
    buf[3] = header.source & 0xFF
    buf[4] = header.source >> 8
    c = header.counter
    buf[5] = c & 0xFF
    buf[6] = (c >> 8) & 0xFF
    buf[7] = c >> 16
    buf[8] = header.rank


def decode_header(buf, length, into=None):
    """Return the Header of a received frame of length bytes in buf.

    Checks the length (13 to 255 B, within buf) and the protocol version
    (only 1; 0, 2 and the escape 3 raise FormatError(`VERSION`)). into: a
    Header to fill instead of allocating one.
    """
    if length < OVERHEAD or length > MAX_FRAME or length > len(buf):
        raise FormatError(LENGTH)
    v = buf[0]
    if v >> 6 != PROTOCOL_VERSION:
        raise FormatError(VERSION)
    h = into if into is not None else Header(0, 0, 0, 0)
    h.epoch = (v >> 5) & 1
    h.dodag_version = (v >> 1) & 0x0F
    h.dest = buf[1] | buf[2] << 8
    h.source = buf[3] | buf[4] << 8
    h.counter = buf[5] | buf[6] << 8 | buf[7] << 16
    h.rank = buf[8]
    return h


# --- Messages ----------------------------------------------------------------


def _check_message(m):
    mode = m.mode
    if type(mode) is not int or mode < 0 or mode > FIRST_HOP:
        raise ValueError("mode out of range")
    _check_range(m.cls, 2, "class")
    _check_range(m.port, 255, "port")
    if _HAS_ORIGIN[mode]:
        _check_range(m.origin, 0xFFFF, "origin")
    elif m.origin is not None:
        raise ValueError("origin not carried in this mode")
    if _HAS_DEST[mode]:
        _check_range(m.dest, 0xFFFF, "dest")
    elif m.dest is not None:
        raise ValueError("dest not carried in this mode")
    if has_sequence(mode, m.cls):
        _check_range(m.seq, 0xFFFF, "seq")
    elif m.seq is not None:
        raise ValueError("seq not carried in this mode and class")
    route = m.route
    if route is not None:
        if mode != DOWNWARD and mode != P2P:
            raise ValueError("routing header outside a source route")
        if len(route) > MAX_ROUTE_HOPS:
            raise ValueError("route too long")
        for hop in route:
            _check_range(hop, 0xFFFF, "hop")
    if len(m.payload) > 255:
        raise ValueError("payload too long")


def _element_size(m):
    n = _ADDRESS_BYTES[m.mode] + len(m.payload)
    if has_sequence(m.mode, m.cls):
        n += 2
    if m.route is not None:
        n += 1 + 2 * len(m.route)
    return n


def _same_group(a, b):
    return (
        a.mode == b.mode
        and a.cls == b.cls
        and a.port == b.port
        and (a.route is None) == (b.route is None)
        and len(a.payload) == len(b.payload)
    )


def _group_end(messages, i):
    # End of the group starting at i: consecutive messages sharing one header.
    first = messages[i]
    j = i + 1
    n = len(messages)
    while j < n and j - i < 255 and _same_group(first, messages[j]):
        j += 1
    return j


def frame_size(messages):
    """Return the length of a frame carrying messages (header and tag included).

    Messages are checked (ValueError if a field is out of range or set when
    its mode or class does not carry it) and bundled as `encode` does.
    """
    size = OVERHEAD
    n = len(messages)
    i = 0
    while i < n:
        j = _group_end(messages, i)
        size += 2  # control, port
        if j - i > 1:
            size += 2  # length, count
        elif j < n:
            size += 1  # length (not last)
        while i < j:
            _check_message(messages[i])
            size += _element_size(messages[i])
            i += 1
    return size


def _put16(buf, pos, value):
    buf[pos] = value & 0xFF
    buf[pos + 1] = value >> 8
    return pos + 2


def encode(buf, header, messages, limit=MAX_FRAME):
    """Write a frame into buf; return its length (tag included).

    The body is left in clear and the tag zeroed, for the security layer to
    seal in place. limit: maximum frame length (`frame_limit`). Raises
    ValueError for a field out of range, or a frame longer than limit or buf.
    """
    size = frame_size(messages)
    if size > limit or size > MAX_FRAME or size > len(buf):
        raise ValueError("frame too long")
    encode_header(buf, header)
    pos = HEADER_SIZE
    n = len(messages)
    i = 0
    while i < n:
        j = _group_end(messages, i)
        first = messages[i]
        control = first.mode << 5 | first.cls << 3
        if first.route is not None:
            control |= _CONTROL_ROUTE
        if j - i > 1:
            control |= _CONTROL_BUNDLE
        if j == n:
            control |= _CONTROL_LAST
        buf[pos] = control
        buf[pos + 1] = first.port
        pos += 2
        if j - i > 1:
            buf[pos] = len(first.payload)
            buf[pos + 1] = j - i
            pos += 2
        elif j < n:
            buf[pos] = len(first.payload)
            pos += 1
        while i < j:
            m = messages[i]
            if _HAS_ORIGIN[m.mode]:
                pos = _put16(buf, pos, m.origin)
            if _HAS_DEST[m.mode]:
                pos = _put16(buf, pos, m.dest)
            if m.seq is not None:
                pos = _put16(buf, pos, m.seq)
            if m.route is not None:
                buf[pos] = len(m.route)
                pos += 1
                for hop in m.route:
                    pos = _put16(buf, pos, hop)
            k = len(m.payload)
            buf[pos : pos + k] = m.payload
            pos += k
            i += 1
    for k in range(TAG_SIZE):
        buf[pos + k] = 0
    return pos + TAG_SIZE


def decode_messages(buf, start, end):
    """Return the messages of the clear body buf[start:end], as a list.

    Bundles are expanded into one Message per element; payloads are
    memoryviews into buf. An empty body gives an empty list. Raises
    FormatError(`TRUNCATED` or `MALFORMED`) on a body that cannot be parsed.
    """
    mv = memoryview(buf)
    out = []
    pos = start
    while pos < end:
        if pos + 2 > end:
            raise FormatError(TRUNCATED)
        control = buf[pos]
        port = buf[pos + 1]
        pos += 2
        mode = control >> 5
        cls = (control >> 3) & 3
        if mode > FIRST_HOP or cls == 3:
            raise FormatError(MALFORMED)
        routed = control & _CONTROL_ROUTE
        if routed and mode != DOWNWARD and mode != P2P:
            raise FormatError(MALFORMED)
        last = control & _CONTROL_LAST
        if control & _CONTROL_BUNDLE:
            if pos + 2 > end:
                raise FormatError(TRUNCATED)
            length = buf[pos]
            count = buf[pos + 1]
            pos += 2
            if count < 2:
                raise FormatError(MALFORMED)
        else:
            count = 1
            if last:
                length = -1  # to the end of the body
            else:
                if pos >= end:
                    raise FormatError(TRUNCATED)
                length = buf[pos]
                pos += 1
        sequenced = has_sequence(mode, cls)
        fixed = _ADDRESS_BYTES[mode] + (2 if sequenced else 0) + (1 if routed else 0)
        while count:
            if pos + fixed > end:
                raise FormatError(TRUNCATED)
            origin = dest = seq = route = None
            if _HAS_ORIGIN[mode]:
                origin = buf[pos] | buf[pos + 1] << 8
                pos += 2
            if _HAS_DEST[mode]:
                dest = buf[pos] | buf[pos + 1] << 8
                pos += 2
            if sequenced:
                seq = buf[pos] | buf[pos + 1] << 8
                pos += 2
            if routed:
                hops = buf[pos]
                pos += 1
                if hops > MAX_ROUTE_HOPS:
                    raise FormatError(MALFORMED)
                if pos + 2 * hops > end:
                    raise FormatError(TRUNCATED)
                route = tuple(buf[p] | buf[p + 1] << 8 for p in range(pos, pos + 2 * hops, 2))
                pos += 2 * hops
            if length < 0:
                payload_end = end
            else:
                payload_end = pos + length
                if payload_end > end:
                    raise FormatError(TRUNCATED)
            out.append(Message(mode, cls, port, mv[pos:payload_end], origin, dest, seq, route))
            pos = payload_end
            count -= 1
        if last and pos != end:
            raise FormatError(MALFORMED)
    return out


def decode(buf, length):
    """Return (Header, messages) of a frame of length bytes whose body is clear.

    For frames already opened by the security layer, and for tests; raises
    FormatError as `decode_header` and `decode_messages`.
    """
    header = decode_header(buf, length)
    return header, decode_messages(buf, HEADER_SIZE, length - TAG_SIZE)

"""BON (Binary Object Notation) v1.0 -- single-file pure-Python implementation.

A direct port of the reference C implementation in ``clang/bon.cpp`` /
``clang/bon.h``.  The wire format is byte-for-byte identical, the object model is
identical, and every algorithm (number encoding, TLV walking, validation, JSON
rendering) is a transliteration of the C++ original.  Nothing outside the
standard library is used and no C extension is involved.

The public surface
------------------
The module exports these names, and everything else in it is private:

* :class:`bonlib` -- the ported C API.  One static method per ``bon_*`` function
  declared in ``clang/bon.h``, with the prefix dropped, so C code translates by
  search and replace::

      bon_get_int_bytes(n, &bytes, &size, &neg)  ->  bonlib.get_int_bytes(n)
      bon_w_int64(w, v)                           ->  bonlib.w_int64(w, v)
      bon_iter_next(&it)                          ->  bonlib.iter_next(it)

  Nothing is instantiated, so ``bonlib.w_int64(w, v)`` and
  ``lib = bonlib(); lib.w_int64(w, v)`` are the same call.  A node is only ever
  obtained from :class:`bonlib` -- parsing, navigation, the getters, the writer
  and the cursors all hang off it, so there is no way to reach a ``bon_*``
  function without going through the class.
* :class:`bonpy` -- BON <-> plain Python objects (``to_python`` / ``from_python`` /
  ``dumps`` / ``loads`` and the ``DecimalValue`` / ``ObjectPairs`` value types),
  plus the conveniences the C has no word for: :meth:`bonpy.get_int` for exact
  bignums, :meth:`bonpy.stream_bytes`, :meth:`bonpy.tlv_count`,
  :meth:`bonpy.iter_items`, :meth:`bonpy.iter_children`, :meth:`bonpy.mode_name`
  and :meth:`bonpy.writer_root_type`.  None of it is a ``bon_*`` function, which
  is exactly why it is a separate class.
* :class:`bontools` -- the json-shaped front end, and the only part of this module
  whose *call shape* is a design choice rather than a port.  :func:`bon.load`,
  :func:`bon.loads`, :func:`bon.dump` and :func:`bon.dumps` are the four
  entry points, and :attr:`bonlib.Bon.bontools` gives a node its path builder::

      import bon

      with open("data.bon", "rb") as handle:
          data = bon.load(handle)              # a bon object
      data.bontools.kv.key("name").get()       # -> "Alice"
      data.bontools.kv.key("age").set(31)      # rewrites the document in place

  :class:`bontype` pins a value to a BON type where Python's own types are
  ambiguous or lossy -- a NUMBER written from decimal text, a STRING that is not
  valid UTF-8, a BLOB used as a key.  Everything below is a thin composition of
  the two layers above; no new encoding lives here.

  Two names are deliberately close and cannot be swapped, so both say so in their
  docstrings: ``bon.loads`` yields a **bon object** and ``bonpy.loads`` yields
  **plain Python data**, and ``bon.dumps`` serialises a bon object while
  ``bonpy.dumps`` encodes Python data.

Object model
------------
A *bon object is the byte stream itself*.  One shared buffer holds the whole
document and every :class:`bonlib.Bon` is only a *window* (offset + length) into it,
always starting exactly on a TLV boundary.  The root window starts right after
the 3 magic bytes, so ``magic + root TLV`` is the complete file.  Navigation
hands out child windows that view the very same bytes -- no copying, no
reparsing, no intermediate tree.  This is what makes :meth:`bonlib.data`,
:meth:`bonlib.stream` and :meth:`bonlib.get_bytes` true zero-copy views.

Two C++ mechanisms map onto their Python equivalents:

* **Reference counting becomes garbage collection.**  In C a child keeps the
  buffer alive through an atomic refcount, so ``bon_unref(root)`` and then using
  the child is legal.  In Python a child simply holds a reference to the shared
  buffer object, so ``del root`` and then using the child is legal too -- and
  *forgetting* to release anything is never a use-after-free.
  :meth:`bonlib.ref`, :meth:`bonlib.unref` and :meth:`bonlib.free` exist for API
  completeness and do nothing; the guarantee the C API asks you to maintain by
  hand comes for free.
* **The thread-local error slot becomes exceptions.**  Every fallible call raises
  :class:`bonlib.BonError`, carrying the same ``BON_E_*`` code and the same message the
  C version would have latched.  :meth:`bonlib.last_error`,
  :meth:`bonlib.last_error_code` and :meth:`bonlib.clear_error` mirror the C slot
  for callers that prefer polling.  The one non-raising call is
  :meth:`bonlib.find`, which returns ``None`` for a genuinely absent key instead
  of raising, because "no such key" is a normal answer rather than a failure.

Out-parameters are return values, since a Python ``bytes``/``str``/``memoryview``
already knows its own length: ``bon_get_int_bytes(n, &bytes, &size, &neg)``
becomes ``bonlib.get_int_bytes(n) -> (payload, negative)`` and
``bon_get_decimal(n, &coefficient, &scale)`` becomes
``bonlib.get_decimal(n) -> (coefficient, scale)``.  The ``size`` argument the C
demands is optional everywhere.

Magnitudes past 64 bits are read with :meth:`bonlib.get_int_str` and written
with :meth:`bonlib.w_int_dec` -- decimal text in, decimal text out -- which is
exactly how the C spells them.  Python's unbounded ``int`` is what the format's
VARINT mode was designed for, but the C API has no arbitrary-precision accessor
and neither does this port, so ``int(bonlib.get_int_str(n))`` is the route to an
exact Python integer.

The format imposes no nesting depth limit, so nothing here recurses: validation,
JSON rendering, the Python mapping layer and the ``bontools`` re-encoder all drive
an explicit heap stack, and a document nested a million levels deep is handled
without approaching Python's recursion limit.

Editing is re-encoding
---------------------
BON is immutable by construction: no C function writes into a parsed document,
and the writer only ever appends.  So ``.set()`` cannot patch a TLV in place.  It
decodes the document, applies the change, encodes a new one, and re-points the node
it was called on -- which is why ``data.bontools.kv.key("a").set(1)`` is visible
through ``data`` on the next ``get()`` with no reassignment.  The consequences are
worth knowing:

* Nodes taken out of the document *before* the write keep pointing at the old
  bytes.  Re-read, or take the node ``set()`` returns.
* An untouched document re-encodes byte for byte, and a value that is not valid
  UTF-8 is carried through as raw bytes rather than decoded, so an edit never
  corrupts what it did not touch.
* A ``.key()`` step replaces the first member with that key and appends one if it
  is absent; an ``.index()`` step replaces or appends, and refuses to leave a hole,
  since BON has no way to represent one.

Quick start
-----------
::

    from bon import bonlib

    w = bonlib.writer_new(bonlib.BON_OBJECT)
    bonlib.w_string(w, "name"); bonlib.w_string(w, "Alice")
    bonlib.w_string(w, "age");  bonlib.w_int64(w, 30)      # -> uint8, minimal mode
    bonlib.w_string(w, "tags")
    bonlib.w_array_begin(w)
    bonlib.w_string(w, "c"); bonlib.w_string(w, "bon")
    bonlib.w_array_end(w)
    root = bonlib.writer_finish(w)        # or `with ... as w:`

    bonlib.at(root, 0)                    # first value ("Alice")
    bonlib.find(root, b"name")            # value for a STRING key
    bonlib.to_json(root)                  # debug JSON view
    bonlib.save_file(root, "out.bon")

    bonlib.validate(root)                 # strict, whole-subtree check
    print(bonlib.to_hex(bonlib.stream(root)))

And the same document through the json-shaped layer::

    import bon

    data = bon.loads(open("out.bon", "rb").read())
    data.bontools.kv.key("name").get()    # "Alice"
    data.bontools.kv.key("tags").index(1).get()
    data.bontools.kv.key("age").set(31)   # visible on `data` straight away
    bon.dump(data, "out2.bon")
"""

from __future__ import annotations

import math
import os
import struct
import sys
import threading
from decimal import Decimal
from typing import Any, Callable, Iterator, Mapping, NamedTuple

__all__ = [
    # The C API, one function per bon_* entry point in clang/bon.h.
    "bonlib",
    # The BON <-> plain Python mapping layer (not part of the C library).
    "bonpy",
    # The json-shaped convenience layer built on those two.
    "bontools", "bontype",
    # bontools' four entry points, exposed directly on the module.
    "load", "loads", "dump", "dumps",
]


# ---------------------------------------------------------------------------
# Format constants
# ---------------------------------------------------------------------------

#: File magic, the three bytes ``62 6F 6E`` ("bon").
MAGIC = b"bon"
#: Length of :data:`MAGIC`.
MAGIC_SIZE = len(MAGIC)
#: Library version string.
VERSION = "1.0.0"
VERSION_MAJOR = 1
VERSION_MINOR = 0
VERSION_PATCH = 0

BON_NONE = 0x00
BON_BOOL = 0x01
BON_NUMBER = 0x02
BON_FLOAT = 0x03
BON_STRING = 0x04
BON_ARRAY = 0x05
BON_OBJECT = 0x06
BON_BLOB = 0x07
BON_DECIMAL = 0x08

BON_MODE_UINT8 = 0x00
BON_MODE_UINT16 = 0x01
BON_MODE_UINT32 = 0x02
BON_MODE_UINT64 = 0x03
BON_MODE_VARINT = 0x04
BON_MODE_NUINT8 = 0x05
BON_MODE_NUINT16 = 0x06
BON_MODE_NUINT32 = 0x07
BON_MODE_NUINT64 = 0x08

BON_E_OK = 0
BON_E_INVALID = 1
BON_E_MAGIC = 2
BON_E_TRUNCATED = 3
BON_E_TYPE = 4
BON_E_MODE = 5
BON_E_LENGTH = 6
BON_E_ROOT = 7
BON_E_RANGE = 8
BON_E_NOKEY = 9
BON_E_DEPTH = 10  # reserved: the format imposes no nesting limit
BON_E_STATE = 11
BON_E_IO = 12
BON_E_MEMORY = 13
BON_E_USAGE = 14

_TYPE_NAMES = {
    BON_NONE: "NONE",
    BON_BOOL: "BOOL",
    BON_NUMBER: "NUMBER",
    BON_FLOAT: "FLOAT",
    BON_STRING: "STRING",
    BON_ARRAY: "ARRAY",
    BON_OBJECT: "OBJECT",
    BON_BLOB: "BLOB",
    BON_DECIMAL: "DECIMAL",
}

_MODE_NAMES = {
    BON_MODE_UINT8: "uint8",
    BON_MODE_UINT16: "uint16",
    BON_MODE_UINT32: "uint32",
    BON_MODE_UINT64: "uint64",
    BON_MODE_VARINT: "varint",
    BON_MODE_NUINT8: "-uint8",
    BON_MODE_NUINT16: "-uint16",
    BON_MODE_NUINT32: "-uint32",
    BON_MODE_NUINT64: "-uint64",
}

_ERROR_MESSAGES = {
    BON_E_OK: "ok",
    BON_E_INVALID: "invalid bon object",
    BON_E_MAGIC: "bad magic number",
    BON_E_TRUNCATED: "truncated data",
    BON_E_TYPE: "type mismatch",
    BON_E_MODE: "reserved or unknown number mode",
    BON_E_LENGTH: "length field inconsistency",
    BON_E_ROOT: "root type must be ARRAY or OBJECT",
    BON_E_RANGE: "value out of range",
    BON_E_NOKEY: "key not found",
    BON_E_DEPTH: "maximum nesting depth exceeded",
    BON_E_STATE: "invalid writer state",
    BON_E_IO: "input/output error",
    BON_E_MEMORY: "out of memory",
    BON_E_USAGE: "invalid usage",
}

#: Payload width, in bytes, of every fixed-width number mode.
_FIXED_MODE_SIZE = {
    BON_MODE_UINT8: 1, BON_MODE_UINT16: 2, BON_MODE_UINT32: 4, BON_MODE_UINT64: 8,
    BON_MODE_NUINT8: 1, BON_MODE_NUINT16: 2, BON_MODE_NUINT32: 4, BON_MODE_NUINT64: 8,
}

_CONTAINERS = (BON_ARRAY, BON_OBJECT)
_CONTAINER_SET = frozenset(_CONTAINERS)

_U64_MAX = 0xFFFFFFFFFFFFFFFF
_U64_SIGN = 0x8000000000000000
_I64_MIN = -0x8000000000000000
_I64_MAX = 0x7FFFFFFFFFFFFFFF

_U64 = struct.Struct("<Q")
_F64 = struct.Struct("<d")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class BonError(Exception):
    """Raised by every fallible operation in this module.

    ``code`` is the same ``BON_E_*`` constant the C implementation latches into
    its thread-local slot and ``message`` is the same free-form description it
    formats into its 256-byte buffer, so a message from this port is directly
    comparable with one from the C library.
    """

    __slots__ = ("code", "message")

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return f"bon: {self.message} [{_error_message(self.code)}]"


def _error_message(code: int) -> str:
    """Return the short description of a ``BON_E_*`` error code."""
    return _ERROR_MESSAGES.get(code, "unknown error")


_slot = threading.local()


def _fail(code: int, message: str) -> Any:
    """Latch ``code`` in the thread-local slot, then raise :class:`bonlib.BonError`."""
    _slot.code = code
    _slot.message = message
    raise BonError(code, message)


def _clear_error() -> None:
    _slot.code = BON_E_OK
    _slot.message = ""


def _last_error_code() -> int:
    """Code of the most recent failure on this thread (``BON_E_OK`` if none)."""
    return getattr(_slot, "code", BON_E_OK)


def _last_error() -> tuple[int, str] | None:
    """``(code, message)`` of the most recent failure on this thread, else ``None``."""
    code = getattr(_slot, "code", BON_E_OK)
    if code == BON_E_OK:
        return None
    return code, getattr(_slot, "message", "")


def _clear_error_pub() -> None:
    """Reset the thread-local error slot."""
    _clear_error()


# ---------------------------------------------------------------------------
# Small shared value objects
# ---------------------------------------------------------------------------


class _DecimalValue(NamedTuple):
    """A DECIMAL payload: the value is ``coefficient * 10 ** -scale``.

    Returned by ``Bon._get_decimal``, and accepted by
    ``Writer._w_decimal`` and :func:`_dumps`, so a decimal survives a round
    trip with its exact coefficient and scale.
    """

    coefficient: int
    scale: int

    @property
    def value(self) -> Decimal:
        """The exact :class:`decimal.Decimal` this payload denotes."""
        sign, digits, exp = Decimal(self.coefficient).as_tuple()
        return Decimal((sign, digits, exp - self.scale))


class _ObjectPairs(list):
    """An ordered list of ``(key, value)`` pairs -- the OBJECT counterpart of
    :class:`list`.

    BON objects may repeat keys and may key on *any* type, so they cannot be
    squeezed into a :class:`dict` without losing information.
    :func:`_to_python` returns this type and :func:`_dumps` accepts it, which is
    what makes ``bonpy.dumps(bonpy.to_python(root)) == bytes(bonlib.stream(root))`` hold exactly.
    """

    __slots__ = ()


# ---------------------------------------------------------------------------
# Low-level byte helpers
# ---------------------------------------------------------------------------


def _as_buffer(data: Any, size: int | None, what: str) -> memoryview:
    """Normalise ``data`` to a flat byte ``memoryview``, optionally truncated."""
    if isinstance(data, (bytes, bytearray)):
        view = memoryview(data)
    else:
        try:
            view = memoryview(data)
        except TypeError:
            raise TypeError(f"{what} must be bytes-like, not {type(data).__name__}") from None
        try:
            if view.ndim != 1 or view.itemsize != 1:
                view = view.cast("B")
        except TypeError:
            view = memoryview(bytes(view))  # not C-contiguous: materialise it
    total = view.nbytes
    if size is None:
        return view
    if size < 0:
        _fail(BON_E_LENGTH, f"negative size {size}")
    if size > total:
        _fail(BON_E_LENGTH, f"size {size} exceeds the {total}-byte buffer")
    return view if size == total else view[:size]


def _require_int(value: Any, what: str) -> int:
    """Guard a Python-level argument that must be an ``int`` (``bool`` excluded).

    A wrong *type* is a caller bug rather than a document problem, so this
    raises ``TypeError`` instead of setting a ``BON_E_*`` code the way a range
    violation does.
    """
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"expected int for {what}, not {type(value).__name__}")
    return value


def _rd_u64(data: memoryview, off: int) -> int:
    """Read a little-endian uint64 at ``off``."""
    return _U64.unpack_from(data, off)[0]


def _u64_to_i64(value: int) -> int:
    """Reinterpret a uint64 as a two's-complement int64."""
    return value - (1 << 64) if value >= _U64_SIGN else value


def _parse_tlv(data: memoryview, off: int, avail: int) -> tuple[int, int, int] | None:
    """Decode the TLV header at ``off``; return ``(type, total, count)`` or ``None``.

    ``avail`` bounds the bytes the node may occupy.  Only headers are read, which
    is all navigation needs; payload invariants are :func:`_validate_tlv`'s job.
    """
    if avail < 1:
        return None
    t = data[off]
    if t == BON_NONE:
        return t, 1, 0
    if t == BON_BOOL:
        return (t, 2, 0) if avail >= 2 else None
    if t == BON_NUMBER:
        if avail < 2:
            return None
        mode = data[off + 1]
        width = _FIXED_MODE_SIZE.get(mode)
        if width is not None:
            total = 2 + width
            return (t, total, 0) if total <= avail else None
        if mode == BON_MODE_VARINT:
            if avail < 10:
                return None
            length = _rd_u64(data, off + 2)
            if length > avail - 10:
                return None
            return t, 10 + length, 0
        return None
    if t == BON_FLOAT:
        return (t, 9, 0) if avail >= 9 else None
    if t == BON_DECIMAL:
        return (t, 17, 0) if avail >= 17 else None
    if t == BON_STRING or t == BON_BLOB:
        if avail < 9:
            return None
        length = _rd_u64(data, off + 1)
        if length > avail - 9:
            return None
        return t, 9 + length, 0
    if t == BON_ARRAY or t == BON_OBJECT:
        if avail < 17:
            return None
        total = _rd_u64(data, off + 1)
        if total > avail - 17:
            return None
        return t, 17 + total, _rd_u64(data, off + 9)
    return None


def _trim_2c(data: memoryview, off: int, size: int) -> int:
    """Length of the shortest little-endian two's-complement form of a payload.

    Trailing bytes that merely restate the sign are removable, and the format
    requires a varint payload to already be in this minimal form.
    """
    while size > 1:
        top = data[off + size - 1]
        below = data[off + size - 2]
        if top == 0x00 and (below & 0x80) == 0:
            size -= 1
        elif top == 0xFF and (below & 0x80) != 0:
            size -= 1
        else:
            break
    return size


def _int_to_2c(value: int) -> bytes:
    """Minimal little-endian two's-complement encoding of a Python ``int``.

    The negative case is one byte shorter than the naive ``bit_length()`` split
    because ``-128`` already fits in ``80``: the highest bit of the leading byte
    *is* the sign bit, so a negative number needs ``(|v| - 1).bit_length()`` bits
    plus the sign.  This matches the reference implementation's
    ``bon_get_int_bytes()`` byte for byte (``-128`` is one byte, ``-129`` two).
    """
    if value >= 0:
        size = value.bit_length() // 8 + 1
    else:
        size = (-value - 1).bit_length() // 8 + 1
    return value.to_bytes(size, "little", signed=True)


def _number_int(data: memoryview, off: int) -> int:
    """Decode any NUMBER payload into an exact Python ``int``.

    All nine modes collapse to one expression: a fixed unsigned mode *is* the
    value, a ``-uint*`` mode stores ``|value| - 1`` so the value is
    ``-(stored + 1)``, and the VARINT branch is a sign-extended little-endian
    read.  Unbounded Python ints are what remove the need for a decimal
    conversion helper.
    """
    mode = data[off + 1]
    if mode == BON_MODE_VARINT:
        length = _rd_u64(data, off + 2)
        if length == 0:
            _fail(BON_E_LENGTH, "varint payload is empty")
        return int.from_bytes(data[off + 10:off + 10 + length], "little", signed=True)
    width = _FIXED_MODE_SIZE.get(mode)
    if width is None:
        _fail(BON_E_MODE, f"reserved number mode 0x{mode:02X}")
    stored = int.from_bytes(data[off + 2:off + 2 + width], "little")
    return -(stored + 1) if mode > BON_MODE_UINT64 else stored


def _pick_mode(stored: int, negative: bool) -> tuple[int, int]:
    """Smallest fixed-width mode holding ``stored``, returning ``(mode, width)``.

    ``stored`` is the value itself when non-negative and ``|value| - 1`` when
    negative -- the ``-uint`` family spends every bit on magnitude, so a value
    fits as soon as ``|value| - 1`` does.
    """
    base = BON_MODE_NUINT8 if negative else BON_MODE_UINT8
    for step, bits in enumerate((8, 16, 32, 64)):
        if stored < (1 << bits):
            return base + step, bits // 8
    return (BON_MODE_NUINT64 if negative else BON_MODE_UINT64), 8


def _read_scalar(data: memoryview, off: int, size: int, t: int,
                 lenient: bool = False) -> Any:
    """Decode one non-container node straight out of the buffer, no node needed.

    ``lenient`` is for re-encoding, not for reading: a STRING that is not valid
    UTF-8 comes back as a :class:`bontype` holding its raw bytes, which is the
    only lossless way to hold one.  :func:`_to_python` leaves it off, so plain
    decoding still raises the way it always did.
    """
    if t == BON_NONE:
        return None
    if t == BON_BOOL:
        return data[off + 1] != 0
    if t == BON_NUMBER:
        return _number_int(data, off)
    if t == BON_FLOAT:
        return _F64.unpack_from(data, off + 1)[0]
    if t == BON_STRING:
        raw = bytes(data[off + 9:off + size])
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            if not lenient:
                raise
            return bontype.string(raw)
    if t == BON_BLOB:
        return bytes(data[off + 9:off + size])
    if t == BON_DECIMAL:
        return _DecimalValue(
            _u64_to_i64(_rd_u64(data, off + 1)),
            _rd_u64(data, off + 9),
        ).value
    _fail(BON_E_TYPE, f"type 0x{t:02X} is not a scalar")


def _key_bytes(key: Any, what: str = "key") -> memoryview:
    """Accept a ``str`` (encoded as UTF-8) or any bytes-like key."""
    if isinstance(key, str):
        return memoryview(key.encode("utf-8"))
    if isinstance(key, (bytes, bytearray, memoryview)):
        return memoryview(key)
    _fail(BON_E_TYPE, f"{what} must be str or bytes, not {type(key).__name__}")


def _need(node: Any) -> "Bon":
    if node is None:
        _fail(BON_E_INVALID, "null bon object")
    if not isinstance(node, Bon):
        raise TypeError(f"expected a Bon object, not {type(node).__name__}")
    return node


def _type_name(t: int) -> str:
    return _TYPE_NAMES.get(t, "UNKNOWN")


def _type_name_of(node_or_type: Any) -> str:
    """``"STRING"``, ``"ARRAY"``, ... for a node or a bare type code."""
    return _type_name(node_or_type._type) if isinstance(node_or_type, Bon) \
        else _type_name(node_or_type)


def _mode_name_of(mode: int) -> str:
    """``"uint16"``, ``"-uint8"``, ``"varint"``, ... for a number mode code."""
    return _MODE_NAMES.get(mode, f"reserved(0x{mode:02X})")


# ---------------------------------------------------------------------------
# Buffer and node
# ---------------------------------------------------------------------------


class _Buf:
    """The shared byte stream a document lives in.

    Every node of one document points at the same ``_Buf``; that single shared
    reference *is* the whole lifetime story.  ``borrowed`` only records that the
    bytes came from the caller -- Python's own ownership of the underlying
    ``memoryview`` keeps them alive for us, which is the one thing the C
    ``bon_from_stream_ref()`` contract asks you to arrange by hand.
    """

    __slots__ = ("data", "borrowed")

    def __init__(self, data: memoryview, borrowed: bool = False) -> None:
        self.data = data
        self.borrowed = borrowed


class Bon:
    """One TLV, seen in place inside a document.

    Nodes are not built by hand: ``_from_stream``, ``Writer._finish`` and
    the navigation methods hand them out.  A node owns nothing but a reference to
    the shared buffer, so keeping a child is enough to keep the whole document
    readable after every other node has been dropped.
    """

    __slots__ = ("_buf", "_off", "_size", "_type", "_count")

    def __init__(self, buf: _Buf, off: int, size: int, type_: int, count: int) -> None:
        self._buf = buf
        self._off = off
        self._size = size
        self._type = type_
        self._count = count

    def __repr__(self) -> str:
        return f"<Bon {self._get_type_name} size={self._size} count={self._count}>"

    @property
    def _get_tlv_count(self) -> int:
        """Number of TLVs stored in this container.

        For an ARRAY this equals :attr:`bonlib.count`; for an OBJECT ``count`` is the
        number of *pairs*, so this is twice as large.  Any loop that walks
        children with a cursor needs this one, not ``count``.
        """
        return self._count * 2 if self._type == BON_OBJECT else self._count

    # -- identity --------------------------------------------------------

    @property
    def _get_type(self) -> int:
        """The node's ``BON_*`` type code."""
        return self._type

    @property
    def _get_type_name(self) -> str:
        """The node's type as a name, e.g. ``"STRING"``."""
        return _type_name(self._type)

    @property
    def _get_size(self) -> int:
        """Byte length of this node's own TLV, **without** the 3 magic bytes.

        A file is exactly ``MAGIC_SIZE + bonlib.size(root)`` bytes; :attr:`bonlib.stream`
        hands out those bytes.  Getting this wrong is the single most common
        mistake with the C API, so the rule is stated here too.
        """
        return self._size

    @property
    def _get_count(self) -> int:
        """Element / pair count of a container, ``0`` for every other type."""
        return self._count if self._type in _CONTAINER_SET else 0

    @property
    def _get_is_root(self) -> bool:
        """True when this node *is* the document root rather than a nested value."""
        return self._off == MAGIC_SIZE

    @property
    def _get_root(self) -> "Bon":
        """The document root, or ``self`` when this node already is it."""
        if self._get_is_root:
            return self
        data = self._buf.data
        head = _parse_tlv(data, MAGIC_SIZE, data.nbytes - MAGIC_SIZE)
        if head is None:
            _fail(BON_E_TRUNCATED, "malformed root TLV")
        t, total, count = head
        if t not in _CONTAINER_SET:
            _fail(BON_E_ROOT, f"root type 0x{t:02X} must be ARRAY or OBJECT")
        return Bon(self._buf, MAGIC_SIZE, total, t, count)

    @property
    def _get_data(self) -> memoryview:
        """Zero-copy view of this node's TLV bytes: type byte, header, payload."""
        return self._buf.data[self._off:self._off + self._size]

    @property
    def _get_stream(self) -> memoryview:
        """Zero-copy view of the whole file, magic included.  Root nodes only."""
        if not self._get_is_root:
            _fail(BON_E_STATE, "bon object is not a root object")
        return self._buf.data

    @property
    def _get_stream_bytes(self) -> bytes:
        """A ``bytes`` copy of the whole file.  Root nodes only."""
        return bytes(self._get_stream)

    def _get_payload(self) -> memoryview:
        """Zero-copy view of a STRING / BLOB payload, length prefix excluded."""
        if self._type not in (BON_STRING, BON_BLOB):
            _fail(BON_E_TYPE, f"{self._get_type_name} has no byte payload")
        return self._buf.data[self._off + 9:self._off + self._size]

    def _validate(self) -> "Bon":
        """Strictly check this whole subtree, raising :class:`bonlib.BonError` on any problem.

        Magic, root rules, reserved type and mode codes, minimal varints, BOOL
        payloads, length fields and container counts are all verified.  Returns
        ``self`` so the call can be chained.
        """
        return _validate(self)

    # -- navigation ------------------------------------------------------

    def _windows(self) -> Iterator[tuple[int, int, int, int]]:
        """Walk the direct children in order, yielding ``(off, size, type, count)``.

        One sequential, allocation-free cursor -- the same shape as the cursor in
        the C ``validate_tree()``: an open container costs one frame, never a
        Python call frame, so nesting depth costs heap rather than stack.
        """
        data = self._buf.data
        pos = self._off + 17
        end = self._off + self._size
        i = 0
        while pos < end:
            head = _parse_tlv(data, pos, end - pos)
            if head is None:
                _fail(BON_E_LENGTH, f"child {i} of {self._get_type_name} is malformed")
            t, total, count = head
            yield pos, total, t, count
            pos += total
            i += 1

    def _child(self, flat: int) -> "Bon":
        """Child at a *flat* TLV index; an OBJECT interleaves keys and values.

        Walks from the container start, so this is O(flat) -- the same trade the
        C reference makes.  Iterators never call it per element, they share a
        cursor instead, so whole-container traversals stay O(n).
        """
        data = self._buf.data
        pos = self._off + 17
        end = self._off + self._size
        i = 0
        while True:
            head = _parse_tlv(data, pos, end - pos)
            if head is None:
                _fail(BON_E_LENGTH, f"child {i} of {self._get_type_name} is malformed")
            if i == flat:
                break
            pos += head[1]
            i += 1
        t, total, count = head
        return Bon(self._buf, pos, total, t, count)

    def _index(self, index: int, which: int) -> "Bon":
        if self._type not in _CONTAINER_SET:
            _fail(BON_E_TYPE, f"{self._get_type_name} has no child nodes")
        if index < 0:
            index += self._count
        if not 0 <= index < self._count:
            _fail(BON_E_RANGE, f"index {index} out of range (count {self._count})")
        step = 2 if self._type == BON_OBJECT else 1
        # An ARRAY has no key half, so bon_value_at() on one is a type error
        # rather than a walk that runs off the end of the container.
        if not 0 <= which < step:
            _fail(BON_E_TYPE, f"{self._get_type_name} has no child {which}")
        return self._child(index * step + which)

    def _at(self, index: int) -> "Bon":
        """Child ``index``: an ARRAY element, or an OBJECT value.

        Negative indices count from the end, as usual in Python.
        """
        return self._index(index, 1 if self._type == BON_OBJECT else 0)

    def _key_at(self, index: int) -> "Bon":
        """Key half of the ``index``-th pair of an OBJECT."""
        return self._index(index, 0)

    def _value_at(self, index: int) -> "Bon":
        """Value half of the ``index``-th pair of an OBJECT."""
        return self._index(index, 1)

    def _find(self, key: Any) -> "Bon | None":
        """Value of the first pair whose key equals ``key``, or ``None``.

        Keys are compared byte for byte, so only STRING keys can ever match and
        no Unicode normalisation happens.  An absent key yields ``None`` instead
        of an exception, because "no such key" is an answer rather than a failure.
        """
        if self._type != BON_OBJECT:
            _fail(BON_E_TYPE, f"{self._get_type_name} is not an OBJECT")
        data = self._buf.data
        needle = _key_bytes(key)
        for flat, (off, total, t, _count) in enumerate(self._windows()):
            # Only even flat indexes are keys of this object; a STRING *value*
            # that happens to equal the needle must never be mistaken for a key.
            if flat % 2 or t != BON_STRING:
                continue
            if total - 9 == needle.nbytes and data[off + 9:off + total] == needle:
                return self._child(flat + 1)
        return None

    def _key_count(self, key: Any) -> int:
        """How many times ``key`` occurs; objects may legitimately repeat keys."""
        if self._type != BON_OBJECT:
            _fail(BON_E_TYPE, f"{self._get_type_name} is not an OBJECT")
        data = self._buf.data
        needle = _key_bytes(key)
        hits = 0
        for flat, (off, total, t, _count) in enumerate(self._windows()):
            if flat % 2 or t != BON_STRING:
                continue
            if total - 9 == needle.nbytes and data[off + 9:off + total] == needle:
                hits += 1
        return hits

    def _key_is(self, index: int, key: Any) -> bool:
        """Whether the ``index``-th key of an OBJECT equals ``key``."""
        needle = _key_bytes(key)
        key_node = self._key_at(index)
        if key_node._type != BON_STRING:
            return False
        data = key_node._buf.data
        return key_node._size - 9 == needle.nbytes \
            and data[key_node._off + 9:key_node._off + key_node._size] == needle

    def _key_bytes(self, index: int) -> bytes:
        """Raw UTF-8 bytes of the ``index``-th key (STRING keys only)."""
        key = self._key_at(index)
        if key._type != BON_STRING:
            _fail(BON_E_TYPE, f"key {index} is {key._get_type_name}, not STRING")
        return key._get_payload().tobytes()

    def _key_string(self, index: int) -> str:
        """The ``index``-th key of an OBJECT, decoded as UTF-8."""
        return self._key_bytes(index).decode("utf-8")

    def _iter_items(self) -> Iterator[tuple["Bon", "Bon"]]:
        """Iterate an OBJECT's ``(key, value)`` node pairs in document order."""
        if self._type != BON_OBJECT:
            _fail(BON_E_TYPE, f"{self._get_type_name} is not an OBJECT")
        windows = self._windows()
        for off, total, t, count in windows:
            voff, vtotal, vt, vcount = next(windows, (None, 0, 0, 0))
            if voff is None:
                _fail(BON_E_LENGTH, "object key without a value")
            yield (Bon(self._buf, off, total, t, count),
                   Bon(self._buf, voff, vtotal, vt, vcount))

    def _iter_children(self) -> Iterator["Bon"]:
        """Iterate an ARRAY's elements, or an OBJECT's values.

        Keys are not included: use :meth:`bonpy.iter_items` for pairs or
        :meth:`bonlib.iter_init` with ``which=0`` for keys alone.
        """
        if self._type not in _CONTAINER_SET:
            _fail(BON_E_TYPE, f"{self._get_type_name} is not a container")
        step = 1 if self._type == BON_OBJECT else 0
        return (Bon(self._buf, off, total, t, count)
                for index, (off, total, t, count) in enumerate(self._windows())
                if index % 2 == step)

    def _iter(self, which: int = 1) -> "Iter":
        """Create a cursor: ``which=1`` walks values, ``which=0`` walks keys."""
        return Iter(self, which)

    # -- scalar readers --------------------------------------------------

    def _raw(self, want: int) -> memoryview:
        if self._type != want:
            _fail(BON_E_TYPE, f"{self._get_type_name} is not {_type_name(want)}")
        return self._buf.data

    def _get_none(self) -> None:
        """Assert the node is a NONE and return ``None``."""
        self._raw(BON_NONE)
        return None

    def _get_bool(self) -> bool:
        """The BOOL payload."""
        return self._raw(BON_BOOL)[self._off + 1] != 0

    def _get_int(self) -> int:
        """The NUMBER payload as an exact Python ``int``, at any precision.

        This is the one reader the C API does not have: it needs
        ``bon_get_int64()``, then ``bon_get_uint64()``, then
        ``bon_get_int_str()`` before an integer wider than 64 bits is readable
        at all.  Python bignums just decode.
        """
        self._raw(BON_NUMBER)
        return _number_int(self._buf.data, self._off)

    def _get_int64(self) -> int:
        """The NUMBER payload, or raise ``BON_E_RANGE`` if it misses ``int64``."""
        value = self._get_int()
        if not _I64_MIN <= value <= _I64_MAX:
            _fail(BON_E_RANGE, f"value {value} does not fit in int64")
        return value

    def _get_uint64(self) -> int:
        """The NUMBER payload as unsigned, or raise if it is negative or too wide."""
        value = self._get_int()
        if value < 0:
            _fail(BON_E_RANGE, "value is negative")
        if value > _U64_MAX:
            _fail(BON_E_RANGE, f"value {value} does not fit in uint64")
        return value

    def _get_int_str(self) -> str:
        """The NUMBER payload in decimal, at any precision."""
        return str(self._get_int())

    def _get_int_bytes(self) -> tuple[bytes, bool]:
        """``(payload, negative)`` for this NUMBER, plus the reference's width rules.

        The C's ``bon_get_int_bytes()`` deliberately does *not* normalise to one
        shape, and neither does this:

        * VARINT hands back the payload exactly as it sits on the wire, sign taken
          from the top bit of the last byte.
        * A ``-uint*`` mode is converted to the shortest two's-complement form of
          the value it denotes (``-128`` is one byte, ``-129`` two).
        * A positive fixed mode hands back the **mode's full width** -- with one
          extra ``0x00`` when the top bit would otherwise read as a sign -- so
          ``65536`` in INT32 is four bytes, not the three a minimal encoder
          would choose.  That is what the reference prints, and keeping it means
          a BON dump stays diffable against ``bon_write_hex()`` output.

        Feeding the result back to :meth:`Writer._w_int_bytes` is lossless either
        way.
        """
        self._raw(BON_NUMBER)
        data = self._buf.data
        off = self._off
        mode = data[off + 1]
        if mode == BON_MODE_VARINT:
            size = _rd_u64(data, off + 2)
            if size == 0:
                _fail(BON_E_LENGTH, "varint payload is empty")
            payload = bytes(data[off + 10:off + 10 + size])
            return payload, bool(payload[-1] & 0x80)
        width = _FIXED_MODE_SIZE.get(mode)
        if width is None:
            _fail(BON_E_MODE, f"reserved number mode 0x{mode:02X}")
        stored = int.from_bytes(data[off + 2:off + 2 + width], "little")
        if mode > BON_MODE_UINT64:
            return _int_to_2c(-(stored + 1)), True
        payload = stored.to_bytes(width, "little")
        if payload[-1] & 0x80:
            payload += b"\x00"
        return payload, False

    def _get_number_mode(self) -> int:
        """The NUMBER's wire mode code, e.g. ``BON_MODE_NUINT16``."""
        self._raw(BON_NUMBER)
        return self._buf.data[self._off + 1]

    def _get_double(self) -> float:
        """The FLOAT payload as a Python ``float``."""
        self._raw(BON_FLOAT)
        return _F64.unpack_from(self._buf.data, self._off + 1)[0]

    def _get_decimal(self) -> _DecimalValue:
        """The DECIMAL payload as ``_DecimalValue(coefficient, scale)``."""
        self._raw(BON_DECIMAL)
        return _DecimalValue(_u64_to_i64(_rd_u64(self._buf.data, self._off + 1)),
                            _rd_u64(self._buf.data, self._off + 9))

    def _get_bytes(self) -> memoryview:
        """Zero-copy view of a STRING / BLOB payload."""
        return self._get_payload()

    def _get_string(self, errors: str = "strict") -> str:
        """The STRING payload decoded as UTF-8.

        ``errors`` goes to :meth:`bytes.decode`, so ``"surrogateescape"`` recovers
        text from bytes that are not valid UTF-8.  The format does not require a
        STRING to be well-formed, and :meth:`bonlib.get_bytes` always works regardless.
        """
        self._raw(BON_STRING)
        return self._get_payload().tobytes().decode("utf-8", errors)

    def _get_value(self) -> Any:
        """This node as plain Python data, recursively (see :func:`_to_python`)."""
        return _to_python(self)

    # -- the convenience layer -------------------------------------------

    @property
    def bontools(self) -> "bontools":
        """The json-shaped view of this node, rooted here (see :class:`bontools`).

        So the everyday read and write look the way they do in ``json``::

            data.bontools.kv.key("name").get()
            data.bontools.kv.key("name").set("Alice")

        Bound to *this* node, not to the document root: a child node gives a view
        into that child, and :meth:`_Kv.set` re-points whichever node it was
        called on.
        """
        return bontools(self)

    # -- rendering -------------------------------------------------------

    def _to_json(self) -> str:
        """One-way JSON debug view of this node (see :func:`_to_json`)."""
        return _to_json(self)

    def _to_python(self) -> Any:
        """This node as plain Python data, recursively (see :func:`_to_python`)."""
        return _to_python(self)


# ---------------------------------------------------------------------------
# Iterator
# ---------------------------------------------------------------------------


class Iter:
    """Cursor over a container, mirroring the ``bon_iter_*`` family.

    ``which=1`` walks values (an OBJECT, as in the C API) and ``which=0`` walks
    keys -- which for an ARRAY is the same as walking its elements.  The cursor
    rides on a shared sequential walk, so it costs O(1) memory and O(n) in total.

            :meth:`bonlib.iter_index` is the 0-based position of the item just produced, ready to be
    handed back to :meth:`Bon._at` or :meth:`Bon._value_at`.  Each produced node
    views the shared buffer, so a cursor stays usable even if the container node
    itself is dropped.
    """

    __slots__ = ("_node", "_which", "_step", "_iter", "_index")

    def __init__(self, node: "Bon", which: int = 1) -> None:
        _need(node)
        if which not in (0, 1):
            _fail(BON_E_USAGE, "which must be 0 or 1")
        if node._type not in _CONTAINER_SET:
            _fail(BON_E_TYPE, f"{node._get_type_name} is not a container")
        if which == 1 and node._type != BON_OBJECT:
            _fail(BON_E_TYPE, "value iteration requires an OBJECT")
        self._node = node
        self._which = which
        self._step = 1 if node._type == BON_ARRAY else 2
        self._iter = node._windows()
        self._index = 0

    def __iter__(self) -> "Iter":
        return self

    def __next__(self) -> "Bon":
        if self._node is None:
            # Mirrors bon_iter_next()'s "iterator is not initialized".
            _fail(BON_E_USAGE, "bon cursor is not initialized")
        windows = self._iter
        for off, total, t, count in windows:
            # An ARRAY has one TLV per item; an OBJECT has two, and ``which``
            # decides whether the cursor hands back the key or the value -- so
            # the other half of the pair has to be consumed either way.
            if self._step == 1 or self._which == 0:
                node = Bon(self._node._buf, off, total, t, count)
                if self._step == 2 and next(windows, None) is None:
                    _fail(BON_E_LENGTH, "object key without a value")
                self._index += 1
                return node
            # which == 1 on an OBJECT: the key sits on the wire first, skip it.
            voff, vtotal, vt, vcount = next(windows, (None, 0, 0, 0))
            if voff is None:
                _fail(BON_E_LENGTH, "object key without a value")
            self._index += 1
            return Bon(self._node._buf, voff, vtotal, vt, vcount)
        raise StopIteration

    @property
    def _get_index(self) -> int:
        """0-based index of the item just produced (0 before the first item)."""
        return self._index - 1 if self._index else 0

    def _get_pairs(self) -> Iterator[tuple["Bon", "Bon"]]:
        """Iterate this OBJECT's ``(key, value)`` node pairs instead."""
        return _iter_items(self._node)

    def _key_next(self) -> tuple["Bon", "Bon"]:
        """The next ``(key, value)`` pair, advancing the cursor by one pair.

        The pair cursor shares its position with ``iter_next``, so an OBJECT
        cursor sits on a pair boundary either way and the two walks can be mixed
        without desynchronising -- which is what the C's ``it->pos`` does.
        """
        if self._node is None:
            _fail(BON_E_USAGE, "bon cursor is not initialized")
        if self._node._type != BON_OBJECT:
            _fail(BON_E_TYPE, "pair iteration requires an OBJECT")
        windows = self._iter
        koff, ktotal, kt, kcount = next(windows, (None, 0, 0, 0))
        if koff is None:
            raise StopIteration
        voff, vtotal, vt, vcount = next(windows, (None, 0, 0, 0))
        if voff is None:
            _fail(BON_E_LENGTH, "object key without a value")
        self._index += 1
        return (Bon(self._node._buf, koff, ktotal, kt, kcount),
                Bon(self._node._buf, voff, vtotal, vt, vcount))

    def _close(self) -> None:
        """Stop the cursor early.  Optional: garbage collection would do it anyway."""
        self._iter = iter(())
        self._node = None  # type: ignore[assignment]
        self._index = 0

    def __enter__(self) -> "Iter":
        return self

    def __exit__(self, *exc: Any) -> bool:
        self._close()
        return False


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _root_of(buf: _Buf) -> "Bon":
    """Turn a whole stream into a root node, enforcing the document-level rules."""
    data = buf.data
    size = data.nbytes
    if size < MAGIC_SIZE or bytes(data[:MAGIC_SIZE]) != MAGIC:
        _fail(BON_E_MAGIC, "stream does not start with the 'bon' magic")
    if size < MAGIC_SIZE + 17:
        _fail(BON_E_TRUNCATED, "stream is too short to hold a root value")
    head = _parse_tlv(data, MAGIC_SIZE, size - MAGIC_SIZE)
    if head is None:
        _fail(BON_E_TRUNCATED, f"malformed root TLV at offset {MAGIC_SIZE}")
    t, total, count = head
    if t not in _CONTAINER_SET:
        _fail(BON_E_ROOT, f"root type 0x{t:02X} must be ARRAY or OBJECT")
    if MAGIC_SIZE + total != size:
        _fail(BON_E_LENGTH,
              f"{size - MAGIC_SIZE - total} trailing byte(s) after the root value")
    return Bon(buf, MAGIC_SIZE, total, t, count)


def _from_stream(data: Any, size: int | None = None) -> "Bon":
    """Parse ``data`` and **copy** the bytes, so the document stands on its own.

    ``data`` is any bytes-like object; ``size`` optionally truncates it first.
    """
    _clear_error()
    if data is None:
        _fail(BON_E_INVALID, "null stream")
    return _root_of(_Buf(memoryview(bytes(_as_buffer(data, size, "stream")))))


def _from_stream_ref(data: Any, size: int | None = None) -> "Bon":
    """Parse ``data`` in place, **borrowing** the caller's bytes -- zero copy.

    Python owns the buffer you hand in, so unlike the C version there is nothing
    to keep alive by hand; the only rule left is the one the C API cannot enforce
    either: do not mutate the bytes while the document is in use, or the cached
    offsets stop pointing where you think they do.
    """
    _clear_error()
    if data is None:
        _fail(BON_E_INVALID, "null stream")
    return _root_of(_Buf(_as_buffer(data, size, "stream"), borrowed=True))


def _from_file(path: Any) -> "Bon":
    """Read a file and parse it; the bytes are copied into the document."""
    _clear_error()
    target = os.fspath(path)
    try:
        with open(target, "rb") as handle:
            data = handle.read()
    except OSError as exc:
        _fail(BON_E_IO, f"cannot read {target!r}: {exc.strerror or exc}")
    return _from_stream(data)


def _save_file(node: "Bon", path: Any) -> None:
    """Write ``node`` to ``path`` as a complete file.  Root nodes only."""
    _clear_error()
    _need(node)
    if not node._get_is_root:
        _fail(BON_E_STATE, "only a root bon object can be saved as a file")
    target = os.fspath(path)
    try:
        with open(target, "wb") as handle:
            handle.write(node._get_stream_bytes)
    except OSError as exc:
        _fail(BON_E_IO, f"cannot write {target!r}: {exc.strerror or exc}")


def _new_array() -> "Bon":
    """An empty ARRAY document."""
    w = Writer(BON_ARRAY)
    try:
        return w._finish()
    finally:
        w._free()


def _new_object() -> "Bon":
    """An empty OBJECT document."""
    w = Writer(BON_OBJECT)
    try:
        return w._finish()
    finally:
        w._free()


def _validate(node: "Bon") -> "Bon":
    """Strictly check a whole subtree, raising :class:`bonlib.BonError` on any problem.

    For a root node the magic and the root rules come first; then every TLV is
    checked against an explicit stack: reserved type and mode codes, minimal
    varints, BOOL payloads in ``{0, 1}``, length fields matching byte for byte,
    and container counts accounting for exactly the bytes present.  Returns
    ``node``.
    """
    _clear_error()
    _need(node)
    data = node._buf.data
    if node._get_is_root:
        if data.nbytes < MAGIC_SIZE or bytes(data[:MAGIC_SIZE]) != MAGIC:
            _fail(BON_E_MAGIC, "bad magic")
        if node._type not in _CONTAINER_SET:
            _fail(BON_E_ROOT, "root type must be ARRAY or OBJECT")
        if node._off + node._size != data.nbytes:
            _fail(BON_E_LENGTH, "trailing bytes after the root value")
    _validate_tree(data, node._off, node._size)
    return node


def _validate_tlv(data: memoryview, off: int, avail: int) -> tuple[int, int]:
    """Check one TLV in place: header, payload invariants, container length.

    Containers are *not* descended into here; :func:`_validate_tree` walks them
    with an explicit stack, so one level of nesting costs one cursor rather than
    one call frame.
    """
    head = _parse_tlv(data, off, avail)
    if head is None:
        _fail(BON_E_TRUNCATED, "malformed TLV")
    t, total, count = head
    if total > avail:
        _fail(BON_E_LENGTH, "value overruns its parent")
    if t == BON_NONE or t == BON_FLOAT or t == BON_DECIMAL:
        return total, count
    if t == BON_BOOL:
        if data[off + 1] > 1:
            _fail(BON_E_LENGTH, "BOOL payload is not 0 or 1")
        return total, count
    if t == BON_NUMBER:
        mode = data[off + 1]
        if mode == BON_MODE_VARINT:
            length = _rd_u64(data, off + 2)
            if length == 0:
                _fail(BON_E_LENGTH, "varint payload is empty")
            if _trim_2c(data, off + 10, length) != length:
                _fail(BON_E_MODE, "varint payload is not minimal")
            return total, count
        if mode not in _FIXED_MODE_SIZE:
            _fail(BON_E_MODE, f"reserved number mode 0x{mode:02X}")
        return total, count
    if t == BON_STRING or t == BON_BLOB:
        if total - 9 != _rd_u64(data, off + 1):
            _fail(BON_E_LENGTH, "length field does not match the value")
        return total, count
    if total - 17 != _rd_u64(data, off + 1):
        _fail(BON_E_LENGTH, "total_len does not match the container")
    return total, count


def _validate_tree(data: memoryview, off: int, avail: int) -> None:
    """Validate every TLV under ``off`` using one cursor per open container.

    An OBJECT is walked TLV by TLV like any other container, which is why its
    budget is twice the pair count: a key is validated and descended into exactly
    like a value.  The cursors live in a list, so depth costs heap, not stack, and
    a document nested a million levels deep validates fine.
    """
    stack: list[list[int]] = []  # [pos, end, tlvs_left]
    node_off = off
    room = avail
    from_cursor = -1

    while True:
        total, count = _validate_tlv(data, node_off, room)
        if from_cursor >= 0:
            owner = stack[from_cursor]
            owner[0] += total
            owner[2] -= 1
        t = data[node_off]
        if t == BON_ARRAY or t == BON_OBJECT:
            if t == BON_ARRAY:
                left = count
            else:
                left = _U64_MAX if count > _U64_MAX // 2 else count * 2
            stack.append([node_off + 17, node_off + total, left])

        # Pull the next value out of the innermost container that still owes one,
        # unwinding finished containers on the way.
        while True:
            if not stack:
                return
            cursor = stack[-1]
            if cursor[2] == 0:
                if cursor[0] != cursor[1]:
                    _fail(BON_E_LENGTH,
                          "container holds more bytes than its count accounts for")
                stack.pop()
                continue
            if cursor[0] >= cursor[1]:
                _fail(BON_E_LENGTH,
                      "container holds fewer values than its count says")
            node_off = cursor[0]
            room = cursor[1] - cursor[0]
            from_cursor = len(stack) - 1
            break


# ---------------------------------------------------------------------------
# Navigation helpers
# ---------------------------------------------------------------------------


def _iter_items(node: "Bon") -> Iterator[tuple["Bon", "Bon"]]:
    """Iterate an OBJECT's ``(key, value)`` node pairs in document order."""
    return _need(node)._iter_items()


def _iter_children(node: "Bon") -> Iterator["Bon"]:
    """Iterate an ARRAY's elements, or an OBJECT's values."""
    return _need(node)._iter_children()


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

_JSON_ESCAPES = {
    0x22: '\\"', 0x5C: "\\\\", 0x08: "\\b", 0x0C: "\\f",
    0x0A: "\\n", 0x0D: "\\r", 0x09: "\\t",
}
_PRINTABLE = frozenset(range(0x20, 0x7F)) - {0x22, 0x5C}


def _json_escape(data: memoryview, out: list[str]) -> None:
    """Append a quoted JSON string literal for a raw UTF-8 payload.

    Decoding first (rather than one ``chr(byte)`` per byte) is what keeps
    multi-byte sequences readable: ``"h\\xc3\\xa9llo"`` must print as ``h茅llo``,
    not as ``h脙漏llo``.  Invalid bytes land on lone surrogates, which
    :func:`_write_json` can still turn back into the original octets.
    """
    raw = bytes(data)
    if not raw:
        out.append('""')
        return
    if all(byte in _PRINTABLE for byte in raw):
        out.append('"' + raw.decode("ascii") + '"')
        return
    chunk: list[str] = []
    for ch in raw.decode("utf-8", "surrogateescape"):
        code = ord(ch)
        if code < 0x20 or code == 0x22 or code == 0x5C:
            escape = _JSON_ESCAPES.get(code)
            chunk.append(escape if escape is not None else "\\u%04X" % code)
        else:
            chunk.append(ch)
    out.append('"' + "".join(chunk) + '"')


def _json_float(value: float) -> str:
    """Render a FLOAT exactly like the reference implementation's ``%.17g``.

    Matching C's ``printf("%.17g")`` keeps the debug dump diffable against
    ``bon_write_json()``: ``3.0`` prints as ``3``, and non-finite values print as
    ``nan`` / ``inf`` rather than a JSON5 spelling that C never emits.
    """
    return "%.17g" % value


def _json_scalar(data: memoryview, off: int, size: int, t: int, out: list[str]) -> None:
    """Render one non-container node into ``out``."""
    if t == BON_NONE:
        out.append("null")
    elif t == BON_BOOL:
        out.append("true" if data[off + 1] else "false")
    elif t == BON_NUMBER:
        out.append(str(_number_int(data, off)))
    elif t == BON_FLOAT:
        out.append(_json_float(_F64.unpack_from(data, off + 1)[0]))
    elif t == BON_STRING:
        _json_escape(data[off + 9:off + size], out)
    elif t == BON_BLOB:
        out.append('"#bon:blob:%d:%s"'
                   % (size - 9, bytes(data[off + 9:off + size]).hex()))
    elif t == BON_DECIMAL:
        out.append('"#bon:decimal:%dE-%d"'
                   % (_u64_to_i64(_rd_u64(data, off + 1)), _rd_u64(data, off + 9)))
    else:
        _fail(BON_E_TYPE, f"unknown type 0x{t:02X}")


_JSON_KEY = 0
_JSON_VALUE = 1


def _to_json(node: "Bon") -> str:
    """One-way JSON debug view of a node.

    ``NONE``, ``BOOL``, ``NUMBER``, ``FLOAT``, ``STRING``, ``ARRAY`` and
    ``OBJECT`` map straight onto JSON.  Whatever JSON cannot hold losslessly gets
    a readable tag: DECIMAL becomes ``"#bon:decimal:<coeff>E-<scale>"``, a BLOB
    ``"#bon:blob:<size>:<hex>"``, and a non-STRING object key
    ``"#bon:<TYPE>:<value>"``.  FLOAT uses the reference implementation's
    ``%.17g`` spelling so _dumps stay diffable, which means ``3.0`` prints as
    ``3``.  This is a debugging aid rather than a reversible encoding, and its
    exact spelling is not guaranteed across versions.

    A STRING payload is decoded as UTF-8, so valid UTF-8 comes back as the text
    it was written from; bytes that are not valid UTF-8 become lone surrogates,
    which :func:`_write_json` writes back out unchanged.
    """
    _need(node)
    out: list[str] = []
    # frame = [node, window cursor, next index, child count, key/value state, quote]
    stack: list[list[Any]] = []
    pending: "Bon | None" = node
    pending_quote = False

    while True:
        if pending is not None:
            current = pending
            quoted = pending_quote
            pending = None
            t = current._type
            if t in _CONTAINER_SET:
                out.append("{" if t == BON_OBJECT else "[")
                stack.append([current, current._windows(), 0, current._count,
                              _JSON_KEY, quoted])
            else:
                _json_scalar(current._buf.data, current._off, current._size, t, out)
                if quoted:
                    out.append('"')

        if not stack:
            break
        frame = stack[-1]
        current = frame[0]
        index = frame[2]
        if index >= frame[3]:
            out.append("}" if current._type == BON_OBJECT else "]")
            if frame[5]:
                out.append('"')
            stack.pop()
            continue

        off, size, t, count = next(frame[1], (None, 0, 0, 0))
        if off is None:
            _fail(BON_E_LENGTH, "container holds fewer values than its count says")

        if current._type == BON_OBJECT:
            if frame[4] == _JSON_KEY:
                if index:
                    out.append(",")
                frame[4] = _JSON_VALUE
                if t == BON_STRING:
                    _json_escape(current._buf.data[off + 9:off + size], out)
                    continue
                out.append('"#bon:')
                out.append(_type_name(t))
                out.append(":")
                # The key may itself be a container, so its closing quote has to
                # wait for the whole subtree; the frame's flag arranges exactly
                # that, because the key's frames always sit above this one.
                pending = Bon(current._buf, off, size, t, count)
                pending_quote = True
                continue
            out.append(":")
            frame[4] = _JSON_KEY
            frame[2] = index + 1
            pending = Bon(current._buf, off, size, t, count)
            pending_quote = False
            continue

        if index:
            out.append(",")
        frame[2] = index + 1
        pending = Bon(current._buf, off, size, t, count)
        pending_quote = False

    return "".join(out)


def _write_text(text: str, out: Any) -> None:
    """Write ``text`` to a file path, or to a text stream / file object."""
    if out is None:
        out = sys.stdout
    if isinstance(out, (str, bytes, os.PathLike)):
        target = os.fspath(out)
        try:
            with open(target, "w", encoding="utf-8", errors="surrogateescape",
                      newline="\n") as handle:
                handle.write(text)
        except OSError as exc:
            _fail(BON_E_IO, f"cannot write {target!r}: {exc.strerror or exc}")
        return
    try:
        out.write(text)
    except (OSError, ValueError) as exc:
        _fail(BON_E_IO, f"write error: {exc}")


def _write_json(node: "Bon", out: Any = None) -> None:
    """Write :func:`_to_json` plus a trailing newline to a stream or a path."""
    _write_text(_to_json(node) + "\n", out)


def _to_hex(data: Any, size: int | None = None, width: int = 16) -> str:
    """Hex + ASCII dump of ``data``, in the layout of ``bon_write_hex``."""
    raw = bytes(_as_buffer(data, size, "buffer"))
    if not raw:
        return "(empty)\n"
    half = (width >> 1) - 1
    out: list[str] = []
    for off in range(0, len(raw), width):
        row = raw[off:off + width]
        cells = ["%08X  " % off]
        for i in range(width):
            cells.append("%02X " % row[i] if i < len(row) else "   ")
            if i == half:
                cells.append(" ")
        cells.append(" ")
        cells.append("".join(chr(b) if 0x20 <= b < 0x7F else "." for b in row))
        out.append("".join(cells) + "\n")
    return "".join(out)


def _write_hex(data: Any, out: Any = None, size: int | None = None,
               width: int = 16) -> None:
    """Write :func:`_to_hex` to a stream or a path."""
    _write_text(_to_hex(data, size, width), out)


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


class _Frame:
    """One open container in the writer: where its length field sits, how far it has got."""

    __slots__ = ("len_pos", "count", "is_object", "pending_key")

    def __init__(self, len_pos: int, is_object: bool) -> None:
        self.len_pos = len_pos
        self.count = 0
        self.is_object = is_object
        self.pending_key = False


class _Close:
    """Stack marker asking the writer to close the container just finished."""

    __slots__ = ("type_",)

    def __init__(self, type_: int) -> None:
        self.type_ = type_


_CLOSE_ARRAY = _Close(BON_ARRAY)
_CLOSE_OBJECT = _Close(BON_OBJECT)


class Writer:
    """Streaming document builder; the Python face of ``bon_writer``.

    ``Writer(root_type)`` emits the magic and the root container header straight
    away, so the first value written belongs to the root and
    :meth:`bonlib.w_array_begin` / :meth:`bonlib.w_object_begin` only nest deeper.  Container
    lengths and counts are back-patched when a container closes, so :attr:`bonlib.data`
    is only final after :meth:`bonlib.writer_finish`; take byte-exact output from the finished
    root.

    A failed write latches the writer into a failed state that every later call
    reports and that :meth:`bonlib.writer_finish` refuses to run.  Use the writer as a context
    manager, or call :meth:`bonlib.free`, so an abandoned writer releases its buffer::

        with bonlib.writer_new(bonlib.BON_OBJECT) as w:
            bonlib.w_string(w, "k")
            bonlib.w_int64(w, 1)
            root = bonlib.writer_finish(w)

    :meth:`bonlib.writer_finish` consumes the writer only when it succeeds, exactly like
    ``bon_writer_finish``.
    """

    __slots__ = ("_buf", "_stack", "_root_type", "_failed", "_error", "_consumed")

    def __init__(self, root_type: int) -> None:
        _clear_error()
        if root_type not in _CONTAINER_SET:
            _fail(BON_E_ROOT,
                  f"root type 0x{root_type:02X} must be ARRAY or OBJECT")
        self._buf = bytearray(MAGIC)
        self._buf.append(root_type)
        self._buf += bytes(16)  # placeholder: total_len + count
        self._stack = [_Frame(4, root_type == BON_OBJECT)]
        self._root_type = root_type
        self._failed = False
        self._error: tuple[int, str] | None = None
        self._consumed = False

    @property
    def _get_root_type(self) -> int:
        """``BON_ARRAY`` or ``BON_OBJECT`` -- the container this writer opened."""
        return self._root_type

    def __repr__(self) -> str:
        if self._consumed:
            state = "finished"
        elif self._failed:
            state = "failed"
        else:
            state = "open"
        return f"<bonlib.Writer {state} type={_type_name_of(self._root_type)} " \
               f"size={len(self._buf)} depth={len(self._stack)}>"

    # -- internals -------------------------------------------------------

    def _ok(self) -> "Writer":
        # Every entry point clears the slot first, exactly like the C's
        # clear_err(), so probing a latched writer always reports E_STATE
        # rather than replaying the error that latched it.
        _clear_error()
        if self._consumed:
            _fail(BON_E_STATE, "bon writer has already been finished")
        if self._failed:
            _fail(BON_E_STATE, "bon writer is in a failed state")
        return self

    def _abort(self, code: int, message: str) -> Any:
        """Latch a failure into the writer, then raise it."""
        self._failed = True
        self._error = (code, message)
        _fail(code, message)

    def _put(self, *values: int) -> None:
        self._buf += bytes(values)

    def _put_u64(self, value: int, width: int = 8) -> None:
        self._buf += value.to_bytes(width, "little")

    def _finish_value(self) -> None:
        """Account for one completed TLV in the innermost open container.

        For an OBJECT a key counts the pair and sets ``pending_key``; the value
        that has to follow clears it, so a half-written pair is caught at close
        instead of silently corrupting the count.
        """
        frame = self._stack[-1]
        if frame.is_object:
            if not frame.pending_key:
                frame.count += 1
                frame.pending_key = True
            else:
                frame.pending_key = False
        else:
            frame.count += 1

    def _write_int(self, value: int, force_varint: bool = False) -> None:
        """Encode ``value`` as NUMBER, smallest lossless mode unless forced to varint.

        The reference encoder keeps a negative number fixed-width only while the
        *magnitude* fits a uint64, so ``-2**64`` -- whose magnitude is one past the
        top -- takes the varint even though ``-uint64`` could hold its stored form.
        """
        negative = value < 0
        stored = -value - 1 if negative else value
        fits = stored < _U64_MAX if negative else stored <= _U64_MAX
        if not force_varint and fits:
            mode, width = _pick_mode(stored, negative)
            self._put(BON_NUMBER, mode)
            self._put_u64(stored, width)
        else:
            payload = _int_to_2c(value)
            self._put(BON_NUMBER, BON_MODE_VARINT)
            self._put_u64(len(payload))
            self._buf += payload
        self._finish_value()

    def _len_prefixed(self, type_: int, raw: bytes) -> None:
        self._put(type_)
        self._put_u64(len(raw))
        self._buf += raw
        self._finish_value()

    def _begin(self, type_: int) -> None:
        self._ok()
        pos = len(self._buf)
        self._put(type_)
        self._put_u64(0)
        self._put_u64(0)
        self._stack.append(_Frame(pos + 1, type_ == BON_OBJECT))

    def _end(self, type_: int) -> None:
        self._ok()
        if len(self._stack) < 2:
            self._abort(BON_E_STATE, f"unbalanced {_type_name(type_).lower()} end")
        frame = self._stack[-1]
        if (BON_OBJECT if frame.is_object else BON_ARRAY) != type_:
            self._abort(BON_E_STATE, "unbalanced container end")
        if frame.is_object and frame.pending_key:
            self._abort(BON_E_STATE, "object key without a value")
        self._stack.pop()
        self._patch(frame)
        self._finish_value()

    def _patch(self, frame: _Frame) -> None:
        """Back-fill a closed container's ``total_len`` and element count."""
        pos = frame.len_pos
        self._buf[pos:pos + 8] = (len(self._buf) - pos - 16).to_bytes(8, "little")
        self._buf[pos + 8:pos + 16] = frame.count.to_bytes(8, "little")

    # -- lifecycle -------------------------------------------------------

    @property
    def _get_data(self) -> memoryview:
        """Zero-copy view of the bytes written so far.

        A live view of the writer's own buffer: read it, do not write to it.
        Back-patched container lengths and counts are only correct after
        :meth:`bonlib.writer_finish`.
        """
        self._ok()
        return memoryview(self._buf)

    @property
    def _get_size(self) -> int:
        """Number of bytes written so far."""
        return 0 if self._consumed else len(self._buf)

    @property
    def _get_depth(self) -> int:
        """Number of currently open containers, the root included."""
        return 0 if self._consumed else len(self._stack)

    def _finish(self) -> "Bon":
        """Close the root, parse the result and return it.

        Consumes the writer **only when this succeeds**.  On failure the writer
        stays alive but poisoned -- it can neither be finished nor written to
        again, and :meth:`bonlib.free` (or the context manager) releases it.
        """
        if self._consumed:
            _fail(BON_E_STATE, "bon writer has already been finished")
        if self._failed:
            code, message = self._error or (BON_E_STATE, "bon writer is in a failed state")
            self._poison()
            _fail(code, message)
        if len(self._stack) != 1:
            left = len(self._stack) - 1
            self._poison()
            _fail(BON_E_STATE, f"{left} container(s) left open")
        frame = self._stack[0]
        if frame.is_object and frame.pending_key:
            self._poison()
            _fail(BON_E_STATE, "root object key without a value")
        self._patch(frame)
        data = bytes(self._buf)
        try:
            node = _from_stream(data)
        except BonError:
            self._poison()  # _from_stream already latched the real reason
            raise
        self._consumed = True
        self._buf = bytearray()
        self._stack = []
        return node

    def _poison(self) -> None:
        """Drop everything a writer that can no longer succeed was holding."""
        self._failed = True
        self._stack = []
        self._buf = bytearray()

    def _free(self) -> None:
        """Release the writer's buffers.  Always safe, and idempotent."""
        if not self._consumed:
            self._poison()

    def _close(self) -> None:
        """Alias of :meth:`bonlib.free`, for ``with`` blocks."""
        self._free()

    def __enter__(self) -> "Writer":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        self._free()
        return False

    # -- scalar writers --------------------------------------------------

    def _w_none(self) -> "Writer":
        """Write NONE."""
        self._ok()
        self._put(BON_NONE)
        self._finish_value()
        return self

    def _w_bool(self, value: Any) -> "Writer":
        """Write BOOL from any truthy or falsey value."""
        self._ok()
        self._put(BON_BOOL, 1 if value else 0)
        self._finish_value()
        return self

    def _w_int(self, value: int) -> "Writer":
        """Write NUMBER at **any** precision, choosing the smallest lossless mode.

        This is the Python shortcut that stands in for the C library's
        ``bon_w_int_bytes()`` / ``bon_w_int_dec()`` pair: ``2**70`` and
        ``-2**70 - 1`` land in the VARINT mode, everything narrower gets a fixed
        mode.  One call, exact for every Python ``int``.
        """
        self._ok()
        self._write_int(_require_int(value, "w_int"))
        return self

    def _w_int64(self, value: int) -> "Writer":
        """Write NUMBER from a signed 64-bit value (``BON_E_RANGE`` outside it)."""
        self._ok()
        _require_int(value, "w_int64")
        if not _I64_MIN <= value <= _I64_MAX:
            _fail(BON_E_RANGE, f"value {value} does not fit in int64")
        self._write_int(value)
        return self

    def _w_uint64(self, value: int) -> "Writer":
        """Write NUMBER from an unsigned 64-bit value (``BON_E_RANGE`` outside it)."""
        self._ok()
        _require_int(value, "w_uint64")
        if not 0 <= value <= _U64_MAX:
            _fail(BON_E_RANGE, f"value {value} does not fit in uint64")
        self._write_int(value)
        return self

    def _w_double(self, value: float) -> "Writer":
        """Write FLOAT as IEEE 754 ``binary64``."""
        self._ok()
        if not isinstance(value, (int, float)):
            raise TypeError(f"expected float for w_double, not {type(value).__name__}")
        self._put(BON_FLOAT)
        self._buf += _F64.pack(float(value))
        self._finish_value()
        return self

    def _w_decimal(self, coefficient: int, scale: int) -> "Writer":
        """Write DECIMAL, whose value is ``coefficient * 10 ** -scale``."""
        self._ok()
        _require_int(coefficient, "w_decimal coefficient")
        _require_int(scale, "w_decimal scale")
        if not _I64_MIN <= coefficient <= _I64_MAX:
            _fail(BON_E_RANGE, f"coefficient {coefficient} does not fit in int64")
        if not 0 <= scale <= _U64_MAX:
            _fail(BON_E_RANGE, f"scale {scale} does not fit in uint64")
        self._put(BON_DECIMAL)
        self._buf += _U64.pack(coefficient & _U64_MAX)
        self._buf += _U64.pack(scale)
        self._finish_value()
        return self

    def _w_string(self, value: Any, size: int | None = None) -> "Writer":
        """Write STRING from a ``str`` (encoded UTF-8) or raw bytes.

        ``size`` truncates the encoded bytes, which is how a STRING holding an
        embedded NUL -- or a deliberately split multi-byte sequence -- is written.
        """
        self._ok()
        if isinstance(value, str):
            raw = value.encode("utf-8")
            if size is not None:
                if size < 0:
                    _fail(BON_E_USAGE, f"negative size {size}")
                raw = raw[:size]
        elif isinstance(value, (bytes, bytearray, memoryview)):
            raw = bytes(_as_buffer(value, size, "string payload"))
        else:
            raise TypeError(f"string payload must be str or bytes, not {type(value).__name__}")
        self._len_prefixed(BON_STRING, raw)
        return self

    def _w_blob(self, data: Any, size: int | None = None) -> "Writer":
        """Write BLOB from any bytes-like object; ``size`` truncates it first."""
        self._ok()
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError(f"blob payload must be bytes-like, not {type(data).__name__}")
        self._len_prefixed(BON_BLOB, bytes(_as_buffer(data, size, "blob payload")))
        return self

    def _w_int_bytes(self, data: Any, size: int | None = None) -> "Writer":
        """Write NUMBER from minimal little-endian two's-complement bytes (or an ``int``).

        Any width is accepted: the payload is trimmed to its shortest form and the
        smallest lossless wire mode is chosen, so ``b"\\xff\\xff"`` and ``b"\\xff"``
        both mean ``-1``.
        """
        self._ok()
        return self._w_int_payload(data, size, force_varint=False)

    def _w_int_bytes_varint(self, data: Any, size: int | None = None) -> "Writer":
        """Like :meth:`bonlib.w_int_bytes` but forced into the VARINT mode.

        Only worth it when a peer needs the fixed-width semantics to be absent;
        the automatic mode is always smaller otherwise.
        """
        self._ok()
        return self._w_int_payload(data, size, force_varint=True)

    def _w_int_payload(self, data: Any, size: int | None, force_varint: bool) -> "Writer":
        if isinstance(data, int) and not isinstance(data, bool):
            self._write_int(data, force_varint)
            return self
        if data is None:
            raise TypeError("integer payload must be int or bytes-like, not None")
        raw = _as_buffer(data, size, "integer payload")
        if raw.nbytes == 0:
            self._write_int(0, force_varint)
            return self
        value = int.from_bytes(raw[:_trim_2c(raw, 0, raw.nbytes)], "little", signed=True)
        self._write_int(value, force_varint)
        return self

    def _w_int_dec(self, digits: Any, size: int | None = None) -> "Writer":
        """Write NUMBER from a decimal text such as ``b"-12345"`` or ``"12345"``.

        The C library's own spelling of arbitrary precision; Python callers
        normally just use :meth:`bonlib.w_int64`.  Anything that is not an optional ``-``
        followed by decimal digits is rejected with ``BON_E_USAGE``.
        """
        self._ok()
        if isinstance(digits, (bytes, bytearray, memoryview)):
            text = bytes(_as_buffer(digits, size, "decimal text")).decode("ascii", "replace")
        elif isinstance(digits, str):
            text = digits
        else:
            raise TypeError(f"decimal text must be str or bytes, not {type(digits).__name__}")
        body = text[1:] if text.startswith("-") else text
        if not body or not all("0" <= char <= "9" for char in body):
            # Matches the reference: a rejected decimal poisons the writer, so
            # every later call fails with BON_E_STATE until it is freed.
            self._abort(BON_E_USAGE, f"{text[:24]!r} is not a decimal integer")
        self._write_int(int(text))
        return self

    # -- containers ------------------------------------------------------

    def _w_array_begin(self) -> "Writer":
        """Open a nested ARRAY."""
        self._begin(BON_ARRAY)
        return self

    def _w_array_end(self) -> "Writer":
        """Close the innermost ARRAY, back-patching its length and count."""
        self._ok()
        self._end(BON_ARRAY)
        return self

    def _w_object_begin(self) -> "Writer":
        """Open a nested OBJECT."""
        self._begin(BON_OBJECT)
        return self

    def _w_object_end(self) -> "Writer":
        """Close the innermost OBJECT, back-patching its length and count."""
        self._ok()
        self._end(BON_OBJECT)
        return self


# ---------------------------------------------------------------------------
# High level: plain Python values <-> BON
# ---------------------------------------------------------------------------


class _PairSink:
    """Collects one TLV at a time into ``(key, value)`` pairs of an OBJECT."""

    __slots__ = ("target", "half", "key")

    def __init__(self, target: "_ObjectPairs") -> None:
        self.target = target
        self.half = 0
        self.key: Any = None

    def __call__(self, value: Any) -> None:
        if self.half == 0:
            self.key = value
            self.half = 1
        else:
            # The pair is emitted as a tuple only once both halves are known, so
            # a half-written pair can never be observed holding a None.
            self.target.append((self.key, value))
            self.half = 0
            self.key = None


def _to_python(node: "Bon", lenient: bool = False) -> Any:
    """Convert a node into plain Python data, losslessly and without recursion.

    ``NONE -> None``, ``BOOL -> bool``, ``NUMBER -> int``, ``FLOAT -> float``,
    ``STRING -> str``, ``BLOB -> bytes``, ``DECIMAL -> decimal.Decimal``,
    ``ARRAY -> list`` and ``OBJECT -> _ObjectPairs`` (a list of ``(key, value)``
    pairs -- BON objects may repeat keys and may key on any type, so a ``dict``
    would lose information).  :func:`_dumps` accepts every one of these, so a
    document survives the round trip byte for byte.

    ``lenient`` is what :meth:`_Kv.set` decodes with: a STRING that is not valid
    UTF-8 raises here, but comes back as a :class:`bontype` holding its raw bytes
    so the document can still be re-encoded.  Everything else is identical.
    """
    _need(node)
    if node._type not in _CONTAINER_SET:
        return _read_scalar(node._buf.data, node._off, node._size, node._type,
                            lenient)

    buf = node._buf
    data = buf.data
    result: Any = _ObjectPairs() if node._type == BON_OBJECT else []
    # frame = [node, window cursor, next TLV index, TLV count, where values go].
    # The bound is a *TLV* count, not the node's own count: an OBJECT's count is
    # the number of pairs, so it must be doubled before it can drive a cursor
    # that walks key/value TLVs one at a time.
    stack: list[list[Any]] = [[node, node._windows(), 0, node._get_tlv_count,
                               _PairSink(result) if node._type == BON_OBJECT
                               else result.append]]

    while stack:
        frame = stack[-1]
        index = frame[2]
        if index >= frame[3]:
            stack.pop()
            continue
        frame[2] = index + 1
        head = next(frame[1], None)
        if head is None:
            _fail(BON_E_LENGTH, "container holds fewer values than its count says")
        off, size, t, count = head
        if t not in _CONTAINER_SET:
            frame[4](_read_scalar(data, off, size, t, lenient))
            continue
        child = Bon(buf, off, size, t, count)
        # Place the (still empty) container first, then fill it from its own
        # frame -- that keeps the parent's value sequence in document order.
        target: Any = _ObjectPairs() if t == BON_OBJECT else []
        frame[4](target)
        stack.append([child, child._windows(), 0, child._get_tlv_count,
                      _PairSink(target) if t == BON_OBJECT else target.append])

    return result


def _is_object_like(value: Any) -> bool:
    return isinstance(value, (_ObjectPairs, Mapping))


def _container_kind(value: Any) -> int | None:
    """``BON_OBJECT``/``BON_ARRAY`` for a Python container, ``None`` for a scalar.

    ``_DecimalValue`` is a ``NamedTuple`` yet is a scalar, so the tuple test has to
    come second -- otherwise ``Decimal("1.5")`` silently encodes as a 2-element
    array of ``[coefficient, scale]``.
    """
    if _is_object_like(value):
        return BON_OBJECT
    if isinstance(value, (list, tuple)) and not isinstance(value, _DecimalValue):
        return BON_ARRAY
    return None


def _children_of(value: Any) -> list[Any]:
    """Flatten a Python container into the TLV sequence the writer expects."""
    if isinstance(value, _ObjectPairs):
        pairs = list(value)
    elif isinstance(value, Mapping):
        pairs = list(value.items())
    else:
        return list(value)
    flat: list[Any] = []
    for pair in pairs:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            _fail(BON_E_USAGE, f"object entry {pair!r} is not a (key, value) pair")
        flat.append(pair[0])
        flat.append(pair[1])
    return flat


def _write_scalar(w: "Writer", value: Any) -> None:
    """Write one non-container Python value."""
    if value is None:
        w._w_none()
    elif isinstance(value, bool):
        w._w_bool(value)
    elif isinstance(value, int):
        w._w_int(value)
    elif isinstance(value, float):
        w._w_double(value)
    elif isinstance(value, str):
        w._w_string(value)
    elif isinstance(value, (bytes, bytearray, memoryview)):
        w._w_blob(value)
    elif isinstance(value, _DecimalValue):
        w._w_decimal(value.coefficient, value.scale)
    elif isinstance(value, Decimal):
        sign, digits, exp = value.as_tuple()
        coefficient = int("".join(map(str, digits)) or "0")
        if sign:
            coefficient = -coefficient
        if exp > 0:  # fold a positive exponent into the coefficient
            coefficient *= 10 ** exp
            exp = 0
        w._w_decimal(coefficient, -exp)
    else:
        _fail(BON_E_USAGE, f"cannot encode {type(value).__name__} as BON")


def _from_python(value: Any, w: "Writer") -> None:
    """Fill an already-open :class:`bonlib.Writer` root with a plain Python value.

    The root container is *already open* when this is called -- :func:`_dumps`
    constructs ``Writer(BON_OBJECT)`` or ``Writer(BON_ARRAY)`` up front -- so this
    function writes the children of ``value`` and neither opens nor closes the
    root.  Opening it again here would bury the document one level deeper than
    every other BON writer in the world writes it.

    Accepts ``None``, ``bool``, ``int``, ``float``, ``str``, ``bytes``-like,
    :class:`Decimal`, :class:`_DecimalValue`, ``list``/``tuple`` and ``dict`` /
    :class:`_ObjectPairs`.  Nested containers are driven by an explicit heap stack
    with a close marker per level, so nesting depth is unbounded and the write
    order is exactly document order.
    """
    kind = _container_kind(value)
    if kind is None:
        _fail(BON_E_USAGE,
              f"_from_python() root must be a list, tuple, dict or _ObjectPairs, "
              f"got {type(value).__name__}")
    if kind != w._get_root_type:
        _fail(BON_E_USAGE,
              f"{_type_name_of(w._get_root_type)} writer cannot hold a "
              f"{_type_name_of(kind)} root")
    # Stack order is document order: pop() takes the next child, and each nested
    # container pushes its close marker below its own children.
    stack: list[Any] = list(reversed(_children_of(value)))
    while stack:
        item = stack.pop()
        if item is _CLOSE_ARRAY:
            w._w_array_end()
            continue
        elif item is _CLOSE_OBJECT:
            w._w_object_end()
            continue
        item_kind = _container_kind(item)
        if item_kind is None:
            _write_scalar(w, item)
            continue
        if item_kind == BON_OBJECT:
            w._w_object_begin()
            stack.append(_CLOSE_OBJECT)
        else:
            w._w_array_begin()
            stack.append(_CLOSE_ARRAY)
        stack.extend(reversed(_children_of(item)))


def _dumps(obj: Any) -> bytes:
    """Encode plain Python data as a complete BON document.

    The root must be an ARRAY or an OBJECT, because the format forbids any other
    root type -- wrap a lone scalar in a one-element list.  ``list``/``tuple``
    become ARRAYs, ``dict``/``_ObjectPairs`` become OBJECTs.
    """
    if not (isinstance(obj, (list, tuple)) or _is_object_like(obj)):
        raise TypeError(
            "the BON root must be a list, tuple, dict or _ObjectPairs, got "
            f"{type(obj).__name__}; wrap a scalar in a list"
        )
    with Writer(BON_OBJECT if _is_object_like(obj) else BON_ARRAY) as w:
        _from_python(obj, w)
        return w._finish()._get_stream_bytes


def _dumps_file(path: Any, obj: Any) -> None:
    """Encode plain Python data with :func:`_dumps` and write it to ``path``."""
    data = _dumps(obj)
    target = os.fspath(path)
    try:
        with open(target, "wb") as handle:
            handle.write(data)
    except OSError as exc:
        _fail(BON_E_IO, f"cannot write {target!r}: {exc.strerror or exc}")


def _loads(data: Any, size: int | None = None) -> Any:
    """Decode a complete BON document into plain Python data (:func:`_to_python`)."""
    return _to_python(_from_stream(data, size))


def _loads_file(path: Any) -> Any:
    """Read a BON file and decode it into plain Python data."""
    return _to_python(_from_file(path))


# ---------------------------------------------------------------------------
# The public surface: bonlib
# ---------------------------------------------------------------------------


def _needle(key: Any, key_size: int | None) -> memoryview:
    """Normalise a C ``(key, key_size)`` pair to a byte view."""
    if key_size is None:
        return _key_bytes(key)
    return _key_bytes(bytes(_as_buffer(key, key_size, "key")))


class bonlib:
    """The whole C library, one function per ``bon_*`` entry point.

    This class is the *only* supported entry point of the module.  Every name
    below is a port of a function declared in ``clang/bon.h``, with the ``bon_``
    prefix dropped, so C code translates by search and replace::

        bon_get_int_bytes(n, &bytes, &size, &neg)   ->   bonlib.get_int_bytes(n)
        bon_w_int64(w, v)                            ->   bonlib.w_int64(w, v)
        bon_iter_next(&it)                           ->   bonlib.iter_next(it)

    Three deliberate differences from C, all of them Python's fault rather than
    the port's:

    * **Errors raise.**  Every ``bon_*`` function that latches an error and
      returns ``-1``/``NULL`` raises :class:`bonlib.BonError` here, carrying the same
      ``code`` and ``message`` the C formats into its thread-local slot.  The
      slot is still readable through :meth:`bonlib.last_error_code` / :meth:`bonlib.last_error`.
    * **Out-parameters become return values.**  ``bon_get_int_bytes(n, &bytes,
      &size, &neg)`` is :meth:`bonlib.get_int_bytes`, returning ``(bytes, negative)``.
    * **Lengths are implicit.**  A ``bytes``/``str``/``memoryview`` knows its own
      length, so the ``size`` argument of the C is optional everywhere.

    Nothing here is instantiated: the methods are static, so ``bonlib.w_int64(w,
    v)`` and ``lib = bonlib(); lib.w_int64(w, v)`` are the same call.  The Python
    object mapping lives in the separate :class:`bonpy` class.
    """

    # -- format constants, straight from bon.h ---------------------------

    MAGIC = MAGIC
    MAGIC_SIZE = MAGIC_SIZE
    VERSION = VERSION
    VERSION_MAJOR = VERSION_MAJOR
    VERSION_MINOR = VERSION_MINOR
    VERSION_PATCH = VERSION_PATCH

    BON_NONE = BON_NONE
    BON_BOOL = BON_BOOL
    BON_NUMBER = BON_NUMBER
    BON_FLOAT = BON_FLOAT
    BON_STRING = BON_STRING
    BON_ARRAY = BON_ARRAY
    BON_OBJECT = BON_OBJECT
    BON_BLOB = BON_BLOB
    BON_DECIMAL = BON_DECIMAL

    BON_MODE_UINT8 = BON_MODE_UINT8
    BON_MODE_UINT16 = BON_MODE_UINT16
    BON_MODE_UINT32 = BON_MODE_UINT32
    BON_MODE_UINT64 = BON_MODE_UINT64
    BON_MODE_VARINT = BON_MODE_VARINT
    BON_MODE_NUINT8 = BON_MODE_NUINT8
    BON_MODE_NUINT16 = BON_MODE_NUINT16
    BON_MODE_NUINT32 = BON_MODE_NUINT32
    BON_MODE_NUINT64 = BON_MODE_NUINT64

    BON_E_OK = BON_E_OK
    BON_E_INVALID = BON_E_INVALID
    BON_E_MAGIC = BON_E_MAGIC
    BON_E_TRUNCATED = BON_E_TRUNCATED
    BON_E_TYPE = BON_E_TYPE
    BON_E_MODE = BON_E_MODE
    BON_E_LENGTH = BON_E_LENGTH
    BON_E_ROOT = BON_E_ROOT
    BON_E_RANGE = BON_E_RANGE
    BON_E_NOKEY = BON_E_NOKEY
    BON_E_DEPTH = BON_E_DEPTH
    BON_E_STATE = BON_E_STATE
    BON_E_IO = BON_E_IO
    BON_E_MEMORY = BON_E_MEMORY
    BON_E_USAGE = BON_E_USAGE

    #: The handle types of the C API, for ``isinstance`` checks and annotations.
    Bon = Bon
    Iter = Iter
    Writer = Writer
    BonError = BonError

    # -- bon_version / bon_last_error* / bon_error_message ----------------

    @staticmethod
    def version() -> str:
        """``bon_version``: the library version string, e.g. ``"1.0.0"``."""
        return VERSION

    @staticmethod
    def last_error_code() -> int:
        """``bon_last_error_code``: code of the last failure on this thread."""
        return _last_error_code()

    @staticmethod
    def last_error() -> tuple[int, str] | None:
        """``bon_last_error``: ``(code, message)`` of the last failure, else ``None``."""
        return _last_error()

    @staticmethod
    def clear_error() -> None:
        """``bon_clear_error``: reset the thread-local error slot."""
        _clear_error_pub()

    @staticmethod
    def error_message(code: int) -> str:
        """``bon_error_message``: the short description of a ``BON_E_*`` code."""
        return _error_message(code)

    # -- bon_from_stream / bon_from_file / bon_save_file ------------------

    @staticmethod
    def from_stream(data: Any, size: int | None = None) -> Bon:
        """``bon_from_stream``: copy a byte buffer and parse its root TLV.

        Only the root TLV is parsed; nested structure is checked by
        :meth:`bonlib.validate`.  The returned node owns a private copy, so the caller
        may reuse or free ``data`` immediately.
        """
        return _from_stream(data, size)

    @staticmethod
    def from_stream_ref(data: Any, size: int | None = None) -> Bon:
        """``bon_from_stream_ref``: parse without copying -- the caller keeps ``data`` alive.

        Cheaper than :meth:`bonlib.from_stream` and the reason a child node can outlive
        its parent: every node is a window into this one buffer.
        """
        return _from_stream_ref(data, size)

    @staticmethod
    def from_file(path: Any) -> Bon:
        """``bon_from_file``: read a file and parse its root TLV."""
        return _from_file(path)

    @staticmethod
    def save_file(node: Bon, path: Any) -> None:
        """``bon_save_file``: write the whole stream of a root node to ``path``."""
        _save_file(node, path)

    @staticmethod
    def new_array() -> Bon:
        """``bon_new_array``: an empty ARRAY document."""
        return _new_array()

    @staticmethod
    def new_object() -> Bon:
        """``bon_new_object``: an empty OBJECT document."""
        return _new_object()

    # -- bon_ref / bon_unref / bon_free ----------------------------------

    @staticmethod
    def ref(node: Bon) -> Bon:
        """``bon_ref``: hand back the same node.

        A no-op by construction -- the Python binding is reference counted by the
        interpreter, so holding the returned name is the whole protocol.
        """
        _need(node)
        return node

    @staticmethod
    def unref(node: Bon) -> None:
        """``bon_unref``: drop one reference.  Nothing to do in Python; see :meth:`bonlib.free`."""
        _need(node)

    @staticmethod
    def free(node: Bon) -> None:
        """``bon_free``: destroy a node.

        Also a no-op: the node is freed once Python drops its last reference,
        which is exactly the guarantee ``bon_free`` gives when called on the last
        owner.  Kept so ported C code reads the same.
        """
        _need(node)

    # -- bon_root / bon_is_root / bon_data / bon_size / bon_stream / bon_magic

    @staticmethod
    def root(node: Bon) -> Bon:
        """``bon_root``: the document root, or ``node`` itself when it already is it."""
        return node._get_root

    @staticmethod
    def is_root(node: Bon) -> bool:
        """``bon_is_root``: whether ``node`` is the document root."""
        _need(node)
        return node._get_is_root

    @staticmethod
    def data(node: Bon) -> memoryview:
        """``bon_data``: zero-copy view of this TLV, magic **not** included.

        A file is exactly ``MAGIC_SIZE + size(root)`` bytes.
        """
        return node._get_data

    @staticmethod
    def size(node: Bon) -> int:
        """``bon_size``: byte length of this node's own TLV, magic excluded."""
        _need(node)
        return node._get_size

    @staticmethod
    def stream(node: Bon) -> memoryview:
        """``bon_stream``: the whole file, magic included.  Root nodes only."""
        return node._get_stream

    @staticmethod
    def magic(node: Bon | None = None) -> bytes:
        """``bon_magic``: the three magic bytes, ``62 6F 6E``."""
        return MAGIC

    # -- bon_type / bon_type_name / bon_valid / bon_count / bon_validate --

    @staticmethod
    def type(node: Bon) -> int:
        """``bon_type``: the node's ``BON_*`` type code."""
        _need(node)
        return node._get_type

    @staticmethod
    def type_name(node: Bon | int | None) -> str:
        """``bon_type_name``: ``"STRING"``, ``"ARRAY"``, ... (``"null"`` for ``None``).

        A bare type code is accepted too, which saves a lookup when you already
        have the code and only want the label.
        """
        if node is None:
            return "null"
        if isinstance(node, Bon):
            return _type_name_of(node)
        return _type_name_of(node)

    @staticmethod
    def valid(node: Bon | None) -> bool:
        """``bon_valid``: whether this handle denotes a well-formed TLV.

        Every node this library hands out was produced by parsing a TLV header, so
        the answer is ``True``; the flag exists because ``bon_valid`` is part of
        the C surface and a port must be able to ask.
        """
        return isinstance(node, Bon)

    @staticmethod
    def count(node: Bon) -> int:
        """``bon_count``: element/pair count of a container, ``0`` for other types."""
        return node._get_count

    @staticmethod
    def validate(node: Bon) -> Bon:
        """``bon_validate``: strictly check the whole subtree, raising on any problem."""
        return _validate(node)

    # -- navigation -------------------------------------------------------

    @staticmethod
    def at(node: Bon, index: int) -> Bon:
        """``bon_at``: the ``index``-th stored TLV (keys and values both)."""
        return node._at(index)

    @staticmethod
    def key_at(node: Bon, index: int) -> Bon:
        """``bon_key_at``: the ``index``-th key node of an OBJECT."""
        return node._key_at(index)

    @staticmethod
    def value_at(node: Bon, index: int) -> Bon:
        """``bon_value_at``: the ``index``-th value node of an OBJECT."""
        return node._value_at(index)

    @staticmethod
    def find(node: Bon, key: Any, key_size: int | None = None) -> Bon | None:
        """``bon_find``: first value whose key equals ``key``, else ``None``.

        ``key`` may be ``str`` (encoded as UTF-8) or bytes; ``key_size`` truncates
        a larger buffer, as the C's ``size_t`` argument would.
        """
        return node._find(_needle(key, key_size))

    @staticmethod
    def key_count(node: Bon, key: Any = None, key_size: int | None = None) -> int:
        """``bon_key_count``: how often a key occurs, or the pair count when ``key`` is ``None``."""
        if key is None:
            return node._get_count
        return node._key_count(_needle(key, key_size))

    @staticmethod
    def key_is(node: Bon, index: int, key: Any, key_size: int | None = None) -> bool:
        """``bon_key_is``: whether the ``index``-th key equals ``key``."""
        return node._key_is(index, _needle(key, key_size))

    @staticmethod
    def key_cstr(node: Bon, index: int) -> bytes:
        """``bon_key_cstr``: the ``index``-th key as raw bytes (a C string in C)."""
        return node._key_bytes(index)

    # -- scalar getters ---------------------------------------------------

    @staticmethod
    def get_bool(node: Bon) -> bool:
        """``bon_get_bool``: the BOOL payload."""
        return node._get_bool()

    @staticmethod
    def get_int64(node: Bon) -> int:
        """``bon_get_int64``: the value as a signed 64-bit int (``BON_E_RANGE`` if it does not fit)."""
        return node._get_int64()

    @staticmethod
    def get_uint64(node: Bon) -> int:
        """``bon_get_uint64``: the value as an unsigned 64-bit int (``BON_E_RANGE`` if negative or too large)."""
        return node._get_uint64()

    @staticmethod
    def get_int_str(node: Bon) -> str:
        """``bon_get_int_str``: the value in decimal, for any magnitude.

        This is how a NUMBER wider than 64 bits is read: the C has no other way,
        and neither does this port, so ``int(bonlib.get_int_str(n))`` is the
        spelling for "give me the exact Python int".
        """
        return node._get_int_str()

    @staticmethod
    def get_int_bytes(node: Bon) -> tuple[bytes, bool]:
        """``bon_get_int_bytes``: ``(payload, negative)`` in the reference's own width rules.

        VARINT hands back the wire payload untouched; a ``-uint*`` mode the
        shortest two's-complement form of the value; a positive fixed mode the
        **mode's full width**, plus a ``0x00`` when the top bit would otherwise
        read as a sign -- so ``65536`` stored as INT32 is four bytes
        ``00 00 01 00``, not the three a minimal encoder would pick.
        """
        return node._get_int_bytes()

    @staticmethod
    def get_number_mode(node: Bon) -> int:
        """``bon_get_number_mode``: the ``BON_MODE_*`` code this NUMBER was stored with."""
        return node._get_number_mode()

    @staticmethod
    def get_double(node: Bon) -> float:
        """``bon_get_double``: the FLOAT payload as an IEEE-754 double."""
        return node._get_double()

    @staticmethod
    def get_decimal(node: Bon) -> tuple[int, int]:
        """``bon_get_decimal``: ``(coefficient, scale)``; the value is ``coefficient * 10 ** -scale``."""
        value = node._get_decimal()
        return value.coefficient, value.scale

    @staticmethod
    def get_bytes(node: Bon) -> memoryview:
        """``bon_get_bytes``: zero-copy view of a BLOB payload, length prefix excluded."""
        return node._get_bytes()

    @staticmethod
    def get_string(node: Bon, errors: str = "strict") -> str:
        """``bon_get_string``: a STRING payload decoded as UTF-8 text."""
        return node._get_string(errors)

    @staticmethod
    def get_cstr(node: Bon) -> bytes:
        """``bon_get_cstr``: a STRING payload as raw bytes, the way a C caller would read it."""
        return bytes(node._get_payload())

    # -- writer lifecycle -------------------------------------------------

    @staticmethod
    def writer_new(root_type: int) -> Writer:
        """``bon_writer_new``: start a document whose root is ARRAY or OBJECT."""
        return Writer(root_type)

    @staticmethod
    def writer_data(writer: Writer) -> memoryview:
        """``bon_writer_data``: view of the bytes written so far, header fields included.

        Container lengths and counts are only final after :meth:`bonlib.writer_finish`,
        which is also when the finished document becomes readable.
        """
        return writer._get_data

    @staticmethod
    def writer_size(writer: Writer) -> int:
        """``bon_writer_size``: how many bytes have been written so far."""
        if writer._consumed:
            _fail(BON_E_STATE, "bon writer has already been finished")
        return writer._get_size

    @staticmethod
    def writer_depth(writer: Writer) -> int:
        """``bon_writer_depth``: number of open containers, the root included."""
        return writer._get_depth

    @staticmethod
    def writer_finish(writer: Writer) -> Bon:
        """``bon_writer_finish``: close the root and parse the result.

        Consumes the writer, exactly as the C does -- the handle is dead
        afterwards, and :meth:`bonlib.writer_free` releases its buffer.
        """
        return writer._finish()

    @staticmethod
    def writer_free(writer: Writer) -> None:
        """``bon_writer_free``: release a writer, finished or not."""
        writer._free()

    # -- writer values ----------------------------------------------------

    @staticmethod
    def w_none(writer: Writer) -> Writer:
        """``bon_w_none``: write a NONE value."""
        writer._w_none()
        return writer

    @staticmethod
    def w_bool(writer: Writer, value: Any) -> Writer:
        """``bon_w_bool``: write a BOOL value."""
        writer._w_bool(value)
        return writer

    @staticmethod
    def w_int64(writer: Writer, value: Any) -> Writer:
        """``bon_w_int64``: write a NUMBER from a signed 64-bit int, smallest mode."""
        writer._w_int64(value)
        return writer

    @staticmethod
    def w_uint64(writer: Writer, value: Any) -> Writer:
        """``bon_w_uint64``: write a NUMBER from an unsigned 64-bit int, smallest mode."""
        writer._w_uint64(value)
        return writer

    @staticmethod
    def w_double(writer: Writer, value: Any) -> Writer:
        """``bon_w_double``: write a FLOAT from an IEEE-754 double."""
        writer._w_double(value)
        return writer

    @staticmethod
    def w_decimal(writer: Writer, coefficient: Any, scale: Any) -> Writer:
        """``bon_w_decimal``: write a DECIMAL as ``coefficient * 10 ** -scale``."""
        writer._w_decimal(coefficient, scale)
        return writer

    @staticmethod
    def w_string(writer: Writer, utf8: Any, size: int | None = None) -> Writer:
        """``bon_w_string``: write a STRING from UTF-8 bytes or text."""
        writer._w_string(utf8, size)
        return writer

    @staticmethod
    def w_blob(writer: Writer, data: Any, size: int | None = None) -> Writer:
        """``bon_w_blob``: write a BLOB from bytes."""
        writer._w_blob(data, size)
        return writer

    @staticmethod
    def w_int_bytes(writer: Writer, little_endian_2c: Any, size: int | None = None) -> Writer:
        """``bon_w_int_bytes``: write a NUMBER from little-endian two's-complement bytes.

        The payload is trimmed to its shortest form and the smallest lossless
        mode is chosen, so ``b"\\xff\\xff"`` and ``b"\\xff"`` both mean ``-1``.
        """
        writer._w_int_bytes(little_endian_2c, size)
        return writer

    @staticmethod
    def w_int_bytes_varint(writer: Writer, little_endian_2c: Any, size: int | None = None) -> Writer:
        """``bon_w_int_bytes_varint``: like :meth:`bonlib.w_int_bytes` but forced into the VARINT mode."""
        writer._w_int_bytes_varint(little_endian_2c, size)
        return writer

    @staticmethod
    def w_int_dec(writer: Writer, digits: Any, size: int | None = None) -> Writer:
        """``bon_w_int_dec``: write a NUMBER from decimal text, e.g. ``"-12345678901234567890"``.

        The C's own spelling of an unbounded integer, and the one to use for
        magnitudes past ``2**64-1``: 32 bytes of digits cost more than the mode
        table saves, but they are the only C-shaped way to write one.
        """
        writer._w_int_dec(digits, size)
        return writer

    # -- writer containers ------------------------------------------------

    @staticmethod
    def w_array_begin(writer: Writer) -> Writer:
        """``bon_w_array_begin``: open an ARRAY inside the writer."""
        writer._w_array_begin()
        return writer

    @staticmethod
    def w_array_end(writer: Writer) -> Writer:
        """``bon_w_array_end``: close the innermost ARRAY."""
        writer._w_array_end()
        return writer

    @staticmethod
    def w_object_begin(writer: Writer) -> Writer:
        """``bon_w_object_begin``: open an OBJECT inside the writer."""
        writer._w_object_begin()
        return writer

    @staticmethod
    def w_object_end(writer: Writer) -> Writer:
        """``bon_w_object_end``: close the innermost OBJECT."""
        writer._w_object_end()
        return writer

    # -- iterators --------------------------------------------------------

    @staticmethod
    def iter_init(node: Bon, which: int = 0) -> Iter:
        """``bon_iter_init``: start a cursor over a container.

        ``which=0`` walks every stored TLV in order (an OBJECT therefore yields
        key, value, key, value...), ``which=1`` walks only the values and needs
        an OBJECT.  The C fills a caller-allocated ``bon_iter``; Python gets the
        finished cursor as the return value.
        """
        return Iter(node, which)

    @staticmethod
    def iter_next(cursor: Iter) -> Bon:
        """``bon_iter_next``: the next node, or ``None`` once the cursor is done."""
        try:
            return next(cursor)
        except StopIteration:
            return None

    @staticmethod
    def iter_key_next(cursor: Iter) -> tuple[Bon, Bon] | None:
        """``bon_iter_key_next``: the next ``(key, value)`` pair of an OBJECT, else ``None``.

        Each call advances the cursor by exactly one pair, so successive calls
        walk the object -- and interleave with :meth:`bonlib.iter_next` on the same
        cursor without losing their place.
        """
        try:
            return cursor._key_next()
        except StopIteration:
            return None

    @staticmethod
    def iter_index(cursor: Iter) -> int:
        """``bon_iter_index``: 0-based index of the item just produced.

        0 before the first item, and still the last index once the cursor is
        exhausted -- so it can be handed straight back to :meth:`bonlib.at` or
        :meth:`bonlib.value_at`.
        """
        return cursor._get_index

    @staticmethod
    def iter_done(cursor: Iter) -> None:
        """``bon_iter_done``: release a cursor early.

        This is the C's iterator destructor, *not* an exhaustion test: it drops
        the cursor's reference to its container so the cursor can be forgotten.
        Python would collect it anyway, so calling it is optional; after it, the
        cursor is uninitialised and further use raises.
        """
        cursor._close()

    # -- diagnostics ------------------------------------------------------

    @staticmethod
    def write_json(out: Any, node: Bon) -> None:
        """``bon_write_json``: write the JSON rendering of ``node`` to a path or stream.

        The C takes a ``FILE*``; a path is accepted too.  Use :meth:`bonlib.to_json` for
        the string form.
        """
        _write_json(node, out)

    @staticmethod
    def write_hex(out: Any, data: Any, size: int | None = None, width: int = 16) -> None:
        """``bon_write_hex``: write a hex + ASCII dump of ``data`` to a path or stream.

        ``size`` truncates a larger buffer, as the C's ``size_t`` argument would;
        ``width`` is a Python extension, the C always uses its 16-byte layout.
        """
        _write_hex(data, out, size, width)

    @staticmethod
    def to_json(node: Bon) -> str:
        """The string :meth:`bonlib.write_json` would write, without the trailing newline.

        A convenience the C gets from ``open_memstream``; the rendering itself is
        byte-for-byte the reference's, ``%.17g`` floats and all.
        """
        return node._to_json()

    @staticmethod
    def to_hex(data: Any, size: int | None = None, width: int = 16) -> str:
        """The string :meth:`bonlib.write_hex` would write.

        Handy for looking at a stream: ``bonlib.to_hex(bonlib.stream(root))``.
        """
        return _to_hex(data, size, width)


# ---------------------------------------------------------------------------
# The public surface: bonpy
# ---------------------------------------------------------------------------


class bonpy:
    """BON <-> plain Python objects -- the layer the C library does not have.

    Nothing in here is a port of a ``bon_*`` function, which is exactly why it
    lives in its own class: :class:`bonlib` stays a faithful C surface, and this
    is the convenience layer built on top of it.

    :meth:`bonpy.to_python` maps a node tree onto ``None`` / ``bool`` / ``int`` /
    ``float`` / ``str`` / ``bytes`` / :class:`decimal.Decimal` / ``list`` /
    :class:`bonpy.ObjectPairs`; :meth:`bonpy.from_python` walks that back.  A DECIMAL keeps
    its exact coefficient and scale through :class:`bonpy.DecimalValue`, and an OBJECT
    becomes :class:`bonpy.ObjectPairs` rather than a ``dict`` because BON objects may
    repeat keys and may key on any type at all.
    """

    #: Exact ``(coefficient, scale)`` pair, so a DECIMAL survives a round trip.
    DecimalValue = _DecimalValue
    #: Ordered ``(key, value)`` pairs: the OBJECT counterpart of :class:`list`.
    ObjectPairs = _ObjectPairs

    @staticmethod
    def get_int(node: Bon) -> int:
        """A NUMBER's value as an exact Python ``int``, at any precision.

        The one scalar reader the C API does not have: it makes you try
        :meth:`bonlib.get_int64`, then :meth:`bonlib.get_uint64`, then fall back
        to :meth:`bonlib.get_int_str` and parse the text yourself.  Python's
        bignums just decode, which is what the format's VARINT mode is for.
        """
        return node._get_int()

    @staticmethod
    def stream_bytes(node: Bon) -> memoryview:
        """The whole document as bytes: the ``bon`` magic plus the root TLV.

        The C has no call that returns it -- :meth:`bonlib.magic` and
        :meth:`bonlib.stream` hand out the two halves, and
        :meth:`bonlib.save_file` puts them on disk -- so this convenience lives
        here.  A zero-copy view of the document buffer, like
        :meth:`bonlib.data`.
        """
        return node._get_stream_bytes

    @staticmethod
    def mode_name(mode: int) -> str:
        """``"uint16"``, ``"-uint8"``, ``"varint"``, ... for a NUMBER mode code.

        A readable form of what :meth:`bonlib.get_number_mode` hands back; the C
        only offers the code.
        """
        return _MODE_NAMES.get(mode, f"reserved(0x{mode:02X})")

    @staticmethod
    def writer_root_type(writer: Writer) -> int:
        """``BON_ARRAY`` or ``BON_OBJECT``: the container a writer opened.

        The C never asks a writer what it is building; the Python layer tracked
        it for error messages, and this surfaces it.
        """
        return writer._root_type

    @staticmethod
    def tlv_count(node: Bon) -> int:
        """How many TLVs the subtree holds, containers included.

        Another convenience with no C counterpart; the C reaches the same number
        by walking with :meth:`bonlib.iter_init`.
        """
        return node._get_tlv_count

    @staticmethod
    def iter_items(node: Bon) -> Iterator[tuple[Bon, Bon]]:
        """An OBJECT's ``(key, value)`` node pairs, in document order.

        A generator over what :meth:`bonlib.iter_key_next` walks one pair at a
        time, so repeated keys are all yielded rather than collapsed the way
        :meth:`bonlib.find` collapses them.
        """
        return _iter_items(node)

    @staticmethod
    def iter_children(node: Bon) -> Iterator[Bon]:
        """An ARRAY's elements, or an OBJECT's values with the keys skipped.

        The generator form of a value cursor; for an OBJECT that is
        ``bon_iter_init(node, 1)`` in the C, and for an ARRAY ``which=0``.
        """
        return _iter_children(node)

    @staticmethod
    def to_python(node: Bon) -> Any:
        """Decode a node tree into plain Python data."""
        return _to_python(node)

    @staticmethod
    def from_python(value: Any, writer: Writer) -> None:
        """Fill an open writer's root container with a plain Python value.

        The root must already be open, so this writes children only -- the C
        counterpart of walking a document by hand, and what :meth:`bonpy.dumps` uses.
        """
        _from_python(value, writer)

    @staticmethod
    def dumps(obj: Any) -> bytes:
        """Encode plain Python data as a complete BON document.

        ``list``/``tuple`` become ARRAYs, ``dict``/:class:`bonpy.ObjectPairs` become
        OBJECTs, and the root has to be one of those two because the format
        allows no other root type.
        """
        return _dumps(obj)

    @staticmethod
    def dumps_file(path: Any, obj: Any) -> None:
        """Encode plain Python data with :meth:`bonpy.dumps` and write it to ``path``."""
        _dumps_file(path, obj)

    @staticmethod
    def loads(data: Any, size: int | None = None) -> Any:
        """Decode a complete BON document into plain Python data."""
        return _loads(data, size)

    @staticmethod
    def loads_file(path: Any) -> Any:
        """Read a BON file and decode it into plain Python data."""
        return _loads_file(path)
# ---------------------------------------------------------------------------
# The public surface: bontype
# ---------------------------------------------------------------------------


class bontype:
    """A Python value pinned to one BON type.

    A plain Python value already says what it wants to be: ``str`` is a STRING,
    ``int`` a NUMBER, ``float`` a FLOAT, ``bool`` a BOOL, ``Decimal`` a DECIMAL,
    ``bytes`` a BLOB, ``list`` an ARRAY, ``dict``/``ObjectPairs`` an OBJECT.
    :class:`bontype` is for what Python cannot say on its own -- a NUMBER spelled
    as decimal text, a STRING whose bytes are not valid UTF-8, a BLOB used as a
    key -- and it is accepted anywhere a plain value is, keys and values alike::

        bontools.kv.key("tags").index(0).set(bontype.number("11"))
        bontools.kv.key(bontype.string(b"\\xff")).get()      # a raw STRING key

    The instances are inert: they carry a type code and a payload, and the writing
    side decides which ``bonlib`` function to call.
    """

    __slots__ = ("type", "value")

    def __init__(self, type_: int, value: Any) -> None:
        self.type = type_
        self.value = value

    def __repr__(self) -> str:
        return f"bontype.{_type_name_of(self.type).lower()}({self.value!r})"

    @staticmethod
    def none(value: Any = None) -> "bontype":
        """A NONE node.  ``value`` is ignored and only kept for the repr."""
        return bontype(BON_NONE, value)

    @staticmethod
    def bool(value: Any) -> "bontype":
        """A BOOL node."""
        return bontype(BON_BOOL, value)

    @staticmethod
    def number(value: Any) -> "bontype":
        """A NUMBER node.

        ``value`` is an ``int`` or decimal text -- ``"11"``, or
        ``"1267650600228229401496703205376"`` -- because past 64 bits the C's own
        route is :meth:`bonlib.w_int_dec`, text in and text out.
        """
        return bontype(BON_NUMBER, value)

    @staticmethod
    def float(value: Any) -> "bontype":
        """A FLOAT node (IEEE-754 binary64, like ``bon_w_double``)."""
        return bontype(BON_FLOAT, value)

    @staticmethod
    def decimal(coefficient: Any, scale: Any = 0) -> "bontype":
        """A DECIMAL node, kept exactly as ``coefficient * 10 ** -scale``."""
        return bontype(BON_DECIMAL, _DecimalValue(int(coefficient), int(scale)))

    @staticmethod
    def string(value: Any) -> "bontype":
        """A STRING node.

        ``value`` is a ``str`` or the raw UTF-8 bytes; bytes need no decoding,
        which is how a STRING that is not valid UTF-8 gets written.
        """
        return bontype(BON_STRING, value)

    @staticmethod
    def blob(value: Any) -> "bontype":
        """A BLOB node, i.e. opaque bytes with no text meaning."""
        return bontype(BON_BLOB, value)

    @staticmethod
    def array(value: Any = ()) -> "bontype":
        """An ARRAY node holding ``value`` as its elements."""
        return bontype(BON_ARRAY, value)

    @staticmethod
    def object(value: Any = ()) -> "bontype":
        """An OBJECT node holding ``value``: a mapping, or a sequence of pairs."""
        return bontype(BON_OBJECT, value)


# ---------------------------------------------------------------------------
# The public surface: bontools
# ---------------------------------------------------------------------------


class bontools:
    """The json-shaped front end: :class:`bonlib` and :class:`bonpy`, wired together.

    Nothing here is a new algorithm.  The four entry points are one-liners over
    the two layers below (:meth:`load`, :meth:`loads`, :meth:`dump`,
    :meth:`dumps`), and the path builder reads with the C navigation functions
    and writes with the Python mapping layer.  What is new is only the shape of
    the calls, which is the point::

        import bon

        with open("data.bon", "rb") as handle:
            data = bon.load(handle)                       # a bon object

        data.bontools.kv.key("name").get()                # -> "Alice"
        data.bontools.kv.key("age").set(30)               # rewrite the document
        bon.dump(data, "out.bon")

    Those four names are exposed on the module too, so ``bon.load(...)`` is the
    same call.  :meth:`kv` works both ways round: on the class it is an unbound
    builder -- root it with :meth:`_Kv.doc` -- and on a node it is rooted there.
    """

    __slots__ = ("_node",)

    def __init__(self, node: "Bon | None" = None) -> None:
        self._node = node

    def __repr__(self) -> str:
        if self._node is None:
            return "<bontools unbound>"
        return (f"<bontools {bonlib.type_name(self._node)} "
                f"size={bonlib.size(self._node)}>")

    # -- the four entry points -------------------------------------------
    @staticmethod
    def load(source: Any) -> "Bon":
        """Parse a whole document from a path or an open binary file.

        ``json.load`` takes either, so this does too: anything with ``read()`` is
        read where it stands, anything else is treated as a path.
        """
        if hasattr(source, "read"):
            return bonlib.from_stream(source.read())
        return bonlib.from_file(source)

    @staticmethod
    def loads(data: Any, size: int | None = None) -> "Bon":
        """Parse a BON byte stream (``bon`` magic and all) into a bon object.

        The mirror of :meth:`load`.  It yields a **bon object**, not plain Python
        data -- that is :meth:`bonpy.loads`.
        """
        return bonlib.from_stream(data, size)

    @staticmethod
    def dump(node: "Bon", target: Any) -> None:
        """Write a bon object to a path or an open binary file.

        The document is written whole, magic included, so the result feeds
        straight back into :meth:`load`.
        """
        data = bontools.dumps(node)
        if hasattr(target, "write"):
            target.write(data)
            return
        bonlib.save_file(node, target)

    @staticmethod
    def dumps(node: "Bon") -> bytes:
        """Serialise a bon object to a complete BON byte stream.

        Takes a **bon object**.  :meth:`bonpy.dumps` is the other direction: it
        encodes plain Python data.  The two cannot be swapped, and each says so
        rather than guessing.
        """
        if not isinstance(node, Bon):
            raise TypeError(
                f"bon.dumps() needs a bon object, not {type(node).__name__}; "
                f"bonpy.dumps() is what encodes plain Python data"
            )
        return bytes(bonpy.stream_bytes(node))


class _kv_accessor:
    """``bontools.kv`` unbound, ``node.bontools.kv`` rooted at that node.

    A descriptor so the builder is reachable from the class as well as from an
    instance; ``data.bontools.kv`` is the spelling you normally want.
    """

    __slots__ = ()

    def __get__(self, obj: "bontools | None", objtype: type | None = None) -> "_Kv":
        return _Kv(None if obj is None else obj._node)


class _Kv:
    """A path into a document: ``.key()`` / ``.index()`` steps, then read or write.

    Every step returns a **new** builder, the way ``str`` methods do, so a
    half-built path is a value you can keep::

        addr = bontools.kv.key("address")               # no document yet
        data.bontools.kv.key("a").key(1).index(2).set(11)

    ``.key(v)`` addresses an OBJECT member -- a ``str`` becomes a STRING key, and
    so do raw ``bytes``, which is how ``bon_find`` reads them; ``bontype`` pins
    anything else.  ``.index(i)`` addresses the *i*-th element of an ARRAY, or
    the *i*-th value of an OBJECT.  ``.get()`` returns plain Python data and
    ``.set(v)`` writes, returning the bon object.
    """

    __slots__ = ("_node", "_path")

    def __init__(self, node: "Bon | None" = None,
                 path: "tuple[tuple[str, Any], ...]" = ()) -> None:
        self._node = node
        self._path = path

    def __repr__(self) -> str:
        steps = ".".join(f"key({segment!r})" if kind == "key" else f"index({segment})"
                         for kind, segment in self._path) or "(root)"
        root = "unbound" if self._node is None else bonlib.type_name(self._node)
        return f"<kv {steps} on {root}>"

    # -- building ---------------------------------------------------------
    def doc(self, node: "Bon") -> "_Kv":
        """Re-root this path at another document."""
        return _Kv(node, self._path)

    def key(self, value: Any) -> "_Kv":
        """Step into the OBJECT member stored under ``value``."""
        return _Kv(self._node, self._path + (("key", value),))

    def index(self, index: Any) -> "_Kv":
        """Step into the ``index``-th element (ARRAY) or value (OBJECT)."""
        return _Kv(self._node, self._path + (("index", _index_of(index)),))

    # -- reading / writing ------------------------------------------------
    def get(self, raw: bool = False, node: Any = None) -> Any:
        """The value at this path as plain Python data, or ``None`` if absent.

        ``raw=True`` hands back the bon object instead, which is the zero-copy
        view and the only way to read a STRING that is not valid UTF-8.
        """
        current = self._walk(self._resolve(node))
        if current is None:
            return None
        return current if raw else bonpy.to_python(current)

    def set(self, value: Any, node: Any = None) -> "Bon":
        """Write ``value`` at this path, and return the bon object.

        A ``key`` step replaces the first member with that key and appends one if
        the key is absent; an ``index`` step replaces or appends, and refuses to
        leave a hole.  BON documents are immutable by construction -- no C
        function writes into one -- so this re-encodes the document and re-points
        the node, which is why the object you called it on still holds the new
        bytes afterwards.  Nodes taken out of the document *before* the write
        keep pointing at the old ones.

        A document that holds a STRING which is not valid UTF-8 is re-encoded
        byte for byte, because that STRING is carried through as raw bytes rather
        than decoded -- it is the only way to keep both the type and the bytes.
        """
        target = self._resolve(node)
        if not self._path:
            _fail(BON_E_USAGE, "set() needs at least one key() or index() step")
        structure = _to_python(target, lenient=True)
        _store(structure, self._path, value, self._describe())
        _rebind(target, _encode(structure, bonlib.type(target)))
        return target

    # -- internals --------------------------------------------------------
    def _resolve(self, node: Any) -> "Bon":
        target = self._node if node is None else node
        if target is None:
            _fail(BON_E_USAGE,
                  "no document to work on: use data.bontools.kv..., or "
                  "bontools.kv.doc(data)...")
        return target

    def _walk(self, node: "Bon") -> "Bon | None":
        current: "Bon | None" = node
        for kind, segment in self._path:
            if current is None:
                return None
            current = (_find_key(current, segment) if kind == "key"
                       else _child_at(current, segment))
        return current

    def _describe(self) -> str:
        steps = ", ".join(f"key({segment!r})" if kind == "key" else f"index({segment})"
                          for kind, segment in self._path)
        return f"{steps} of {_type_name_of(bonlib.type(self._node))}"


#: The path builder: unbound on the class, rooted at a node on an instance.
#: Assigned here because the descriptor and the builder are defined below.
bontools.kv = _kv_accessor()


# ---------------------------------------------------------------------------
# bontools: walking a path
# ---------------------------------------------------------------------------


def _find_key(node: "Bon", key: Any) -> "Bon | None":
    """The first value stored under ``key``, or ``None`` -- the first, like json.

    A STRING key goes straight to :meth:`bonlib.find`, which is the C's own
    byte-for-byte walk.  Anything else compares node by node, since the C's
    ``bon_find`` only ever matches STRINGs.
    """
    if _key_type(key) == BON_STRING and not isinstance(key, bontype):
        return bonlib.find(node, _key_raw(key))
    for pair_key, pair_value in bonpy.iter_items(node):
        if _key_matches(pair_key, key):
            return pair_value
    return None


def _child_at(node: "Bon", index: int) -> "Bon | None":
    """The ``index``-th element of an ARRAY, or the ``index``-th value of an OBJECT."""
    kind = bonlib.type(node)
    if kind not in _CONTAINER_SET:
        _fail(BON_E_TYPE,
              f"cannot take index {index} of a {_type_name_of(kind)} node")
    try:
        return bonlib.at(node, index) if kind == BON_ARRAY \
            else bonlib.value_at(node, index)
    except BonError as exc:
        if exc.code == BON_E_RANGE:
            return None                         # out of range reads as absent
        raise


def _key_raw(key: Any) -> memoryview:
    """A STRING key as the bytes it is compared by.

    Text is encoded the way :func:`_key_stored` encodes it, so a ``str`` and the
    ``bytes`` it came from are one key -- and ``surrogateescape`` means a key that
    is not valid UTF-8 survives the round trip instead of failing to encode.
    """
    if isinstance(key, str):
        return memoryview(key.encode("utf-8", "surrogateescape"))
    return _key_bytes(key)


def _key_matches(node: "Bon", key: Any) -> bool:
    """Whether a key node is the key asked for, compared the way BON stores it.

    Compared in place rather than by decoding the document:
    :meth:`bonlib.get_cstr` hands back the STRING bytes as written, so a key that
    is not valid UTF-8 still finds its match, and the type is part of the test --
    ``1`` is not ``"1"``.
    """
    kind = _key_type(key)
    if bonlib.type(node) != kind:
        return False
    if kind == BON_STRING:
        return bonlib.get_cstr(node) == bytes(_key_raw(_typed(key)))
    if kind == BON_BLOB:
        return bytes(bonlib.get_bytes(node)) == bytes(_typed(key))
    if kind == BON_DECIMAL:
        return bonlib.get_decimal(node) == _decimal_pair(_typed(key))
    return bonpy.to_python(node) == _key_python(key)


# ---------------------------------------------------------------------------
# bontools: writing through a path
# ---------------------------------------------------------------------------


def _store(structure: Any, path: "tuple[tuple[str, Any], ...]", value: Any,
           where: str) -> None:
    """Apply ``path`` to a decoded document, then drop ``value`` at the end of it."""
    for kind, segment in path[:-1]:
        structure = _descend(structure, kind, segment, where)
    last_kind, last_segment = path[-1]
    if last_kind == "key":
        _store_key(structure, last_segment, value, where)
    else:
        _store_index(structure, last_segment, value, where)


def _descend(container: Any, kind: str, segment: Any, where: str) -> Any:
    """The Python container one step further down, to be mutated in place."""
    if kind == "key":
        if isinstance(container, Mapping):
            key = _key_python(segment)
            if key in container:
                return container[key]
        elif isinstance(container, list):
            for pair in container:
                if _py_key_equal(pair[0], segment):
                    return pair[1]
    elif isinstance(container, list):
        position = int(segment)
        if -len(container) <= position < len(container):
            return container[position]
    _fail(BON_E_NOKEY, f"no {'key ' if kind == 'key' else ''}{segment!r} in {where}")


def _store_key(container: Any, key: Any, value: Any, where: str) -> None:
    """Replace the first member with this key, or append one."""
    if isinstance(container, Mapping):
        container[_key_stored(key)] = value
        return
    for index, pair in enumerate(container):
        if _py_key_equal(pair[0], key):
            container[index] = (pair[0], value)     # first match wins, like find()
            return
    container.append((_key_stored(key), value))


def _store_index(container: Any, index: int, value: Any, where: str) -> None:
    """Replace or append one position; BON has no holes to fill."""
    if not isinstance(container, list):
        _fail(BON_E_TYPE,
              f"cannot index into a {_python_kind(container)} at {where}")
    position = int(index)
    if -len(container) <= position < len(container):
        container[position] = value
    elif position == len(container):
        container.append(value)                     # json appends to the end
    else:
        _fail(BON_E_RANGE,
              f"cannot write index {position} of a {len(container)}-element "
              f"sequence at {where}: BON has no holes")


# ---------------------------------------------------------------------------
# bontools: keys, indexes and DECIMAL payloads
# ---------------------------------------------------------------------------


def _typed(value: Any) -> Any:
    """The payload of a :class:`bontype`, or the value itself."""
    return value.value if isinstance(value, bontype) else value


def _index_of(index: Any) -> int:
    """Validate an ``.index()`` argument."""
    index = _typed(index)
    if isinstance(index, bool) or not isinstance(index, int):
        _fail(BON_E_TYPE, f"index must be an int, not {type(index).__name__}")
    return index


def _key_type(key: Any) -> int:
    """The BON type a key will have.  ``bytes`` mean a STRING, as in ``bon_find``."""
    if isinstance(key, bontype):
        return key.type
    if key is None:
        return BON_NONE
    if isinstance(key, bool):
        return BON_BOOL
    if isinstance(key, int):
        return BON_NUMBER
    if isinstance(key, float):
        return BON_FLOAT
    if isinstance(key, (str, bytes, bytearray, memoryview)):
        return BON_STRING
    if isinstance(key, (Decimal, _DecimalValue)):
        return BON_DECIMAL
    _fail(BON_E_TYPE, f"a BON key cannot be {type(key).__name__}")


def _key_stored(key: Any) -> Any:
    """The key to put in a new member, ready for :func:`_write_typed`.

    A key position is a STRING unless a :class:`bontype` says otherwise, so bytes
    are pinned to STRING here -- as a *value* the same bytes would be a BLOB.  They
    are also kept raw: a key is compared byte for byte, and decoding then
    re-encoding is a round trip that fails on bytes that were never valid UTF-8.
    """
    if isinstance(key, bontype):
        return key
    if isinstance(key, (bytes, bytearray, memoryview)):
        return bontype.string(bytes(key))
    if isinstance(key, str):
        return bontype.string(key.encode("utf-8", "surrogateescape"))
    return key


def _key_python(key: Any) -> Any:
    """A key as the plain Python value :meth:`bonpy.to_python` would produce.

    A STRING built from raw bytes comes back as text, decoded the way
    :meth:`bonlib.get_string` does it, so a key written as bytes finds a key that
    came out of the document as a ``str`` -- and vice versa.
    """
    if isinstance(key, bontype):
        if key.type == BON_NUMBER and not isinstance(key.value, int):
            return _number_text_to_int(key.value)
        if key.type == BON_DECIMAL and not isinstance(key.value, _DecimalValue):
            return _DecimalValue(*_decimal_pair(key.value))
        key = key.value
    if isinstance(key, (bytes, bytearray, memoryview)):
        return bytes(key).decode("utf-8", "surrogateescape")
    return key


def _py_key_equal(left: Any, right: Any) -> bool:
    """Compare a decoded key with a requested one, types included."""
    if left is right:
        return True
    if isinstance(left, bool) != isinstance(right, bool):
        return False
    return _key_python(left) == _key_python(right)


def _number_text_to_int(text: Any) -> int:
    """A NUMBER key given as decimal text, read the way :meth:`bonlib.get_int` reads it."""
    if isinstance(text, (bytes, bytearray, memoryview)):
        text = bytes(text).decode("ascii", "strict")
    if isinstance(text, str):
        try:
            return int(text, 10)
        except ValueError:
            _fail(BON_E_TYPE, f"{text!r} is not a decimal integer")
    if isinstance(text, int):
        return text
    _fail(BON_E_TYPE, f"a NUMBER key needs an int or decimal text, "
                      f"not {type(text).__name__}")


def _decimal_pair(value: Any) -> "tuple[int, int]":
    """``(coefficient, scale)`` for a DECIMAL payload of any accepted shape."""
    if isinstance(value, _DecimalValue):
        return value.coefficient, value.scale
    if isinstance(value, Decimal):
        return _DecimalValue(value).coefficient, _DecimalValue(value).scale
    if isinstance(value, (tuple, list)) and len(value) == 2:
        return int(value[0]), int(value[1])
    _fail(BON_E_TYPE,
          f"a DECIMAL needs (coefficient, scale), a Decimal or a DecimalValue, "
          f"not {type(value).__name__}")


def _python_kind(value: Any) -> str:
    """What a decoded document holds here, for error messages."""
    if isinstance(value, list):
        return "sequence"
    if isinstance(value, Mapping):
        return "mapping"
    return f"{type(value).__name__} value"


# ---------------------------------------------------------------------------
# bontools: writing a value
# ---------------------------------------------------------------------------


def _value_kind(value: Any) -> "int | None":
    """The BON type ``value`` will be written as, or ``None`` if it is not a value."""
    if isinstance(value, bontype):
        return value.type
    kind = _container_kind(value)
    if kind is not None:
        return kind
    if value is None:
        return BON_NONE
    if isinstance(value, bool):
        return BON_BOOL
    if isinstance(value, int):
        return BON_NUMBER
    if isinstance(value, float):
        return BON_FLOAT
    if isinstance(value, str):
        return BON_STRING
    if isinstance(value, (bytes, bytearray, memoryview)):
        return BON_BLOB
    if isinstance(value, (Decimal, _DecimalValue)):
        return BON_DECIMAL
    return None


def _value_sequence(value: Any) -> list:
    """The TLV sequence a container contributes to a document."""
    if isinstance(value, bontype):
        inner = value.value
        if value.type == BON_OBJECT and not _is_object_like(inner):
            inner = _ObjectPairs(inner)           # a bare sequence of pairs
        return _children_of(inner)
    return _children_of(value)


def _write_typed(w: "Writer", value: Any) -> None:
    """Write one non-container value, calling the ``bonlib`` writer for its type."""
    kind = _value_kind(value)
    payload = _typed(value)
    if kind == BON_STRING:
        w._w_string(payload)
    elif kind == BON_BLOB:
        w._w_blob(payload)
    elif kind == BON_BOOL:
        w._w_bool(payload)
    elif kind == BON_NONE:
        w._w_none()
    elif kind == BON_FLOAT:
        w._w_double(payload)
    elif kind == BON_NUMBER:
        _write_number(w, payload)
    elif kind == BON_DECIMAL:
        if isinstance(payload, (tuple, list)) and not isinstance(payload, _DecimalValue):
            w._w_decimal(payload[0], payload[1])
        else:
            _write_scalar(w, payload)             # Decimal / DecimalValue, bonpy's rules
    else:
        _fail(BON_E_USAGE, f"cannot encode {type(value).__name__} as BON")


def _write_number(w: "Writer", value: Any) -> None:
    """A NUMBER, picking the ``bon_w_*`` a C caller would have had to choose.

    ``int64`` while it fits, ``uint64`` up to 2**64-1, decimal text past that --
    which is exactly the escalation the C API forces on anyone who wants a
    magnitude wider than 64 bits.
    """
    if isinstance(value, (str, bytes, bytearray, memoryview)):
        w._w_int_dec(value)
        return
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(BON_E_TYPE, f"a NUMBER needs an int or decimal text, "
                          f"not {type(value).__name__}")
    if _I64_MIN <= value <= _I64_MAX:
        w._w_int64(value)
    elif 0 <= value <= _U64_MAX:
        w._w_uint64(value)
    else:
        w._w_int_dec(str(value))


def _encode(value: Any, root_type: int) -> bytes:
    """Encode a decoded document -- :class:`bontype` values included -- to bytes.

    The same explicit-stack discipline as :func:`_from_python`: one close marker
    per level, so nesting depth costs no Python stack.
    """
    with Writer(root_type) as w:
        _emit(w, value)
        return w._finish()._get_stream_bytes


def _emit(w: "Writer", root: Any) -> None:
    """Fill an already-open writer with ``root``'s children, pins included."""
    kind = _value_kind(root)
    if kind not in _CONTAINER_SET:
        _fail(BON_E_USAGE, "a document root must be an ARRAY or an OBJECT")
    if kind != w._get_root_type:
        _fail(BON_E_USAGE,
              f"cannot write a {_type_name_of(kind)} root into a "
              f"{_type_name_of(w._get_root_type)} document")
    # Stack order is document order: pop() takes the next child, and each nested
    # container pushes its close marker below its own children.
    stack: list[Any] = list(reversed(_value_sequence(root)))
    while stack:
        item = stack.pop()
        if item is _CLOSE_ARRAY:
            w._w_array_end()
            continue
        if item is _CLOSE_OBJECT:
            w._w_object_end()
            continue
        item_kind = _value_kind(item)
        if item_kind not in _CONTAINER_SET:
            _write_typed(w, item)
            continue
        if item_kind == BON_OBJECT:
            w._w_object_begin()
            stack.append(_CLOSE_OBJECT)
        else:
            w._w_array_begin()
            stack.append(_CLOSE_ARRAY)
        stack.extend(reversed(_value_sequence(item)))


def _rebind(node: "Bon", data: bytes) -> None:
    """Point an existing node at a freshly built document, in place.

    BON is immutable by construction, so an edit means building a new document;
    copying its offsets onto the node the caller already holds is what makes
    ``data.bontools.kv...set(v)`` visible through ``data`` afterwards.
    """
    fresh = _root_of(_Buf(memoryview(data)))
    node._buf = fresh._buf
    node._off = fresh._off
    node._size = fresh._size
    node._type = fresh._type
    node._count = fresh._count


# ---------------------------------------------------------------------------
# bontools on the module itself
# ---------------------------------------------------------------------------


def load(source: Any) -> "Bon":
    """Parse a whole document from a path or an open binary file into a bon object.

    The same call as :meth:`bontools.load`, exposed here so the common case reads
    like ``json.load``.
    """
    return bontools.load(source)


def loads(data: Any, size: int | None = None) -> "Bon":
    """Parse a BON byte stream into a **bon object**.

    Note the difference from :meth:`bonpy.loads`, which hands back plain Python
    data: this one hands back the document, which is what ``.bontools`` and the
    ``bonlib`` navigation functions want.
    """
    return bontools.loads(data, size)


def dump(node: "Bon", target: Any) -> None:
    """Write a bon object to a path or an open binary file."""
    return bontools.dump(node, target)


def dumps(node: "Bon") -> bytes:
    """Serialise a **bon object** to a complete BON byte stream.

    Note the difference from :meth:`bonpy.dumps`, which encodes plain Python
    data: this one re-serialises a document that already is a bon object.
    """
    return bontools.dumps(node)

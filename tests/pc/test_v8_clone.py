"""The V8 value format, walked structurally rather than scanned.

The poisoning test is the point of this module. A pattern-anchored decoder
reads the number after a field name; conversation text in the same buffer can
supply that field name, and the panel then shows a number out of somebody's
chat.
"""
import logging
import struct
import time

from pc import v8_clone
from tests.support import v8_fixture as vfx


def _varint(n: int) -> bytes:
    """Local, so a hand-built buffer below does not borrow the writer."""
    out = bytearray()
    while True:
        b = n & 0x7f
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _s(text: str) -> bytes:
    """A one-byte-per-character string, varint length."""
    return b'"' + _varint(len(text)) + text.encode("latin-1")


def _d(value: float) -> bytes:
    """A double: the N tag and eight little-endian bytes."""
    return b"N" + struct.pack("<d", value)


def _smi(value: int) -> bytes:
    """An integer: the I tag and a zigzag varint."""
    z = (value << 1) if value >= 0 else ((-value << 1) - 1)
    return b"I" + _varint(z)


def _dense(elements: bytes, count: int, props: bytes = b"",
           n_props: int = 0) -> bytes:
    """A dense array, spelled out from the format description.

    `A <len>`, then that many elements BARE -- no index in front of any of
    them -- then any named properties as key/value pairs, then
    `$ <props> <len>`. Written here rather than taken from the fixture
    because the fixture had the same misreading the parser did, so a test
    built on it agreed with the bug.
    """
    return (b"A" + _varint(count) + elements + props
            + b"$" + _varint(n_props) + _varint(count))


def test_parses_a_flat_object():
    assert v8_clone.parse(vfx.dumps({"a": 1.5, "b": True})) == {
        "a": 1.5, "b": True}


def test_parses_the_nested_window_shape():
    doc = {"rate_limit_info": {"status": "allowed", "unifiedWindows": {
        "five_hour": {"resetsAt": 1788628200.0, "utilization": 0.05},
        "seven_day": {"resetsAt": 1788933600.0, "utilization": 0.17}}}}
    got = v8_clone.parse(vfx.dumps(doc))
    windows = got["rate_limit_info"]["unifiedWindows"]
    assert windows["seven_day"]["resetsAt"] == 1788933600.0


def test_reads_a_zero_utilization_written_as_an_integer():
    """The minute after a reset, utilization is exactly 0 and V8 writes it as
    a Smi. A decoder that requires a double misses it."""
    got = v8_clone.parse(vfx.dumps(vfx.ints({"utilization": 0})))
    assert got["utilization"] == 0


def test_a_field_name_inside_a_string_cannot_be_mistaken_for_a_field():
    """Conversation text mentioning the field name must not become data."""
    doc = {"text": "resetsAt is the field we care about",
           "resetsAt": 1788933600.0}
    assert v8_clone.parse(vfx.dumps(doc))["resetsAt"] == 1788933600.0


def test_string_lengths_are_varints_not_single_bytes():
    long_key = "k" * 300
    assert v8_clone.parse(vfx.dumps({long_key: 1.0}))[long_key] == 1.0


def test_returns_none_for_a_buffer_it_does_not_understand():
    assert v8_clone.parse(b"\x99\x99\x99") is None


def test_never_raises_on_a_truncated_buffer():
    full = vfx.dumps({"a": 1.0})
    for cut in range(1, len(full)):
        # Knowable: a cut buffer is never a complete value, so it is None.
        assert v8_clone.parse(full[:cut]) is None


# --- The cases where a plausible implementation is silently wrong ---


def test_a_one_byte_string_at_the_varint_boundary_does_not_desynchronise():
    """Reading one length byte works for every string under 128 characters
    and then silently shifts every field that follows."""
    for n in (127, 128, 129, 300):
        doc = {"text": "x" * n, "resetsAt": 1788933600.0}
        got = v8_clone.parse(vfx.dumps(doc))
        assert got["text"] == "x" * n
        assert got["resetsAt"] == 1788933600.0


def test_zero_is_a_reading_and_absence_is_not():
    """0 as a Smi, 0.0 as a double, and a missing field are three different
    answers. UNKNOWN is never spelled as zero."""
    smi = v8_clone.parse(vfx.dumps(vfx.ints({"utilization": 0})))
    dbl = v8_clone.parse(vfx.dumps({"utilization": 0.0}))
    absent = v8_clone.parse(vfx.dumps({"other": 1.0}))
    assert smi["utilization"] == 0 and "utilization" in smi
    assert dbl["utilization"] == 0.0 and "utilization" in dbl
    assert "utilization" not in absent


def test_reads_negative_integers():
    """Zigzag is easy to decode backwards; -1 and 1 differ by one byte."""
    doc = vfx.ints({"a": -1, "b": 1, "c": -7, "d": -300, "e": 0})
    got = v8_clone.parse(vfx.dumps(doc))
    assert got == {"a": -1, "b": 1, "c": -7, "d": -300, "e": 0}


def test_every_truncation_of_a_nested_buffer_returns_none():
    """No proper prefix of a complete value is itself a complete value, so
    the answer is knowable: None at every cut, and never an exception."""
    full = vfx.dumps({"rate_limit_info": {"unifiedWindows": {
        "seven_day": {"resetsAt": 1788933600.0, "utilization": 0.17}}}})
    for cut in range(1, len(full)):
        assert v8_clone.parse(full[:cut]) is None


def _chain(levels: int) -> dict:
    """`levels` nested objects, the innermost empty."""
    top = inner = {}
    for _ in range(levels - 1):
        nxt = {}
        inner["n"] = nxt
        inner = nxt
    return top


def test_max_depth_levels_parse_and_the_next_one_does_not():
    """MAX_DEPTH is 64 levels, not 65. The guard runs on entry to _read with
    the outermost value at depth 0, so `>` would have allowed one too many."""
    assert v8_clone.parse(vfx.dumps(_chain(v8_clone.MAX_DEPTH))) is not None
    assert v8_clone.parse(vfx.dumps(_chain(v8_clone.MAX_DEPTH + 1))) is None
    assert v8_clone.parse(vfx.dumps(_chain(v8_clone.MAX_DEPTH * 8))) is None


def test_nesting_well_within_max_depth_still_carries_its_leaf():
    """So the depth guard cannot pass by rejecting everything."""
    shallow = inner = {}
    for _ in range(8):
        nxt = {}
        inner["n"] = nxt
        inner = nxt
    inner["leaf"] = 1.0
    got = v8_clone.parse(vfx.dumps(shallow))
    for _ in range(8):
        got = got["n"]
    assert got["leaf"] == 1.0


# --- Arrays: dense elements are bare, sparse ones are index/value pairs ---


def test_a_dense_array_holds_bare_elements():
    """`A <len>` is followed by the elements themselves. A reader that
    expects index/value pairs consumes two elements per slot: an even-length
    array decodes to the wrong thing and an odd-length one runs off the end
    and refuses the whole record."""
    for values in ([], [1.0], [1.0, 2.0], [1.0, 2.0, 3.0], [4.0] * 7):
        payload = _dense(b"".join(_d(v) for v in values), len(values))
        assert v8_clone.parse(vfx.wrap(payload)) == values


def test_an_odd_length_array_no_longer_swallows_the_record():
    """The reviewer's finding, kept as a test: an array of three beside the
    field we actually read, in one object."""
    payload = (b"o"
               + _s("ids") + _dense(_d(1.0) + _d(2.0) + _d(3.0), 3)
               + _s("resetsAt") + _d(1788933600.0)
               + b"{" + _varint(2))
    assert v8_clone.parse(vfx.wrap(payload)) == {
        "ids": [1.0, 2.0, 3.0], "resetsAt": 1788933600.0}


def test_the_fixture_writes_the_dense_shape_the_format_describes():
    """Pins the fixture to the description rather than to the parser -- the
    two agreeing with each other is what hid the bug."""
    assert vfx.dumps([1.0, 2.0, 3.0]) == vfx.wrap(
        _dense(_d(1.0) + _d(2.0) + _d(3.0), 3))


def test_a_named_property_after_dense_elements_does_not_derail_the_walk():
    payload = (b"o"
               + _s("a") + _dense(_d(1.0), 1,
                                  props=_s("note") + _d(2.0), n_props=1)
               + _s("b") + _d(3.0)
               + b"{" + _varint(2))
    assert v8_clone.parse(vfx.wrap(payload)) == {"a": [1.0], "b": 3.0}


def test_a_sparse_array_is_read_as_index_value_pairs():
    """The other array form, tag `a`: indices are written in front of the
    values and holes simply have no pair. The holes collapse -- a Python
    list has no hole -- so this is order-preserving, not index-preserving."""
    payload = (b"a" + _varint(5)
               + _smi(0) + _d(1.0)
               + _smi(4) + _d(2.0)
               + b"@" + _varint(0) + _varint(5))
    assert v8_clone.parse(vfx.wrap(payload)) == [1.0, 2.0]


def test_nested_arrays_and_objects_keep_their_shape():
    inner = _dense(_d(1.0) + _d(2.0), 2)
    payload = _dense(inner + b"o" + _s("k") + _d(3.0) + b"{" + _varint(1), 2)
    assert v8_clone.parse(vfx.wrap(payload)) == [[1.0, 2.0], {"k": 3.0}]


def test_an_array_longer_than_its_buffer_is_refused():
    payload = _dense(_d(1.0), 1)[:1] + _varint(10 ** 6) + _d(1.0)
    assert v8_clone.parse(vfx.wrap(payload)) is None


def test_a_back_reference_to_an_object_that_does_not_exist():
    """No table is kept, so a back-reference yields None rather than a guess
    -- and a dangling id must not raise either."""
    payload = b"o" + _s("a") + b"^" + _varint(9999) + b"{" + _varint(1)
    assert v8_clone.parse(vfx.wrap(payload)) == {"a": None}


def test_an_unknown_tag_ends_the_parse():
    payload = b"o" + _s("a") + b"\x7f" + b"{" + _varint(1)
    assert v8_clone.parse(vfx.wrap(payload)) is None


def test_parses_arrays_and_the_other_scalar_tags():
    doc = {"list": [1.0, 2.0], "null": None, "no": False,
           "utf16": "resets → soon"}
    got = v8_clone.parse(vfx.dumps(doc))
    assert got["list"] == [1.0, 2.0]
    assert got["null"] is None
    assert got["no"] is False
    assert got["utf16"] == "resets → soon"


def test_rejects_non_buffers_and_empty_input():
    for bad in (None, "not bytes", 7, b"", b"\xff", b"\xff\x0f"):
        assert v8_clone.parse(bad) is None


def test_hostile_buffers_are_refused_outright():
    """Every one of these is unreadable, so None is the knowable answer --
    a runaway varint, an unterminated object, a length past the end, an
    endless header, a truncated back-reference."""
    for bad in (b"\xff\x0fo" + b"\x80" * 40,
                b"\xff\x0f" + b'"' + b"\xff" * 9 + b"\x7f",
                b"\xff\x0fA" + b"\xff" * 10,
                b"\xff" * 200,
                b"\xff\x0fo" + _s("a") + b"^" + b"\x80" * 5,
                b"\xff\x0fo" + _s("a") + _d(1.0)):
        assert v8_clone.parse(bad) is None


def test_a_long_run_of_continuation_bytes_is_refused_promptly():
    """A varint with no width cap widens its accumulator seven bits per
    byte, so a run of 0xff costs O(n^2): half a megabyte of it inside one
    conversation record was tens of seconds of a blocked poll. It always
    terminated -- "never raises" held and "never hangs" did not."""
    for bad in (b"\xff\x0f" + b'"' + b"\xff" * 500_000,
                b"\xff\x0fo" + b"\xff" * 500_000,
                b"\xff" * 500_000,
                b"\xff\x0fA" + b"\xff" * 500_000):
        start = time.monotonic()
        assert v8_clone.parse(bad) is None
        assert time.monotonic() - start < 2.0


def test_the_module_logs_nothing_at_any_level(caplog):
    """README.md's "Something's wrong?" section promises bridge.log holds nothing secret, and this parser
    is pointed at the customer's conversations."""
    caplog.set_level(logging.NOTSET)
    v8_clone.parse(vfx.dumps({"secret": "conversation text"}))
    v8_clone.parse(b"\xff\x0f\x7fnot a value at all")
    v8_clone.parse(b"\xff\x0fo" + _s("k") + b"N\x01\x02")
    assert caplog.records == []

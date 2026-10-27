"""Bound supported PDF streams before PDFium sees the document.

Only a single classic cross-reference table and losslessly encoded streams are admitted.
Unsupported structures are refused rather than passed to a second, less bounded parser.
"""

from __future__ import annotations

import base64
import re
import zlib
from typing import Any, cast

MAX_STREAM_BYTES = 32 * 1024 * 1024
MAX_STREAM_TOTAL = 64 * 1024 * 1024
MAX_OBJECTS = 100_000
_SPACE = b"\x00\t\n\f\r "
_DELIMITERS = _SPACE + b"()<>[]{}/%"
_LINE_END = re.compile(rb"[\r\n]")


class Refused(ValueError):
    """The document exceeds a guard or uses an unsupported structure."""


class _Tokens:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0
        self.count = 0

    def space(self) -> None:
        while self.pos < len(self.data):
            if self.data[self.pos] in _SPACE:
                self.pos += 1
            elif self.data[self.pos] == 37:
                end = _LINE_END.search(self.data, self.pos)
                self.pos = end.end() if end else len(self.data)
            else:
                break

    def word(self) -> bytes:
        self.space()
        start = self.pos
        while self.pos < len(self.data) and self.data[self.pos] not in _DELIMITERS:
            self.pos += 1
        if start == self.pos:
            raise Refused("unsupported PDF syntax")
        return self.data[start : self.pos]

    def value(self, depth: int = 0) -> Any:
        self.count += 1
        if depth > 32 or self.count > 1_000_000:
            raise Refused("max_pdf_structure exceeded")
        self.space()
        data, start = self.data, self.pos
        if data[start : start + 2] == b"<<":
            self.pos += 2
            result: dict[bytes, Any] = {}
            while True:
                self.space()
                if data[self.pos : self.pos + 2] == b">>":
                    self.pos += 2
                    return result
                key = self.value(depth + 1)
                if not isinstance(key, bytes) or not key.startswith(b"/") or key in result:
                    raise Refused("unsupported PDF dictionary")
                result[key] = self.value(depth + 1)
        if data[start : start + 1] == b"[":
            self.pos += 1
            values: list[Any] = []
            while True:
                self.space()
                if data[self.pos : self.pos + 1] == b"]":
                    self.pos += 1
                    return values
                values.append(self.value(depth + 1))
        if data[start : start + 1] == b"/":
            self.pos += 1
            name_start = self.pos
            while self.pos < len(data) and data[self.pos] not in _DELIMITERS:
                self.pos += 1
            name = data[name_start : self.pos]
            if re.search(rb"#(?![0-9a-fA-F]{2})", name):
                raise Refused("unsupported PDF name")
            return b"/" + re.sub(rb"#([0-9a-fA-F]{2})", lambda m: bytes([int(m[1], 16)]), name)
        if data[start : start + 1] == b"(":
            self.pos += 1
            nesting = 1
            while self.pos < len(data) and nesting:
                char = data[self.pos]
                self.pos += 1
                if char == 92:
                    self.pos += 1
                elif char == 40:
                    nesting += 1
                elif char == 41:
                    nesting -= 1
            if nesting or self.pos > len(data):
                raise Refused("invalid PDF string")
            return None
        if data[start : start + 1] == b"<":
            end = data.find(b">", start + 1)
            if end < 0 or re.search(
                rb"[^0-9a-fA-F]", data[start + 1 : end].translate(None, _SPACE)
            ):
                raise Refused("invalid PDF hex string")
            self.pos = end + 1
            return None
        word = self.word()
        if re.fullmatch(rb"[+-]?\d+", word):
            number = int(word)
            saved = self.pos
            try:
                generation = self.word()
                if generation.isdigit() and self.word() == b"R":
                    return (number, int(generation))
            except Refused:
                # Failed lookahead leaves a plain integer, not an indirect reference.
                pass
            self.pos = saved
            return number
        if word in (b"true", b"false", b"null") or re.fullmatch(rb"[+-]?(?:\d+\.\d*|\.\d+)", word):
            return None
        raise Refused("unsupported PDF value")


def _decode(data: bytes, filters: Any) -> bytes:
    if filters is None:
        filters = []
    elif isinstance(filters, bytes):
        filters = [filters]
    if not isinstance(filters, list) or len(cast(list[Any], filters)) > 4:
        raise Refused("unsupported PDF stream filter")
    for filter_name in cast(list[Any], filters):
        if filter_name in (b"/FlateDecode", b"/Fl"):
            decoder = zlib.decompressobj()
            data = decoder.decompress(data, MAX_STREAM_BYTES + 1)
            if len(data) > MAX_STREAM_BYTES or decoder.unconsumed_tail:
                raise Refused("max_pdf_stream_bytes exceeded")
            if not decoder.eof or decoder.unused_data:
                raise Refused("invalid PDF compressed stream")
        elif filter_name in (b"/ASCII85Decode", b"/A85"):
            # PDFium stops decoding at these otherwise legal PDF whitespace bytes.
            if b"\x00" in data or b"\f" in data:
                raise Refused("unsupported PDF ASCII85 whitespace")
            raw = data.translate(None, _SPACE).removesuffix(b"~>")
            zeros = raw.count(b"z")
            groups, remainder = divmod(len(raw) - zeros, 5)
            if 4 * (zeros + groups) + max(0, remainder - 1) > MAX_STREAM_BYTES:
                raise Refused("max_pdf_stream_bytes exceeded")
            data = base64.a85decode(raw, adobe=False, ignorechars=_SPACE)
        elif filter_name in (b"/ASCIIHexDecode", b"/AHx"):
            raw = data.translate(None, _SPACE)
            if not raw.endswith(b">"):
                raise Refused("invalid PDF hex stream")
            raw = raw[:-1]
            data = bytes.fromhex((raw + (b"0" if len(raw) % 2 else b"")).decode("ascii"))
        else:
            raise Refused("unsupported PDF stream filter")
        if len(data) > MAX_STREAM_BYTES:
            raise Refused("max_pdf_stream_bytes exceeded")
    return data


def _has_inline_image(data: bytes) -> bool:
    tokens = _Tokens(data)
    while tokens.pos < len(data):
        tokens.space()
        start = tokens.pos
        if data[start : start + 1] in (b"(", b"/", b"<", b"["):
            tokens.value()
        elif start < len(data) and data[start] not in _DELIMITERS:
            word = tokens.word()
            if re.fullmatch(rb"[+\-0-9.]*BI", word):
                return True
        else:
            tokens.pos += 1
    return False


def check_pdf(data: bytes) -> None:
    """Refuse unbounded, encrypted, incremental or ambiguous PDF input before native parsing."""
    if not re.match(rb"%PDF-1\.[0-7][\r\n]", data):
        raise Refused("unsupported PDF header")
    end = re.search(rb"startxref\s+(\d+)\s+%%EOF\s*$", data)
    if end is None:
        raise Refused("invalid PDF cross-reference")
    offset = int(end[1])
    table = _Tokens(data[offset : end.start()])
    if table.word() != b"xref":
        raise Refused("unsupported PDF cross-reference stream")
    entries: dict[int, tuple[int, int]] = {}
    declared: set[int] = set()
    while True:
        first = table.word()
        if first == b"trailer":
            break
        count = table.word()
        if not first.isdigit() or not count.isdigit():
            raise Refused("invalid PDF cross-reference")
        start, size = int(first), int(count)
        if size > MAX_OBJECTS or len(declared) + size > MAX_OBJECTS:
            raise Refused("max_pdf_objects exceeded")
        for number in range(start, start + size):
            position, generation, state = table.word(), table.word(), table.word()
            if number in declared or not position.isdigit() or not generation.isdigit():
                raise Refused("invalid PDF cross-reference")
            declared.add(number)
            if state == b"n":
                entries[number] = (int(position), int(generation))
            elif state != b"f":
                raise Refused("invalid PDF cross-reference")
    trailer = table.value()
    table.space()
    if (
        not isinstance(trailer, dict)
        or any(key in trailer for key in (b"/Prev", b"/XRefStm", b"/Encrypt"))
        or table.pos != len(table.data)
    ):
        raise Refused("unsupported PDF trailer")
    positions = sorted((pos, number, gen) for number, (pos, gen) in entries.items())
    prefix = _Tokens(data[: positions[0][0]] if positions else data[:offset])
    prefix.space()
    if prefix.pos != len(prefix.data):
        raise Refused("unindexed PDF object")
    objects: dict[tuple[int, int], Any] = {}
    streams: dict[tuple[int, int], tuple[dict[bytes, Any], bytes]] = {}
    for index, (pos, number, generation) in enumerate(positions):
        stop = positions[index + 1][0] if index + 1 < len(positions) else offset
        if not 0 < pos < stop <= offset:
            raise Refused("invalid PDF object offset")
        tokens = _Tokens(data[pos:stop])
        if tokens.word() != str(number).encode() or tokens.word() != str(generation).encode():
            raise Refused("invalid PDF object identity")
        if tokens.word() != b"obj":
            raise Refused("invalid PDF object")
        value = tokens.value()
        objects[(number, generation)] = value
        following = tokens.word()
        if following == b"stream" and isinstance(value, dict):
            value = cast(dict[bytes, Any], value)
            if value.get(b"/Type") in (b"/ObjStm", b"/XRef"):
                raise Refused("unsupported PDF object stream")
            tail = tokens.data[tokens.pos :]
            if not tail.startswith((b"\r\n", b"\n")):
                raise Refused("invalid PDF stream delimiter")
            tail = tail[2:] if tail.startswith(b"\r\n") else tail[1:]
            streams[(number, generation)] = (value, tail)
        else:
            tokens.space()
            if following != b"endobj" or tokens.pos != len(tokens.data):
                raise Refused("invalid PDF object ending")

    def check_references(value: Any) -> None:
        if isinstance(value, tuple):
            if cast(tuple[int, int], value) not in objects:
                raise Refused("unresolved PDF reference")
        elif isinstance(value, dict):
            for nested in cast(dict[bytes, Any], value).values():
                check_references(nested)
        elif isinstance(value, list):
            for nested in cast(list[Any], value):
                check_references(nested)

    for value in objects.values():
        check_references(value)
    check_references(trailer)
    root_reference = cast(dict[bytes, Any], trailer).get(b"/Root")
    if not isinstance(root_reference, tuple):
        raise Refused("invalid PDF catalog reference")
    root = objects.get(cast(tuple[int, int], root_reference))
    if not isinstance(root, dict) or cast(dict[bytes, Any], root).get(b"/Type") != b"/Catalog":
        raise Refused("invalid PDF catalog")
    total = 0
    decoded_streams: dict[tuple[int, int], bytes] = {}
    for identity, (info, tail) in streams.items():
        length = info.get(b"/Length")
        if isinstance(length, tuple):
            length = objects.get(cast(tuple[int, int], length))
        if type(length) is not int or not 0 <= length <= MAX_STREAM_BYTES:
            raise Refused("max_pdf_stream_bytes exceeded")
        rest = _Tokens(tail[length:])
        if rest.word() != b"endstream" or rest.word() != b"endobj":
            raise Refused("invalid PDF stream length")
        rest.space()
        if rest.pos != len(rest.data) or info.get(b"/DecodeParms") is not None:
            raise Refused("unsupported PDF stream parameters")
        if any(key in info for key in (b"/F", b"/FFilter", b"/FDecodeParms")):
            raise Refused("external PDF streams are refused")
        decoded = _decode(tail[:length], info.get(b"/Filter"))
        total += len(decoded)
        if total > MAX_STREAM_TOTAL:
            raise Refused("max_pdf_stream_total exceeded")
        decoded_streams[identity] = decoded

    lookups = 0

    def resolve(value: Any, depth: int = 0) -> Any:
        nonlocal lookups
        lookups += 1
        if depth > 32 or lookups > MAX_OBJECTS * 10:
            raise Refused("max_pdf_structure exceeded")
        if isinstance(value, tuple):
            return resolve(objects[cast(tuple[int, int], value)], depth + 1)
        return value

    def content(value: Any, depth: int = 0) -> list[bytes]:
        nonlocal lookups
        lookups += 1
        if depth > 32 or lookups > MAX_OBJECTS * 10:
            raise Refused("max_pdf_structure exceeded")
        if isinstance(value, tuple):
            reference = cast(tuple[int, int], value)
            if reference in decoded_streams:
                return [decoded_streams[reference]]
            return content(objects[reference], depth + 1)
        if isinstance(value, list):
            return [chunk for item in cast(list[Any], value) for chunk in content(item, depth + 1)]
        if value is None:
            return []
        raise Refused("invalid PDF content reference")

    groups: list[list[bytes]] = []
    for identity, (info, _) in streams.items():
        if resolve(info.get(b"/Subtype")) == b"/Form" or resolve(info.get(b"/PatternType")) == 1:
            groups.append([decoded_streams[identity]])
    pending = list(objects.values())
    while pending:
        value = pending.pop()
        if isinstance(value, list):
            pending.extend(cast(list[Any], value))
        if not isinstance(value, dict):
            continue
        info = cast(dict[bytes, Any], value)
        pending.extend(info.values())
        if resolve(info.get(b"/Type")) == b"/Page":
            groups.append(content(info.get(b"/Contents")))
        procedures = resolve(info.get(b"/CharProcs"))
        if isinstance(procedures, dict):
            groups.extend(content(item) for item in cast(dict[bytes, Any], procedures).values())
        appearances = resolve(info.get(b"/AP"))
        if isinstance(appearances, dict):
            for key in (b"/N", b"/R", b"/D"):
                appearance = cast(dict[bytes, Any], appearances).get(key)
                if isinstance(appearance, tuple) and appearance in decoded_streams:
                    groups.append(content(appearance))
                else:
                    states = resolve(appearance)
                    if isinstance(states, dict):
                        groups.extend(
                            content(item) for item in cast(dict[bytes, Any], states).values()
                        )
                    elif states is not None:
                        raise Refused("invalid PDF appearance reference")
    scanned = 0
    for chunks in groups:
        scanned += sum(map(len, chunks))
        if scanned > MAX_STREAM_TOTAL:
            raise Refused("max_pdf_content_total exceeded")
        if _has_inline_image(b"".join(chunks)):
            raise Refused("inline PDF images are refused")

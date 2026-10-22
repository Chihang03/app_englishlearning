"""Read a named top-level JSON array with memory bounded by one item."""
from __future__ import annotations

import json
import io
from pathlib import Path
from typing import Iterator, TextIO, Any


class _Reader:
    def __init__(self, source: TextIO):
        self.source = source
        self.buffer = ""
        self.position = 0
        self.eof = False
        self.decoder = json.JSONDecoder()

    def fill(self) -> bool:
        self.buffer = self.buffer[self.position:]
        self.position = 0
        chunk = self.source.read(65536)
        self.buffer += chunk
        self.eof = not chunk
        return bool(chunk)

    def peek(self) -> str:
        while True:
            while self.position < len(self.buffer):
                character = self.buffer[self.position]
                if character not in " \r\n\t":
                    return character
                self.position += 1
            if self.eof or not self.fill():
                return ""

    def expect(self, character: str) -> None:
        if self.peek() != character:
            raise ValueError(f"Expected JSON {character!r}")
        self.position += 1

    def value(self) -> Any:
        self.peek()
        while True:
            try:
                value, end = self.decoder.raw_decode(self.buffer, self.position)
                # A number may end exactly at a read boundary and continue.
                if end == len(self.buffer) and not self.eof:
                    self.fill()
                    continue
                if end < len(self.buffer) and self.buffer[end] not in " \r\n\t,:]}":
                    if not self.eof:
                        self.fill()
                        continue
                    raise ValueError("Invalid JSON value")
                self.position = end
                return value
            except json.JSONDecodeError:
                if self.eof:
                    raise
                self.fill()

    def array(self) -> Iterator[Any]:
        self.expect("[")
        if self.peek() != "]":
            while True:
                yield self.value()
                if self.peek() != ",":
                    break
                self.expect(",")
        self.expect("]")

    def canonical(self) -> Iterator[bytes]:
        character = self.peek()
        if character == "{":
            self.expect("{")
            yield b"{"
            if self.peek() != "}":
                while True:
                    key = self.value()
                    if not isinstance(key, str):
                        raise ValueError("JSON object keys must be strings")
                    yield json.dumps(key, ensure_ascii=False).encode("utf-8")
                    self.expect(":")
                    yield b":"
                    yield from self.canonical()
                    if self.peek() != ",":
                        break
                    self.expect(",")
                    yield b","
            self.expect("}")
            yield b"}"
        elif character == "[":
            yield b"["
            for index, item in enumerate(self.array()):
                if index:
                    yield b","
                yield json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            yield b"]"
        else:
            yield json.dumps(self.value(), ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _items(path: Path, key: str, metadata: dict) -> Iterator[Any]:
    with path.open(encoding="utf-8") as source:
        reader = _Reader(source)
        reader.expect("{")
        found = False
        if reader.peek() != "}":
            while True:
                name = reader.value()
                if not isinstance(name, str):
                    raise ValueError("JSON object keys must be strings")
                reader.expect(":")
                if name == key:
                    if found:
                        raise ValueError(f"Duplicate JSON array {key!r}")
                    found = True
                    yield from reader.array()
                else:
                    metadata[name] = reader.value()
                if reader.peek() != ",":
                    break
                reader.expect(",")
        reader.expect("}")
        if reader.peek():
            raise ValueError("Unexpected data after JSON object")
        if not found:
            raise ValueError(f"Missing JSON array {key!r}")


def array_items(path: Path, key: str) -> Iterator[Any]:
    yield from _items(path, key, {})


def object_metadata(path: Path, array_key: str) -> dict:
    metadata: dict = {}
    for _ in _items(path, array_key, metadata):
        pass
    return metadata


def canonical_chunks(document: bytes) -> Iterator[bytes]:
    if not document:
        return
    with io.TextIOWrapper(io.BytesIO(document), encoding="utf-8-sig") as source:
        reader = _Reader(source)
        yield from reader.canonical()
        if reader.peek():
            raise ValueError("Unexpected data after JSON document")

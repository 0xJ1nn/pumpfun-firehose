"""Anchor IDL loading and Borsh event decoding.

pump.fun publishes the IDLs for the bonding-curve program and the PumpSwap AMM.
Loading them at runtime means PumpFire can decode **every** event the programs
emit, not only the three the live book needs. New events become available by
refreshing the JSON files; no decoder code change is required.
"""

from __future__ import annotations

import hashlib
import json
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .base58 import b58encode

_IDL_DIR = Path(__file__).parent / "idl"
_MAX_VEC = 100_000


class DecodeError(ValueError):
    """The payload does not match the IDL layout."""


class Reader:
    """A bounds-checked little-endian Borsh reader."""

    __slots__ = ("data", "offset")

    def __init__(self, data: bytes, offset: int = 0) -> None:
        self.data = data
        self.offset = offset

    @property
    def remaining(self) -> int:
        return len(self.data) - self.offset

    def take(self, size: int) -> bytes:
        end = self.offset + size
        if size < 0 or end > len(self.data):
            raise DecodeError("payload ended early")
        chunk = self.data[self.offset : end]
        self.offset = end
        return chunk

    def u8(self) -> int:
        return self.take(1)[0]

    def u16(self) -> int:
        return int(struct.unpack("<H", self.take(2))[0])

    def u32(self) -> int:
        return int(struct.unpack("<I", self.take(4))[0])

    def u64(self) -> int:
        return int(struct.unpack("<Q", self.take(8))[0])

    def u128(self) -> int:
        return int.from_bytes(self.take(16), "little", signed=False)

    def i8(self) -> int:
        return int(struct.unpack("<b", self.take(1))[0])

    def i16(self) -> int:
        return int(struct.unpack("<h", self.take(2))[0])

    def i32(self) -> int:
        return int(struct.unpack("<i", self.take(4))[0])

    def i64(self) -> int:
        return int(struct.unpack("<q", self.take(8))[0])

    def i128(self) -> int:
        return int.from_bytes(self.take(16), "little", signed=True)

    def f32(self) -> float:
        return float(struct.unpack("<f", self.take(4))[0])

    def f64(self) -> float:
        return float(struct.unpack("<d", self.take(8))[0])

    def boolean(self) -> bool:
        return self.u8() != 0

    def string(self) -> str:
        length = self.u32()
        if length > _MAX_VEC:
            raise DecodeError("string is too long")
        return self.take(length).decode("utf-8", "replace")

    def pubkey(self) -> str:
        return b58encode(self.take(32))

    def bytes(self) -> str:
        length = self.u32()
        if length > _MAX_VEC:
            raise DecodeError("byte array is too long")
        return self.take(length).hex()


_PRIMITIVES = {
    "bool": "boolean",
    "u8": "u8",
    "u16": "u16",
    "u32": "u32",
    "u64": "u64",
    "u128": "u128",
    "i8": "i8",
    "i16": "i16",
    "i32": "i32",
    "i64": "i64",
    "i128": "i128",
    "f32": "f32",
    "f64": "f64",
    "string": "string",
    "pubkey": "pubkey",
    "publicKey": "pubkey",
    "bytes": "bytes",
}


def decode_value(reader: Reader, spec: Any, types: dict[str, Any]) -> Any:
    if isinstance(spec, str):
        method = _PRIMITIVES.get(spec)
        if method is None:
            raise DecodeError(f"unsupported primitive type: {spec}")
        return getattr(reader, method)()
    if isinstance(spec, list):
        # Some IDLs describe fixed arrays as [type, length].
        if len(spec) == 2 and isinstance(spec[1], int):
            return [decode_value(reader, spec[0], types) for _ in range(spec[1])]
        raise DecodeError("unsupported array spec")
    if not isinstance(spec, dict):
        raise DecodeError(f"unsupported type spec: {spec!r}")
    if "defined" in spec:
        defined = spec["defined"]
        name = defined.get("name") if isinstance(defined, dict) else defined
        if not isinstance(name, str):
            raise DecodeError("invalid defined type")
        return decode_defined(reader, name, types)
    if "option" in spec or "coption" in spec:
        inner = spec.get("option", spec.get("coption"))
        present = reader.u8()
        if present == 0:
            return None
        if present != 1:
            raise DecodeError("invalid option tag")
        return decode_value(reader, inner, types)
    if "vec" in spec:
        length = reader.u32()
        if length > _MAX_VEC:
            raise DecodeError("vector is too long")
        return [decode_value(reader, spec["vec"], types) for _ in range(length)]
    if "array" in spec:
        array = spec["array"]
        if isinstance(array, list) and len(array) == 2 and isinstance(array[1], int):
            return [decode_value(reader, array[0], types) for _ in range(array[1])]
        raise DecodeError("invalid array spec")
    raise DecodeError(f"unsupported type spec: {spec!r}")


def decode_fields(reader: Reader, fields: list[Any], types: dict[str, Any]) -> Any:
    if not fields:
        return {}
    if isinstance(fields[0], dict) and "name" in fields[0]:
        return {
            field["name"]: decode_value(reader, field.get("type"), types)
            for field in fields
        }
    return [decode_value(reader, field, types) for field in fields]


def decode_defined(reader: Reader, name: str, types: dict[str, Any]) -> Any:
    typedef = types.get(name)
    if typedef is None:
        raise DecodeError(f"IDL has no type named {name}")
    body = typedef.get("type") or typedef
    kind = body.get("kind")
    if kind == "struct":
        return decode_fields(reader, body.get("fields") or [], types)
    if kind == "enum":
        index = reader.u8()
        variants = body.get("variants") or []
        if index >= len(variants):
            raise DecodeError(f"enum {name} has no variant {index}")
        variant = variants[index]
        fields = variant.get("fields") or []
        if not fields:
            return {"variant": variant.get("name")}
        if isinstance(fields[0], dict) and "name" in fields[0]:
            values = decode_fields(reader, fields, types)
        else:
            values = [decode_value(reader, field, types) for field in fields]
        return {"variant": variant.get("name"), "fields": values}
    if kind == "alias":
        return decode_value(reader, body.get("value"), types)
    raise DecodeError(f"unsupported defined type kind: {kind}")


def event_discriminator(name: str) -> bytes:
    return hashlib.sha256(f"event:{name}".encode()).digest()[:8]


@dataclass(slots=True)
class EventDef:
    name: str
    discriminator: bytes
    fields: list[Any] | None = None


@dataclass(slots=True)
class DecodedEvent:
    program: str
    name: str
    fields: Any
    extra_bytes: int
    discriminator: str


class Idl:
    def __init__(self, name: str, address: str | None, events: dict[bytes, EventDef], types: dict[str, Any]):
        self.name = name
        self.address = address
        self.events = events
        self.types = types

    @classmethod
    def from_file(cls, path: Path) -> Idl:
        doc = json.loads(path.read_text(encoding="utf-8"))
        metadata = doc.get("metadata") or {}
        name = metadata.get("name") or path.stem
        address = doc.get("address") or metadata.get("address")
        types = {item["name"]: item for item in doc.get("types") or []}
        events: dict[bytes, EventDef] = {}
        for item in doc.get("events") or []:
            event_name = item["name"]
            computed = event_discriminator(event_name)
            listed = item.get("discriminator")
            discriminator = bytes(listed) if listed is not None else computed
            event = EventDef(event_name, discriminator, item.get("fields"))
            events[discriminator] = event
            if discriminator != computed:
                # Accept both the published tag and the Anchor-derived tag.
                events.setdefault(computed, event)
        return cls(name, address, events, types)

    def decode(self, data: bytes) -> DecodedEvent | None:
        if len(data) < 8:
            return None
        event = self.events.get(data[:8])
        if event is None:
            return None
        reader = Reader(data, 8)
        if event.fields is not None:
            fields = decode_fields(reader, event.fields, self.types)
        else:
            fields = decode_defined(reader, event.name, self.types)
        return DecodedEvent(
            program=self.name,
            name=event.name,
            fields=fields,
            extra_bytes=reader.remaining,
            discriminator=data[:8].hex(),
        )


class IdlRegistry:
    """All bundled IDLs, address-aware and discriminator-aware."""

    def __init__(self, idls: dict[str, Idl]):
        self.idls = idls
        self.by_name = dict(idls)
        self.by_address = {idl.address: idl.name for idl in idls.values() if idl.address}

    @classmethod
    def from_dir(cls, directory: Path = _IDL_DIR) -> IdlRegistry:
        idls: dict[str, Idl] = {}
        for path in sorted(directory.glob("*.json")):
            idl = Idl.from_file(path)
            idls[idl.name] = idl
        return cls(idls)

    def address_for(self, program: str) -> str | None:
        idl = self.by_name.get(program)
        return idl.address if idl else None

    def decode(self, program: str | None, data: bytes) -> DecodedEvent | None:
        if program:
            name = self.by_address.get(program, program)
            idl = self.by_name.get(name)
            if idl is not None:
                return idl.decode(data)
        for idl in self.idls.values():
            decoded = idl.decode(data)
            if decoded is not None:
                return decoded
        return None

    def decode_any(self, data: bytes) -> DecodedEvent | None:
        return self.decode(None, data)

    def summary(self) -> dict[str, Any]:
        return {
            name: {
                "address": idl.address,
                "events": len({event.name for event in idl.events.values()}),
                "types": len(idl.types),
            }
            for name, idl in self.idls.items()
        }


_REGISTRY: IdlRegistry | None = None


def default_registry() -> IdlRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = IdlRegistry.from_dir()
    return _REGISTRY

"""Binary WebSocket frames used by MAX Web (api.oneme.ru).

The header and integer extension are described in WearOSMaxClient's public
protocol notes (github.com/kopley8-jpg/WearOSMaxClient, src/shared/max).
"""
from __future__ import annotations

import struct

import lz4.block
import msgpack

VERSION = 10
HEADER = struct.Struct(">BBHHI")
LENGTH_MASK = (1 << 24) - 1
MAX_DECODED_BYTES = 32 * 1024 * 1024
LONG_FIELDS = {"chatId", "chatIds", "messageId", "messageIds", "cid",
               "fileId", "photoId", "videoId", "audioId"}


def _wire_value(value, field=None):
    if isinstance(value, dict):
        return {key: _wire_value(item, key) for key, item in value.items()}
    if isinstance(value, list):
        return [_wire_value(item, field) for item in value]
    if field in LONG_FIELDS and value is not None:
        # The web service expects these IDs as signed 64-bit scalar extensions.
        return msgpack.ExtType(1, struct.pack(">Bq", 0xd3, int(value)))
    return value


def _decode_extension(code: int, raw: bytes) -> int:
    # An extended integer contains one MessagePack integer, including its tag.
    if code != 1 or not 1 <= len(raw) <= 9:
        raise ValueError("Unsupported MAX extension")
    value = msgpack.unpackb(raw)
    if type(value) is not int:
        raise ValueError("Invalid MAX integer")
    return value


def _map_keys(values: dict) -> dict:
    # The former JSON transport exposed ID-keyed maps (e.g. messages) as strings.
    return {str(k) if type(k) is int else k: v for k, v in values.items()}


def encode_frame(frame: dict) -> bytes:
    payload = frame["payload"]
    body = b"" if payload is None else msgpack.packb(_wire_value(payload), use_bin_type=True)
    if len(body) > LENGTH_MASK:
        raise ValueError("MAX frame is too large")
    # An uncompressed block is valid at any size. Compress only when worthwhile.
    factor = 0
    if len(body) > 32:
        compressed = lz4.block.compress(body, store_size=False)
        if len(compressed) < len(body):
            factor = min(255, (len(body) + len(compressed) - 1) // len(compressed))
            body = compressed
    length = (factor << 24) | len(body)
    return HEADER.pack(VERSION, frame["cmd"], frame["seq"], frame["opcode"], length) + body


def decode_frame(raw: bytes) -> dict:
    if not isinstance(raw, bytes) or len(raw) < HEADER.size:
        raise ValueError("Incomplete MAX binary frame")
    version, cmd, seq, opcode, packed_length = HEADER.unpack_from(raw)
    size = packed_length & LENGTH_MASK
    if version != VERSION or size != len(raw) - HEADER.size:
        raise ValueError("Invalid MAX frame header")
    body = raw[HEADER.size:]
    factor = packed_length >> 24
    if factor:
        capacity = min(MAX_DECODED_BYTES, size * factor)
        if not capacity:
            raise ValueError("Empty compressed MAX frame")
        body = lz4.block.decompress(body, uncompressed_size=capacity)
    payload = msgpack.unpackb(body, raw=False, strict_map_key=False, ext_hook=_decode_extension,
                              object_hook=_map_keys) if body else None
    return {"ver": version, "cmd": cmd, "seq": seq, "opcode": opcode, "payload": payload}

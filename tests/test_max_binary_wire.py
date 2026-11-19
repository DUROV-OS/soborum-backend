import struct
import socket
from types import SimpleNamespace

import lz4.block
import msgpack
import pytest

from app.max import client, wire
from app.max.client import MaxSession
from app.max.listener import FrameHandler


def test_header_and_push_ack_keep_int64_ids():
    signed = -(2**54 + 23)
    raw = wire.encode_frame({"cmd": 1, "seq": 65535, "opcode": 128,
                             "payload": {"chatId": signed, "messageId": str(2**58 + 7)}})
    assert raw[:6] == bytes.fromhex("0a01ffff0080")
    meta = struct.unpack_from(">I", raw, 6)[0]
    factor, size = divmod(meta, 1 << 24)
    body = lz4.block.decompress(raw[10:], uncompressed_size=size * factor) if factor else raw[10:]
    fields = msgpack.unpackb(body)
    assert fields["chatId"] == msgpack.ExtType(1, b"\xd3" + signed.to_bytes(8, "big", signed=True))
    assert wire.decode_frame(raw)["payload"] == {"chatId": signed, "messageId": 2**58 + 7}


def test_compressed_provider_reply_keeps_numeric_map_keys_and_short_extended_numbers():
    body = msgpack.packb({"messages": {32: [{"id": msgpack.ExtType(1, b"\xd2\x00\x00\x00\x7b")}]},
                          "text": "x" * 600})
    compressed = lz4.block.compress(body, store_size=False)
    factor = (len(body) + len(compressed) - 1) // len(compressed)
    data = struct.pack(">BBHHI", 10, 1, 19, 49, factor << 24 | len(compressed)) + compressed
    assert wire.decode_frame(data)["payload"]["messages"]["32"][0]["id"] == 123


@pytest.mark.parametrize("raw", [b"", b"{}", bytes.fromhex("0a010001000600000007"),
                                  bytes.fromhex("09010001000600000000")])
def test_invalid_frame_fails_before_authentication(raw):
    with pytest.raises(ValueError):
        wire.decode_frame(raw)


def test_sequence_wrap_and_keepalive_reply():
    frames = []
    session = MaxSession("")
    session.seq = 65535
    session.send_frame = frames.append
    assert session._send(1, {"interactive": False}) == 0
    handler = FrameHandler(frames.append)
    handler.handle({"ver": 10, "cmd": 0, "seq": 65535, "opcode": 1, "payload": None})
    handler.handle({"ver": 10, "cmd": 0, "seq": 0, "opcode": 1, "payload": None})
    assert [f["seq"] for f in frames] == [0, 65535, 0]


def test_connection_tries_second_dns_address_and_verifies_original_host(monkeypatch):
    attempts = []
    dns_ips = ["192.0.2.1", "192.0.2.2"]

    class Socket:
        def __init__(self, *args): pass
        def __enter__(self): return self
        def __exit__(self, *args): self.close()
        def close(self): pass
        def settimeout(self, seconds): pass
        def connect(self, address):
            attempts.append(address)
            if address[0] == dns_ips[0]:
                raise TimeoutError

    hosts = []
    def wrap(tcp, *, server_hostname):
        hosts.append(server_hostname)
        return "verified TLS socket"

    addresses = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, 443)) for ip in dns_ips]
    monkeypatch.setattr(client.urllib.request, "getproxies", lambda: {})
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args: addresses)
    monkeypatch.setattr(socket, "socket", Socket)
    monkeypatch.setattr(client.ssl, "create_default_context", lambda: SimpleNamespace(wrap_socket=wrap))
    assert client._service_socket() == "verified TLS socket"
    assert attempts == [(ip, 443) for ip in dns_ips] and hosts == ["api.oneme.ru"]


def test_configured_proxy_stays_in_use(monkeypatch):
    monkeypatch.setattr(client.urllib.request, "getproxies", lambda: {"https": "http://proxy.test"})
    assert client._service_socket() is None

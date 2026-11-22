import base64
import hashlib
import zlib
from collections import Counter

import pytest
import rlp
from eth_hash.auto import keccak

from scripts.capture_robinhood_nitro_feed import (
    CUSTOM_EXTENSION,
    GUID,
    decode_batch,
    decompress,
    validate_handshake,
)


def handshake_bytes(nonce, extra_header=b""):
    accept = base64.b64encode(hashlib.sha1(nonce + GUID, usedforsecurity=False).digest())
    return (
        b"HTTP/1.1 101 Switching Protocols\r\n"
        b"Upgrade: websocket\r\n"
        b"Connection: upgrade\r\n"
        b"Sec-WebSocket-Accept: " + accept + b"\r\n"
        b"Sec-WebSocket-Extensions: Arbitrum-permessage-deflate; server_no_context_takeover; "
        b"client_no_context_takeover\r\n" + extra_header + b"\r\n"
    )


def test_nitro_handshake_accepts_optional_headers_missing_and_rejects_wrong_chain():
    nonce = base64.b64encode(b"fixed-test-nonce")
    result = validate_handshake(handshake_bytes(nonce), nonce)
    assert result["compression"] == CUSTOM_EXTENSION.lower()
    assert result["nitro_chain_header_present"] is False
    assert result["nitro_server_header_present"] is False

    with pytest.raises(ValueError, match="arbitrum-chain-id"):
        validate_handshake(handshake_bytes(nonce, b"Arbitrum-Chain-Id: 46630\r\n"), nonce)


def test_nitro_deflate_uses_static_dictionary_and_fresh_context():
    dictionary = bytes(range(1, 240)) * 20
    payloads = [dictionary[-100:], dictionary[-110:-10]]
    for payload in payloads:
        compressor = zlib.compressobj(wbits=-15, zdict=dictionary)
        wire = compressor.compress(payload) + compressor.flush(zlib.Z_SYNC_FLUSH)
        assert wire[-4:] == b"\0\0\xff\xff"
        assert decompress(wire[:-4], dictionary) == payload
        with pytest.raises(zlib.error, match="distance"):
            decompress(wire[:-4], None)


def test_nitro_l2_batch_hashes_signed_envelopes_preserving_order():
    address, calldata = bytes(range(20)), b"\x12\x34\x56\x78"
    signed = b"\x02" + rlp.encode([4663, 0, 1, 2, 21000, address, 0, calldata, [], 0, 3, 4])
    message = b"\x04" + signed
    batch_prefix = b"\x03\x00\x00\x00\x00\x00\x00\x00\x01\x06"
    batch = batch_prefix + len(message).to_bytes(8, "big") + message
    kinds = Counter()
    assert decode_batch(batch, kinds) == [
        {
            "hash": "0x" + keccak(signed).hex(),
            "transaction_type": 2,
            "to": "0x" + address.hex(),
            "selector": "0x12345678",
        }
    ]
    assert kinds == {3: 1, 6: 1, 4: 1}
    with pytest.raises(ValueError, match="truncated L2 batch item"):
        decode_batch(batch[:-1], Counter())

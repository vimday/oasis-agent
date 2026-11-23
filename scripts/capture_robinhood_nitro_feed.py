"""One bounded public Robinhood Nitro feed capture and independent offline decode."""

import argparse
import base64
import hashlib
import json
import os
import re
import socket
import ssl
import time
import zlib
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path

import certifi
import httpx
import rlp
from eth_hash.auto import keccak

from oasis_agent.arbitrage.cycle_evidence import read_bounded
from oasis_agent.evidence.robinhood_nitro import FeedMessage
from oasis_agent.models.common import normalize_hash32
from scripts.capture_public_rpc import durable_new

HOST = "feed.mainnet.chain.robinhood.com"
SOURCE = f"wss://{HOST}"
FEED_HOSTS = {"primary": HOST, "delayed": "delayed-feed.mainnet.chain.robinhood.com"}
PAIR_SCHEMA = "oasis-robinhood-nitro-feed-comparison-v1"
CHAIN_ID = 4663
START_SECONDS = 7
READ_SECONDS = 25
MAX_FRAMES = 144
MAX_PAYLOAD = 2 * 1024 * 1024
MAX_TOTAL = 12 * 1024 * 1024
MAX_HEADERS = 16 * 1024
GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
CUSTOM_EXTENSION = "Arbitrum-permessage-deflate"
STANDARD_EXTENSION = "permessage-deflate"
PARAMS = {"server_no_context_takeover", "client_no_context_takeover"}
EXPECTED_DICT_HASH = "d96272e5e58bd299c3466bc2c519e210afd192d897a4456c4c88322f8d563fa9"
NITRO_REF = "3599aca"
SOURCES = {
    "dictionary.go": (
        f"https://raw.githubusercontent.com/OffchainLabs/nitro/{NITRO_REF}/wsbroadcastserver/dictionary.go"
    ),
    "robinhood-chain-info.json": (
        "https://cdn.robinhood.com/assets/generated_assets/hoodchain_docsite/"
        "chain-node-configs/robinhood-chain-info.json"
    ),
}
MAX_SOURCE = 256 * 1024
REPO_ROOT = Path(__file__).resolve().parents[1]


def utc_now():
    return datetime.now(UTC).isoformat()


def stamp():
    return {"utc": utc_now(), "monotonic_ns": time.monotonic_ns()}


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def json_new(path, value):
    durable_new(path, (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode())


def unique_keys(pairs):
    values = {}
    for key, value in pairs:
        if key in values:
            raise ValueError(f"duplicate JSON field: {key}")
        values[key] = value
    return values


def load_json(raw):
    return json.loads(raw.decode("utf-8"), object_pairs_hook=unique_keys)


def uint(value):
    if type(value) is not int or not 0 <= value < 2**64:
        raise ValueError("expected uint64")
    return value


def source_config(sources):
    provenance = load_json(read_bounded(sources / "sources.json", MAX_SOURCE))
    config_name = "robinhood-chain-info.json"
    raw_config = read_bounded(sources / config_name, MAX_SOURCE)
    dictionary_go = read_bounded(sources / "dictionary.go", MAX_SOURCE)
    for name, url in SOURCES.items():
        if provenance[name]["source"] != url:
            raise ValueError(f"unexpected official source URL for {name}")
    if sha(raw_config) != provenance[config_name]["sha256"]:
        raise ValueError("official config SHA mismatch")
    if sha(dictionary_go) != provenance["dictionary.go"]["sha256"]:
        raise ValueError("pinned Nitro dictionary source SHA mismatch")
    rows = load_json(raw_config)
    if (
        len(rows) != 1
        or rows[0]["chain-id"] != CHAIN_ID
        or rows[0]["parent-chain-id"] != 1
        or rows[0]["chain-config"]["chainId"] != CHAIN_ID
    ):
        raise ValueError("unexpected Robinhood chain configuration")
    offset = uint(rows[0]["chain-config"]["arbitrum"]["GenesisBlockNum"])
    dictionary_match = re.search(rb"return \[\]byte\(`([^`]*)`\)", dictionary_go)
    if dictionary_match is None:
        raise ValueError("Nitro dictionary source has unexpected syntax")
    dictionary = dictionary_match.group(1)
    if sha(dictionary) != EXPECTED_DICT_HASH:
        raise ValueError("dictionary bytes SHA mismatch")
    return offset, dictionary, sha(raw_config)


class FeedReader:
    def __init__(self, connection):
        self.connection = connection
        self.pending = bytearray()
        self.partial = bytearray()

    def more(self, deadline, max_read=64 * 1024):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("sample deadline")
        self.connection.settimeout(remaining)
        piece = self.connection.recv(min(max_read, 64 * 1024))
        if not piece:
            raise EOFError("feed ended")
        self.pending.extend(piece)

    def headers(self, deadline):
        while True:
            end = self.pending.find(b"\r\n\r\n")
            if end != -1 and end + 4 <= MAX_HEADERS:
                result = bytes(self.pending[: end + 4])
                del self.pending[: end + 4]
                return result
            if len(self.pending) >= MAX_HEADERS:
                raise ValueError("handshake headers over cap")
            self.more(deadline, MAX_HEADERS - len(self.pending))

    def exact(self, count, deadline):
        while len(self.pending) < count:
            self.more(deadline, count - len(self.pending))
        out = bytes(self.pending[:count])
        del self.pending[:count]
        self.partial.extend(out)
        return out

    def frame(self, deadline, remaining_total):
        self.partial.clear()
        if remaining_total < 2:
            raise ValueError("sample wire bytes cap reached")
        first, second = self.exact(2, deadline)
        if first & 0x30 or second & 0x80:
            raise ValueError("unexpected RSV bits or masked upstream frame")
        opcode, size = first & 0x0F, second & 0x7F
        if size in (126, 127):
            length_size = 2 if size == 126 else 8
            if remaining_total < 2 + length_size:
                raise ValueError("sample wire bytes cap reached")
            size = int.from_bytes(self.exact(length_size, deadline), "big")
            minimum = 126 if length_size == 2 else 65536
            if size < minimum:
                raise ValueError("non-minimal frame length")
        if size > MAX_PAYLOAD:
            raise ValueError("frame payload over cap")
        if len(self.partial) + size > remaining_total:
            raise ValueError("remaining wire bytes cap would be exceeded")
        if opcode not in (0, 1, 2, 8, 9, 10):
            raise ValueError("unknown WebSocket opcode")
        if opcode >= 8 and (size > 125 or first & 0xC0 != 0x80):
            raise ValueError("invalid WebSocket control frame")
        content = self.exact(size, deadline)
        return first, content, bytes(self.partial)


def handshake(root, reader, nonce, deadline, *, host=HOST):
    offer = "; server_no_context_takeover; client_no_context_takeover"
    request = (
        f"GET / HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {nonce.decode()}\r\nSec-WebSocket-Version: 13\r\n"
        f"Sec-WebSocket-Extensions: {CUSTOM_EXTENSION}{offer}, {STANDARD_EXTENSION}{offer}\r\n"
        "Arbitrum-Feed-Client-Version: 2\r\n\r\n"
    ).encode("ascii")
    durable_new(root / "handshake.request.bin", request)
    reader.connection.sendall(request)
    response = reader.headers(deadline)
    durable_new(root / "handshake.response.bin", response)
    return validate_handshake(response, nonce)


def validate_handshake(response, nonce):
    status, *headers = response.decode("latin1").split("\r\n")
    status_parts = status.split(maxsplit=2)
    if len(status_parts) < 2 or status_parts[0] != "HTTP/1.1":
        raise ValueError("invalid WebSocket HTTP response status")
    result = {"status": int(status_parts[1]), "headers": {}}
    parsed = result["headers"]
    for line in headers:
        if line:
            name, value = line.split(":", 1)
            key = name.lower()
            parsed[key] = f"{parsed[key]}, {value.strip()}" if key in parsed else value.strip()
    if result["status"] != 101:
        raise ValueError(f"WebSocket HTTP {result['status']}")
    accept = base64.b64encode(hashlib.sha1(nonce + GUID, usedforsecurity=False).digest()).decode()
    if parsed.get("sec-websocket-accept") != accept:
        raise ValueError("invalid WebSocket accept")
    if parsed.get("upgrade", "").lower() != "websocket" or "upgrade" not in parsed.get(
        "connection", ""
    ).lower().replace(" ", "").split(","):
        raise ValueError("wrong WebSocket upgrade headers")
    # The official client treats these headers as optional by default. The public
    # TLS proxy may omit them, so a separate block lookup is required for mapping.
    for key, expected in {
        "arbitrum-chain-id": str(CHAIN_ID),
        "arbitrum-feed-server-version": "2",
    }.items():
        if key in parsed and parsed[key] != expected:
            raise ValueError(f"unexpected optional handshake field: {key}")
    result["nitro_chain_header_present"] = "arbitrum-chain-id" in parsed
    result["nitro_server_header_present"] = "arbitrum-feed-server-version" in parsed
    extension = parsed.get("sec-websocket-extensions", "")
    parts = [part.strip().lower() for part in extension.split(";")]
    if parts[0] in (CUSTOM_EXTENSION.lower(), STANDARD_EXTENSION):
        if set(parts[1:]) != PARAMS or len(parts) != 3:
            raise ValueError("unsupported negotiated compression params")
    elif parts != [""]:
        raise ValueError("unsupported negotiated extension")
    result["compression"] = parts[0] or "none"
    return result


def send_pong(connection, content, deadline):
    mask = os.urandom(4)
    masked = bytes(value ^ mask[i % 4] for i, value in enumerate(content))
    connection.settimeout(max(0.01, deadline - time.monotonic()))
    connection.sendall(bytes((0x8A, 0x80 | len(content))) + mask + masked)


def capture(root, sources, *, host=HOST):
    if host not in FEED_HOSTS.values():
        raise ValueError("unknown Robinhood feed host")
    if any((root / name).exists() for name in ("scope.json", "STARTED", "STOP")):
        raise ValueError("original WebSocket scope already started")
    offset, _, config_sha = source_config(sources)
    scope = {
        "schema": "oasis-robinhood-nitro-forward-v1",
        "opened": stamp(),
        "endpoint": f"wss://{host}",
        "max_connections": 1,
        "max_start_seconds": START_SECONDS,
        "max_read_seconds": READ_SECONDS,
        "max_frames": MAX_FRAMES,
        "max_frame_payload_bytes": MAX_PAYLOAD,
        "max_total_bytes": MAX_TOTAL,
        "requested_sequence": None,
        "retry": False,
        "cost_usd": 0,
        "chain_id": CHAIN_ID,
        "genesis_block_offset": offset,
        "chain_config_sha256": config_sha,
        "nitro_source_ref": NITRO_REF,
        "dictionary_sha256": EXPECTED_DICT_HASH,
        "source_manifest_sha256": sha((sources / "sources.json").read_bytes()),
        "collector_sha256": sha(Path(__file__).read_bytes()),
        "documentation": "https://docs.robinhood.com/chain/connecting/",
        "execution_eligible": False,
    }
    json_new(root / "scope.json", scope)
    (root / "frames").mkdir()
    durable_new(root / "STARTED", b"one WebSocket connection reserved; no retries\n")
    context = ssl.create_default_context(cafile=certifi.where())
    frames, received_bytes, transport, stop = [], 0, {}, "unknown"
    handshake_complete, reader, connected = None, None, None
    deadline = time.monotonic() + START_SECONDS
    try:
        with socket.create_connection((host, 443), timeout=START_SECONDS) as tcp:
            tcp.settimeout(max(0.01, deadline - time.monotonic()))
            with context.wrap_socket(tcp, server_hostname=host) as connection:
                connected = stamp()
                reader = FeedReader(connection)
                transport = {
                    "tls_version": connection.version(),
                    "tls_peer_cert_sha256": sha(connection.getpeercert(binary_form=True)),
                }
                transport.update(
                    handshake(root, reader, base64.b64encode(os.urandom(16)), deadline, host=host)
                )
                handshake_complete = stamp()
                deadline = time.monotonic() + READ_SECONDS
                for frame_index in range(1, MAX_FRAMES + 1):
                    if time.monotonic() >= deadline:
                        break
                    flags, content, wire = reader.frame(deadline, MAX_TOTAL - received_bytes)
                    received = stamp()
                    received_bytes += len(wire)
                    relative = f"frames/{frame_index:04}.ws"
                    durable_new(root / relative, wire)
                    row = {
                        "frame": frame_index,
                        "raw_frame": relative,
                        "raw_frame_sha256": sha(wire),
                        "payload_sha256": sha(content),
                        "received": received,
                        "wire_bytes": len(wire),
                        "payload_bytes": len(content),
                        "opcode": flags & 0x0F,
                        "fin": bool(flags & 0x80),
                        "rsv1": bool(flags & 0x40),
                    }
                    json_new(root / f"frames/{frame_index:04}.json", row)
                    frames.append(row)
                    if row["opcode"] == 8:
                        raise EOFError("server sent WebSocket close")
                    if row["opcode"] == 9:
                        send_pong(connection, content, deadline)
                stop = "frame_cap" if len(frames) == MAX_FRAMES else "time_cap"
    except TimeoutError as exc:
        stop = "time_cap" if handshake_complete else f"TimeoutError: {exc}"
        if reader and (reader.partial or reader.pending):
            durable_new(root / "unfinished.ws", bytes(reader.partial + reader.pending))
    except (EOFError, OSError, ValueError, KeyError) as exc:
        stop = f"{type(exc).__name__}: {str(exc)[:240]}"
        if reader and (reader.partial or reader.pending):
            durable_new(root / "unconsumed.bin", bytes(reader.partial + reader.pending))
    finally:
        done = stamp()
        raw_handshake = root / "handshake.response.bin"
        json_new(
            root / "capture.json",
            {
                "scope_sha256": sha((root / "scope.json").read_bytes()),
                "started": scope["opened"],
                "tls_connected": connected,
                "handshake_complete": handshake_complete,
                "handshake_response_sha256": sha(raw_handshake.read_bytes())
                if raw_handshake.exists()
                else None,
                "handshake_request_sha256": sha((root / "handshake.request.bin").read_bytes())
                if (root / "handshake.request.bin").exists()
                else None,
                "stopped": done,
                "reason": stop,
                "transport": transport,
                "frames": frames,
                "frames_used": len(frames),
                "bytes_received": received_bytes,
            },
        )
        json_new(root / "STOP", {"reason": stop, "attempts": 1, "requests_to_rpc": 0})
    return stop


def pick_signed(raw, typed):
    """Describe signed Ethereum transaction envelopes. Hash bytes, never log signed bytes."""
    destination, selector = None, None
    try:
        data = rlp.decode(raw[1:] if typed else raw, strict=True)
        to_idx = 3 if typed is None else 4 if typed == 1 else 5
        address, calldata = data[to_idx], data[to_idx + 2]
        destination = "0x" + address.hex() if len(address) == 20 else None
        selector = "0x" + calldata[:4].hex() if len(calldata) >= 4 else None
    except IndexError, TypeError, AttributeError, rlp.DecodingError:
        pass
    return {
        "hash": "0x" + keccak(raw).hex(),
        "transaction_type": typed or 0,
        "to": destination,
        "selector": selector,
    }


def decode_batch(raw, kinds, depth=0):
    if depth >= 16 or not raw:
        raise ValueError("invalid L2 batch subtree")
    kind = raw[0]
    kinds[kind] += 1
    if kind == 4:
        signed = raw[1:]
        if not signed or (signed[0] not in (1, 2, 3, 4) and signed[0] < 192):
            kinds["unhandled_signed_encoding"] += 1
            return []
        typed = signed[0] if signed[0] in (1, 2, 3, 4) else None
        return [pick_signed(signed, typed)]
    if kind != 3:
        return []
    signed_rows = []
    cursor = 1
    while cursor < len(raw):
        start = cursor + 8
        if start > len(raw):
            raise ValueError("truncated L2 batch item length")
        size = int.from_bytes(raw[cursor:start], "big")
        if not 0 < size <= len(raw) - start:
            raise ValueError("truncated L2 batch item")
        cursor = start + size
        signed_rows.extend(decode_batch(raw[start:cursor], kinds, depth + 1))
    return signed_rows


def decompress(payload, dictionary):
    decompressor = (
        zlib.decompressobj(wbits=-15, zdict=dictionary) if dictionary else zlib.decompressobj(-15)
    )
    decoded = decompressor.decompress(payload + b"\0\0\xff\xff", MAX_PAYLOAD + 1)
    if len(decoded) > MAX_PAYLOAD or decompressor.unconsumed_tail or decompressor.unused_data:
        raise ValueError("invalid or over-cap WebSocket inflate")
    return decoded


def decode(root, sources, *, host=HOST):
    if host not in FEED_HOSTS.values():
        raise ValueError("unknown Robinhood feed host")
    offset, dictionary, config_sha = source_config(sources)
    capture_manifest = read_bounded(root / "capture.json", MAX_SOURCE)
    capture_data = load_json(capture_manifest)
    if not capture_data["handshake_complete"]:
        raise ValueError("no verified feed handshake to replay")
    scope_raw = read_bounded(root / "scope.json", MAX_SOURCE)
    scope = load_json(scope_raw)
    if (
        sha(scope_raw) != capture_data["scope_sha256"]
        or scope["endpoint"] != f"wss://{host}"
        or scope["chain_id"] != CHAIN_ID
        or scope["genesis_block_offset"] != offset
        or scope["chain_config_sha256"] != config_sha
        or scope["source_manifest_sha256"]
        != sha(read_bounded(sources / "sources.json", MAX_SOURCE))
    ):
        raise ValueError("capture scope and source evidence disagree")
    response = read_bounded(root / "handshake.response.bin", MAX_HEADERS)
    if sha(response) != capture_data["handshake_response_sha256"]:
        raise ValueError("handshake response hash mismatch")
    request = read_bounded(root / "handshake.request.bin", MAX_HEADERS)
    if (
        sha(request) != capture_data["handshake_request_sha256"]
        or f"\r\nHost: {host}\r\n".encode() not in request
    ):
        raise ValueError("handshake request hash mismatch")
    nonce = re.search(rb"Sec-WebSocket-Key: ([^\r]+)\r\n", request).group(1)
    protocol = validate_handshake(response, nonce)["compression"]
    envelopes, ordered, confirmations, partial = [], [], [], None
    signed_kinds, header_kinds = Counter(), Counter()
    rows = capture_data["frames"]
    if len(rows) != capture_data["frames_used"] or len(rows) > MAX_FRAMES:
        raise ValueError("frame count disagrees with capture budget")
    if sum(uint(row["wire_bytes"]) for row in rows) != capture_data["bytes_received"]:
        raise ValueError("wire bytes disagree with capture")
    if capture_data["bytes_received"] > MAX_TOTAL:
        raise ValueError("wire bytes exceed capture budget")
    previous_monotonic = None
    for index, row in enumerate(rows, start=1):
        relative = f"frames/{index:04}.ws"
        if row["frame"] != index or row["raw_frame"] != relative:
            raise ValueError("unexpected frame identity or path")
        metadata = load_json(read_bounded(root / f"frames/{index:04}.json", MAX_HEADERS))
        monotonic = uint(row["received"]["monotonic_ns"])
        if metadata != row or (previous_monotonic is not None and monotonic < previous_monotonic):
            raise ValueError("frame metadata or receive clock disagrees with capture")
        previous_monotonic = monotonic
        raw = read_bounded(root / relative, MAX_PAYLOAD + 10)
        if sha(raw) != row["raw_frame_sha256"] or len(raw) != row["wire_bytes"]:
            raise ValueError("raw frame hash or length mismatch")
        size = uint(row["payload_bytes"])
        if len(raw) < 2 or raw[0] & 0x30 or raw[1] & 0x80:
            raise ValueError("truncated, masked, or reserved WebSocket header")
        header_size = {126: 4, 127: 10}.get(raw[1] & 0x7F, 2)
        declared = int.from_bytes(raw[2:header_size], "big") if header_size > 2 else raw[1] & 0x7F
        if size != declared or size > MAX_PAYLOAD or size + header_size != len(raw):
            raise ValueError("inconsistent raw frame payload length")
        data = raw[len(raw) - size :]
        opcode = row["opcode"]
        if sha(data) != row["payload_sha256"] or raw[0] & 0x0F != opcode:
            raise ValueError("payload hash or opcode mismatch")
        rsv1, fin = bool(raw[0] & 0x40), bool(raw[0] & 0x80)
        if rsv1 != row["rsv1"] or fin != row["fin"]:
            raise ValueError("raw WebSocket flags disagree")
        if opcode in (8, 9, 10):
            if size > 125 or rsv1 or not fin:
                raise ValueError("invalid WebSocket control frame")
            continue
        if opcode not in (0, 1, 2):
            raise ValueError("unknown WebSocket opcode")
        if opcode in (1, 2):
            if partial is not None:
                raise ValueError("interleaved WebSocket data messages")
            partial = {"compressed": rsv1, "wire": bytearray(), "frames": []}
        elif partial is None or rsv1:
            raise ValueError("invalid WebSocket continuation")
        partial["wire"].extend(data)
        partial["frames"].append(row["frame"])
        if len(partial["wire"]) > MAX_PAYLOAD:
            raise ValueError("concatenated message payload exceeds cap")
        if not fin:
            continue
        payload = bytes(partial["wire"])
        if partial["compressed"]:
            if protocol == "none":
                raise ValueError("compressed message without extension")
            decoded = decompress(
                payload, dictionary if protocol == CUSTOM_EXTENSION.lower() else None
            )
        else:
            decoded = payload
        envelope = load_json(decoded)
        if uint(envelope["version"]) != 1:
            raise ValueError("unsupported Nitro message version")
        sequences = []
        for message in envelope.get("messages", []):
            seq = uint(message["sequenceNumber"])
            sections = message["message"]
            body = sections["message"]
            header = body["header"]
            kind = uint(header["kind"])
            header_kinds[kind] += 1
            raw_l2 = base64.b64decode(body["l2Msg"], validate=True) if body["l2Msg"] else b""
            sign = (
                base64.b64decode(message["signatureV2"], validate=True)
                if message.get("signatureV2")
                else b""
            )
            hash_value = message.get("blockHash")
            ordered.append(
                {
                    "sequence": seq,
                    "block_by_config": seq + offset,
                    "feed_block_hash": normalize_hash32(hash_value) if hash_value else None,
                    "parent_chain_block": uint(header["blockNumber"]),
                    "feed_timestamp": uint(header["timestamp"]),
                    "incoming_kind": kind,
                    "delayed_messages_read": uint(sections["delayedMessagesRead"]),
                    "signature_v2_bytes": len(sign),
                    "l2_payload_bytes": len(raw_l2),
                    "l2_payload_sha256": sha(raw_l2),
                    "signed_eth_transactions": decode_batch(raw_l2, signed_kinds)
                    if raw_l2 and kind == 3
                    else [],
                    "frame": row["frame"],
                    "received": row["received"],
                }
            )
            sequences.append(seq)
        confirmation = envelope.get("confirmedSequenceNumberMessage")
        if confirmation is not None:
            confirmations.append(uint(confirmation["sequenceNumber"]))
        if not sequences and confirmation is None:
            raise ValueError("empty Nitro feed envelope")
        envelopes.append(
            {
                "compressed": partial["compressed"],
                "compressed_or_wire_payload_sha256": sha(payload),
                "frames": partial["frames"],
                "decoded_payload_sha256": sha(decoded),
                "wire_payload_bytes": len(payload),
                "decoded_payload_bytes": len(decoded),
                "sequences": sequences,
                "received": row["received"],
            }
        )
        partial = None
    intervals = [b["sequence"] - a["sequence"] for a, b in pairwise(ordered)]
    receive_span = (
        ordered[-1]["received"]["monotonic_ns"] - ordered[0]["received"]["monotonic_ns"]
        if ordered
        else None
    )
    summary = {
        "schema": "oasis-robinhood-nitro-replay-v1",
        "chain_id": CHAIN_ID,
        "capture_sha256": sha(capture_manifest),
        "genesis_block_offset": offset,
        "compression": protocol,
        "messages": len(ordered),
        "envelopes": len(envelopes),
        "compressed_envelopes": sum(row["compressed"] for row in envelopes),
        "first_sequence": ordered[0]["sequence"] if ordered else None,
        "last_sequence": ordered[-1]["sequence"] if ordered else None,
        "first_receive_utc": ordered[0]["received"]["utc"] if ordered else None,
        "last_receive_utc": ordered[-1]["received"]["utc"] if ordered else None,
        "receive_span_seconds": receive_span / 1e9 if receive_span is not None else None,
        "unique_sequences": len({row["sequence"] for row in ordered}),
        "forward_gap_missing": sum(distance - 1 for distance in intervals if distance > 1),
        "non_increasing_pairs": sum(distance <= 0 for distance in intervals),
        "feed_block_hashes": sum(row["feed_block_hash"] is not None for row in ordered),
        "signatures_v2_present": sum(row["signature_v2_bytes"] > 0 for row in ordered),
        "signatures_verified": 0,
        "parent_header_kinds": dict(header_kinds),
        "signed_envelope_kinds": {str(key): count for key, count in signed_kinds.items()},
        "signed_eth_hashes": sum(len(row["signed_eth_transactions"]) for row in ordered),
        "confirmation_envelopes": len(confirmations),
        "incomplete_fragment": partial is not None,
        "method_rpc_reads": 0,
        "evidence_limits": [
            "single feed source",
            "receipt/state absence",
            "no finality validation",
        ],
    }
    data = {
        "summary": summary,
        "messages": ordered,
        "envelopes": envelopes,
        "confirmations": confirmations,
    }
    return data


def capture_pair(root, sources):
    source_config(sources)
    root.mkdir(parents=True, exist_ok=False)
    scope_path = root / "pair-scope.json"
    json_new(
        scope_path,
        {
            "schema": PAIR_SCHEMA,
            "chain_id": CHAIN_ID,
            "hosts": FEED_HOSTS,
            "started_ns": time.monotonic_ns(),
            "max_connections_per_host": 1,
            "cost_usd": 0,
        },
    )
    with ThreadPoolExecutor(max_workers=len(FEED_HOSTS)) as workers:
        captures = {}
        for source, host in FEED_HOSTS.items():
            path = root / source
            path.mkdir()
            captures[source] = workers.submit(capture, path, sources, host=host)
        stops = {source: future.result() for source, future in captures.items()}
    json_new(
        root / "pair-stop.json",
        {
            "scope_sha256": sha(scope_path.read_bytes()),
            "stopped_ns": time.monotonic_ns(),
            "reasons": stops,
            "captures": {
                source: sha(read_bounded(root / source / "capture.json", MAX_SOURCE))
                for source in FEED_HOSTS
            },
        },
    )


def compare_messages(primary, delayed, *, started_ns, stopped_ns):
    def indexed(raw):
        observations = {}
        for row in raw:
            message = FeedMessage.from_row(row)
            received = uint(row["received"]["monotonic_ns"])
            if not started_ns <= received <= stopped_ns:
                raise ValueError("feed observation outside paired capture clock")
            previous = observations.get(message.sequence)
            if previous is not None:
                if previous[0] != message:
                    raise ValueError(f"feed conflicts at sequence {message.sequence}")
                received = min(received, previous[1])
            observations[message.sequence] = message, received
        return observations

    main = indexed(primary)
    backup = indexed(delayed)
    if not main or not backup:
        raise ValueError("both feeds need retained messages for comparison")
    matched = []
    unbound = 0
    for sequence in sorted(main.keys() & backup.keys()):
        first, primary_ns = main[sequence]
        second, delayed_ns = backup[sequence]
        if first.semantic_sha256 != second.semantic_sha256 or (
            first.block_hash is not None
            and second.block_hash is not None
            and first.block_hash != second.block_hash
        ):
            raise ValueError(f"feeds disagree at sequence {sequence}")
        if first.block_hash is None or second.block_hash is None:
            unbound += 1
            continue
        matched.append(
            {
                "sequence": sequence,
                "block_hash": first.block_hash,
                "primary_receive_ns": primary_ns,
                "delayed_receive_ns": delayed_ns,
                "delayed_minus_primary_ns": delayed_ns - primary_ns,
            }
        )
    first_main, last_main = min(main), max(main)
    return {
        "primary_sequences": len(main),
        "delayed_sequences": len(backup),
        "matched_same_hash": len(matched),
        "matched_without_two_hashes": unbound,
        "primary_internal_missing": last_main - first_main + 1 - len(main),
        "primary_missing_seen_on_delayed": sorted(
            sequence
            for sequence, (message, _) in backup.items()
            if first_main < sequence < last_main and sequence not in main and message.block_hash
        ),
        "pairs": matched,
    }


def decode_pair_capture(root, sources):
    raw_scope = read_bounded(root / "pair-scope.json", MAX_HEADERS)
    scope = load_json(raw_scope)
    stop = load_json(read_bounded(root / "pair-stop.json", MAX_HEADERS))
    if (
        scope["schema"] != PAIR_SCHEMA
        or scope["chain_id"] != CHAIN_ID
        or scope["hosts"] != FEED_HOSTS
        or stop["scope_sha256"] != sha(raw_scope)
        or set(stop["captures"]) != FEED_HOSTS.keys()
        or set(stop["reasons"]) != FEED_HOSTS.keys()
    ):
        raise ValueError("paired feed scope and capture identities disagree")
    feeds = {}
    for source, host in FEED_HOSTS.items():
        path = root / source
        manifest = read_bounded(path / "capture.json", MAX_SOURCE)
        if sha(manifest) != stop["captures"][source] or (
            load_json(manifest)["reason"] != stop["reasons"][source]
        ):
            raise ValueError("paired feed capture manifest changed")
        feeds[source] = decode(path, sources, host=host)
    return scope, stop, feeds


def summarize_pair_capture(scope, stop, feeds):
    return {
        "schema": PAIR_SCHEMA,
        "chain_id": CHAIN_ID,
        "scope_sha256": stop["scope_sha256"],
        "capture_sha256": stop["captures"],
        "reasons": stop["reasons"],
        **compare_messages(
            feeds["primary"]["messages"],
            feeds["delayed"]["messages"],
            started_ns=uint(scope["started_ns"]),
            stopped_ns=uint(stop["stopped_ns"]),
        ),
        "rpc_verified": False,
        "execution_eligible": False,
    }


def compare_pair(root, sources):
    return summarize_pair_capture(*decode_pair_capture(root, sources))


def prepare(source_dir):
    """Fetch only the public static inputs. No WebSocket or chain RPC is opened."""
    source_dir.mkdir(parents=True, exist_ok=True)
    if any((source_dir / name).exists() for name in (*SOURCES, "sources.json")):
        raise ValueError("source directory already contains captured inputs")
    rows = {}
    with httpx.Client(timeout=7, trust_env=False, follow_redirects=False) as client:
        for name, url in SOURCES.items():
            chunks = bytearray()
            with client.stream("GET", url, headers={"Accept-Encoding": "identity"}) as response:
                response.raise_for_status()
                for chunk in response.iter_raw():
                    if len(chunks) + len(chunk) > MAX_SOURCE:
                        raise ValueError("official source bytes exceed cap")
                    chunks.extend(chunk)
            raw = bytes(chunks)
            durable_new(source_dir / name, raw)
            rows[name] = {
                "source": url,
                "retrieved_utc": utc_now(),
                "sha256": sha(raw),
                "bytes": len(raw),
                "exit": 0,
            }
    json_new(source_dir / "sources.json", rows)
    offset, dictionary, config_hash = source_config(source_dir)
    return {
        "chain_id": CHAIN_ID,
        "genesis_block_offset": offset,
        "dictionary_sha256": sha(dictionary),
        "chain_config_sha256": config_hash,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("prepare", "capture", "decode", "capture-pair", "compare-pair")
    )
    parser.add_argument("evidence_dir", type=Path)
    parser.add_argument("--sources-dir", type=Path, help="defaults to EVIDENCE_DIR/sources")
    args = parser.parse_args()
    root = args.evidence_dir.resolve()
    sources = args.sources_dir.resolve() if args.sources_dir else root / "sources"
    if args.action == "prepare":
        print(json.dumps(prepare(sources), sort_keys=True))
        return 0
    if args.action in {"capture", "capture-pair"}:
        if root.is_relative_to(REPO_ROOT) and not root.is_relative_to(REPO_ROOT / "reports"):
            raise ValueError(
                "store raw feed frames under ignored reports/ or outside the repository"
            )
        if args.action == "capture-pair":
            capture_pair(root, sources)
            stops = load_json(read_bounded(root / "pair-stop.json", MAX_HEADERS))["reasons"]
            print(json.dumps(stops, sort_keys=True))
            return int(any(reason not in {"time_cap", "frame_cap"} for reason in stops.values()))
        root.mkdir(parents=True, exist_ok=True)
        capture(root, sources)
        recorded = load_json((root / "capture.json").read_bytes())
        print(
            json.dumps(
                {
                    key: recorded[key]
                    for key in (
                        "frames_used",
                        "handshake_complete",
                        "bytes_received",
                        "reason",
                        "transport",
                    )
                },
                sort_keys=True,
            )
        )
        return int(
            not recorded["handshake_complete"]
            or recorded["reason"] not in {"time_cap", "frame_cap"}
        )
    if args.action == "compare-pair":
        report = compare_pair(root, sources)
        print(json.dumps(report, sort_keys=True, separators=(",", ":")))
        return int(not report["matched_same_hash"])
    data = decode(root, sources)
    artifact = root / "decode.json"
    print(json.dumps(data["summary"], sort_keys=True))
    if artifact.exists():
        expected = (json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n").encode()
        if artifact.read_bytes() != expected:
            raise ValueError("replayed decode differed from saved evidence")
    else:
        json_new(artifact, data)
    return int(not data["summary"]["messages"])


if __name__ == "__main__":
    raise SystemExit(main())

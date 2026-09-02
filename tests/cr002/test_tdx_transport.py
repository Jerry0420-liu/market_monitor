from __future__ import annotations

import socket
import socketserver
import struct
from datetime import UTC, datetime, timedelta
from threading import Thread
from typing import cast

import pytest
from market_monitor_data.tdx.protocol import TdxProtocol
from market_monitor_data.tdx.transport import TdxServer, TdxServerPool, TdxTransportError


def _frame(body: bytes) -> bytes:
    return struct.pack("<IIIHH", 0, 0, 0, len(body), len(body)) + body


class _Handler(socketserver.BaseRequestHandler):
    response = b""
    requests: list[bytes] = []

    def handle(self) -> None:
        self.__class__.requests.append(self.request.recv(4096))
        self.request.sendall(self.__class__.response)


class _Server:
    def __init__(self, response: bytes, port: int = 0) -> None:
        handler = type("ReplyHandler", (_Handler,), {"response": response, "requests": []})
        self._server = socketserver.TCPServer(("127.0.0.1", port), handler)
        self._server.timeout = 0.1
        self._handler = cast(type[_Handler], handler)
        self._thread = Thread(target=self._server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def requests(self) -> list[bytes]:
        return list(self._handler.requests)

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()


class _SetupHandler(socketserver.BaseRequestHandler):
    expected: tuple[bytes, ...] = ()
    received: list[bytes] = []

    def handle(self) -> None:
        for index, expected in enumerate(self.__class__.expected):
            data = bytearray()
            while len(data) < len(expected):
                data.extend(self.request.recv(len(expected) - len(data)))
            self.__class__.received.append(bytes(data))
            self.request.sendall(
                _frame(b"answer" if index == len(self.__class__.expected) - 1 else b"setup")
            )


def _setup_server(
    expected: tuple[bytes, ...],
) -> tuple[socketserver.TCPServer, Thread, type[_SetupHandler]]:
    handler = type("SetupHandler", (_SetupHandler,), {"expected": expected, "received": []})
    server = socketserver.TCPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, handler


def _unused_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_pool_fails_over_to_healthy_node_and_records_cooldown() -> None:
    reply = _frame(b"healthy")
    server = _Server(reply)
    server.start()
    bad_port = _unused_port()
    now = datetime(2026, 8, 21, 1, tzinfo=UTC)
    try:
        pool = TdxServerPool(
            (TdxServer("127.0.0.1", bad_port), TdxServer("127.0.0.1", server.port)),
            TdxProtocol(),
            timeout_seconds=0.2,
            cooldown_seconds=30,
            clock=lambda: now,
        )

        assert pool.request(b"probe", probe=True) == b"healthy"

        failed, healthy = pool.health_snapshot()
        assert failed.consecutive_failures == 1
        assert failed.cooldown_until == now + timedelta(seconds=30)
        assert healthy.consecutive_failures == 0
        assert healthy.last_successful_probe == now
        assert server.requests == [b"probe"]
    finally:
        server.close()


def test_pool_retries_a_cooled_node_after_its_deadline() -> None:
    now = datetime(2026, 8, 21, 1, tzinfo=UTC)
    bad_port = _unused_port()
    healthy = _Server(_frame(b"fallback"))
    healthy.start()
    recovered: _Server | None = None
    try:
        pool = TdxServerPool(
            (TdxServer("127.0.0.1", bad_port), TdxServer("127.0.0.1", healthy.port)),
            TdxProtocol(),
            timeout_seconds=0.2,
            cooldown_seconds=30,
            clock=lambda: now,
        )
        assert pool.request(b"first") == b"fallback"

        recovered = _Server(_frame(b"recovered"), bad_port)
        recovered.start()
        now += timedelta(seconds=31)

        assert pool.request(b"second") == b"recovered"
        assert recovered.requests == [b"second"]
        assert pool.health_snapshot()[0].consecutive_failures == 0
    finally:
        healthy.close()
        if recovered is not None:
            recovered.close()


def test_pool_sends_setup_and_query_on_one_connection() -> None:
    server, thread, handler = _setup_server((b"setup-1", b"setup-2", b"query"))
    try:
        pool = TdxServerPool(
            (TdxServer("127.0.0.1", int(server.server_address[1])),),
            TdxProtocol(),
            timeout_seconds=0.2,
        )

        assert pool.request(b"query", setup_payloads=(b"setup-1", b"setup-2")) == b"answer"
        assert handler.received == [b"setup-1", b"setup-2", b"query"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_pool_reuses_a_healthy_connection_without_repeating_setup() -> None:
    expected = (b"setup-1", b"setup-2", b"query-1", b"query-2")

    class Handler(socketserver.BaseRequestHandler):
        connections = 0
        received: list[bytes] = []

        def handle(self) -> None:
            self.__class__.connections += 1
            for payload in expected:
                data = bytearray()
                while len(data) < len(payload):
                    chunk = self.request.recv(len(payload) - len(data))
                    if not chunk:
                        return
                    data.extend(chunk)
                self.__class__.received.append(bytes(data))
                self.request.sendall(_frame(b"ack"))

    server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        pool = TdxServerPool(
            (TdxServer("127.0.0.1", int(server.server_address[1])),),
            TdxProtocol(),
            timeout_seconds=0.2,
        )

        assert pool.request(b"query-1", setup_payloads=(b"setup-1", b"setup-2")) == b"ack"
        assert pool.request(b"query-2", setup_payloads=(b"setup-1", b"setup-2")) == b"ack"
        assert Handler.connections == 1
        assert Handler.received == list(expected)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_pool_stays_probing_until_every_representative_probe_succeeds() -> None:
    server = _Server(_frame(b"ok"))
    server.start()
    now = datetime(2026, 8, 21, 1, tzinfo=UTC)
    try:
        pool = TdxServerPool(
            (TdxServer("127.0.0.1", server.port),),
            TdxProtocol(),
            required_probes=frozenset({"SH_STOCK", "MINUTE_BAR"}),
            clock=lambda: now,
        )

        assert pool.health_snapshot()[0].state == "PROBING"
        assert pool.request(b"stock", probe_name="SH_STOCK") == b"ok"
        assert pool.health_snapshot()[0].state == "PROBING"
        assert pool.request(b"bar", probe_name="MINUTE_BAR") == b"ok"
        assert pool.health_snapshot()[0].state == "HEALTHY"
    finally:
        server.close()


def test_pool_rejects_a_probe_when_its_semantic_validator_fails() -> None:
    server = _Server(_frame(b"wrong-identity"))
    server.start()
    now = datetime(2026, 8, 21, 1, tzinfo=UTC)
    try:
        pool = TdxServerPool(
            (TdxServer("127.0.0.1", server.port),),
            TdxProtocol(),
            cooldown_seconds=30,
            required_probes=frozenset({"SH_STOCK"}),
            clock=lambda: now,
        )

        def reject(_: bytes) -> None:
            raise ValueError("unexpected symbol")

        with pytest.raises(TdxTransportError, match="unexpected symbol"):
            pool.request(b"stock", probe_name="SH_STOCK", validator=reject)

        node = pool.health_snapshot()[0]
        assert node.state == "COOLDOWN"
        assert node.successful_probes == ()
    finally:
        server.close()


def test_pool_records_failover_and_recovery_totals() -> None:
    now = datetime(2026, 8, 21, 1, tzinfo=UTC)

    class RecoveringHandler(socketserver.BaseRequestHandler):
        fail = True

        def handle(self) -> None:
            self.request.recv(4096)
            if not self.__class__.fail:
                self.request.sendall(_frame(b"recovered"))

    recovered_server = socketserver.TCPServer(("127.0.0.1", 0), RecoveringHandler)
    recovered_thread = Thread(target=recovered_server.serve_forever, daemon=True)
    recovered_thread.start()
    healthy = _Server(_frame(b"fallback"))
    healthy.start()
    try:
        pool = TdxServerPool(
            (
                TdxServer("127.0.0.1", int(recovered_server.server_address[1])),
                TdxServer("127.0.0.1", healthy.port),
            ),
            TdxProtocol(),
            timeout_seconds=0.2,
            cooldown_seconds=30,
            clock=lambda: now,
        )

        assert pool.request(b"first") == b"fallback"
        first = pool.statistics()
        assert (first.requests, first.failed_attempts, first.failovers, first.recoveries) == (
            1,
            1,
            1,
            0,
        )

        RecoveringHandler.fail = False
        now += timedelta(seconds=31)
        assert pool.request(b"second") == b"recovered"
        second = pool.statistics()
        assert (second.requests, second.failed_attempts, second.failovers, second.recoveries) == (
            2,
            1,
            1,
            1,
        )
    finally:
        healthy.close()
        recovered_server.shutdown()
        recovered_server.server_close()
        recovered_thread.join()


def test_pool_exposes_the_server_that_supplied_the_last_valid_response() -> None:
    server = _Server(_frame(b"source"))
    server.start()
    try:
        pool = TdxServerPool((TdxServer("127.0.0.1", server.port),), TdxProtocol())

        assert pool.request(b"source") == b"source"
        assert pool.last_server() == f"127.0.0.1:{server.port}"
    finally:
        server.close()

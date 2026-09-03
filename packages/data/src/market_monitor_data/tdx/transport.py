from __future__ import annotations

import socket
import struct
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from market_monitor_data.tdx.protocol import TdxProtocol, TdxProtocolError


class TdxTransportError(RuntimeError):
    """No configured TDX node returned a valid response."""


@dataclass(frozen=True)
class TdxServer:
    host: str
    port: int = 7709

    def __post_init__(self) -> None:
        if not self.host:
            raise ValueError("TDX host must not be empty")
        if not 1 <= self.port <= 65535:
            raise ValueError("TDX port must be between 1 and 65535")


@dataclass(frozen=True)
class TdxNodeHealth:
    host: str
    port: int
    state: str
    connect_latency_ms: int | None
    request_latency_ms: int | None
    last_successful_probe: datetime | None
    last_failed_probe: datetime | None
    consecutive_failures: int
    cooldown_until: datetime | None
    successful_probes: tuple[str, ...]


@dataclass(frozen=True)
class TdxPoolStatistics:
    requests: int
    failed_attempts: int
    failovers: int
    recoveries: int


@dataclass
class _NodeState:
    server: TdxServer
    connection: socket.socket | None = None
    connect_latency_ms: int | None = None
    request_latency_ms: int | None = None
    last_successful_probe: datetime | None = None
    last_failed_probe: datetime | None = None
    consecutive_failures: int = 0
    cooldown_until: datetime | None = None
    successful_probes: set[str] = field(default_factory=set)

    def snapshot(self, now: datetime, required_probes: frozenset[str]) -> TdxNodeHealth:
        cooling_down = self.cooldown_until is not None and self.cooldown_until > now
        if cooling_down:
            state = "COOLDOWN"
        elif self.consecutive_failures:
            state = "DEGRADED"
        elif required_probes.difference(self.successful_probes):
            state = "PROBING"
        else:
            state = "HEALTHY"
        return TdxNodeHealth(
            host=self.server.host,
            port=self.server.port,
            state=state,
            connect_latency_ms=self.connect_latency_ms,
            request_latency_ms=self.request_latency_ms,
            last_successful_probe=self.last_successful_probe,
            last_failed_probe=self.last_failed_probe,
            consecutive_failures=self.consecutive_failures,
            cooldown_until=self.cooldown_until,
            successful_probes=tuple(sorted(self.successful_probes)),
        )


class TdxServerPool:
    def __init__(
        self,
        servers: tuple[TdxServer, ...],
        protocol: TdxProtocol,
        *,
        timeout_seconds: float = 3.0,
        cooldown_seconds: int = 30,
        required_probes: frozenset[str] = frozenset(),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not servers:
            raise ValueError("at least one TDX server is required")
        if timeout_seconds <= 0:
            raise ValueError("TDX timeout must be positive")
        if cooldown_seconds < 0:
            raise ValueError("TDX cooldown must be non-negative")
        if any(not probe.strip() for probe in required_probes):
            raise ValueError("TDX required probe names must not be empty")
        self._nodes = tuple(_NodeState(server) for server in servers)
        self._protocol = protocol
        self._timeout_seconds = timeout_seconds
        self._cooldown_seconds = cooldown_seconds
        self._required_probes = required_probes
        self._clock = clock or (lambda: datetime.now(UTC))
        self._requests = 0
        self._failed_attempts = 0
        self._failovers = 0
        self._recoveries = 0
        self._last_server: TdxServer | None = None

    def request(
        self,
        payload: bytes,
        *,
        probe: bool = False,
        probe_name: str | None = None,
        validator: Callable[[bytes], None] | None = None,
        setup_payloads: tuple[bytes, ...] = (),
    ) -> bytes:
        if not payload:
            raise TdxTransportError("TDX request payload must not be empty")
        if any(not setup for setup in setup_payloads):
            raise TdxTransportError("TDX setup payload must not be empty")
        if probe_name is not None and not probe_name.strip():
            raise TdxTransportError("TDX probe name must not be empty")
        self._requests += 1
        now = self._clock()
        errors: list[str] = []
        for node in self._nodes:
            if node.cooldown_until is not None and node.cooldown_until > now:
                continue
            try:
                response, connect_latency, request_latency = self._request_node(
                    node, payload, setup_payloads
                )
                if validator is not None:
                    validator(response)
            except (OSError, TdxProtocolError, TimeoutError, ValueError) as error:
                self._close_node_connection(node)
                self._record_failure(node, now)
                errors.append(f"{node.server.host}:{node.server.port}: {error}")
                continue
            node.connect_latency_ms = connect_latency
            node.request_latency_ms = request_latency
            if node.consecutive_failures:
                self._recoveries += 1
            if errors:
                self._failovers += 1
            node.consecutive_failures = 0
            node.cooldown_until = None
            if probe or probe_name is not None:
                node.last_successful_probe = now
            if probe_name is not None:
                node.successful_probes.add(probe_name)
            self._last_server = node.server
            return response
        detail = "; ".join(errors) or "all nodes are in cooldown"
        raise TdxTransportError(f"no TDX node accepted request: {detail}")

    def health_snapshot(self) -> tuple[TdxNodeHealth, ...]:
        now = self._clock()
        return tuple(node.snapshot(now, self._required_probes) for node in self._nodes)

    def statistics(self) -> TdxPoolStatistics:
        return TdxPoolStatistics(
            self._requests,
            self._failed_attempts,
            self._failovers,
            self._recoveries,
        )

    def last_server(self) -> str | None:
        if self._last_server is None:
            return None
        return f"{self._last_server.host}:{self._last_server.port}"

    def _request_node(
        self,
        node: _NodeState,
        payload: bytes,
        setup_payloads: tuple[bytes, ...],
    ) -> tuple[bytes, int, int]:
        started = time.monotonic()
        connection = node.connection
        reused_connection = connection is not None
        if connection is None:
            connection = socket.create_connection(
                (node.server.host, node.server.port), timeout=self._timeout_seconds
            )
            connected = time.monotonic()
            connection.settimeout(self._timeout_seconds)
            node.connection = connection
            requests = (*setup_payloads, payload)
        else:
            connected = started
            requests = (payload,)
        response = b""
        try:
            for request in requests:
                connection.sendall(request)
                header = _recv_exact(connection, self._protocol.RESPONSE_HEADER_SIZE)
                compressed_length = struct.unpack("<H", header[12:14])[0]
                body = _recv_exact(connection, compressed_length)
                response = self._protocol.decode_response(header, body)
        except (OSError, TdxProtocolError, TimeoutError):
            if not reused_connection:
                raise
            self._close_node_connection(node)
            return self._request_node(node, payload, setup_payloads)
        completed = time.monotonic()
        return (
            response,
            int((connected - started) * 1000),
            int((completed - connected) * 1000),
        )

    def close(self) -> None:
        for node in self._nodes:
            self._close_node_connection(node)

    @staticmethod
    def _close_node_connection(node: _NodeState) -> None:
        connection = node.connection
        node.connection = None
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass

    def _record_failure(self, node: _NodeState, observed_at: datetime) -> None:
        self._failed_attempts += 1
        node.consecutive_failures += 1
        node.successful_probes.clear()
        node.last_failed_probe = observed_at
        node.cooldown_until = observed_at + timedelta(seconds=self._cooldown_seconds)


def _recv_exact(connection: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = connection.recv(size - len(chunks))
        if not chunk:
            raise TdxProtocolError("truncated TDX response")
        chunks.extend(chunk)
    return bytes(chunks)

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class DeliveryMessage:
    intent_uid: str
    idempotency_key: str
    channel: str
    context: Mapping[str, Any]


@dataclass(frozen=True)
class DeliveryResult:
    delivered: bool
    retryable: bool
    provider_reference: str | None
    error_code: str | None


class DeliveryAdapter(Protocol):
    def send(self, message: DeliveryMessage) -> DeliveryResult: ...


class InAppAdapter:
    def __init__(self) -> None:
        self._messages: dict[str, DeliveryMessage] = {}

    @property
    def messages(self) -> tuple[DeliveryMessage, ...]:
        return tuple(self._messages.values())

    def send(self, message: DeliveryMessage) -> DeliveryResult:
        self._messages.setdefault(message.idempotency_key, message)
        return DeliveryResult(True, False, message.intent_uid, None)


def _open(request: Request, timeout: float) -> Any:
    return urlopen(request, timeout=timeout)  # noqa: S310 - owner-configured explicit endpoint


class WebhookAdapter:
    def __init__(
        self,
        url: str | None,
        *,
        enabled: bool = False,
        timeout: float = 5.0,
        opener: Callable[[Request, float], Any] = _open,
    ) -> None:
        self._url = url
        self._enabled = enabled
        self._timeout = timeout
        self._opener = opener

    def send(self, message: DeliveryMessage) -> DeliveryResult:
        if not self._enabled or not self._url:
            return DeliveryResult(False, False, None, "CHANNEL_DISABLED")
        body = json.dumps(
            message.context,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
        request = Request(
            self._url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Idempotency-Key": message.idempotency_key,
            },
        )
        try:
            with self._opener(request, self._timeout) as response:
                status = int(response.status)
            if 200 <= status < 300:
                return DeliveryResult(True, False, str(status), None)
            return DeliveryResult(False, status >= 500, None, f"HTTP_{status}")
        except HTTPError as error:
            return DeliveryResult(False, error.code >= 500, None, f"HTTP_{error.code}")
        except URLError, TimeoutError, OSError:
            return DeliveryResult(False, True, None, "CHANNEL_UNAVAILABLE")

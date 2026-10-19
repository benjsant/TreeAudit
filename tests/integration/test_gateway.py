"""Gateway relay tests with real sockets on 127.0.0.1 (ephemeral ports)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import pytest

from treeaudit.core.errors import ConfigError
from treeaudit.gateway import GatewayConfig, start_gateway

Handler = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]


async def _start_upstream(handler: Handler) -> tuple[asyncio.Server, int]:
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def _start_gateway(upstream_port: int, **kwargs: float) -> tuple[asyncio.Server, int]:
    config = GatewayConfig(
        listen_host="127.0.0.1",
        listen_port=0,
        upstream_host="127.0.0.1",
        upstream_port=upstream_port,
        **kwargs,  # type: ignore[arg-type]
    )
    server = await start_gateway(config, container_mode=False)
    return server, server.sockets[0].getsockname()[1]


async def _echo_until_eof(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    data = await reader.read()  # until the client half-closes
    writer.write(b"echo:" + data)
    await writer.drain()
    writer.close()


def test_relays_both_directions_and_half_close() -> None:
    async def scenario() -> bytes:
        upstream, upstream_port = await _start_upstream(_echo_until_eof)
        gateway, port = await _start_gateway(upstream_port)
        async with upstream, gateway:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            payload = b"x" * 300_000  # larger than one buffer
            writer.write(payload)
            await writer.drain()
            writer.write_eof()
            answer = await asyncio.wait_for(reader.read(), 5)
            writer.close()
            return answer

    answer = asyncio.run(scenario())
    assert answer == b"echo:" + b"x" * 300_000


def test_http_request_goes_through_unchanged() -> None:
    received: list[bytes] = []

    async def http_upstream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        received.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
        await writer.drain()
        writer.close()

    async def scenario() -> bytes:
        upstream, upstream_port = await _start_upstream(http_upstream)
        gateway, port = await _start_gateway(upstream_port)
        async with upstream, gateway:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"GET /health HTTP/1.1\r\nHost: 127.0.0.1:8765\r\n\r\n")
            await writer.drain()
            answer = await asyncio.wait_for(reader.read(), 5)
            writer.close()
            return answer

    answer = asyncio.run(scenario())
    assert answer.endswith(b"\r\n\r\nok")
    # The Host header reaches the app untouched, so its Host check still applies.
    assert b"Host: 127.0.0.1:8765" in received[0]


def test_unreachable_upstream_closes_the_client() -> None:
    async def scenario() -> bytes:
        # Reserve a port, then free it so nothing listens there.
        placeholder, dead_port = await _start_upstream(_echo_until_eof)
        placeholder.close()
        await placeholder.wait_closed()
        gateway, port = await _start_gateway(dead_port, connect_timeout=2.0)
        async with gateway:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            answer = await asyncio.wait_for(reader.read(), 5)
            writer.close()
            return answer

    assert asyncio.run(scenario()) == b""


def test_connection_limit() -> None:
    release = asyncio.Event()

    async def hold(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await release.wait()
        writer.close()

    async def scenario() -> bytes:
        upstream, upstream_port = await _start_upstream(hold)
        gateway, port = await _start_gateway(upstream_port, max_connections=1)
        async with upstream, gateway:
            _, first = await asyncio.open_connection("127.0.0.1", port)
            await asyncio.sleep(0.2)  # let the first connection occupy the only slot
            second_reader, second = await asyncio.open_connection("127.0.0.1", port)
            refused = await asyncio.wait_for(second_reader.read(), 5)
            release.set()
            first.close()
            second.close()
            return refused

    assert asyncio.run(scenario()) == b""


def test_idle_connections_are_closed() -> None:
    async def silent(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read()
        writer.close()

    async def scenario() -> bytes:
        upstream, upstream_port = await _start_upstream(silent)
        gateway, port = await _start_gateway(upstream_port, idle_timeout=0.3)
        async with upstream, gateway:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            answer = await asyncio.wait_for(reader.read(), 5)
            writer.close()
            return answer

    assert asyncio.run(scenario()) == b""


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10"])
def test_gateway_refuses_non_loopback_outside_container(host: str) -> None:
    config = GatewayConfig(
        listen_host=host, listen_port=0, upstream_host="127.0.0.1", upstream_port=1
    )
    with pytest.raises(ConfigError):
        asyncio.run(start_gateway(config, container_mode=False))

"""TCP relay between the host-published port and the app container (D-020).

The ``app`` container, which sees the audited data, sits on an internal Docker
network without outbound access. Docker cannot publish a port from such a
network, so this relay runs in a separate container that has no data mount at
all: it publishes the port on the host loopback and forwards each connection,
byte for byte, to a single fixed upstream. It does not parse HTTP; the Host
check and every other protection stay in the app.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from treeaudit.core.config import check_bind_host

logger = logging.getLogger(__name__)

BUFFER_SIZE = 64 * 1024


@dataclass(frozen=True, slots=True)
class GatewayConfig:
    listen_host: str
    listen_port: int
    upstream_host: str
    upstream_port: int
    max_connections: int = 64
    connect_timeout: float = 5.0
    idle_timeout: float = 300.0


async def _close(writer: asyncio.StreamWriter) -> None:
    writer.close()
    try:
        await writer.wait_closed()
    except OSError as exc:
        logger.debug("error while closing a relay connection: %r", exc)


async def _pipe(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, idle_timeout: float
) -> None:
    """Copy one direction until EOF, then propagate the half-close."""
    try:
        while True:
            data = await asyncio.wait_for(reader.read(BUFFER_SIZE), idle_timeout)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except TimeoutError:
        logger.debug("relay direction idle for %.0fs, closing", idle_timeout)
    except OSError as exc:
        logger.debug("relay direction closed: %r", exc)
    if writer.can_write_eof() and not writer.is_closing():
        try:
            writer.write_eof()
        except OSError as exc:
            logger.debug("could not propagate EOF: %r", exc)


class Relay:
    """Accepts client connections and forwards them to the upstream."""

    def __init__(self, config: GatewayConfig) -> None:
        self.config = config
        self._active = 0

    async def handle(
        self, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter
    ) -> None:
        if self._active >= self.config.max_connections:
            logger.warning("connection limit (%d) reached, refusing", self.config.max_connections)
            await _close(client_writer)
            return
        self._active += 1
        try:
            await self._relay(client_reader, client_writer)
        finally:
            self._active -= 1

    async def _relay(
        self, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter
    ) -> None:
        cfg = self.config
        try:
            upstream_reader, upstream_writer = await asyncio.wait_for(
                asyncio.open_connection(cfg.upstream_host, cfg.upstream_port),
                cfg.connect_timeout,
            )
        except (OSError, TimeoutError) as exc:
            logger.warning(
                "upstream %s:%d unreachable: %r", cfg.upstream_host, cfg.upstream_port, exc
            )
            await _close(client_writer)
            return
        try:
            await asyncio.gather(
                _pipe(client_reader, upstream_writer, cfg.idle_timeout),
                _pipe(upstream_reader, client_writer, cfg.idle_timeout),
            )
        finally:
            await _close(upstream_writer)
            await _close(client_writer)


async def start_gateway(config: GatewayConfig, *, container_mode: bool) -> asyncio.Server:
    """Validate the listening address and start accepting connections."""
    check_bind_host(config.listen_host, container_mode=container_mode)
    relay = Relay(config)
    server = await asyncio.start_server(relay.handle, config.listen_host, config.listen_port)
    logger.info(
        "gateway listening on %s:%d, forwarding to %s:%d",
        config.listen_host,
        config.listen_port,
        config.upstream_host,
        config.upstream_port,
    )
    return server


async def serve_gateway(config: GatewayConfig, *, container_mode: bool) -> None:
    server = await start_gateway(config, container_mode=container_mode)
    async with server:
        await server.serve_forever()

"""``serve`` (the web interface) and ``gateway`` (the Docker relay, D-020)."""

from __future__ import annotations

import argparse
import dataclasses
import os
from collections.abc import Sequence

from treeaudit.archive.destination import ArchiveDirReason, evaluate_archive_dir
from treeaudit.cli.common import EXIT_OK, load_with_disks, logger
from treeaudit.core.config import CONTAINER_BIND_HOST, AppConfig, is_container_mode
from treeaudit.core.datadir import prepare_data_dir
from treeaudit.roots.external import DISKS_ENV_VAR, ExternalDisk
from treeaudit.roots.mountinfo import read_mountinfo
from treeaudit.roots.registry import (
    RootStatus,
    current_home,
    evaluate_roots,
    with_external_disks,
    with_personal_folder,
)


def serve(args: argparse.Namespace) -> int:
    # Imported lazily: the web stack is not needed for the other commands.
    """Start the local web interface (127.0.0.1 natively, D-010)."""
    import uvicorn

    from treeaudit.web.app import create_app

    config, disks = load_with_disks(args)
    prepare_data_dir(config.data_dir)
    statuses = evaluate_roots(config, read_mountinfo(), current_home())
    for status in statuses:
        if status.accepted:
            logger.info("root %s accepted", status.root.id)
        else:
            logger.warning("root %s rejected: %s", status.root.id, status.reason)
    archive_status = evaluate_archive_dir(config, read_mountinfo(), current_home())
    if archive_status.enabled:
        logger.info("archiving enabled")
    elif archive_status.reason in (ArchiveDirReason.NOT_CONFIGURED, ArchiveDirReason.NOT_MOUNTED):
        logger.info("archiving disabled: no archive directory")
    else:
        logger.warning("archive directory rejected: %s", archive_status.reason)

    servers: list[uvicorn.Server] = []

    def stop() -> None:
        servers[0].should_exit = True

    declared = dataclasses.replace(config, roots=config.declared_roots)

    def detect() -> tuple[AppConfig, tuple[RootStatus, ...], Sequence[ExternalDisk]]:
        """Natively, the machine's mounts are seen live: same detection as at startup."""
        mounts = read_mountinfo()
        found, found_disks = with_external_disks(declared, mounts, os.environ)
        found = with_personal_folder(found, os.environ, current_home())
        return found, evaluate_roots(found, mounts, current_home()), found_disks

    app = create_app(
        config,
        statuses,
        external_disks=disks,
        external_enabled=config.external_disks or DISKS_ENV_VAR in os.environ,
        # In Docker, ./treeaudit stops the containers; natively there may be no terminal.
        quit=None if config.container_mode else stop,
        # In Docker the container only sees what was mounted at its start.
        detect=None if config.container_mode else detect,
    )
    servers.append(
        uvicorn.Server(
            uvicorn.Config(
                app,
                host=config.bind_host,
                port=config.port,
                server_header=False,
                proxy_headers=False,
                log_level="info",
                # URLs carry searched words and typed folder names: not logged
                # (security review V1). Errors still are.
                access_log=False,
            )
        )
    )
    servers[0].run()
    # Let a cancelled job clean up (an archive removes its partial file).
    app.state.jobs.cancel()
    app.state.jobs.wait()
    return EXIT_OK


def gateway(args: argparse.Namespace) -> int:
    """Relay connections to the app container (Docker only, D-020)."""
    import asyncio

    from treeaudit.gateway import GatewayConfig, serve_gateway

    container_mode = is_container_mode()
    config = GatewayConfig(
        listen_host=CONTAINER_BIND_HOST if container_mode else "127.0.0.1",
        listen_port=args.listen_port,
        upstream_host=args.upstream_host,
        upstream_port=args.upstream_port,
    )
    asyncio.run(serve_gateway(config, container_mode=container_mode))
    return EXIT_OK

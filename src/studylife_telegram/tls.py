"""Optional HTTPS listener for the StudyAtrium -> studylife-telegram hop (account-deletion purge).

StudyAtrium's backend calls `POST /internal/users/{id}/purge` with a shared secret
(X-StudyLife-Shared-Secret) and the hashes of the user's API keys, so the hop has to be
encrypted. StudyAtrium validates the certificate against its own internal CA, host name included
(its `AddonTls:*` settings, docs/SECURITY-ASSURANCE.md section 8.3). In k3s the certificate is the
cert-manager one issued by `studylife-internal-ca-issuer` (the Certificate manifest in k8s/);
anywhere else it is any certificate the server's CA setting trusts.

When `STUDYLIFE_TELEGRAM_TLS_CERT_FILE` and `STUDYLIFE_TELEGRAM_TLS_KEY_FILE` are set,
`tls_listener` starts a second `uvicorn.Server` on the same event loop that serves the same app
over HTTPS on `STUDYLIFE_TELEGRAM_TLS_PORT` (default 8443). The plain port 8000 keeps running for
the kubelet probes, so the TLS listener can be rolled out before StudyAtrium switches its URL (see
README.md "TLS").

Failure is loud on purpose: a certificate that cannot be loaded or a port that cannot be bound
raises during startup, which makes the process exit instead of running without the TLS listener
StudyAtrium expects.

uvicorn never reloads certificates on its own. `watch_certificates` polls the two files and, when
cert-manager has rewritten them, loads the new pair into the live `SSLContext`, so every new
handshake uses the renewed certificate without a restart.
"""

import asyncio
import contextlib
import logging
import ssl
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import uvicorn

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TlsFiles:
    """The PEM certificate chain and private key a TLS listener serves."""

    cert_file: Path
    key_file: Path


def tls_files(cert_file: str | None, key_file: str | None) -> TlsFiles | None:
    """`None` when TLS is not configured (both unset). Raises when only one is set or a file is
    missing, so a half-configured TLS setup stops the start instead of serving plain HTTP."""
    if not cert_file and not key_file:
        return None
    if not cert_file or not key_file:
        raise ValueError("The TLS certificate file and key file must be set together")
    files = TlsFiles(Path(cert_file), Path(key_file))
    for path in (files.cert_file, files.key_file):
        if not path.is_file():
            raise ValueError(f"TLS file {path} does not exist")
    return files


def build_tls_server(app: Any, port: int, files: TlsFiles, *, log_level: str) -> uvicorn.Server:
    """A `uvicorn.Server` that serves `app` over HTTPS on `port` (0 = OS-assigned, for tests).

    `lifespan="off"`: the app's lifespan already runs once on the plain server that started the
    process; a second run would build a second set of stores and start this server again.
    TLS 1.2 is the minimum, the rest are Python's `ssl` defaults (no compression, no
    renegotiation, secure cipher list)."""
    config = uvicorn.Config(
        app,
        # Bound inside the pod/container network, same as the main app's CMD in the Dockerfile.
        host="0.0.0.0",
        port=port,
        log_level=log_level.lower(),
        lifespan="off",
        ssl_certfile=str(files.cert_file),
        ssl_keyfile=str(files.key_file),
    )
    config.load()
    assert config.ssl is not None  # set by load() because ssl_certfile is given
    config.ssl.minimum_version = ssl.TLSVersion.TLSv1_2
    server = uvicorn.Server(config)
    server.lifespan = config.lifespan_class(config)
    return server


async def start_tls_server(server: uvicorn.Server) -> asyncio.Task[None]:
    """Binds `server` now (raising on failure) and returns the task that serves it until
    `server.should_exit` is set. This skips
    `Server.serve()` so no second set of process-wide signal handlers is installed."""
    try:
        await server.startup()
    except SystemExit as exc:
        raise RuntimeError(
            f"Could not start the HTTPS listener on port {server.config.port}"
        ) from exc
    if server.should_exit:
        raise RuntimeError(f"Could not start the HTTPS listener on port {server.config.port}")

    async def _run() -> None:
        try:
            await server.main_loop()
        finally:
            await server.shutdown()

    return asyncio.create_task(_run())


async def stop_tls_servers(servers: list[uvicorn.Server], tasks: list[asyncio.Task[None]]) -> None:
    """Graceful stop: lets each server close its listening socket cleanly."""
    for server in servers:
        server.should_exit = True
    for task in tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await task


def bound_port(server: uvicorn.Server) -> int:
    """The port a started server actually listens on (useful with port 0)."""
    sockets = server.servers[0].sockets
    return int(sockets[0].getsockname()[1])


def _fingerprint(files: TlsFiles) -> tuple[tuple[int, int, int], ...]:
    # A Kubernetes Secret volume swaps a symlink on update; stat() follows it, so inode, size or
    # mtime change when cert-manager writes the renewed pair.
    result = []
    for path in (files.cert_file, files.key_file):
        st = path.stat()
        result.append((st.st_ino, st.st_size, st.st_mtime_ns))
    return tuple(result)


def reload_certificates(servers: list[uvicorn.Server], files: TlsFiles) -> bool:
    """Loads the current certificate/key pair into every server's `SSLContext`. Returns False
    (and keeps serving the old pair) when the files cannot be loaded, e.g. mid-update."""
    try:
        for server in servers:
            assert server.config.ssl is not None
            server.config.ssl.load_cert_chain(str(files.cert_file), str(files.key_file))
    except (OSError, ssl.SSLError) as exc:
        logger.warning("TLS certificate reload failed, keeping the previous certificate: %s", exc)
        return False
    logger.info("TLS certificate reloaded from %s", files.cert_file)
    return True


async def watch_certificates(
    servers: list[uvicorn.Server], files: TlsFiles, interval_seconds: float
) -> None:
    """Runs until cancelled: reloads the certificate whenever the files change."""
    last = _fingerprint(files)
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            current = _fingerprint(files)
        except OSError as exc:
            logger.warning("TLS certificate files not readable: %s", exc)
            continue
        if current != last and reload_certificates(servers, files):
            last = current


@contextlib.asynccontextmanager
async def tls_listener(
    app: Any,
    files: TlsFiles | None,
    port: int,
    reload_interval_seconds: float,
    *,
    log_level: str = "info",
) -> AsyncIterator[list[uvicorn.Server]]:
    """Runs the HTTPS listener for the lifetime of the `async with` block. With `files=None`
    (TLS not configured) it does nothing and yields an empty list. Otherwise it binds the
    listener (a failure raises before the caller serves anything), keeps the certificate fresh
    and stops everything cleanly on exit."""
    if files is None:
        yield []
        return
    servers = [build_tls_server(app, port, files, log_level=log_level)]
    tasks: list[asyncio.Task[None]] = []
    watcher: asyncio.Task[None] | None = None
    try:
        for server in servers:
            tasks.append(await start_tls_server(server))
        watcher = asyncio.create_task(watch_certificates(servers, files, reload_interval_seconds))
        yield servers
    finally:
        if watcher is not None:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher
        await stop_tls_servers(servers, tasks)

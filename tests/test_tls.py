"""TLS listener (tls.py): real handshakes against a throwaway private CA, the way StudyAtrium's
server connects to the purge hop (its own CA, host name checked). Real sockets on OS-assigned
ports instead of the TestClient, which has no network stack."""

import asyncio
import contextlib
import datetime
import ipaddress
import ssl
from pathlib import Path

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from fastapi import FastAPI

from studylife_telegram.tls import (
    TlsFiles,
    bound_port,
    build_tls_server,
    reload_certificates,
    start_tls_server,
    stop_tls_servers,
    tls_files,
    tls_listener,
    watch_certificates,
)


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _make_ca(common_name: str = "test-ca") -> tuple[x509.Certificate, ec.EllipticCurvePrivateKey]:
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(_name(common_name))
        .issuer_name(_name(common_name))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    return cert, key


def _write_leaf(
    directory: Path,
    ca: tuple[x509.Certificate, ec.EllipticCurvePrivateKey],
    common_name: str = "localhost",
) -> TlsFiles:
    ca_cert, ca_key = ca
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(_name(common_name))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    files = TlsFiles(directory / "tls.crt", directory / "tls.key")
    files.cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    files.key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return files


def _client_context(ca_cert: x509.Certificate) -> ssl.SSLContext:
    context = ssl.create_default_context(
        cadata=ca_cert.public_bytes(serialization.Encoding.PEM).decode()
    )
    return context


def _app() -> FastAPI:
    app = FastAPI()

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


async def _peer_serial(port: int, ca_cert: x509.Certificate) -> int:
    _, writer = await asyncio.open_connection(
        "127.0.0.1", port, ssl=_client_context(ca_cert), server_hostname="localhost"
    )
    try:
        der = writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
        return x509.load_der_x509_certificate(der).serial_number
    finally:
        writer.close()
        await writer.wait_closed()


def test_tls_files_unset_means_tls_off() -> None:
    assert tls_files(None, None) is None
    assert tls_files("", "") is None


def test_tls_files_require_both(tmp_path: Path) -> None:
    (tmp_path / "tls.crt").write_text("x")
    with pytest.raises(ValueError, match="set together"):
        tls_files(str(tmp_path / "tls.crt"), None)


def test_tls_files_must_exist(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        tls_files(str(tmp_path / "missing.crt"), str(tmp_path / "missing.key"))


def test_unloadable_certificate_fails_at_build(tmp_path: Path) -> None:
    files = TlsFiles(tmp_path / "tls.crt", tmp_path / "tls.key")
    files.cert_file.write_text("not a certificate")
    files.key_file.write_text("not a key")
    with pytest.raises(ssl.SSLError):
        build_tls_server(_app(), 0, files, log_level="WARNING")


async def test_serves_https_validated_against_the_private_ca(tmp_path: Path) -> None:
    ca = _make_ca()
    files = _write_leaf(tmp_path, ca)
    server = build_tls_server(_app(), 0, files, log_level="WARNING")
    task = await start_tls_server(server)
    try:
        port = bound_port(server)
        async with httpx.AsyncClient(verify=_client_context(ca[0])) as client:
            response = await client.get(f"https://localhost:{port}/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
    finally:
        await stop_tls_servers([server], [task])


async def test_rejects_plain_http_and_untrusted_clients(tmp_path: Path) -> None:
    ca = _make_ca()
    files = _write_leaf(tmp_path, ca)
    server = build_tls_server(_app(), 0, files, log_level="WARNING")
    task = await start_tls_server(server)
    try:
        port = bound_port(server)
        async with httpx.AsyncClient() as client:
            with pytest.raises(httpx.HTTPError):
                await client.get(f"http://localhost:{port}/health")
        other_ca = _make_ca("other-ca")
        async with httpx.AsyncClient(verify=_client_context(other_ca[0])) as client:
            with pytest.raises(httpx.ConnectError):
                await client.get(f"https://localhost:{port}/health")
    finally:
        await stop_tls_servers([server], [task])


def test_minimum_tls_version_is_1_2(tmp_path: Path) -> None:
    ca = _make_ca()
    files = _write_leaf(tmp_path, ca)
    server = build_tls_server(_app(), 0, files, log_level="WARNING")
    assert server.config.ssl is not None
    assert server.config.ssl.minimum_version == ssl.TLSVersion.TLSv1_2


async def test_bind_failure_raises(tmp_path: Path) -> None:
    ca = _make_ca()
    files = _write_leaf(tmp_path, ca)
    first = build_tls_server(_app(), 0, files, log_level="CRITICAL")
    first_task = await start_tls_server(first)
    try:
        second = build_tls_server(_app(), bound_port(first), files, log_level="CRITICAL")
        with pytest.raises(RuntimeError, match="HTTPS listener"):
            await start_tls_server(second)
    finally:
        await stop_tls_servers([first], [first_task])


async def test_renewed_certificate_is_served_without_restart(tmp_path: Path) -> None:
    ca = _make_ca()
    files = _write_leaf(tmp_path, ca)
    server = build_tls_server(_app(), 0, files, log_level="WARNING")
    task = await start_tls_server(server)
    watcher = asyncio.create_task(watch_certificates([server], files, 0.05))
    try:
        port = bound_port(server)
        before = await _peer_serial(port, ca[0])
        renewed = _write_leaf(tmp_path, ca)
        expected = x509.load_pem_x509_certificate(renewed.cert_file.read_bytes()).serial_number
        for _ in range(100):
            if await _peer_serial(port, ca[0]) == expected:
                break
            await asyncio.sleep(0.05)
        assert expected != before
        assert await _peer_serial(port, ca[0]) == expected
    finally:
        watcher.cancel()
        with pytest.raises(asyncio.CancelledError):
            await watcher
        await stop_tls_servers([server], [task])


async def test_broken_renewal_keeps_the_old_certificate(tmp_path: Path) -> None:
    ca = _make_ca()
    files = _write_leaf(tmp_path, ca)
    server = build_tls_server(_app(), 0, files, log_level="WARNING")
    task = await start_tls_server(server)
    try:
        port = bound_port(server)
        before = await _peer_serial(port, ca[0])
        files.cert_file.write_text("half-written")
        assert reload_certificates([server], files) is False
        assert await _peer_serial(port, ca[0]) == before
    finally:
        await stop_tls_servers([server], [task])


async def test_listener_is_a_no_op_without_certificate_files() -> None:
    async with tls_listener(_app(), None, 0, 0.05) as servers:
        assert servers == []


async def test_listener_serves_https_and_stops_on_exit(tmp_path: Path) -> None:
    ca = _make_ca()
    files = _write_leaf(tmp_path, ca)
    async with tls_listener(_app(), files, 0, 0.05, log_level="WARNING") as servers:
        port = bound_port(servers[0])
        async with httpx.AsyncClient(verify=_client_context(ca[0])) as client:
            response = await client.get(f"https://localhost:{port}/health")
        assert response.status_code == 200
    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", port)


async def test_listener_fails_the_start_when_the_certificate_is_broken(tmp_path: Path) -> None:
    files = TlsFiles(tmp_path / "tls.crt", tmp_path / "tls.key")
    files.cert_file.write_text("not a certificate")
    files.key_file.write_text("not a key")
    with pytest.raises(ssl.SSLError):
        async with tls_listener(_app(), files, 0, 0.05):
            pass


def _settings(tmp_path: Path, **tls: object):  # type: ignore[no-untyped-def]
    from cryptography.fernet import Fernet

    from studylife_telegram.config import Settings

    return Settings(
        telegram_bot_token="t",
        telegram_webhook_secret="w",
        telegram_allow_any_chat=True,
        studylife_base_url="https://studylife.example",
        link_encryption_key=Fernet.generate_key().decode(),
        link_db_path=str(tmp_path / "links.db"),
        public_base_url="https://bot.example",
        **tls,  # type: ignore[arg-type]
    )


async def test_lifespan_serves_the_app_over_https_when_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from studylife_telegram import main

    ca = _make_ca()
    files = _write_leaf(tmp_path, ca)
    settings = _settings(
        tmp_path,
        studylife_telegram_tls_cert_file=str(files.cert_file),
        studylife_telegram_tls_key_file=str(files.key_file),
        studylife_telegram_tls_port=0,
    )
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    started: list[object] = []
    real = main.tls_listener

    @contextlib.asynccontextmanager
    async def _spy(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        async with real(*args, **kwargs) as servers:  # type: ignore[arg-type]
            started.extend(servers)
            yield servers

    monkeypatch.setattr(main, "tls_listener", _spy)
    async with main.lifespan(main.app):
        port = bound_port(started[0])  # type: ignore[arg-type]
        async with httpx.AsyncClient(verify=_client_context(ca[0])) as client:
            response = await client.get(f"https://localhost:{port}/healthz")
        assert response.status_code == 200


async def test_lifespan_stays_plain_http_only_without_certificate_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from studylife_telegram import main

    settings = _settings(tmp_path)
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    async with main.lifespan(main.app):
        pass


async def test_lifespan_refuses_half_configured_tls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from studylife_telegram import main

    files = _write_leaf(tmp_path, _make_ca())
    settings = _settings(tmp_path, studylife_telegram_tls_cert_file=str(files.cert_file))
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    with pytest.raises(ValueError, match="set together"):
        async with main.lifespan(main.app):
            pass

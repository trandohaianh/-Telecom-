from __future__ import annotations

import pytest
from telecom_grpc import tls_config


def test_tls_credentials_require_a_client_ca(monkeypatch, tmp_path):
    monkeypatch.setenv("GRPC_SERVER_CERT", str(tmp_path / "server.crt"))
    monkeypatch.setenv("GRPC_SERVER_KEY", str(tmp_path / "server.key"))
    monkeypatch.delenv("GRPC_CLIENT_CA", raising=False)

    with pytest.raises(RuntimeError, match="GRPC_CLIENT_CA is required"):
        tls_config.server_credentials()


def test_tls_credentials_always_require_client_auth(monkeypatch, tmp_path):
    paths = {name: tmp_path / name for name in ("server.crt", "server.key", "client-ca.crt")}
    for name, path in paths.items():
        path.write_bytes(name.encode("ascii"))
    monkeypatch.setenv("GRPC_SERVER_CERT", str(paths["server.crt"]))
    monkeypatch.setenv("GRPC_SERVER_KEY", str(paths["server.key"]))
    monkeypatch.setenv("GRPC_CLIENT_CA", str(paths["client-ca.crt"]))
    captured = {}

    def make_credentials(certificates, *, root_certificates, require_client_auth):
        captured.update(certificates=certificates, roots=root_certificates,
                        require_client_auth=require_client_auth)
        return object()

    monkeypatch.setattr(tls_config.grpc, "ssl_server_credentials", make_credentials)
    tls_config.server_credentials()

    assert captured["certificates"] == ((b"server.key", b"server.crt"),)
    assert captured["roots"] == b"client-ca.crt"
    assert captured["require_client_auth"] is True

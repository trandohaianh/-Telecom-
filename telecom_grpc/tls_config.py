"""TLS credential loading for the telecom gRPC edge."""
from __future__ import annotations

import os

import grpc


def server_credentials():
    """Load mandatory mutual TLS credentials for non-local deployments."""
    roots_path = os.environ.get("GRPC_CLIENT_CA")
    if not roots_path:
        raise RuntimeError("GRPC_CLIENT_CA is required when mutual TLS is enabled")
    cert = open(os.environ["GRPC_SERVER_CERT"], "rb").read()
    key = open(os.environ["GRPC_SERVER_KEY"], "rb").read()
    roots = open(roots_path, "rb").read()
    return grpc.ssl_server_credentials(((key, cert),), root_certificates=roots,
                                       require_client_auth=True)

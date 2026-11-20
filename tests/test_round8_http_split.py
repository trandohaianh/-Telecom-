"""Tách HTTPS theo dự án (20/11/2026, quyết định "mỗi dự án tự có HTTPS"). Mỗi test thất bại trên mã cũ."""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CLOUD = ROOT / "deploy" / "cloud"


def read(name):
    return (CLOUD / name).read_text(encoding="utf-8")


def test_default_compose_runs_its_own_caddy_with_a_static_ip():
    compose = yaml.safe_load(read("docker-compose.yml"))
    caddy, telecom = compose["services"]["caddy"], compose["services"]["telecom"]
    assert "profiles" not in caddy  # no longer opt-in
    assert caddy["networks"]["proxy"]["ipv4_address"] == "${TELECOM_CADDY_IP:-172.29.10.2}"
    assert "edge" not in telecom["networks"] and "edge" not in compose["networks"]  # no FieldOps dependency
    assert compose["networks"]["proxy"]["ipam"]["config"][0]["subnet"] == "${TELECOM_PROXY_SUBNET:-172.29.10.0/24}"
    trusted = telecom["environment"]["FORWARDED_ALLOW_IPS"]
    assert "TELECOM_CADDY_IP" in trusted and "*" not in trusted  # static, fail-closed


def test_shared_edge_is_an_explicit_override_file():
    edge = yaml.safe_load(read("docker-compose.edge.yml"))
    assert edge["networks"]["edge"]["external"] is True
    assert list(edge["services"]) == ["telecom"]


def own_branch(src):
    """The shell block run when MODE=own: up to the next elif/else line."""
    after = src.split('"$MODE" = own', 1)[1]
    return re.split(r"\n\s*(?:elif|else)\b", after, maxsplit=1)[0]


def test_setup_and_deploy_choose_the_proxy_mode_and_default_to_own():
    for script in ("setup.sh", "deploy.sh"):
        src = read(script)
        assert "TELECOM_PROXY_MODE:-own" in src, script
        assert "docker-compose.edge.yml" in src and "--profile standalone" not in src, script
        assert "proxy_ip.sh" not in own_branch(src), script  # own mode never looks for FieldOps


def test_smoke_checks_that_the_proxy_sets_hsts():
    assert "strict-transport-security" in read("smoke.sh").lower()


def test_env_example_documents_the_mode_without_secrets():
    env = read(".env.example")
    assert "TELECOM_PROXY_MODE=own" in env and "TELECOM_CADDY_IP=172.29.10.2" in env
    for line in env.splitlines():
        if line.startswith(("FRAUD_ADMIN_PASSWORD=", "METRICS_TOKEN=", "TWILIO_AUTH_TOKEN=", "DUCKDNS_TOKEN=")):
            assert line.split("=", 1)[1] == "", line  # placeholders only, never a key

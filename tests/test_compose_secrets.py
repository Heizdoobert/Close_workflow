"""Compose files must fail closed on unset secrets and must not hard-code a password."""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
REDIS_COMPOSE = ROOT / "docker-compose.yml"
LOGGING_COMPOSE = ROOT / "docker-compose-logging.yml"
ENV_EXAMPLE = ROOT / ".env.example"
GITIGNORE = ROOT / ".gitignore"


def _read(path):
    return path.read_text(encoding="utf-8")


def test_redis_requirepass_fails_closed_when_unset():
    text = _read(REDIS_COMPOSE)
    assert "${REDIS_PASSWORD:?set REDIS_PASSWORD in .env}" in text
    assert not re.search(r"\$\{REDIS_PASSWORD\}", text)
    assert not re.search(r"\$\{REDIS_PASSWORD(:-[^}]*)?\}", text)


def test_grafana_admin_password_comes_from_env_and_fails_closed():
    text = _read(LOGGING_COMPOSE)
    assert (
        "${GF_SECURITY_ADMIN_PASSWORD:?set GF_SECURITY_ADMIN_PASSWORD in .env}" in text
    )


def test_no_literal_admin_password_remains():
    text = _read(LOGGING_COMPOSE)
    assert not re.search(r"GF_SECURITY_ADMIN_PASSWORD\s*=\s*admin\b", text)
    assert not re.search(r"GF_SECURITY_ADMIN_PASSWORD\s*:\s*admin\b", text)


def test_env_example_exists_with_both_keys_empty():
    assert ENV_EXAMPLE.is_file()
    lines = _read(ENV_EXAMPLE).splitlines()
    for key in ("REDIS_PASSWORD", "GF_SECURITY_ADMIN_PASSWORD"):
        assignments = [line for line in lines if line.startswith(key + "=")]
        assert assignments == [key + "="]


def test_gitignore_lists_dotenv():
    entries = [line.strip() for line in _read(GITIGNORE).splitlines()]
    assert ".env" in entries

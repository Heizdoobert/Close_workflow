"""Compose files must fail closed on unset secrets and must not hard-code a password."""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
REDIS_COMPOSE = ROOT / "docker-compose.yml"
ENV_EXAMPLE = ROOT / ".env.example"
GITIGNORE = ROOT / ".gitignore"


def _read(path):
    return path.read_text(encoding="utf-8")


def test_redis_requirepass_fails_closed_when_unset():
    text = _read(REDIS_COMPOSE)
    assert "${REDIS_PASSWORD:?set REDIS_PASSWORD in .env}" in text
    assert not re.search(r"\$\{REDIS_PASSWORD\}", text)
    assert not re.search(r"\$\{REDIS_PASSWORD(:-[^}]*)?\}", text)


def test_env_example_exists_with_the_key_empty():
    assert ENV_EXAMPLE.is_file()
    lines = _read(ENV_EXAMPLE).splitlines()
    assert [line for line in lines if line.startswith("REDIS_PASSWORD=")] == ["REDIS_PASSWORD="]


def test_gitignore_lists_dotenv():
    entries = [line.strip() for line in _read(GITIGNORE).splitlines()]
    assert ".env" in entries

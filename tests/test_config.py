from pathlib import Path

import pytest

from monitor.config import (
    ConfigError,
    EnvSettings,
    load_config,
    load_dotenv,
    name_from_url,
    normalize_url,
    parse_duration,
)

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize(
    ("value", "seconds"),
    [("10m", 600), ("5s", 5), ("500ms", 0.5), ("1h", 3600), ("1.5s", 1.5), (7, 7), ("30", 30)],
)
def test_parse_duration(value: object, seconds: float) -> None:
    assert parse_duration(value) == seconds


@pytest.mark.parametrize("value", ["10 minutes", "", "-5s", True, None, "5d"])
def test_parse_duration_invalid(value: object) -> None:
    with pytest.raises(ValueError, match="duration"):
        parse_duration(value)


def test_example_config_is_valid() -> None:
    config = load_config(ROOT / "config.example.yaml")
    assert config.monitor.interval == 600
    assert config.monitor.failure_threshold == 2
    assert config.monitor.request_timeout == 5
    # The example ships without active sites: they are added from the bot.
    assert config.services == []


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_defaults(tmp_path: Path) -> None:
    config = load_config(write(tmp_path, "services:\n  - name: A\n    url: https://a.test\n"))
    assert config.monitor.interval == 600
    assert config.monitor.recovery_threshold == 1
    assert config.monitor.max_concurrency == 10
    assert config.services[0].enabled is True
    assert config.services[0].check_url == "https://a.test"


@pytest.mark.parametrize(
    ("text", "error"),
    [
        (
            "services:\n  - {name: A, url: https://a.test}\n  - {name: A, url: https://b.test}\n",
            "duplicate service name",
        ),
        ("services:\n  - {name: A, url: ftp://a.test}\n", "http"),
        ("services:\n  - {name: A, url: not-a-url}\n", "URL"),
        ("services:\n  - {name: A, url: https://a.test, typo_field: 1}\n", "typo_field"),
        ("monitor:\n  failure_threshold: 0\n", "failure_threshold"),
        ("monitor:\n  interval: soon\n", "duration"),
        ("monitor:\n  timezone: Mars/Olympus\n", "timezone"),
        ("- just\n- a list\n", "mapping"),
        ("monitor: [\n", "invalid YAML"),
    ],
)
def test_invalid_config(tmp_path: Path, text: str, error: str) -> None:
    with pytest.raises(ConfigError, match=error):
        load_config(write(tmp_path, text))


def test_missing_config_is_optional(tmp_path: Path) -> None:
    config = load_config(tmp_path / "nope.yaml")
    assert config.services == []
    assert config.monitor.interval == 600


def test_missing_config_required(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml", required=True)


@pytest.mark.parametrize(
    ("raw", "url"),
    [
        ("https://site.ru", "https://site.ru"),
        ("site.ru", "https://site.ru"),
        ("  http://site.ru/health  ", "http://site.ru/health"),
        ("<https://site.ru>", "https://site.ru"),
    ],
)
def test_normalize_url(raw: str, url: str) -> None:
    assert normalize_url(raw) == url


@pytest.mark.parametrize("raw", ["", "   ", "ftp://site.ru", "https://"])
def test_normalize_url_invalid(raw: str) -> None:
    with pytest.raises(ValueError, match="URL"):
        normalize_url(raw)


@pytest.mark.parametrize(
    ("url", "name"),
    [
        ("https://www.site.ru/health", "site.ru"),
        ("https://api.site.ru:8443/x", "api.site.ru"),
    ],
)
def test_name_from_url(url: str, name: str) -> None:
    assert name_from_url(url) == name


def test_env_settings() -> None:
    env = EnvSettings.from_env(
        {
            "TELEGRAM_BOT_TOKEN": " 123:abc ",
            "TELEGRAM_CHAT_ID": "-100",
            "DB_PATH": "/data/x.db",
            "LOG_LEVEL": "debug",
            "LOG_FORMAT": "",
        }
    )
    assert env.telegram_bot_token == "123:abc"
    assert env.telegram_chat_id == "-100"
    assert env.db_path == Path("/data/x.db")
    assert env.log_level == "DEBUG"
    assert env.log_format == "json"
    assert "123:abc" not in repr(env)


def test_env_settings_empty() -> None:
    env = EnvSettings.from_env({})
    assert env.telegram_bot_token is None
    assert env.config_path == Path("config.yaml")


def test_load_dotenv_does_not_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "# comment\nSENTINEL_A=from_file\nSENTINEL_B='quoted value'\nexport SENTINEL_C=x\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SENTINEL_A", "from_env")
    monkeypatch.delenv("SENTINEL_B", raising=False)
    monkeypatch.delenv("SENTINEL_C", raising=False)
    load_dotenv(dotenv)
    import os

    assert os.environ["SENTINEL_A"] == "from_env"
    assert os.environ["SENTINEL_B"] == "quoted value"
    assert os.environ["SENTINEL_C"] == "x"
    monkeypatch.delenv("SENTINEL_B")
    monkeypatch.delenv("SENTINEL_C")

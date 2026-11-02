"""Config file, environment precedence and generated secrets."""

from __future__ import annotations

import os
import socket
import stat

import pytest

from wifisurvey.config import (
    ConfigError,
    ensure_config,
    load_settings,
    port_is_free,
    render_config,
)


def test_defaults_are_generic(tmp_path, monkeypatch):
    for name in list(os.environ):
        if name.startswith("WIFISURVEY_"):
            monkeypatch.delenv(name, raising=False)
    settings = load_settings(tmp_path / "missing.toml", data_dir=str(tmp_path / "d"))
    assert settings.server.host == "0.0.0.0"
    assert settings.server.port == 8800
    assert settings.server.timezone
    assert settings.security.viewer_token == ""
    assert settings.db_path.endswith("survey.db")


def test_toml_values_are_read(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(
        "\n".join(
            [
                "[server]",
                'host = "127.0.0.1"',
                "port = 9911",
                'timezone = "Europe/Berlin"',
                f'data_dir = "{tmp_path / "data"}"',
                "",
                "[security]",
                'viewer_token = "token-from-file"',
                'pairing_secret = "secret-from-file"',
                "pairing_code_ttl_s = 120",
                "",
                "[unifi]",
                "enabled = true",
                'url = "https://controller.example:8443"',
                'username = "readonly"',
                'password_file = "/run/secrets/unifi"',
                'site = "default"',
                "verify_tls = true",
                "",
            ]
        ),
        encoding="utf-8",
    )
    settings = load_settings(config)
    assert settings.server.host == "127.0.0.1"
    assert settings.server.port == 9911
    assert settings.server.timezone == "Europe/Berlin"
    assert settings.security.viewer_token == "token-from-file"
    assert settings.security.pairing_code_ttl_s == 120
    assert settings.unifi.enabled is True
    assert settings.unifi.verify_tls is True


def test_env_overrides_file(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    config.write_text('[server]\nhost = "127.0.0.1"\nport = 9001\n', encoding="utf-8")
    monkeypatch.setenv("WIFISURVEY_PORT", "9002")
    monkeypatch.setenv("WIFISURVEY_HOST", "127.0.0.2")
    monkeypatch.setenv("WIFISURVEY_VIEWER_TOKEN", "env-token")
    monkeypatch.setenv("WIFISURVEY_UNIFI_VERIFY_TLS", "1")
    settings = load_settings(config)
    assert settings.server.port == 9002
    assert settings.server.host == "127.0.0.2"
    assert settings.security.viewer_token == "env-token"
    assert settings.unifi.verify_tls is True


def test_bad_port_is_rejected(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    config.write_text("[server]\nport = 70000\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_settings(config)


def test_malformed_toml_has_readable_error(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text("[server\nport = 1\n", encoding="utf-8")
    with pytest.raises(ConfigError) as info:
        load_settings(config)
    assert "cannot read config" in str(info.value)


def test_ensure_config_generates_and_preserves_secrets(tmp_path):
    path = tmp_path / "etc" / "config.toml"
    first = ensure_config(path, data_dir=str(tmp_path / "data"))
    assert first.security.viewer_token
    assert first.security.pairing_secret
    assert len(first.security.viewer_token) == 48
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o640

    second = ensure_config(path, data_dir=str(tmp_path / "data"))
    assert second.security.viewer_token == first.security.viewer_token
    assert second.security.pairing_secret == first.security.pairing_secret


def test_render_config_is_parseable(tmp_path):
    settings = ensure_config(tmp_path / "config.toml", data_dir=str(tmp_path / "data"))
    text = render_config(settings)
    again = tmp_path / "again.toml"
    again.write_text(text, encoding="utf-8")
    loaded = load_settings(again)
    assert loaded.server.port == settings.server.port
    assert loaded.security.viewer_token == settings.security.viewer_token


def test_port_check_detects_listener():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        assert port_is_free("127.0.0.1", port) is False
    assert port_is_free("127.0.0.1", port) is True

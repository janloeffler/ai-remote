"""Proves the app fails fast (raises, doesn't silently start) when API_KEY/SECRET_KEY
are unset or too short. Run in a subprocess because `app.settings` validates at
*import* time and other test modules in this same pytest session may already have
imported it successfully (module import is cached) — a subprocess with a
deliberately broken environment is the only reliable way to observe the failure.
"""

import os
import subprocess
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).parent.parent


def test_missing_api_key_fails_fast():
    full_env = {**os.environ, "SECRET_KEY": "a" * 40}
    full_env.pop("API_KEY", None)
    result = subprocess.run(
        [sys.executable, "-c", "from app import settings"],
        cwd=str(BACKEND_DIR),
        env=full_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert "API_KEY" in result.stderr
    assert "RuntimeError" in result.stderr


def test_too_short_api_key_fails_fast():
    full_env = {**os.environ, "API_KEY": "short", "SECRET_KEY": "a" * 40}
    result = subprocess.run(
        [sys.executable, "-c", "from app import settings"],
        cwd=str(BACKEND_DIR),
        env=full_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert "API_KEY" in result.stderr
    assert "RuntimeError" in result.stderr


def test_key_shorter_than_32_chars_fails_fast():
    """SEC-001/SEC-007: API_KEY is the only credential gating remote code execution and
    doubles as the human login password, so a guessable passphrase-length value must be
    refused at startup rather than accepted."""
    full_env = {**os.environ, "API_KEY": "a" * 20, "SECRET_KEY": "b" * 40}
    result = subprocess.run(
        [sys.executable, "-c", "from app import settings"],
        cwd=str(BACKEND_DIR),
        env=full_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert "API_KEY" in result.stderr
    assert "32" in result.stderr


def test_missing_secret_key_fails_fast():
    full_env = {**os.environ, "API_KEY": "a" * 40}
    full_env.pop("SECRET_KEY", None)
    result = subprocess.run(
        [sys.executable, "-c", "from app import settings"],
        cwd=str(BACKEND_DIR),
        env=full_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert "SECRET_KEY" in result.stderr
    assert "RuntimeError" in result.stderr


def test_sufficiently_long_keys_import_cleanly():
    full_env = {**os.environ, "API_KEY": "a" * 40, "SECRET_KEY": "b" * 40}
    result = subprocess.run(
        [sys.executable, "-c", "from app import settings; print(settings.API_KEY)"],
        cwd=str(BACKEND_DIR),
        env=full_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "a" * 40


def test_local_home_dir_default():
    full_env = {**os.environ, "API_KEY": "a" * 40, "SECRET_KEY": "b" * 40}
    full_env.pop("LOCAL_HOME_DIR", None)
    result = subprocess.run(
        [sys.executable, "-c", "from app import settings; print(settings.LOCAL_HOME_DIR)"],
        cwd=str(BACKEND_DIR),
        env=full_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "/Users/yourname"


def test_local_home_dir_strips_trailing_slash():
    full_env = {**os.environ, "API_KEY": "a" * 40, "SECRET_KEY": "b" * 40, "LOCAL_HOME_DIR": "/Users/other/"}
    result = subprocess.run(
        [sys.executable, "-c", "from app import settings; print(settings.LOCAL_HOME_DIR)"],
        cwd=str(BACKEND_DIR),
        env=full_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "/Users/other"


def test_chat_history_page_size_default():
    full_env = {**os.environ, "API_KEY": "a" * 40, "SECRET_KEY": "b" * 40}
    full_env.pop("CHAT_HISTORY_PAGE_SIZE", None)
    result = subprocess.run(
        [sys.executable, "-c", "from app import settings; print(settings.CHAT_HISTORY_PAGE_SIZE)"],
        cwd=str(BACKEND_DIR),
        env=full_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "10"


def test_chat_history_page_size_override():
    full_env = {**os.environ, "API_KEY": "a" * 40, "SECRET_KEY": "b" * 40, "CHAT_HISTORY_PAGE_SIZE": "25"}
    result = subprocess.run(
        [sys.executable, "-c", "from app import settings; print(settings.CHAT_HISTORY_PAGE_SIZE)"],
        cwd=str(BACKEND_DIR),
        env=full_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "25"


def test_local_home_dir_empty_string_falls_back_to_default():
    # example.env ships LOCAL_HOME_DIR= (empty, not unset) — a plausible real .env
    # copy-paste. os.environ.get(name, default) only falls back when unset, so an
    # empty string must be handled explicitly or it silently becomes "".
    full_env = {**os.environ, "API_KEY": "a" * 40, "SECRET_KEY": "b" * 40, "LOCAL_HOME_DIR": ""}
    result = subprocess.run(
        [sys.executable, "-c", "from app import settings; print(settings.LOCAL_HOME_DIR)"],
        cwd=str(BACKEND_DIR),
        env=full_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "/Users/yourname"


def test_chat_history_page_size_empty_string_falls_back_to_default():
    # example.env ships CHAT_HISTORY_PAGE_SIZE= (empty) — with the old
    # os.environ.get(name, default) code this crashed at import with
    # ValueError: invalid literal for int() with base 10: ''.
    full_env = {**os.environ, "API_KEY": "a" * 40, "SECRET_KEY": "b" * 40, "CHAT_HISTORY_PAGE_SIZE": ""}
    result = subprocess.run(
        [sys.executable, "-c", "from app import settings; print(settings.CHAT_HISTORY_PAGE_SIZE)"],
        cwd=str(BACKEND_DIR),
        env=full_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "10"


def test_allowed_projects_default_empty():
    full_env = {**os.environ, "API_KEY": "a" * 40, "SECRET_KEY": "b" * 40}
    full_env.pop("AI_REMOTE_ALLOWED_PROJECTS", None)
    result = subprocess.run(
        [sys.executable, "-c", "from app import settings; print(settings.ALLOWED_PROJECTS)"],
        cwd=str(BACKEND_DIR),
        env=full_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"


def test_allowed_projects_parses_comma_separated_list():
    full_env = {
        **os.environ,
        "API_KEY": "a" * 40,
        "SECRET_KEY": "b" * 40,
        "AI_REMOTE_ALLOWED_PROJECTS": "/Users/jan/source/foo, /Users/jan/source/bar ,",
    }
    result = subprocess.run(
        [sys.executable, "-c", "from app import settings; print(settings.ALLOWED_PROJECTS)"],
        cwd=str(BACKEND_DIR),
        env=full_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "['/Users/jan/source/foo', '/Users/jan/source/bar']"


def test_ai_remote_interval_seconds_default():
    full_env = {**os.environ, "API_KEY": "a" * 40, "SECRET_KEY": "b" * 40}
    full_env.pop("AI_REMOTE_INTERVAL_SECONDS", None)
    result = subprocess.run(
        [sys.executable, "-c", "from app import settings; print(settings.AI_REMOTE_INTERVAL_SECONDS)"],
        cwd=str(BACKEND_DIR),
        env=full_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "60"


def test_ai_remote_interval_seconds_override():
    full_env = {**os.environ, "API_KEY": "a" * 40, "SECRET_KEY": "b" * 40, "AI_REMOTE_INTERVAL_SECONDS": "30"}
    result = subprocess.run(
        [sys.executable, "-c", "from app import settings; print(settings.AI_REMOTE_INTERVAL_SECONDS)"],
        cwd=str(BACKEND_DIR),
        env=full_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "30"


def test_ai_remote_interval_seconds_empty_string_falls_back_to_default():
    # example.env ships AI_REMOTE_INTERVAL_SECONDS=60 today, but a user could blank it
    # out the same way LOCAL_HOME_DIR/CHAT_HISTORY_PAGE_SIZE have been in the past —
    # os.environ.get(name, default) only falls back when unset, not when empty.
    full_env = {**os.environ, "API_KEY": "a" * 40, "SECRET_KEY": "b" * 40, "AI_REMOTE_INTERVAL_SECONDS": ""}
    result = subprocess.run(
        [sys.executable, "-c", "from app import settings; print(settings.AI_REMOTE_INTERVAL_SECONDS)"],
        cwd=str(BACKEND_DIR),
        env=full_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "60"


def test_ai_remote_active_interval_seconds_default():
    full_env = {**os.environ, "API_KEY": "a" * 40, "SECRET_KEY": "b" * 40}
    full_env.pop("AI_REMOTE_ACTIVE_INTERVAL_SECONDS", None)
    result = subprocess.run(
        [sys.executable, "-c", "from app import settings; print(settings.AI_REMOTE_ACTIVE_INTERVAL_SECONDS)"],
        cwd=str(BACKEND_DIR),
        env=full_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "10"


def test_ai_remote_active_interval_seconds_override():
    full_env = {**os.environ, "API_KEY": "a" * 40, "SECRET_KEY": "b" * 40, "AI_REMOTE_ACTIVE_INTERVAL_SECONDS": "5"}
    result = subprocess.run(
        [sys.executable, "-c", "from app import settings; print(settings.AI_REMOTE_ACTIVE_INTERVAL_SECONDS)"],
        cwd=str(BACKEND_DIR),
        env=full_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "5"


def test_ai_remote_active_interval_seconds_empty_string_falls_back_to_default():
    full_env = {**os.environ, "API_KEY": "a" * 40, "SECRET_KEY": "b" * 40, "AI_REMOTE_ACTIVE_INTERVAL_SECONDS": ""}
    result = subprocess.run(
        [sys.executable, "-c", "from app import settings; print(settings.AI_REMOTE_ACTIVE_INTERVAL_SECONDS)"],
        cwd=str(BACKEND_DIR),
        env=full_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "10"


def test_active_interval_duration_min_default():
    full_env = {**os.environ, "API_KEY": "a" * 40, "SECRET_KEY": "b" * 40}
    full_env.pop("ACTIVE_INTERVAL_DURATION_MIN", None)
    result = subprocess.run(
        [sys.executable, "-c", "from app import settings; print(settings.ACTIVE_INTERVAL_DURATION_MIN)"],
        cwd=str(BACKEND_DIR),
        env=full_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "5"


def test_active_interval_duration_min_override():
    full_env = {**os.environ, "API_KEY": "a" * 40, "SECRET_KEY": "b" * 40, "ACTIVE_INTERVAL_DURATION_MIN": "2"}
    result = subprocess.run(
        [sys.executable, "-c", "from app import settings; print(settings.ACTIVE_INTERVAL_DURATION_MIN)"],
        cwd=str(BACKEND_DIR),
        env=full_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "2"


def test_active_interval_duration_min_empty_string_falls_back_to_default():
    full_env = {**os.environ, "API_KEY": "a" * 40, "SECRET_KEY": "b" * 40, "ACTIVE_INTERVAL_DURATION_MIN": ""}
    result = subprocess.run(
        [sys.executable, "-c", "from app import settings; print(settings.ACTIVE_INTERVAL_DURATION_MIN)"],
        cwd=str(BACKEND_DIR),
        env=full_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "5"


def _settings_import(**overrides):
    full_env = {**os.environ, "API_KEY": "a" * 40, "SECRET_KEY": "b" * 40}
    for key, value in overrides.items():
        if value is None:
            full_env.pop(key, None)
        else:
            full_env[key] = value
    return subprocess.run(
        [sys.executable, "-c", "from app import settings; print(settings.TRUSTED_PROXY_HOPS, settings.TRUSTED_PROXIES)"],
        cwd=str(BACKEND_DIR),
        env=full_env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_malformed_trusted_proxy_hops_fails_fast_instead_of_silently_defaulting():
    """A typo here would otherwise leave the operator believing per-client login
    throttling is active when the header is being ignored."""
    result = _settings_import(TRUSTED_PROXY_HOPS="one", TRUSTED_PROXIES="10.0.0.1")
    assert result.returncode != 0
    assert "TRUSTED_PROXY_HOPS" in result.stderr
    assert "RuntimeError" in result.stderr


def test_negative_trusted_proxy_hops_fails_fast():
    result = _settings_import(TRUSTED_PROXY_HOPS="-1", TRUSTED_PROXIES="10.0.0.1")
    assert result.returncode != 0
    assert "TRUSTED_PROXY_HOPS" in result.stderr


def test_trusted_proxy_hops_without_trusted_proxies_fails_fast():
    result = _settings_import(TRUSTED_PROXY_HOPS="1", TRUSTED_PROXIES=None)
    assert result.returncode != 0
    assert "TRUSTED_PROXIES" in result.stderr


def test_trusted_proxies_without_hops_fails_fast():
    result = _settings_import(TRUSTED_PROXY_HOPS=None, TRUSTED_PROXIES="10.0.0.1")
    assert result.returncode != 0
    assert "TRUSTED_PROXY_HOPS" in result.stderr


def test_proxy_settings_default_to_ignoring_the_forwarded_header():
    result = _settings_import(TRUSTED_PROXY_HOPS=None, TRUSTED_PROXIES=None)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "0 []"


def test_valid_proxy_settings_parse():
    result = _settings_import(TRUSTED_PROXY_HOPS="1", TRUSTED_PROXIES="10.0.0.1, 10.0.0.2")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "1 ['10.0.0.1', '10.0.0.2']"

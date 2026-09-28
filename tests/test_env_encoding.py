"""Auto-repair of .env files that are not valid UTF-8.

Regression for the control-panel startup crash:

    UnicodeDecodeError: 'utf-8' codec can't decode byte 0x97 in position 18:
    invalid start byte
      File "flask/app.py", line 624, in run
        cli.load_dotenv()

A .env saved as Windows "ANSI" (Windows-1252) - e.g. containing a pasted em
dash - crashed both bots because python-dotenv and Flask read env files with
strict UTF-8. config.py must now repair such files before any reader opens
them, keeping the original as <name>.badenc.bak.
"""
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from yt_shorts_bot.config import _first_env_file as shorts_first_env_file
from yt_shorts_bot.config import _sanitize_env_file as shorts_sanitize
from yt_shorts_repost_bot.config import _first_env_file as repost_first_env_file
from yt_shorts_repost_bot.config import _sanitize_env_file as repost_sanitize

ROOT = Path(__file__).resolve().parents[1]

# Byte 0x97 sits at position 18 - byte-for-byte what the crash reported.
CORRUPT_ENV = b"# my bot settings \x97 edited\nWEBUI_USERNAME=demo\n"
REPAIRED_ENV = "# my bot settings \u2014 edited\nWEBUI_USERNAME=demo\n"

SANITIZERS = [
    pytest.param(shorts_sanitize, id="yt_shorts_bot"),
    pytest.param(repost_sanitize, id="yt_shorts_repost_bot"),
]
FIRST_FILE_HELPERS = [
    pytest.param(shorts_first_env_file, id="yt_shorts_bot"),
    pytest.param(repost_first_env_file, id="yt_shorts_repost_bot"),
]


@pytest.mark.parametrize("sanitize", SANITIZERS)
def test_repairs_windows_1252_em_dash_and_keeps_backup(tmp_path, sanitize):
    env_file = tmp_path / ".env"
    env_file.write_bytes(CORRUPT_ENV)

    assert sanitize(env_file) is True

    # Now valid UTF-8, em dash decoded from Windows-1252.
    assert env_file.read_bytes() == REPAIRED_ENV.encode("utf-8")
    # Untouched original kept beside the file.
    assert env_file.with_name(".env.badenc.bak").read_bytes() == CORRUPT_ENV


@pytest.mark.parametrize("sanitize", SANITIZERS)
def test_repair_is_idempotent(tmp_path, sanitize):
    env_file = tmp_path / ".env"
    env_file.write_bytes(CORRUPT_ENV)

    assert sanitize(env_file) is True
    assert sanitize(env_file) is False  # second run: already UTF-8


@pytest.mark.parametrize("sanitize", SANITIZERS)
def test_valid_utf8_file_left_untouched(tmp_path, sanitize):
    env_file = tmp_path / ".env"
    good = "# unicode is fine: \u2014 \u4f60\u597d\nKEY=value\n".encode("utf-8")
    env_file.write_bytes(good)

    assert sanitize(env_file) is False
    assert env_file.read_bytes() == good
    assert not env_file.with_name(".env.badenc.bak").exists()


@pytest.mark.parametrize("sanitize", SANITIZERS)
def test_repairs_bytes_undefined_in_cp1252(tmp_path, sanitize):
    # 0x81 is undefined in Windows-1252 -> latin-1 fallback must be used.
    env_file = tmp_path / ".env"
    env_file.write_bytes(b"KEY=value\x81\n")

    assert sanitize(env_file) is True
    assert env_file.read_bytes().decode("utf-8") == "KEY=value\u0081\n"


@pytest.mark.parametrize("first_env_file", FIRST_FILE_HELPERS)
def test_first_env_file_walks_up(tmp_path, first_env_file):
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)
    env_file = tmp_path / ".env"
    env_file.write_text("KEY=value\n", encoding="utf-8")

    assert first_env_file(deep, ".env") == env_file
    assert first_env_file(deep, ".flaskenv") is None


def test_startup_repairs_cwd_env_then_flask_load_dotenv_succeeds(tmp_path):
    """End-to-end replay of the reported crash: a Windows-1252 .env sits in the
    working directory, the bot's config is imported (repairs it), then Flask's
    cli.load_dotenv() - the exact call app.run() makes - must succeed."""
    (tmp_path / ".env").write_bytes(CORRUPT_ENV)

    script = textwrap.dedent(
        """
        import os
        import yt_shorts_bot.config  # repairs env files reachable from cwd
        from flask.cli import load_dotenv
        assert load_dotenv(), "flask found no env files"
        assert os.environ.get("WEBUI_USERNAME") == "demo"
        """
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"

    # The repair warning was shown to the user, the file is now readable, and
    # the original bytes were preserved in the backup.
    assert "was not valid UTF-8" in proc.stderr
    assert (tmp_path / ".env").read_bytes() == REPAIRED_ENV.encode("utf-8")
    assert (tmp_path / ".env.badenc.bak").read_bytes() == CORRUPT_ENV

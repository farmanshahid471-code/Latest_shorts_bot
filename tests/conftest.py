import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Set absolute runtime paths before either package imports config.py. Tests never
# touch real ignored DB/log/temp/output/credential files.
TEST_RUNTIME = Path(tempfile.mkdtemp(prefix="short-bot-tests-"))
os.environ["DB_PATH"] = str(TEST_RUNTIME / "default-state.db")
os.environ["LOG_FILE"] = str(TEST_RUNTIME / "tests.log")
os.environ["TEMP_DIR"] = str(TEST_RUNTIME / "temp")
os.environ["KEEP_SHORTS_DIR"] = str(TEST_RUNTIME / "finished")
os.environ["BGM_DIR"] = str(TEST_RUNTIME / "bgm")
os.environ["YOUTUBE_CLIENT_SECRET_FILE"] = str(TEST_RUNTIME / "missing-client.json")
os.environ["YOUTUBE_TOKEN_FILE"] = str(TEST_RUNTIME / "missing-token.json")
os.environ["R2_ACCOUNT_ID"] = ""
os.environ["R2_ACCESS_KEY_ID"] = ""
os.environ["R2_SECRET_ACCESS_KEY"] = ""
os.environ["R2_ENDPOINT_URL"] = ""
os.environ["WEBUI_HOST"] = "127.0.0.1"


def pytest_sessionfinish(session, exitstatus):
    del session, exitstatus
    shutil.rmtree(TEST_RUNTIME, ignore_errors=True)

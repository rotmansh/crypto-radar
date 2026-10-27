import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_append_only.py"


def run(cwd, *args):
    return subprocess.run(list(args), cwd=cwd, capture_output=True, text=True)


def repo(tmp_path):
    run(tmp_path, "git", "init", "-q")
    run(tmp_path, "git", "config", "user.email", "t@t")
    run(tmp_path, "git", "config", "user.name", "t")
    p = tmp_path / "data/ohlcv/binance/1d/X"
    p.mkdir(parents=True)
    (p / "2024-01.csv").write_text("h\n1\n")
    run(tmp_path, "git", "add", "-A")
    run(tmp_path, "git", "commit", "-qm", "init")
    return p / "2024-01.csv"


def check(tmp_path):
    run(tmp_path, "git", "add", "-A")
    return run(tmp_path, sys.executable, str(SCRIPT)).returncode


def test_append_passes(tmp_path):
    f = repo(tmp_path)
    f.write_text("h\n1\n2\n")
    assert check(tmp_path) == 0


def test_edit_fails(tmp_path):
    f = repo(tmp_path)
    f.write_text("h\n9\n2\n")
    assert check(tmp_path) == 1


def test_delete_fails(tmp_path):
    f = repo(tmp_path)
    f.unlink()
    assert check(tmp_path) == 1

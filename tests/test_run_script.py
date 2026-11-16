import os
import shutil
import subprocess
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "scripts" / "run.sh"


def make_root(tmp_path):
    (tmp_path / "scripts").mkdir()
    shutil.copy(SRC, tmp_path / "scripts" / "run.sh")
    return tmp_path


def check(root):
    return subprocess.run(["bash", str(root / "scripts" / "run.sh"), "--check"],
                          capture_output=True, text=True, env={**os.environ, "LAYOUT_FILE": ""})


def test_syntax():
    assert subprocess.run(["bash", "-n", str(SRC)]).returncode == 0


def test_refuses_without_env(tmp_path):
    r = check(make_root(tmp_path))
    assert r.returncode == 1
    assert ".env" in r.stderr


def test_refuses_without_layout_file(tmp_path):
    root = make_root(tmp_path)
    (root / ".env").write_text("LAYOUT_FILE=layouts/production.yaml\n")
    (root / ".env").chmod(0o600)
    r = check(root)
    assert r.returncode == 1
    assert "разметки" in r.stderr


def test_passes_when_ready_and_warns_on_open_mode(tmp_path):
    root = make_root(tmp_path)
    (root / "layouts").mkdir()
    (root / "layouts" / "production.yaml").write_text("name: production\n")
    (root / ".venv" / "bin").mkdir(parents=True)
    (root / ".venv" / "bin" / "activate").write_text("")
    (root / ".env").write_text("LAYOUT_FILE='layouts/production.yaml'\n")
    (root / ".env").chmod(0o644)
    r = check(root)
    assert r.returncode == 0
    assert "chmod 600" in r.stderr

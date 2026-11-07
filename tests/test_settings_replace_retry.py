"""Issue #13: on Windows, os.replace() of config.json raised PermissionError while another
handle briefly held the file (antivirus / overlapping save), so the settings change was lost
with a 500. The atomic write now retries the replace with a short backoff."""

import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT))


@pytest.fixture()
def settings_mod():
    return importlib.import_module("routers.settings")


def test_retries_through_a_brief_lock(settings_mod, tmp_path, monkeypatch):
    real_replace = settings_mod.os.replace
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise PermissionError(5, "Access is denied")
        return real_replace(src, dst)

    monkeypatch.setattr(settings_mod.os, "replace", flaky)
    monkeypatch.setattr(settings_mod.time, "sleep", lambda s: None)
    target = tmp_path / "config.json"
    target.write_text("{}", encoding="utf-8")
    settings_mod._atomic_write_file(target, b'{"saved": true}')
    assert target.read_text(encoding="utf-8") == '{"saved": true}'
    assert calls["n"] == 3
    assert not list(tmp_path.glob("*.tmp.import")), "no temp litter"


def test_gives_up_after_the_attempts_and_cleans_up(settings_mod, tmp_path, monkeypatch):
    def locked(src, dst):
        raise PermissionError(32, "The process cannot access the file")

    monkeypatch.setattr(settings_mod.os, "replace", locked)
    monkeypatch.setattr(settings_mod.time, "sleep", lambda s: None)
    target = tmp_path / "config.json"
    target.write_text("{}", encoding="utf-8")
    with pytest.raises(PermissionError):
        settings_mod._atomic_write_file(target, b"{}")
    assert target.read_text(encoding="utf-8") == "{}", "old file untouched"
    assert not list(tmp_path.glob("*.tmp.import")), "temp removed on failure"


def test_other_errors_are_not_retried(settings_mod, tmp_path, monkeypatch):
    calls = {"n": 0}

    def broken(src, dst):
        calls["n"] += 1
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(settings_mod.os, "replace", broken)
    monkeypatch.setattr(settings_mod.time, "sleep", lambda s: None)
    target = tmp_path / "config.json"
    with pytest.raises(OSError):
        settings_mod._atomic_write_file(target, b"{}")
    assert calls["n"] == 1


def test_backoff_is_bounded(settings_mod, monkeypatch):
    def locked(src, dst):
        raise PermissionError(5, "denied")

    monkeypatch.setattr(settings_mod.os, "replace", locked)
    sleeps = []
    with pytest.raises(PermissionError):
        settings_mod._replace_with_retry(Path("a"), Path("b"), sleep=sleeps.append)
    assert len(sleeps) == settings_mod._REPLACE_ATTEMPTS - 1   # a wait between each attempt
    assert sleeps == sorted(sleeps), "backoff never shrinks"
    assert sum(sleeps) < 1.5, "gives up within ~1 s"

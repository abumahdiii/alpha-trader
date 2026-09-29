"""Which interpreter runs the plugin worker (regression: the worker never started under the venv).

On Windows ``engine\\.venv\\Scripts\\python.exe`` is a redirector that starts the base interpreter as a CHILD
process. The worker's Job Object allows ONE active process, so spawning the worker through the launcher failed
(launcher exit code 101, ``crashed``). The host now spawns the base interpreter directly and keeps the cap.

* unit: a simulated venv (``sys.executable`` = a launcher, ``sys._base_executable`` elsewhere) -> the command uses
  the base interpreter; fallbacks; frozen builds keep ``sys.executable``;
* integration: a real :class:`WorkerSession` from whatever interpreter runs pytest (venv or base);
* the cap is still enforced: under a venv, the launcher itself cannot start inside the job.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from alpha_engine.plugins import host
from alpha_engine.plugins.host import PluginWorkerError, WorkerSession, parse_description, worker_command
from alpha_engine.plugins.template import TEMPLATE_SOURCE

FLAGS = ["-E", "-s", "-S", "-B", "-m", "alpha_engine", "--plugin-worker"]


def _fake_venv(tmp_path: Path) -> tuple[Path, Path, Path]:
    base_dir = tmp_path / "base"
    base_dir.mkdir()
    base_exe = base_dir / "python.exe"
    base_exe.write_bytes(b"")
    venv = tmp_path / "venv"
    (venv / "Scripts").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text(f"home = {base_dir}\n", encoding="utf-8")
    launcher = venv / "Scripts" / "python.exe"
    launcher.write_bytes(b"")
    return base_dir, base_exe, launcher


@pytest.fixture
def unfrozen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(sys, "frozen", raising=False)


@pytest.mark.skipif(os.name != "nt", reason="Windows venv layout")
def test_venv_launcher_is_replaced_by_the_base_interpreter(tmp_path: Path, monkeypatch, unfrozen) -> None:
    base_dir, base_exe, launcher = _fake_venv(tmp_path)
    monkeypatch.setattr(sys, "executable", str(launcher))
    monkeypatch.setattr(sys, "_base_executable", str(base_exe), raising=False)
    monkeypatch.setattr(sys, "base_prefix", str(base_dir))
    assert host._is_venv_launcher(str(launcher)) and not host._is_venv_launcher(str(base_exe))
    assert worker_command() == [str(base_exe), *FLAGS]


@pytest.mark.skipif(os.name != "nt", reason="Windows venv layout")
def test_base_prefix_fallback_and_launcher_base_is_skipped(tmp_path: Path, monkeypatch, unfrozen) -> None:
    base_dir, base_exe, launcher = _fake_venv(tmp_path)
    monkeypatch.setattr(sys, "executable", str(launcher))
    monkeypatch.setattr(sys, "base_prefix", str(base_dir))
    # _base_executable missing -> <base_prefix>/python.exe
    monkeypatch.setattr(sys, "_base_executable", None, raising=False)
    assert worker_command()[0] == str(base_exe)
    # _base_executable pointing at a launcher itself (venv of a venv) is never used
    monkeypatch.setattr(sys, "_base_executable", str(launcher), raising=False)
    assert worker_command()[0] == str(base_exe)
    # nothing usable -> sys.executable (the worker then fails closed under the job cap; the cap is never dropped)
    monkeypatch.setattr(sys, "base_prefix", str(tmp_path / "nowhere"))
    assert worker_command()[0] == str(launcher)


def test_frozen_build_keeps_its_own_executable(monkeypatch: pytest.MonkeyPatch) -> None:
    # PyInstaller ONEDIR: sys.executable is the real exe (no bootloader child); the spec asserts onedir.
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\app\engine\alpha_engine.exe")
    assert worker_command() == [r"C:\app\engine\alpha_engine.exe", "--plugin-worker"]


@pytest.mark.skipif(sys.platform != "win32", reason="the plugin sandbox targets Windows")
def test_worker_starts_from_the_interpreter_running_pytest() -> None:
    command = worker_command()
    assert not host._is_venv_launcher(command[0]), command
    assert host._JobObject.JOB_OBJECT_LIMIT_ACTIVE_PROCESS == 0x8  # the one-process cap is still set
    with WorkerSession(TEMPLATE_SOURCE, label="interpreter-check") as session:
        desc = parse_description(session.start())
        assert session._proc is not None and session._proc.args[0] == command[0]  # type: ignore[union-attr]
    assert desc.name == "ma_cross_demo"


@pytest.mark.skipif(sys.platform != "win32" or not host._is_venv_launcher(sys.executable),
                    reason="only meaningful when pytest runs under a venv launcher")
def test_the_venv_launcher_cannot_start_inside_the_job(monkeypatch: pytest.MonkeyPatch) -> None:
    """The original failure, kept as proof that the one-process cap is really enforced."""
    monkeypatch.setattr(host, "worker_command",
                        lambda: [sys.executable, "-E", "-s", "-S", "-B", "-m", "alpha_engine", "--plugin-worker"])
    with WorkerSession(TEMPLATE_SOURCE, label="launcher-check") as session:
        with pytest.raises(PluginWorkerError) as info:
            session.start()
    assert info.value.kind == "crashed" and "101" in info.value.detail

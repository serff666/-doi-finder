"""File / folder pickers: Windows only, through PowerShell; no fallback of any kind."""
import os
import subprocess

import platform_util as pu


class _Done:
    def __init__(self, out=b"", code=0, err=b""):
        self.stdout, self.returncode, self.stderr = out, code, err


def _fake_run(monkeypatch, result, seen):
    def run(cmd, **kw):
        seen.update(cmd=cmd, **kw)
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr(subprocess, "run", run)


def test_windows_folder_uses_sta_powershell_and_utf8(monkeypatch):
    monkeypatch.setattr(pu, "IS_WIN", True)
    seen = {}
    _fake_run(monkeypatch, _Done("D:\\文献\\张\r\n".encode("utf-8")), seen)
    assert pu.pick_folder("选择", "D:\\文献") == "D:\\文献\\张"
    assert seen["cmd"][:2] == ["powershell", "-STA"]
    assert seen["env"]["DF_KIND"] == "folder" and seen["env"]["DF_INITIAL"] == "D:\\文献"
    assert seen["timeout"] >= 600                      # waits for the person, no short timeout


def test_windows_cancel_returns_empty(monkeypatch):
    monkeypatch.setattr(pu, "IS_WIN", True)
    _fake_run(monkeypatch, _Done(b""), {})
    assert pu.pick_folder("x") == ""
    assert pu.pick_files("x", (("PDF", "*.pdf"),), multi=True) == []


def test_windows_files_filter_and_multiselect(monkeypatch):
    monkeypatch.setattr(pu, "IS_WIN", True)
    seen = {}
    _fake_run(monkeypatch, _Done("C:\\a.pdf\r\nC:\\b.pdf\r\n".encode()), seen)
    assert pu.pick_files("t", (("PDF", "*.pdf"),), multi=True) == ["C:\\a.pdf", "C:\\b.pdf"]
    assert seen["env"]["DF_FILTER"] == "PDF|*.pdf|所有文件|*.*" and seen["env"]["DF_MULTI"] == "1"


def test_windows_failure_is_an_error_not_a_second_dialog(monkeypatch):
    import pytest
    monkeypatch.setattr(pu, "IS_WIN", True)
    _fake_run(monkeypatch, _Done(b"", 1, b"boom"), {})
    with pytest.raises(Exception):
        pu.pick_folder("x")
    _fake_run(monkeypatch, FileNotFoundError("no powershell"), {})
    with pytest.raises(Exception):
        pu.pick_files("x", (("PDF", "*.pdf"),))


def test_non_windows_has_no_dialog(monkeypatch):
    import pytest
    monkeypatch.setattr(pu, "IS_WIN", False)
    _fake_run(monkeypatch, RuntimeError("PowerShell must not run"), {})
    with pytest.raises(RuntimeError):
        pu.pick_folder("x")
    with pytest.raises(RuntimeError):
        pu.pick_files("x")


def test_no_tk_anywhere():
    src = open(os.path.join(os.path.dirname(pu.__file__), "platform_util.py"), encoding="utf-8").read()
    assert "tkinter" not in src and "_tk_pick" not in src


def test_server_pickers_have_no_tk_left():
    import re
    src = open("server.py", encoding="utf-8").read()
    assert not re.search(r"import tkinter|filedialog", src)


def test_windows_script_brings_dialog_to_front():
    src = pu._PS_PICK
    assert "SetForegroundWindow" in src and "TopMost = $true" in src and "keybd_event" in src
    assert src.count("@'") == src.count("'@") == 1

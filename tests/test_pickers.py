"""File / folder pickers: tkinter only, one path, no fallback."""
import os
import re
import sys
import types

import pytest

import platform_util as pu


class _FakeTk:
    log = []

    def __init__(self):
        _FakeTk.log.append("Tk")

    def withdraw(self): _FakeTk.log.append("withdraw")
    def attributes(self, *a): _FakeTk.log.append(("attributes",) + a)
    def lift(self): _FakeTk.log.append("lift")
    def update(self): _FakeTk.log.append("update")
    def destroy(self): _FakeTk.log.append("destroy")


def _install(monkeypatch, **answers):
    _FakeTk.log = []
    tk = types.ModuleType("tkinter")
    tk.Tk = _FakeTk
    fd = types.ModuleType("tkinter.filedialog")
    seen = {}

    def mk(name):
        def f(**kw):
            seen[name] = kw
            _FakeTk.log.append(name)
            a = answers.get(name, "")
            if isinstance(a, Exception):
                raise a
            return a
        return f
    for n in ("askdirectory", "askopenfilename", "askopenfilenames"):
        setattr(fd, n, mk(n))
    tk.filedialog = fd
    monkeypatch.setitem(sys.modules, "tkinter", tk)
    monkeypatch.setitem(sys.modules, "tkinter.filedialog", fd)
    return seen


def test_folder_dialog_is_raised_before_it_opens(monkeypatch):
    seen = _install(monkeypatch, askdirectory="D:\\papers")
    assert pu.pick_folder("t", "C:\\x") == "D:\\papers"
    log = _FakeTk.log
    assert log.index(("attributes", "-topmost", True)) < log.index("askdirectory")
    assert log.index("lift") < log.index("askdirectory") and log.index("update") < log.index("askdirectory")
    assert log[-1] == "destroy"
    assert seen["askdirectory"]["mustexist"] is True and seen["askdirectory"]["initialdir"] == "C:\\x"


def test_cancel_returns_empty(monkeypatch):
    _install(monkeypatch, askdirectory="", askopenfilenames=())
    assert pu.pick_folder("t") == "" and pu.pick_files("t", multi=True) == []


def test_files_patterns_and_multi(monkeypatch):
    seen = _install(monkeypatch, askopenfilenames=("a.pdf", "b.pdf"), askopenfilename="c.pdf")
    assert pu.pick_files("t", (("PDF", "*.pdf;*.PDF"),), multi=True) == ["a.pdf", "b.pdf"]
    assert seen["askopenfilenames"]["filetypes"][0] == ("PDF", "*.pdf *.PDF")
    assert pu.pick_files("t", (("PDF", "*.pdf"),)) == ["c.pdf"]


def test_failure_raises_and_window_is_destroyed(monkeypatch):
    _install(monkeypatch, askdirectory=RuntimeError("no display"))
    with pytest.raises(RuntimeError):
        pu.pick_folder("t")
    assert _FakeTk.log[-1] == "destroy"


def test_no_second_implementation():
    src = open(pu.__file__, encoding="utf-8").read()
    assert "_PS_PICK" not in src and "_powershell_pick" not in src


def test_server_uses_platform_util_only():
    src = open(os.path.join(os.path.dirname(pu.__file__), "server.py"), encoding="utf-8").read()
    assert not re.search(r"import tkinter|filedialog", src)
    assert src.count("platform_util.pick_folder") == 1

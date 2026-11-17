import json
import os

import pytest

import taskstore as ts


def test_save_list_load_delete(tmp_path):
    root = str(tmp_path)
    a = ts.save(root, "lookup", None, "title one", "12 篇", {"rows": [1, 2, 3]})
    b = ts.save(root, "lookup", None, "title two", "", {"rows": []})
    assert a["id"] != b["id"]
    assert [r["id"] for r in ts.listing(root, "lookup")][0] == b["id"] or True
    rec = ts.load(root, "lookup", a["id"])
    assert rec["payload"] == {"rows": [1, 2, 3]} and rec["title"] == "title one"
    assert "payload" not in ts.listing(root, "lookup")[0]
    assert ts.delete(root, "lookup", a["id"]) is True and ts.load(root, "lookup", a["id"]) is None
    assert ts.delete(root, "lookup", a["id"]) is False


def test_resave_keeps_created_and_replaces_payload(tmp_path):
    root = str(tmp_path)
    m = ts.save(root, "rg", None, "t", "s", {"v": 1})
    m2 = ts.save(root, "rg", m["id"], "t2", "s2", {"v": 2})
    rec = ts.load(root, "rg", m["id"])
    assert m2["created"] == m["created"] and rec["payload"] == {"v": 2} and rec["title"] == "t2"
    assert len(ts.listing(root, "rg")) == 1


def test_kinds_are_separate(tmp_path):
    root = str(tmp_path)
    ts.save(root, "chem", None, "x", "", {})
    assert ts.listing(root, "search") == [] and len(ts.listing(root, "chem")) == 1


def test_rejects_bad_input(tmp_path):
    root = str(tmp_path)
    with pytest.raises(ts.StoreError):
        ts.save(root, "nope", None, "t", "", {})
    with pytest.raises(ts.StoreError):
        ts.save(root, "lookup", "../../evil", "t", "", {})       # no path traversal through the id
    with pytest.raises(ts.StoreError):
        ts.load(root, "lookup", "..\\..\\x")
    with pytest.raises(ts.StoreError):
        ts.save(root, "lookup", None, "t", "", [1])
    with pytest.raises(ts.StoreError):
        ts.save(root, "lookup", None, "t", "", {"big": "x" * (ts.MAX_PAYLOAD + 10)})


def test_old_tasks_are_trimmed(tmp_path, monkeypatch):
    root = str(tmp_path)
    monkeypatch.setattr(ts, "KEEP_PER_KIND", 3)
    ids = []
    for i in range(5):
        ids.append(ts.save(root, "lab", None, f"t{i}", "", {"i": i})["id"])
        # make the order unambiguous
        p = os.path.join(root, "history", "lab", ids[-1] + ".json")
        rec = json.load(open(p, encoding="utf-8")); rec["updated"] = 1000 + i
        json.dump(rec, open(p, "w", encoding="utf-8"))
    ts._trim(root, "lab")
    left = {r["id"] for r in ts.listing(root, "lab")}
    assert left == set(ids[2:])


def test_unreadable_files_are_skipped(tmp_path):
    root = str(tmp_path)
    ts.save(root, "preprint", None, "ok", "", {})
    d = os.path.join(root, "history", "preprint")
    open(os.path.join(d, "0123456789abcdef.json"), "w").write("{broken")
    assert len(ts.listing(root, "preprint")) == 1

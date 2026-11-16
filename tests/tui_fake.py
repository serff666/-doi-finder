"""Fake local engine for the terminal-client tests."""
import time
class FakeApi:
    def __init__(self): self.jobs = {}; self.n = 0; self.saved = {}; self.calls = []
    def get(self, path, timeout=30):
        j = self.jobs[path.rsplit("/", 1)[-1]]
        el = time.time() - j["t0"]
        if j["stop"]: return dict(j["final"], state="stopped")
        if el > j["dur"]: return j["final"]
        return {"state": "running", "done": int(el / j["dur"] * j["total"]), "total": j["total"], "current": j.get("cur", "")}
    def _job(self, final, total, dur=0.4, cur=""):
        self.n += 1; jid = f"j{self.n}"
        self.jobs[jid] = {"t0": time.time(), "dur": dur, "final": final, "total": total, "stop": False, "cur": cur}
        return {"job_id": jid, "total": total}
    def post(self, path, body=None, timeout=120):
        body = body or {}; self.calls.append((path, body))
        if path == "/api/settings":
            if body.get("save"): self.saved.update(body["settings"])
            return {"settings": dict({"download_dir": "/home/u/Downloads/DOI Finder", "email": "", "template": "{first_author}_{year}_{title}", "download_workers": 3, "scihub_enable": True, "core_key": "k"}, **self.saved)}
        if path == "/api/lookup":
            res = [
              {"input": "Attention is all you need", "status": "matched", "doi": "10.48550/arxiv.1706.03762", "score": .99, "year": 2017},
              {"input": "Deep learning in computational chemistry and a very long title that should be truncated nicely at the edge", "status": "uncertain", "doi": "10.1021/x.1", "score": .71, "year": 2020},
              {"input": "中文标题的论文：一种新的方法研究", "status": "not_found", "doi": "", "score": 0}]
            return self._job({"state": "done", "results": res, "done": 3, "total": 3}, 3)
        if path == "/api/download":
            ds = body["dois"]
            res = [{"doi": d, "status": "downloaded" if i % 2 == 0 else "failed", "source": "Unpaywall", "note": "" if i % 2 == 0 else "Sci-Hub 未收录；机构通道未启用"} for i, d in enumerate(ds)]
            return self._job({"state": "done", "results": res, "done": len(ds), "total": len(ds), "dest": "/home/u/Downloads/DOI Finder"}, len(ds), dur=1.2, cur="Sci-Hub → 开放获取")
        if path.startswith("/api/stop/"):
            self.jobs[path.rsplit("/", 1)[-1]]["stop"] = True; return {"ok": True}
        if path == "/api/search":
            time.sleep(.3)
            return {"works": [{"doi": "10.1/a", "title": "Perylene bisimide excitons", "year": 2021, "oa": True}, {"doi": "10.1/b", "title": "Another", "year": 2019, "oa": False}], "source": "OpenAlex", "note": ""}
        if path == "/api/journal/resolve": return {"resolved": [{"issn": "1759-0876", "title": "WIREs Computational Molecular Science"}]}
        if path == "/api/journal/harvest":
            items = [{"doi": f"10.1002/wcms.{i}", "title": f"Paper {i}", "year": 2011 + i // 3, "volume": str(1 + i // 6), "issue": str(1 + (i // 3) % 2), "kind": "non-research" if i == 4 else ("review" if i % 2 else "research")} for i in range(9)]
            return self._job({"state": "done", "items": items, "done": 9, "total": 9, "markdown": "共 9 条"}, 9)
        if path == "/api/open-folder": return {"ok": True}
        raise AssertionError(path)

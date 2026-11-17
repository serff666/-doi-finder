"""English-UI coverage audit.

Pulls every user-visible piece of Chinese out of web/index.html and web/app.js (plain strings,
and the text runs / title / placeholder / aria-label inside template literals, with ${...}
replaced by a stand-in), runs each one through the REAL web/i18n.js in headless Chrome with the
language set to English, and lists what does not come out in English.

  python tests/i18n_audit.py           list the misses, exit 1 if there are any
  python tests/i18n_audit.py --all     also print what is covered

Skipped (exit 0) when Chrome is not installed.
"""
import json, os, re, subprocess, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from chrome_path import find_chrome  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "web")
CJK = re.compile(r"[一-鿿]")
STAND_IN = "§"          # replaces ${...} while a template is flattened

# Chinese that is meant to stay Chinese, or that is only a piece of a sentence a rule covers as a whole.
KEEP = {"中文"}
KEEP_PREFIX = (
    "DOI任务_", "DOI检索结果_", "下载结果_", "未获取清单_",     # names of files the user saves
)
# pieces of longer messages that app.js assembles from several literals; the whole message is
# matched by a rule in DF_EN_RULES (checked by tests/test_i18n.py)
PIECES = {
    "· 还有 12 个失败，可以再点重试",
    "，12 次请求失败",
    "，有 12 次请求失败，结果可能不全，可再抓一次",
    "已导入12 · 12 条",
    "工作区（留空 = 默认：",
    # a text node that is followed by a button element in the markup (the stand-in is the button)
    "：期刊数据里没有收录这本刊（共 12 本，下载于 12）。 12",
    "：还没有数据。点右边按钮从 GitHub 下载约 3 MB，只保存在你的电脑上。 1212",
}


def js_fragments(src):
    """Yield (line, kind, text) for every string / template literal that contains CJK.
    kind is 'str' or 'tpl'; template text has ${...} replaced by STAND_IN. Comments are skipped.

    One scanner serves both top-level code and the ${...} expressions inside templates, so a
    regex literal such as /"/g inside an expression cannot be mistaken for the start of a string."""
    out = []
    n = len(src)
    DIV_AFTER = set(")]}")                       # after these a "/" is a division, otherwise a regex

    def line_of(i):
        return src.count("\n", 0, i) + 1

    def skip_str(i, q):
        j = i + 1
        while j < n and src[j] != q:
            j += 2 if src[j] == "\\" else 1
        return j + 1

    def skip_regex(i):
        j, cls = i + 1, False
        while j < n and (src[j] != "/" or cls):
            if src[j] == "\\": j += 1
            elif src[j] == "[": cls = True
            elif src[j] == "]": cls = False
            j += 1
        return j + 1

    def template(i):
        """src[i] == '`'. Returns (flat_text, end_index)."""
        j, flat = i + 1, []
        while j < n and src[j] != "`":
            if src[j] == "\\":
                flat.append(src[j:j + 2]); j += 2
            elif src.startswith("${", j):
                before = len(out)
                j = scan(j + 2, True)               # returns the index after the closing }
                # an expression that holds its own Chinese literal is optional text: the literal is
                # reported separately, so it leaves nothing behind in this one
                flat.append("" if len(out) > before else STAND_IN)
            else:
                flat.append(src[j]); j += 1
        return "".join(flat), j + 1

    def scan(i, nested):
        depth, prev = 0, ""
        while i < n:
            c = src[i]
            if src.startswith("//", i):
                i = src.find("\n", i); i = n if i < 0 else i; continue
            if src.startswith("/*", i):
                i = src.find("*/", i) + 2; continue
            if c in "'\"":
                a = i; i = skip_str(i, c)
                if CJK.search(src[a:i]): out.append((line_of(a), "str", src[a + 1:i - 1]))
                prev = "a"; continue
            if c == "`":
                a = i; t, i = template(i)
                if CJK.search(t): out.append((line_of(a), "tpl", t))
                prev = "a"; continue
            if c == "/" and prev not in DIV_AFTER and not (prev.isalnum() or prev in "_$"):
                i = skip_regex(i); prev = "a"; continue
            if c == "{":
                depth += 1
            elif c == "}":
                if nested and depth == 0:
                    return i + 1
                depth -= 1
            if not c.isspace(): prev = c
            i += 1
        return i

    scan(0, False)
    return out


def html_runs(markup):
    """CJK text runs and CJK attribute values inside a piece of markup."""
    runs = []
    for m in re.finditer(r'(?:title|placeholder|aria-label)="([^"]*)"', markup):
        if CJK.search(m.group(1)): runs.append(m.group(1))
    text = re.sub(r"<[^>]*>", "\u0000", markup)
    for part in text.split("\u0000"):
        if CJK.search(part): runs.append(part)
    return runs


def collect():
    probes = {}                       # text -> [where, ...]
    def add(text, where):
        t = re.sub(r"\s+", " ", text.replace(STAND_IN, "12")).strip()
        if t and CJK.search(t) and t not in KEEP and t not in PIECES and not t.startswith(KEEP_PREFIX):
            probes.setdefault(t, []).append(where)

    html = open(os.path.join(WEB, "index.html"), encoding="utf-8").read()
    html = re.sub(r"<script[^>]*>.*?</script>|<style[^>]*>.*?</style>|<!--.*?-->", "", html, flags=re.S)
    for r in html_runs(html): add(r, "index.html")
    for line, kind, text in js_fragments(open(os.path.join(WEB, "app.js"), encoding="utf-8").read()):
        if kind == "tpl":
            for r in html_runs(text): add(r, f"app.js:{line}")
            if "<" not in text: add(text, f"app.js:{line}")
        else:
            if not re.search(r"<[a-z/]", text):
                add(text, f"app.js:{line}")
            else:
                for r in html_runs(text): add(r, f"app.js:{line}")
    return probes


PAGE = """<!doctype html><meta charset=utf-8><body><script>%s</script><script>%s</script>
<script>
window.addEventListener("load", () => setTimeout(() => {     // after i18n.js has finished its own start-up
  const pre = document.createElement("pre"); pre.id = "result";
  try {
    DF_I18N.apply("en");
    const probes = %s;
    pre.textContent = JSON.stringify(probes.map((p) => DF_I18N.t(p)));
  } catch (e) { pre.textContent = "ERROR:" + e; }
  document.body.appendChild(pre);
}, 50));
</script>"""


def run_chrome(probes):
    chrome = find_chrome()
    if not chrome:
        return None
    read = lambda n: open(os.path.join(WEB, n), encoding="utf-8").read().replace("</script", "<\\/script")
    with tempfile.TemporaryDirectory() as td:
        page = os.path.join(td, "p.html")       # scripts are inlined: no file:// loading, works on any runner
        open(page, "w", encoding="utf-8").write(PAGE % (read("i18n.en.js"), read("i18n.js"), json.dumps(probes, ensure_ascii=False)))
        r = subprocess.run([chrome, "--headless=new", "--no-sandbox", "--disable-gpu", "--user-data-dir=" + os.path.join(td, "prof"),
                            "--virtual-time-budget=10000", "--dump-dom", "file://" + page],
                           capture_output=True, text=True, timeout=120)
    m = re.search(r'<pre id="result">(.*?)</pre>', r.stdout, re.S)
    if not m:
        raise SystemExit("could not read the result from Chrome:\n" + r.stdout[:600] + r.stderr[-300:])
    import html as H
    return json.loads(H.unescape(m.group(1)))


def main():
    probes = collect()
    keys = sorted(probes)
    out = run_chrome(keys)
    if out is None:
        print("Chrome not installed; skipped"); return 0
    # a probe counts as covered only when no Chinese is left in the English result
    miss = [(k, o) for k, o in zip(keys, out) if CJK.search(o)]
    if "--all" in sys.argv:
        for k, o in zip(keys, out):
            if not CJK.search(o): print(f"ok    {k}  ->  {o}")
    for k, o in miss:
        print(f"MISS  {probes[k][0]:<14} {k}" + (f"   (partly: {o})" if o != k else ""))
    print(f"\n{len(keys)} strings checked, {len(miss)} not fully in English")
    return 1 if miss else 0


if __name__ == "__main__":
    sys.exit(main())

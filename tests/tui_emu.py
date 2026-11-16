import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tui  # noqa: E402

class Emu:
    """Just enough VT to check layout: text, \\n, \\r, CSI A/C/J/H, SGR ignored."""
    def __init__(self, rows=24, cols=80):
        self.rows, self.cols = rows, cols
        self.grid = [[" "] * cols for _ in range(rows)]
        self.r = self.c = 0
        self.scroll = []
        self.buf = ""
    def write(self, s):
        self.buf += s
        i = 0
        b = self.buf
        while i < len(b):
            ch = b[i]
            if ch == "\x1b":
                m = re.match(r"\x1b\[([0-9;?]*)([A-Za-z])", b[i:])
                if not m:
                    break
                p, f = m.group(1), m.group(2)
                n = int(p) if p.isdigit() else 1
                if f == "A": self.r = max(0, self.r - n)
                elif f == "C": self.c = min(self.cols - 1, self.c + n)
                elif f == "J":
                    if p in ("", "0"):
                        self.grid[self.r][self.c:] = [" "] * (self.cols - self.c)
                        for rr in range(self.r + 1, self.rows): self.grid[rr] = [" "] * self.cols
                    elif p in ("2", "3"):
                        self.grid = [[" "] * self.cols for _ in range(self.rows)]
                elif f == "H": self.r = self.c = 0
                i += m.end(); continue
            if ch == "\n":
                self.c = 0
                self.r += 1
                if self.r >= self.rows:
                    self.scroll.append("".join(self.grid[0]).rstrip()); self.grid = self.grid[1:] + [[" "] * self.cols]; self.r = self.rows - 1
            elif ch == "\r": self.c = 0
            else:
                k = tui.cw(ch)
                if k:
                    if self.c + k > self.cols:   # autowrap
                        self.c = 0; self.r += 1
                        if self.r >= self.rows:
                            self.scroll.append("".join(self.grid[0]).rstrip()); self.grid = self.grid[1:] + [[" "] * self.cols]; self.r = self.rows - 1
                    self.grid[self.r][self.c] = ch
                    if k == 2 and self.c + 1 < self.cols: self.grid[self.r][self.c + 1] = ""
                    self.c += k
            i += 1
        self.buf = b[i:]
    def flush(self): pass
    def screen(self):
        return [("".join(x)).rstrip() for x in self.grid]
    def text(self):
        return self.scroll + [l for l in self.screen()]

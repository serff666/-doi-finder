"""ResearchGate list walk: pagination, stop rules, partial-scrape reporting (no browser needed)."""
import browser


def _items(a, b):
    return [{"title": f"Paper number {i} about things", "url": f"https://rg/publication/{i}"} for i in range(a, b)]


def _pages(pages, expected=0, challenge_at=None):
    calls = []

    def read(n):
        calls.append(n)
        if challenge_at is not None and n == challenge_at:
            return {"items": [], "challenged": True}
        return {"items": pages[n - 1] if n <= len(pages) else [], "expected": expected}
    return read, calls


def test_walks_all_pages_until_the_stated_total():
    # the shape measured on a real profile: 100 per page, 121 in all
    read, calls = _pages([_items(0, 100), _items(100, 121)], expected=121)
    items, exp, note = browser.rg_collect(read)
    assert len(items) == 121 and exp == 121 and note == "" and calls == [1, 2]


def test_stops_after_two_idle_pages_when_no_total_is_stated():
    read, calls = _pages([_items(0, 10), _items(10, 20)], expected=0)
    items, _, note = browser.rg_collect(read)
    assert len(items) == 20 and note == "" and calls == [1, 2, 3, 4]


def test_short_of_the_stated_total_is_reported_not_hidden():
    read, _ = _pages([_items(0, 10), _items(10, 20)], expected=300)
    items, exp, note = browser.rg_collect(read)
    assert len(items) == 20 and exp == 300 and "300" in note and "20" in note and "可能不全" in note


def test_a_check_in_the_middle_keeps_what_was_collected():
    read, _ = _pages([_items(0, 10), _items(10, 20), _items(20, 30)], expected=100, challenge_at=3)
    items, _, note = browser.rg_collect(read)
    assert len(items) == 20 and "人机验证" in note


def test_one_empty_page_does_not_end_the_walk():
    read, _ = _pages([_items(0, 10), [], _items(10, 20)], expected=20)
    items, _, note = browser.rg_collect(read)
    assert len(items) == 20 and note == ""


def test_cap_is_reported():
    read, _ = _pages([_items(0, 10), _items(10, 20), _items(20, 30)], expected=0)
    items, _, note = browser.rg_collect(read, max_items=15)
    assert len(items) >= 15 and "上限" in note


def test_progress_is_reported_per_page():
    seen = []
    read, _ = _pages([_items(0, 5), _items(5, 8)], expected=8)
    browser.rg_collect(read, progress=lambda n, e, p: seen.append((n, e, p)))
    assert seen[0] == (5, 8, 1) and seen[-1][0] == 8


def test_fetch_script_is_wired_to_the_profile_path():
    js = browser._RG_FETCH_JS % '"/profile/X/research?page=2"'
    assert 'fetch("/profile/X/research?page=2"' in js and "DOMParser" in js and "/publication/" in js


def test_a_failing_page_keeps_the_earlier_ones():
    def read(n):
        if n == 2:
            raise RuntimeError("HTTP 500")
        return {"items": _items(0, 10), "expected": 30}
    items, _, note = browser.rg_collect(read)
    assert len(items) == 10 and "第 2 页" in note and "HTTP 500" in note

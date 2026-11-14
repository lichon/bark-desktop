"""Link detection in notification bodies (pure text, no Tk)."""

from __future__ import annotations

import pytest

from bark_pc.ui.links import iter_urls, open_target


def urls(text: str) -> list[str]:
    return [url for _start, _end, url in iter_urls(text)]


def spans(text: str) -> list[tuple[int, int]]:
    return [(start, end) for start, end, _url in iter_urls(text)]


def test_finds_a_plain_https_link():
    assert urls("build done: https://example.com/jobs/412?ok=1#log") == [
        "https://example.com/jobs/412?ok=1#log"
    ]


def test_finds_several_links_in_order():
    body = "a https://one.test/x b http://two.test:8080/y"
    assert urls(body) == ["https://one.test/x", "http://two.test:8080/y"]


def test_spans_cover_exactly_the_address():
    body = "see https://example.com now"
    assert spans(body) == [(4, 23)]
    start, end, _url = next(iter_urls(body))
    assert body[start:end] == "https://example.com"


def test_stops_at_chinese_punctuation():
    assert urls("请看 https://example.com/a?b=1。（详情）") == ["https://example.com/a?b=1"]


def test_link_glued_to_chinese_text_still_matches():
    assert urls("看这个https://example.com/a很好") == ["https://example.com/a"]


def test_drops_sentence_punctuation_after_an_english_link():
    assert urls("open https://example.com/page.") == ["https://example.com/page"]
    assert urls("is it https://example.com/page?") == ["https://example.com/page"]


def test_keeps_balanced_brackets_inside_the_address():
    url = "https://en.wikipedia.org/wiki/Foo_(bar)"
    assert urls(f"({url})") == [url]
    assert urls("see https://example.com/a_(b)") == ["https://example.com/a_(b)"]


def test_drops_markdown_closing_parenthesis():
    assert urls("![shot](https://cdn.test/shot.png)") == ["https://cdn.test/shot.png"]


def test_bare_www_is_recognised():
    assert urls("home page www.example.com/start") == ["www.example.com/start"]


def test_ignores_text_that_is_not_a_web_address():
    assert urls("foo.py 1.5 v2.0.1 me@example.com") == []
    assert urls("https://") == []
    assert urls("") == []
    assert urls(None) == []


@pytest.mark.parametrize(
    ("raw", "target"),
    [
        ("https://example.com/a", "https://example.com/a"),
        ("http://127.0.0.1:8080/x", "http://127.0.0.1:8080/x"),
        ("www.example.com", "https://www.example.com"),
        ("  https://example.com  ", "https://example.com"),
        ("ftp://example.com", None),
        ("javascript:alert(1)", None),
        ("https://", None),
        ("", None),
        (None, None),
    ],
)
def test_open_target_only_hands_web_addresses_to_the_browser(raw, target):
    assert open_target(raw) == target

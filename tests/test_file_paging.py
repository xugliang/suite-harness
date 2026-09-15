import pytest

from suiteharness.tools.workspace import _text_page


@pytest.mark.parametrize("encoding", ["utf-8", "gbk", "gb18030"])
def test_multibyte_pages_preserve_every_character(encoding):
    raw = "中文测试ABCDE更多中文".encode(encoding)
    offset, pages = 0, []
    while offset < len(raw):
        page = _text_page(raw[offset:offset + 6], encoding, 5, 100, offset)
        pages.append(page["content"])
        assert page["bytes"] > 0
        offset = page["next_offset"] or len(raw)
    assert "".join(pages) == raw.decode(encoding)


def test_character_cap_and_invalid_encoding():
    page = _text_page("你好世界".encode(), "utf-8", 12, 2, 0)
    assert page["content"] == "你好"
    assert page["next_offset"] == 6
    with pytest.raises(ValueError):
        _text_page(b"\xff", "utf-8", 10, 100, 0)


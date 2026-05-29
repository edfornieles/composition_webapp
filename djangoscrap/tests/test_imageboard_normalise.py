from djangoscrap.imageboard_ingestion import normalise


def test_extract_reply_targets():
    html = (
        '<a href="#p123" class="quotelink">&gt;&gt;123</a> agreed<br>'
        '<a href="#p456" class="quotelink">&gt;&gt;456</a> wrong'
    )
    assert normalise.extract_reply_targets(html) == [123, 456]


def test_html_to_plain_text_preserves_greentext_and_decodes_entities():
    html = (
        '<a href="#p1" class="quotelink">&gt;&gt;1</a><br>'
        '<span class="quote">&gt;mfw I lift</span><br>'
        'I&#039;m DYEL &amp; cope'
    )
    plain = normalise.html_to_plain_text(html)
    assert ">>1" in plain
    assert ">mfw I lift" in plain
    assert "I'm DYEL & cope" in plain


def test_strip_quotes_removes_quotes_and_greentext():
    plain = ">>123\n>greentext line\nMy real thought.\n>>456 inline ref kept text"
    out = normalise.strip_quotes(plain)
    assert "greentext" not in out
    assert ">>123" not in out
    assert "My real thought." in out
    assert "inline ref kept text" in out


def test_normalise_post_returns_three_values():
    html = '<a href="#p9" class="quotelink">&gt;&gt;9</a><br>just lift bro'
    plain, no_q, refs = normalise.normalise_post(html)
    assert refs == [9]
    assert "just lift bro" in plain
    assert no_q.strip() == "just lift bro"


def test_empty_input_safe():
    assert normalise.html_to_plain_text("") == ""
    assert normalise.strip_quotes("") == ""
    assert normalise.extract_reply_targets("") == []

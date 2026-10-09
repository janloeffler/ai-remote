from app.markdown_filter import render_markdown


def test_table_renders_as_html_table():
    md = "| A | B |\n|---|---|\n| 1 | 2 |\n"
    html = render_markdown(md)
    assert "<table>" in html
    assert "<thead>" in html
    assert "<th>A</th>" in html
    assert "<td>1</td>" in html


def test_fenced_code_block_survives_sanitization_with_highlight_spans():
    md = "```python\nx = 1\n```\n"
    html = render_markdown(md)
    assert '<div class="codehilite">' in html
    assert "<span class=" in html


def test_plain_text_without_markdown_features_still_renders():
    html = render_markdown("just a sentence, no special formatting")
    assert "just a sentence, no special formatting" in html


def test_bare_absolute_path_gets_wrapped_in_code():
    html = render_markdown("Open /Users/yourname/source/demo-project/.env please")
    assert "<code>/Users/yourname/source/demo-project/.env</code>" in html


def test_bare_home_relative_path_gets_wrapped():
    html = render_markdown("Check ~/source/ris for details")
    assert "<code>~/source/ris</code>" in html


def test_path_already_in_backticks_is_not_double_wrapped():
    html = render_markdown("See `/Users/yourname/source/ris`")
    assert html.count("<code>") == 1
    assert "<code><code>" not in html


def test_path_inside_fenced_code_block_is_not_touched():
    md = "```bash\ncd /Users/yourname/source/ris\n```\n"
    html = render_markdown(md)
    assert html.count("/Users/yourname/source/ris") == 1
    assert "<code>/Users/yourname/source/ris</code>" not in html


def test_url_with_domain_and_path_is_not_wrongly_wrapped():
    html = render_markdown("See https://your-domain.example.com/chats/123 for the chat")
    assert "<code>" not in html


def test_prose_with_slashes_is_left_alone():
    html = render_markdown("Use and/or, true/false, or compute a / b carefully")
    assert "<code>" not in html


def test_multiple_paths_in_one_message_all_wrapped():
    html = render_markdown("Compare /etc/hosts and /etc/passwd now")
    assert html.count("<code>") == 2


def test_lone_tilde_without_path_is_not_wrapped():
    html = render_markdown("use ~ for home, or ~/source/x for a real path")
    assert "<code>~</code>" not in html
    assert "<code>~/source/x</code>" in html


def test_table_column_alignment_is_kept_as_class():
    md = "| A | B | C |\n|:--|--:|:-:|\n| 1 | 2 | 3 |\n"
    html = render_markdown(md)
    assert '<th class="align-left">A</th>' in html
    assert '<td class="align-right">2</td>' in html
    assert '<td class="align-center">3</td>' in html
    assert "style=" not in html

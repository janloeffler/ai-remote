from agent.render_core import ImageContext, render_markdown


def test_renders_table_code_and_image_marker():
    text = (
        "| a | b |\n|---|---|\n| 1 | 2 |\n\n"
        "```python\nprint('x')\n```\n\n"
        "[Image: source: /tmp/x.png]\n"
    )
    html = render_markdown(text, ImageContext("claude-code:s1"))
    assert "<table>" in html
    assert "<pre>" in html and "print" in html
    assert 'class="image-fetch"' in html
    assert 'data-path="/tmp/x.png"' in html

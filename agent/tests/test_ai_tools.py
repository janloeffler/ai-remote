import pytest

from agent import ai_tools, executor


def test_defaults_enable_both_with_claude_default():
    assert ai_tools.load({}) == (("claude-code", "cursor"), "claude-code")


def test_default_ai_cursor_when_both_enabled():
    assert ai_tools.load({"DEFAULT_AI": "cursor"})[1] == "cursor"


@pytest.mark.parametrize(
    "env, expected",
    [
        ({"CLAUDE_CODE_ENABLED": "false", "DEFAULT_AI": "claude"}, (("cursor",), "cursor")),
        ({"CURSOR_ENABLED": "false", "DEFAULT_AI": "cursor"}, (("claude-code",), "claude-code")),
    ],
)
def test_single_enabled_tool_overrides_default_ai(env, expected):
    assert ai_tools.load(env) == expected


def test_all_disabled_has_no_default():
    assert ai_tools.load({"CLAUDE_CODE_ENABLED": "false", "CURSOR_ENABLED": "false"}) == ((), None)


def test_invalid_values_fail_loudly():
    with pytest.raises(RuntimeError):
        ai_tools.load({"CURSOR_ENABLED": "flase"})
    with pytest.raises(RuntimeError):
        ai_tools.load({"DEFAULT_AI": "gpt"})


def test_executor_rejects_disabled_tool():
    job = {"target": "cursor:abc", "payload": "{}"}
    assert executor.execute_fetch_full(job, ("claude-code",))["result_text"] == "tool is disabled: cursor"
    assert executor.execute_resume_message(job, [], ("claude-code",))["status"] == "failed"
    new = {"target": "/p", "payload": '{"prompt": "x", "tool": "cursor"}'}
    assert "disabled" in executor.execute_new_session(new, ["/p"], ("claude-code",))["result_text"]

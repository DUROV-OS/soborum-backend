from app.core.mcp_remote import McpTarget, RemoteMcp, as_rows, parse_sse_json, unwrap_tool_result


def test_parse_sse_json_reads_data_line():
    body = (
        'event: message\r\n'
        'data: {"jsonrpc":"2.0","id":1,"result":{"ok":true}}\r\n\r\n'
    )
    assert parse_sse_json(body)["result"]["ok"] is True


def test_unwrap_prefers_structured_result():
    payload = {
        "result": {
            "content": [{"type": "text", "text": "[]"}],
            "structuredContent": {"result": [{"id": 1, "name": "сделка"}]},
            "isError": False,
        }
    }
    assert unwrap_tool_result(payload) == [{"id": 1, "name": "сделка"}]


def test_unwrap_parses_text_json_when_no_structured():
    payload = {
        "result": {
            "content": [{"type": "text", "text": '{"rows":[{"name":"брус"}]}'}],
            "isError": False,
        }
    }
    assert as_rows(unwrap_tool_result(payload))[0]["name"] == "брус"


# --- 0010: внешний token_provider (для app/ai/mcp_write.py) -----------------


def _target() -> McpTarget:
    return McpTarget(name="t", url="https://kb.example/mcp", client_id="a", client_secret="b")


def test_external_token_provider_is_cached_until_forget_auth():
    calls = []

    def provider(force: bool) -> str:
        calls.append(force)
        return "tok-force" if force else "tok-normal"

    remote = RemoteMcp(_target(), token_provider=provider)
    assert remote._access_token() == "tok-normal"
    # Cached - the provider is not asked again on a second call.
    assert remote._access_token() == "tok-normal"
    assert calls == [False]


def test_forget_auth_makes_next_token_request_forced():
    calls = []

    def provider(force: bool) -> str:
        calls.append(force)
        return "tok-2" if force else "tok-1"

    remote = RemoteMcp(_target(), token_provider=provider)
    assert remote._access_token() == "tok-1"
    remote._forget_auth()  # what _rpc calls internally after a 401/403
    assert remote._access_token() == "tok-2"
    assert calls == [False, True]


def test_without_token_provider_behaviour_is_unchanged():
    # Default construction (moysklad/dashboard connectors) must not be
    # affected by the new parameter.
    remote = RemoteMcp(_target())
    assert remote._token_provider is None

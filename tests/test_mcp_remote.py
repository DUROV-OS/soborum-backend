from app.core.mcp_remote import as_rows, parse_sse_json, unwrap_tool_result


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

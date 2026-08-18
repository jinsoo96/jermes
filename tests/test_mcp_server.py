"""단조한 툴을 MCP 로 내주기 - **규율이 통합에도 그대로 나타나는가.**

파일로 내보내는 것(`jermes export`)과 서버로 내주는 것은 다르다. 받는 쪽이 이미 가진
클라이언트로 부를 수 있어야 "플러그인"이다. XGEN 에 편입할 때도 이 길이 가장 깔끔하다
- XGEN 은 `MCPClient` 를 이미 갖고 있어서 우리 쪽 코드가 필요 없다.

여기서 고정하는 것은 배관이 아니라 **규율**이다.
  ① 기본은 검증된 툴만 내준다 - 확인 안 된 것을 남이 부르게 두지 않는다
  ② 주석은 그 툴이 허락받은 권한에서 나온다 - 부르는 쪽이 넓힐 수 없다
  ③ 검증 근거가 설명에 실려 나간다 - 남의 에이전트도 우리 신호를 쓸 수 있다
"""

import io

import pytest
import json

from jermes.ledger import InMemorySkillLedger
from jermes.mcp_server import JermesMcpServer, describe, servable
from jermes.tools import (
    ToolCase, ToolPolicy, ToolReport, synthesize_tool_skill, verify_tool,
)

ADD = "def run(payload):\n    return payload['a'] + payload['b']\n"


def cases(n=12):
    return [ToolCase(case_id=f"case-{i}", payload={"a": i, "b": 1}, expect=i + 1)
            for i in range(n)]


def ledger_with(*skills):
    ledger = InMemorySkillLedger()
    for skill in skills:
        ledger.commit(skill)
    return ledger


def verified_tool(name="adder", policy=None):
    skill = synthesize_tool_skill(name, "두 수를 더한다", ADD, verify_tool(ADD, cases()),
                                  policy=policy, cases=cases())
    return skill


def staged_tool(name="unproven"):
    skill = synthesize_tool_skill(name, "확인 안 된 것", ADD, ToolReport(), cases=cases())
    skill.status = "staged"
    return skill


# ------------------------------------------------- 무엇을 내주는가

def test_only_verified_tools_are_served_by_default():
    """확인 안 된 것을 남의 에이전트가 부르게 하는 것은 우리가 하지 말자고 한 그것이다."""
    ledger = ledger_with(verified_tool(), staged_tool())
    assert list(servable(ledger)) == ["adder"]


def test_unverified_can_be_served_but_only_on_purpose():
    ledger = ledger_with(verified_tool(), staged_tool())
    assert set(servable(ledger, include_staged=True)) == {"adder", "unproven"}


def test_a_guide_skill_is_not_served_as_a_tool():
    """문서는 부를 수 있는 물건이 아니다."""
    from jermes.model import SkillDef

    guide = SkillDef(name="g", kind="guide", scope="user", description="d", body="b")
    guide.verified = True
    guide.status = "active"
    assert servable(ledger_with(guide, verified_tool())) .keys() == {"adder"}


def test_a_tool_record_without_a_script_is_not_served():
    """옛 기록이나 손으로 넣은 것이 있을 수 있다. 못 부르는 걸 내주면 조용한 실패다."""
    from jermes.model import SkillDef

    odd = SkillDef(name="odd", kind="tool", scope="user", description="d",
                   body='{"name": "odd"}')
    odd.verified = True
    odd.status = "active"
    assert servable(ledger_with(odd)) == {}


# ------------------------------------------------- 무엇을 말해주는가

def test_the_annotations_come_from_the_granted_permissions():
    """부르는 쪽이 위험도를 다시 매기지 않아도 되게, 허락받은 그대로 내보낸다."""
    ledger = ledger_with(verified_tool(policy=ToolPolicy.preset("network")))
    record, manifest = servable(ledger)["adder"]
    hints = describe(record, manifest)["annotations"]
    assert hints["openWorldHint"] is True and hints["readOnlyHint"] is False

    plain = ledger_with(verified_tool("pure"))
    record, manifest = servable(plain)["pure"]
    hints = describe(record, manifest)["annotations"]
    assert hints["readOnlyHint"] and not hints["destructiveHint"]


def test_the_verification_evidence_travels_in_the_description():
    """남의 에이전트가 고를 때도 우리 신호를 쓸 수 있어야 한다."""
    ledger = ledger_with(verified_tool())
    record, manifest = servable(ledger)["adder"]
    assert "검증" in describe(record, manifest)["description"]
    assert "dev" in describe(record, manifest)["description"]


# ------------------------------------------------- 프로토콜

def talk(server, *messages) -> list[dict]:
    out = io.StringIO()
    server.serve([json.dumps(m) for m in messages], out=out)
    return [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]


def test_initialize_then_list_then_call():
    server = JermesMcpServer(ledger_with(verified_tool()))
    replies = talk(server,
                   {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                   {"jsonrpc": "2.0", "method": "notifications/initialized"},
                   {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                   {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                    "params": {"name": "adder", "arguments": {"a": 40, "b": 2}}})
    assert [r["id"] for r in replies] == [1, 2, 3]          # 통지에는 답하지 않는다
    assert replies[0]["result"]["serverInfo"]["name"] == "jermes"
    assert [t["name"] for t in replies[1]["result"]["tools"]] == ["adder"]
    body = replies[2]["result"]
    assert not body["isError"] and json.loads(body["content"][0]["text"]) == 42


def test_calling_something_we_do_not_serve_says_so():
    """없는 것을 조용히 빈 결과로 주면 부른 쪽이 원인을 못 찾는다."""
    server = JermesMcpServer(ledger_with(verified_tool()))
    reply = talk(server, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": "없는툴", "arguments": {}}})[0]
    assert reply["result"]["isError"] and "없거나" in reply["result"]["content"][0]["text"]


def test_a_staged_tool_cannot_be_called_through_the_default_server():
    server = JermesMcpServer(ledger_with(staged_tool()))
    reply = talk(server, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": "unproven", "arguments": {"a": 1, "b": 1}}})[0]
    assert reply["result"]["isError"]


def test_a_crashing_tool_returns_an_error_not_a_dead_connection():
    """서버가 죽으면 부른 쪽은 연결 문제로 오진한다."""
    broken = synthesize_tool_skill("boom", "터진다",
                                   "def run(p):\n    return p['없는키']\n",
                                   verify_tool(ADD, cases()), cases=cases())
    reply = talk(JermesMcpServer(ledger_with(broken)),
                 {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                  "params": {"name": "boom", "arguments": {}}})[0]
    assert reply["result"]["isError"] and "KeyError" in reply["result"]["content"][0]["text"]


def test_an_unknown_method_gets_an_error_reply_not_silence():
    reply = talk(JermesMcpServer(ledger_with(verified_tool())),
                 {"jsonrpc": "2.0", "id": 1, "method": "tools/nope"})[0]
    assert reply["error"]["code"] == -32601


def test_a_broken_line_is_skipped_without_killing_the_session():
    server = JermesMcpServer(ledger_with(verified_tool()))
    out = io.StringIO()
    server.serve(["{깨진", "", json.dumps({"jsonrpc": "2.0", "id": 7, "method": "ping"})],
                 out=out)
    assert json.loads(out.getvalue().strip())["id"] == 7


def test_the_caller_cannot_widen_the_permissions():
    """부르는 쪽이 권한을 넓힐 수 있으면 선언이 무의미해진다."""
    ledger = ledger_with(verified_tool())
    server = JermesMcpServer(ledger)
    reply = talk(server, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": "adder", "arguments": {"a": 1, "b": 1},
                                     "policy": {"allow_network": True}}})[0]
    assert not reply["result"]["isError"]        # 그냥 무시된다
    record, manifest = servable(ledger)["adder"]
    assert ToolPolicy.from_dict(manifest.get("policy")).granted() == []


# --- 남의 MCP 도구를 **부르는** 쪽 ------------------------------------------
# 오래 이 자리가 비어 있었다. 찾아서 카드만 보여 주면 "근처의 도구를 다 쓸 수
# 있다"는 말이 성립하지 않는다.

def test_call_tool_speaks_the_protocol():
    from jermes.mcp_client import StdioMcp

    sent = []

    class Fake(StdioMcp):
        def __init__(self):
            super().__init__("noop", [])
            self._replies = {
                "initialize": {"capabilities": {}},
                "tools/call": {"content": [{"type": "text", "text": "42"}]},
            }

        def call(self, method, params=None):
            sent.append((method, params))
            return self._replies.get(method, {})

        def notify(self, method):
            sent.append((method, None))

    out = Fake().call_tool("adder", {"a": 40, "b": 2})
    assert [m for m, _ in sent] == ["initialize", "notifications/initialized",
                                    "tools/call"]
    assert sent[-1][1] == {"name": "adder", "arguments": {"a": 40, "b": 2}}
    assert out["content"][0]["text"] == "42"


def test_result_blocks_become_readable_text(monkeypatch):
    from jermes import mcp_client

    class FakeSession:
        def __enter__(self): return self
        def __exit__(self, *exc): return False
        def call_tool(self, name, args):
            return {"content": [{"type": "text", "text": "first"},
                                {"type": "text", "text": "second"}]}

    monkeypatch.setattr(mcp_client, "StdioMcp", lambda *a, **k: FakeSession())
    ok, text = mcp_client.call_stdio_tool({"command": "x"}, "t", {})
    assert ok and text == "first\nsecond"


def test_server_error_is_reported_as_failure(monkeypatch):
    """서버가 isError 라고 하면 성공으로 세지 않는다."""
    from jermes import mcp_client

    class FakeSession:
        def __enter__(self): return self
        def __exit__(self, *exc): return False
        def call_tool(self, name, args):
            return {"isError": True,
                    "content": [{"type": "text", "text": "boom"}]}

    monkeypatch.setattr(mcp_client, "StdioMcp", lambda *a, **k: FakeSession())
    ok, text = mcp_client.call_stdio_tool({"command": "x"}, "t", {})
    assert not ok and "boom" in text


def test_non_text_result_is_described_not_dropped(monkeypatch):
    from jermes import mcp_client

    class FakeSession:
        def __enter__(self): return self
        def __exit__(self, *exc): return False
        def call_tool(self, name, args):
            return {"content": [{"type": "image", "data": "..."}]}

    monkeypatch.setattr(mcp_client, "StdioMcp", lambda *a, **k: FakeSession())
    ok, text = mcp_client.call_stdio_tool({"command": "x"}, "t", {})
    assert ok and "image" in text


# --- 전송이 두 가지일 때 -----------------------------------------------------
# 설정에 `command` 없는 원격 서버가 4곳 있었는데 stdio 만 말할 줄 알아서
# 한 곳도 못 봤다. 목록에 뜨지도 않으니 없는 것과 같았다.

def test_transport_is_chosen_in_one_place():
    from jermes.mcp_client import HttpMcp, StdioMcp, session_for

    assert isinstance(session_for({"command": "x", "args": []}), StdioMcp)
    assert isinstance(session_for({"url": "https://example/mcp"}), HttpMcp)
    with pytest.raises(ValueError):
        session_for({})


def test_remote_servers_are_loaded_too(tmp_path):
    from jermes.mcp_client import load_servers

    config = tmp_path / "mcp.json"
    config.write_text(json.dumps({"mcpServers": {
        "local": {"command": "node", "args": ["x.js"]},
        "remote": {"type": "http", "url": "https://example/mcp"},
        "broken": {"note": "no command, no url"},
    }}), encoding="utf-8")
    servers = load_servers([config])
    assert set(servers) == {"local", "remote"}, "원격 서버가 빠지면 안 된다"


def test_http_reads_sse_and_plain_json(monkeypatch):
    """서버가 JSON 으로 주든 SSE 로 주든 같은 결과를 읽어야 한다."""
    from jermes.mcp_client import HttpMcp

    class FakeResponse:
        def __init__(self, body, kind):
            self._body, self.headers = body.encode(), {"Content-Type": kind}
        def __enter__(self): return self
        def __exit__(self, *exc): return False
        def read(self): return self._body

    payload = '{"jsonrpc":"2.0","id":1,"result":{"tools":[{"name":"t"}]}}'
    for body, kind in ((payload, "application/json"),
                       (f"event: message\ndata: {payload}\n\n", "text/event-stream")):
        client = HttpMcp("https://example/mcp")
        monkeypatch.setattr("urllib.request.urlopen",
                            lambda *a, **k: FakeResponse(body, kind))
        assert client.call("tools/list")["tools"][0]["name"] == "t"


def test_http_error_says_what_the_server_said(monkeypatch):
    """'HTTPError' 한 낱말로는 못 고친다. 실측: 401 unauthenticated."""
    import urllib.error

    from jermes.mcp_client import HttpMcp

    def boom(*a, **k):
        raise urllib.error.HTTPError(
            "https://example/mcp", 401, "Unauthorized", {},
            io.BytesIO(b'{"error":{"message":"unauthenticated"}}'))

    monkeypatch.setattr("urllib.request.urlopen", boom)
    with pytest.raises(ConnectionError) as caught:
        HttpMcp("https://example/mcp").call("tools/list")
    assert "401" in str(caught.value) and "unauthenticated" in str(caught.value)


def test_child_servers_do_not_inherit_every_secret(monkeypatch):
    """실측: 심어 둔 가짜 비밀 3개가 서드파티 서버에 원문으로 도착했고 환경변수
    96개가 넘어갔다. 우리 툴에는 이미 걸어 둔 방어를 남의 코드에만 안 걸었다."""
    from jermes.mcp_client import child_env

    monkeypatch.setenv("MY_OPENAI_API_KEY", "sk-secret")
    monkeypatch.setenv("DB_PASSWORD", "hunter2")
    monkeypatch.setenv("PATH", "/usr/bin")

    env = child_env()
    assert "PATH" in env, "돌아가려면 PATH 는 있어야 한다"
    assert "MY_OPENAI_API_KEY" not in env
    assert "DB_PASSWORD" not in env

    named = child_env({"MY_OPENAI_API_KEY": "sk-secret"})
    assert named["MY_OPENAI_API_KEY"] == "sk-secret", "설정이 적은 것은 간다"


def test_served_schema_names_the_arguments():
    """실측: inputSchema 가 `{"type":"object","additionalProperties":true}` 뿐이라
    받는 쪽이 인자 이름을 모르고 소비 실험에서 4/4 KeyError 가 났다. 도구를 내주는
    쪽인데 남이 못 쓰면 내주는 의미가 없다.

    정답은 지어내지 않는다. 이 툴은 그 케이스들로 **검증을 통과했으므로** 케이스의
    payload 키가 곧 받는 인자다."""
    from jermes.mcp_server import schema_from_cases

    schema = schema_from_cases({"cases": [
        {"payload": {"a": 1, "b": 2}},
        {"payload": {"a": 5, "b": 6, "verbose": True}},
    ]})

    assert set(schema["properties"]) == {"a", "b", "verbose"}
    assert schema["properties"]["a"]["type"] == "integer"
    assert schema["properties"]["verbose"]["type"] == "boolean"
    # 한 케이스에만 있던 것은 선택 인자다
    assert schema["required"] == ["a", "b"]


def test_served_schema_stays_silent_on_mixed_types():
    """타입이 섞이면 안 적는다. 하나로 우기면 받는 쪽이 틀린 검증을 한다."""
    from jermes.mcp_server import schema_from_cases

    schema = schema_from_cases({"cases": [
        {"payload": {"value": 1}},
        {"payload": {"value": "hello"}},
    ]})
    assert "type" not in schema["properties"]["value"]


def test_served_schema_admits_it_does_not_know():
    """케이스가 없으면 모른다. 빈 properties 를 내주면 "인자가 없다"는 거짓말이 된다."""
    from jermes.mcp_server import schema_from_cases

    schema = schema_from_cases({"cases": []})
    assert schema.get("additionalProperties") is True
    assert "properties" not in schema

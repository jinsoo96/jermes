"""jermes.integrations.xgen - the pip-install-and-go plugin surface.

Pure structural/behavioral tests against the documented contract shapes
(method names, dict keys, sync vs async) - no dependency on the target
harness's own package being installed. `test_xgen_harness_conformance.py` adds
one more test on top of these that runs only when a local checkout of that
package happens to be importable.
"""

import asyncio

import pytest

from jermes import cli
from jermes.integrations import xgen as xg
from jermes.memory import MemoryItem
from jermes.tools import ToolCase, synthesize_tool_skill, verify_tool

ADD = "def run(payload):\n    return payload['a'] + payload['b']\n"


def _cases(n=12):
    return [ToolCase(case_id=f"c{i}", payload={"a": i, "b": i}, expect=i * 2)
            for i in range(1, n + 1)]


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("JERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JERMES_SKILL_PATH", str(tmp_path / "none"))
    monkeypatch.setenv("JERMES_SOURCES", "own")
    xg._SINGLETONS.clear()
    return tmp_path


def _install_adder():
    skill = synthesize_tool_skill("adder", "두 수를 더한다", ADD,
                                  verify_tool(ADD, _cases()), cases=_cases())
    skill.verified = True
    skill.status = "active"
    cli.open_ledger().commit(skill)


# --- ToolSource ---------------------------------------------------------

def test_list_tools_only_shows_verified_ones(home):
    """검증 안 된 것을 남의 에이전트 루프가 부르게 두지 않는다 - jermes serve 와
    같은 규율."""
    unverified = synthesize_tool_skill(
        "half-baked", "미검증 툴", ADD, verify_tool(ADD, _cases()), cases=_cases())
    unverified.verified = False
    unverified.status = "staged"
    cli.open_ledger().commit(unverified)
    _install_adder()

    source = xg.JermesToolSource()
    listed = asyncio.run(source.list_tools())
    names = {t["name"] for t in listed}
    assert names == {"adder"}, names


def test_list_tools_shape_matches_the_documented_contract(home):
    """harness 문서: 각 dict 는 최소 {"name","description"}, input_schema 는
    스네이크케이스(jermes 자신의 MCP 서버는 camelCase 를 쓰므로 그대로 재사용하면
    안 되고 여기서 옮겨 담아야 한다)."""
    _install_adder()
    listed = asyncio.run(xg.JermesToolSource().list_tools())
    assert len(listed) == 1
    tool = listed[0]
    assert tool["name"] == "adder"
    assert "description" in tool
    assert "input_schema" in tool and "inputSchema" not in tool
    assert tool["input_schema"]["properties"].keys() >= {"a", "b"}


def test_call_tool_actually_runs_it_through_the_sandbox(home):
    _install_adder()
    source = xg.JermesToolSource()
    got = asyncio.run(source.call_tool("adder", {"a": 3, "b": 4}))
    assert got == {"content": "7"}


def test_call_tool_on_an_unknown_name_says_so_not_a_stack_trace(home):
    source = xg.JermesToolSource()
    got = asyncio.run(source.call_tool("no-such-tool", {}))
    assert got.get("isError") is True
    assert "no-such-tool" in got["content"]


def test_has_tool_matches_list_tools(home):
    _install_adder()
    source = xg.JermesToolSource()
    assert source.has_tool("adder")
    assert not source.has_tool("subtractor")


def test_a_tool_forged_after_the_source_was_built_is_still_visible(home):
    """리스트를 매번 다시 읽는다 - harness 재시작 없이 방금 검증된 도구가 보여야
    "생성도 되는 자원" 이 실제로 참이다."""
    source = xg.JermesToolSource()
    assert asyncio.run(source.list_tools()) == []
    _install_adder()
    assert [t["name"] for t in asyncio.run(source.list_tools())] == ["adder"]


# --- MemoryStore ---------------------------------------------------------

def test_write_then_search_finds_it(home):
    store = xg.JermesMemoryStore()

    class _Entry:
        scope, memory_key, content = "user", "", "cp949 오류엔 utf-8 로 다시 읽는다"

    key = store.write(_Entry())
    assert key.startswith("user/xgen-")

    found = store.search("cp949 오류", scopes=["user"], top_k=5)
    assert any("utf-8" in e.content for e in found)


def test_search_carries_jermes_trust_as_metadata(home):
    """jermes 는 재서 신뢰를 매기는 물건이다. XGEN 자신의 InMemory 저장소에는
    없는 신호이니 metadata 로 실어 보낸다."""
    items = [MemoryItem(item_id="a", text="측정된 사실", scope="user", trust=0.8)]
    items[0].evidence["measurements"] = [{"cases": 5, "gain": 0.3, "verdict": "helpful"}]
    cli.save_memory(items)

    found = xg.JermesMemoryStore().search("측정된", scopes=["user"], top_k=5)
    assert found and found[0].metadata["trust"] == 0.8
    assert found[0].metadata["measured"] is True


def test_delete_retires_it_does_not_erase_it(home):
    """jermes 전체의 규율 - 지우는 물건이 아니라 내리는 물건이다."""
    cli.save_memory([MemoryItem(item_id="a", text="사실", scope="user")])

    store = xg.JermesMemoryStore()
    assert store.delete("user", "a") is True
    assert store.delete("user", "a") is True   # 이미 내려도 다시 불러도 안전

    raw = {i.item_id: i for i in cli.load_memory()}
    assert raw["a"].status == "retired"       # 사라지지 않았다
    assert not xg.JermesMemoryStore().search("사실", scopes=["user"], top_k=5)


def test_delete_of_an_unknown_key_returns_false(home):
    assert xg.JermesMemoryStore().delete("user", "no-such-id") is False


# --- factories are singletons (harness calls factory() once) ------------

def test_factories_return_the_same_instance_on_repeat_calls(home):
    assert xg.tool_source() is xg.tool_source()
    assert xg.memory_store() is xg.memory_store()


# --- HarnessCapabilitySource / call_harness_tool (jermes -> harness) ----

class _FakeHarnessTool:
    """하네스가 이미 갖고 있는(jermes 와 무관한) 도구 소스 하나를 흉내낸다."""

    def __init__(self, source_id="weather-node", read_only=True):
        self.source_id = source_id
        self._read_only = read_only
        self.calls = []

    async def list_tools(self, filters=None):
        return [{"name": "get_forecast", "description": "지역 날씨 예보를 준다",
                "input_schema": {"type": "object",
                                 "properties": {"city": {"type": "string"}},
                                 "required": ["city"]},
                "annotations": ({"readOnlyHint": True} if self._read_only else {})}]

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        return {"content": f"{args.get('city')}: 맑음, 24도"}

    def has_tool(self, name):
        return name == "get_forecast"


class _FakeToolsModule:
    def __init__(self, *sources):
        self._sources = list(sources)

    def get_tool_sources(self):
        return self._sources


def test_discover_skips_its_own_source_to_avoid_a_loop(home, monkeypatch):
    """`source_id == "jermes"` 인 것(자기 자신, `JermesToolSource`)은 다시
    후보로 넣지 않는다 - 안 그러면 발견이 발견을 낳는 순환이 생긴다."""
    own = xg.JermesToolSource()
    fake = _FakeToolsModule(own, _FakeHarnessTool())
    monkeypatch.setattr(xg, "_harness_tools_module", lambda: fake)

    found = xg.HarnessCapabilitySource().discover()
    assert {c.name for c in found} == {"xgen:weather-node:get_forecast"}


def test_discovered_capabilities_carry_the_hosts_own_annotations(home, monkeypatch):
    monkeypatch.setattr(xg, "_harness_tools_module",
                        lambda: _FakeToolsModule(_FakeHarnessTool(read_only=True)))
    found = xg.HarnessCapabilitySource().discover()
    assert len(found) == 1
    cap = found[0]
    assert cap.read_only is True and cap.annotated is True
    assert cap.invoke == {"via": "xgen_tool_source", "source_id": "weather-node",
                          "tool": "get_forecast",
                          "input_schema": found[0].invoke["input_schema"]}


def test_a_host_asset_becomes_an_ordinary_routable_candidate(home, monkeypatch):
    """이게 요점이다 - jermes 가 만들지 않은 도구가 jermes 자신의 route 후보로
    나온다. `route`/`ask` 는 그게 자기가 단조한 것인지 하네스에서 온 것인지
    모른다(알 필요가 없다) - 같은 규율로 고르고 부른다."""
    from jermes.router import Router

    monkeypatch.setattr(xg, "_harness_tools_module",
                        lambda: _FakeToolsModule(_FakeHarnessTool()))
    pool = [c for c in xg.HarnessCapabilitySource().discover()]
    result = Router(pool).route("서울 날씨 알려줘", limit=1)
    assert result.chosen and result.chosen[0].capability.name == "xgen:weather-node:get_forecast"


def test_call_harness_tool_extracts_input_and_actually_calls_it(home, monkeypatch):
    """입력 뽑기 자체는 LLM 이 하는 일이라 여기서는 그 결과만 고정한다 - 잰
    것은 "뽑고 나서 실제로 부르는가"다, 뽑는 정확도가 아니다(그건
     자신의 시험 영역)."""
    from jermes import cli

    fake = _FakeHarnessTool()
    monkeypatch.setattr(xg, "_harness_tools_module", lambda: _FakeToolsModule(fake))
    monkeypatch.setattr(cli, "_payload_for",
                        lambda *a, **k: ({"city": "서울"}, ""))
    caps = xg.HarnessCapabilitySource().discover()

    from jermes.cli import build_parser
    from jermes.router import Choice

    args = build_parser().parse_args(["ask", "서울", "날씨", "알려줘", "--yes"])
    choice = Choice(capability=caps[0], score=1.0, reasons=[], coverage=1.0)
    rc = xg.call_harness_tool(args, choice)
    assert rc == 0
    assert fake.calls == [("get_forecast", {"city": "서울"})]


def test_call_harness_tool_refuses_an_unannotated_tool_without_risky(home, monkeypatch):
    """서버(호스트 자산)가 위험을 안 말했으면 **모르는 것**이지 안전한 것이 아니다
    - MCP 경로와 같은 규율."""
    from jermes import cli

    fake = _FakeHarnessTool(read_only=False)   # annotations 비움
    monkeypatch.setattr(xg, "_harness_tools_module", lambda: _FakeToolsModule(fake))
    monkeypatch.setattr(cli, "_payload_for",
                        lambda *a, **k: ({"city": "서울"}, ""))
    caps = xg.HarnessCapabilitySource().discover()

    from jermes.cli import build_parser
    from jermes.router import Choice

    args = build_parser().parse_args(["ask", "서울", "날씨", "알려줘", "--yes"])
    choice = Choice(capability=caps[0], score=1.0, reasons=[], coverage=1.0)
    rc = xg.call_harness_tool(args, choice)
    assert rc == 1
    assert fake.calls == []                    # 실제로 부르지는 않았다


def test_capability_source_factory_returns_a_fresh_usable_source(home):
    source = xg.capability_source_factory()
    assert source.name() == "xgen-harness-tools"


# --- 남의 async 서버 안에서 산다 - 이벤트 루프를 막지 않는다 -------------

def test_a_tool_call_does_not_freeze_the_hosts_event_loop(home):
    """`mcp_server.execute()` 는 샌드박스 subprocess 가 끝날 때까지 기다리는
    **동기** 함수다. `async def` 안에서 그냥 부르면 그 몇 초 동안 호스트의
    이벤트 루프 전체가 선다 - 우리 도구 하나 때문에 그 서버의 다른 모든 요청이
    같이 얼어붙는다.

    실측(고치기 전): 2초짜리 도구를 부르는 동안 0.1초마다 뛰어야 할 심장박동이
    **0회**였다. CLI 에서는 프로세스가 어차피 그 일만 하니 안 보이던 결함이,
    남의 async 서버 안에 들어가는 순간 드러난다.
    """
    import time

    slow = "import time\ndef run(p):\n    time.sleep(1)\n    return 'done'\n"
    cases = [ToolCase(case_id=f"c{i}", payload={}, expect="done") for i in range(6)]
    skill = synthesize_tool_skill("slowtool", "느린 도구", slow,
                                  verify_tool(slow, cases), cases=cases)
    skill.verified = True
    skill.status = "active"
    cli.open_ledger().commit(skill)

    async def scenario():
        beats = 0
        stop = asyncio.Event()

        async def heartbeat():
            nonlocal beats
            while not stop.is_set():
                await asyncio.sleep(0.05)
                beats += 1

        pulse = asyncio.create_task(heartbeat())
        started = time.monotonic()
        result = await xg.JermesToolSource().call_tool("slowtool", {})
        elapsed = time.monotonic() - started
        stop.set()
        await pulse
        return beats, elapsed, result

    beats, elapsed, result = asyncio.run(scenario())
    assert result == {"content": '"done"'}, result
    assert elapsed >= 1.0, "도구가 실제로 돌지 않았다면 이 시험은 무의미하다"
    # 막혔다면 0 에 가깝다. 넉넉히 잡아도 절반은 뛰어야 한다.
    assert beats >= (elapsed / 0.05) * 0.5, f"이벤트 루프가 막혔다 (심장박동 {beats}회)"


def test_discovery_works_inside_a_running_event_loop(home, monkeypatch):
    """`asyncio.run()` 은 이미 도는 루프 안에서 `RuntimeError` 로 터진다.
    그리고 하네스는 async 서버다 - **가장 중요한 자리에서만 터진다.** 동기
    문맥에서 돌린 시험은 통과하고 실제 호스트 안에서만 죽는 종류라, 그 문맥을
    시험이 직접 만들어 준다."""
    monkeypatch.setattr(xg, "_harness_tools_module",
                        lambda: _FakeToolsModule(_FakeHarnessTool()))

    async def inside_host():
        return xg.HarnessCapabilitySource().discover()

    found = asyncio.run(inside_host())
    assert [c.name for c in found] == ["xgen:weather-node:get_forecast"]


def test_calling_a_host_tool_works_inside_a_running_event_loop(home, monkeypatch):
    """`call_harness_tool` 도 같은 다리를 건넌다 - 발견만 고치고 호출을 놔두면
    고른 뒤에 터진다."""
    from jermes import cli
    from jermes.cli import build_parser
    from jermes.router import Choice

    fake = _FakeHarnessTool()
    monkeypatch.setattr(xg, "_harness_tools_module", lambda: _FakeToolsModule(fake))
    monkeypatch.setattr(cli, "_payload_for", lambda *a, **k: ({"city": "서울"}, ""))

    async def inside_host():
        caps = xg.HarnessCapabilitySource().discover()
        args = build_parser().parse_args(["ask", "서울", "날씨", "--yes"])
        choice = Choice(capability=caps[0], score=1.0, reasons=[], coverage=1.0)
        return xg.call_harness_tool(args, choice)

    assert asyncio.run(inside_host()) == 0
    assert fake.calls == [("get_forecast", {"city": "서울"})]

"""Drop jermes into an XGEN-harness environment - no host-side code change,
**in both directions.**

The harness engine's own package discovers extensions through setuptools
entry_points and only checks that the loaded object has the right method
names (`typing.Protocol(runtime_checkable)` - structural, not nominal). That
means this module never has to import the harness's package to satisfy its
contracts; it only has to have methods of the right name and shape. Importing
`jermes` still requires nothing beyond the standard library.

**Harness -> jermes** (the harness's own tools/memory become jermes's):
registered in `pyproject.toml`:

    [project.entry-points."xgen_harness.tool_sources"]
    jermes = "jermes.integrations.xgen:tool_source"

    [project.entry-points."xgen_harness.memory_stores"]
    jermes = "jermes.integrations.xgen:memory_store"

The moment both packages are installed in the same environment, the harness's
own (already-live) discovery finds these on its next `get_tool_sources()` /
`get_memory_store()` call - nothing to wire by hand. `JermesToolSource.list_tools`
re-reads jermes's ledger on every call, so a tool forged an hour ago and one
forged a second ago are both visible without a restart.

**jermes -> harness** (whatever the harness *already has* becomes a jermes
capability too - other plugins' tools, other nodes' registered sources):
registered under jermes's own extension groups (`registry.py`), which is how
any host-not-just-this-one plugs a capability source into jermes:

    [project.entry-points."jermes.capability_sources"]
    xgen-harness = "jermes.integrations.xgen:capability_source_factory"

    [project.entry-points."jermes.capability_callers"]
    xgen_tool_source = "jermes.integrations.xgen:call_harness_tool"

`jermes route`/`jermes ask` then see the harness's own registered tools as
ordinary candidates, ranked and gated by the same rules as everything else
(risk annotations, `--risky`, usage history). This is the *other half* of the
picture from the entry_points above: without it, only jermes's own tools were
reachable from inside the harness, and jermes itself still only knew what was
in its own ledger.

Skill *bodies* (guide/config prose meant to be injected as instructions, not
called) are a separate, narrower contract on the harness side
(`xgen_harness.skill_bodies`, one static entry per name) and are covered by
`recall.LedgerSkillSource` + `absorption/SEAM.md`, not this module - that
seam needs one hand-applied line in the harness's own skill_registry and is
tracked there rather than duplicated here.

What this module does NOT do: point jermes's own *storage* at XGEN's spine.
That's a third, separate direction and lives in `host.py`
(`SpineSkillLedger`, `SpineMemoryStore`) - a host builds those itself and
hands them to `JermesAgent` directly; nothing here needs to know about it.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional


def _default_ledger():
    """**캐시하지 않는다.** `JsonlSkillLedger` 는 생성 시점에 파일을 한 번
    접어 메모리에 올린다 - 인스턴스를 붙들고 있으면 그 뒤에 다른 프로세스가
    (`jermes tool ...` 로) 새로 단조한 도구가 파일에는 있어도 이 인스턴스에는
    영영 안 보인다. 실측: 소스를 만든 뒤 도구를 단조했더니 목록이 빈 채로
    남았다 - 매 호출마다 새로 열면(=파일을 다시 읽으면) 해결된다. `jermes`
    CLI 의 매 실행이 이미 그렇게 동작한다 - 여기서 새로 만드는 비용이 아니다.

    지연 임포트인 이유는 entry_point 가 이 모듈을 `ep.load()` 하는 시점(harness
    시작)에 곧바로 CLI 의 argparse 설정까지 끌려오지 않게 하려는 것뿐이다.
    """
    from ..cli import open_ledger

    return open_ledger()


class JermesToolSource:
    """`xgen_harness.tools.ToolSource` 를 구조적으로 만족한다(런타임에 메서드
    이름만 본다 - 하네스의 패키지를 import 하지 않아도 이 클래스는 그 자격을 갖는다).

    **검증된 것만 내준다** - `jermes serve`(MCP) 와 같은 규율이다. 못 본 문제로
    확인 안 된 툴을 남의 에이전트 루프가 부르게 두는 것은 이 물건 전체가
    하지 말자고 한 그것이다. `include_staged=True` 로 명시적으로만 넓힌다.

    조회·실행은 `mcp_server.servable()`/`execute()` 를 그대로 쓴다 - "무엇을
    내주고 어떻게 돌리는지"가 MCP 경로와 여기서 각자 자라면, 한쪽만 정책 복원을
    고치고 다른 쪽은 예전 그대로 남는다(오늘 이미 그 실수를 한 번 했다).
    """

    source_id = "jermes"
    display_name = "Jermes"
    display_name_ko = "제르메스"
    description = "끝난 실행에서 배우고 홀드아웃으로 검증한 도구들"
    # icon 은 일부러 안 채운다 - 이모지는 cp949 콘솔에서 못 찍는 문자가 섞이기
    # 쉽고(이 저장소는 그걸 실측해서 시험으로 막아 뒀다), 안 채우면 호스트가
    # 자기 기본 아이콘으로 폴백한다(선택 필드).
    category = "learned"

    def __init__(self, ledger: Optional[Any] = None,
                 include_staged: bool = False) -> None:
        self._ledger = ledger
        self.include_staged = include_staged

    @property
    def ledger(self):
        return self._ledger if self._ledger is not None else _default_ledger()

    def _list_tools_sync(self, filters: Optional[dict] = None) -> list[dict]:
        from .. import mcp_server

        wanted = set((filters or {}).get("names") or []) or None
        out = []
        for name, (record, manifest) in mcp_server.servable(
                self.ledger, self.include_staged).items():
            if wanted and name not in wanted:
                continue
            mcp_shape = mcp_server.describe(record, manifest)
            out.append({
                "name": mcp_shape["name"],
                "description": mcp_shape["description"],
                "input_schema": mcp_shape["inputSchema"],
                "annotations": mcp_shape["annotations"],
            })
        return out

    async def list_tools(self, filters: Optional[dict] = None) -> list[dict]:
        # 원장 파일을 읽는다 - 짧지만 blocking I/O 다. `call_tool` 과 같은 이유로
        # 스레드에 넘긴다(아래 주석 참조).
        return await asyncio.to_thread(self._list_tools_sync, filters)

    async def call_tool(self, name: str, args: dict) -> dict:
        """**이벤트 루프를 막지 않는다.**

        `mcp_server.execute()` 는 샌드박스 subprocess 를 열고 끝날 때까지
        기다리는 동기 함수다. `async def` 안에서 그냥 부르면 그 몇 초 동안
        호스트의 이벤트 루프 전체가 선다 - 우리 도구 하나 때문에 그 서버의
        다른 모든 요청이 같이 얼어붙는다.

        실측: 2초짜리 도구를 부르는 동안 0.1초마다 뛰어야 할 심장박동이
        **0회**였다(21회 나와야 정상). CLI 에서는 프로세스가 어차피 그 일만
        하니 안 보이던 결함이, 남의 async 서버 안에 들어가는 순간 드러난다.

        `to_thread` 는 그 동기 호출을 워커 스레드로 보낸다. 샌드박스는 어차피
        별도 프로세스라 GIL 을 잡고 있지 않으므로, 스레드는 그냥 기다리기만 한다.
        """
        return await asyncio.to_thread(self._call_tool_sync, name, args)

    def _call_tool_sync(self, name: str, args: dict) -> dict:
        from .. import mcp_server

        ok, text = mcp_server.execute(self.ledger, name, args or {},
                                      self.include_staged)
        return {"content": text} if ok else {"content": text, "isError": True}

    def has_tool(self, name: str) -> bool:
        from .. import mcp_server

        return name in mcp_server.servable(self.ledger, self.include_staged)


def tool_source() -> JermesToolSource:
    """entry_point factory - 매번 새 인스턴스가 아니라 **한 번** 만든다.

    harness 는 `factory()` 를 한 번 불러 그 결과를 등록한다(재호출 안 함).
    인스턴스 자체는 상태가 없다(호출마다 원장을 다시 읽는다) - 굳이 매번 새로
    만들 이유가 없어서 캐시한다.
    """
    if not _SINGLETONS.get("tool_source"):
        _SINGLETONS["tool_source"] = JermesToolSource()
    return _SINGLETONS["tool_source"]


_SINGLETONS: dict[str, Any] = {}


class JermesMemoryStore:
    """`xgen_harness.memory.MemoryStore` 를 구조적으로 만족한다(동기 메서드 3개 -
    ToolSource 와 달리 이쪽은 async 가 아니다. 실제 정의를 그대로 따른 것이지
    임의로 고른 것이 아니다).

    jermes 의 **로컬**(파일) 기억을 XGEN 쪽에 노출한다 - 반대 방향(XGEN 의 spine을
    jermes 저장소로 삼는 것)은 `host.SpineMemoryStore` 다. 둘은 같은 것의 앞뒤가
    아니라 서로 다른 방향의 연결이고, 한 프로세스에 둘 다 쓸 이유는 보통 없다.

    `write`/`delete` 는 jermes 자신의 `load_memory`/`save_memory`(잠금 아래 병합)
    를 그대로 쓴다 - 동시 쓰기 안전은 이미 거기서 해결했다. 여기서 다시 풀지 않는다.
    """

    def __init__(self, scope: str = "user") -> None:
        # XGEN 이 넘기는 `entry.scope` 가 있으면 그걸 쓰고, 없을 때만 이 기본값이다.
        self.default_scope = scope

    def search(self, query: str, *, scopes: list[str], top_k: int = 5,
               types: Optional[list[str]] = None) -> list[Any]:
        from ..cli import load_memory
        from ..memory import recall

        items = load_memory()
        seen: dict[str, Any] = {}
        for scope in (scopes or [self.default_scope]):
            for item in recall(items, limit=top_k, task=query, scope=scope):
                seen[item.item_id] = item
        ranked = sorted(seen.values(), key=lambda i: -i.trust)[:top_k]
        return [_to_memory_entry(item) for item in ranked]

    def write(self, entry: Any) -> str:
        from ..cli import load_memory, save_memory
        from ..memory import MemoryItem

        scope = getattr(entry, "scope", "") or self.default_scope
        key = getattr(entry, "memory_key", "") or _mint_key(entry)
        item = MemoryItem(item_id=key, text=str(getattr(entry, "content", "")),
                          scope=scope)
        before = load_memory()
        known = {i.item_id for i in before}
        save_memory([item, *(i for i in before if i.item_id != key)], known=known)
        return f"{scope}/{key}"

    def delete(self, scope: str, memory_key: str) -> bool:
        from ..cli import load_memory, save_memory

        before = load_memory()
        found = False
        kept = []
        for item in before:
            if item.item_id == memory_key and item.scope == scope:
                # **내린다, 지우지 않는다.** jermes 전체의 규율이다 - "그때 무엇이
                # 참이었나" 를 나중에 물을 수 있어야 한다. 호출자의 delete() 는
                # bool 을 기대하므로, 찾아서 내렸으면 True 를 준다.
                item.status = "retired"
                found = True
            kept.append(item)
        if found:
            save_memory(kept, known={i.item_id for i in before})
        return found


def memory_store() -> JermesMemoryStore:
    if not _SINGLETONS.get("memory_store"):
        _SINGLETONS["memory_store"] = JermesMemoryStore()
    return _SINGLETONS["memory_store"]


def _to_memory_entry(item) -> Any:
    """`MemoryEntry` 모양의 값 객체. 하네스 쪽의 dataclass 를 import 하지 않고
    같은 필드를 가진 우리 자신의 값을 낸다 - 구조가 같으면 받는 쪽은 구분 못 한다."""
    from dataclasses import dataclass, field

    @dataclass
    class _Entry:
        scope: str
        memory_key: str
        content: str
        description: str = ""
        type: str = "fact"
        metadata: dict = field(default_factory=dict)
        score: Optional[float] = None

    return _Entry(
        scope=item.scope, memory_key=item.item_id, content=item.text,
        description=item.text, type="fact",
        # jermes 는 **재서** 신뢰를 매기는 물건이다. 그 근거를 metadata 로 함께
        # 낸다 - XGEN 자신의 InMemory 저장소는 이 신호가 없다.
        metadata={"trust": item.trust, "measured": item.measured,
                 "told_us_nothing": item.told_us_nothing,
                 "source_run_ids": list(item.source_run_ids)},
        score=item.trust,
    )


def _mint_key(entry: Any) -> str:
    """호출자가 `memory_key` 를 안 줬을 때만 쓴다. `xgen-` 접두 - 사람이
    `jermes memory --add` 로 손수 넣은 것(`hand-` 접두)과 출처를 구분해 둔다."""
    text = str(getattr(entry, "content", ""))
    return f"xgen-{abs(hash(text)) % 10**6}"


# ────────────────────────────────────────────── jermes -> harness (반대 방향)
#
# 위까지는 "하네스가 jermes 를 쓴다" 였다. 여기부터는 그 반대 - **jermes 가
# 하네스에 이미 있는 것을 자기 후보로 끌어온다.** 다른 플러그인이 등록해 둔
# 도구까지, jermes 자신의 `route`/`ask` 가 위험 등급·이력 규율을 똑같이 적용해
# 고르고 부른다.

def _run_coro(coro):
    """코루틴 하나를 **호출 문맥이 어느 쪽이든** 끝까지 돌린다.

    `asyncio.run()` 만 쓰면 이미 이벤트 루프가 도는 곳에서 `RuntimeError:
    asyncio.run() cannot be called from a running event loop` 로 터진다.
    그리고 하네스는 async 서버다 - 즉 **가장 중요한 자리에서만 터진다**.
    시험은 동기 문맥에서 돌려서 통과하고, 실제 호스트 안에서만 죽는다.
    실측으로 그 크래시를 재현해서 이 함수를 넣었다.

    루프가 이미 돌고 있으면 별도 스레드에서 새 루프를 돌린다 - 여기 오는
    호출부(`discover`/`call_harness_tool`)가 동기 API 라 결과를 기다려야
    하는데, 그렇다고 남의 루프를 재진입할 수는 없기 때문이다.
    """
    import concurrent.futures

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)          # 도는 루프 없음 - 평범한 경우
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _harness_tools_module():
    """지연 임포트 - `jermes.capability_sources` entry_point 가 이 factory 를
    부르는 시점(discover() 가 실제로 불릴 때)까지 하네스 패키지를 안 연다."""
    from importlib import import_module

    return import_module("xgen" + "_sdk.harness.tools")


class HarnessCapabilitySource:
    """하네스가 **이미 등록해 둔** 도구 소스들(다른 플러그인·다른 노드가 넣은
    것 포함)을 jermes 의 `Capability` 로 옮긴다.

    자기 자신(`source_id == "jermes"`, 이 파일의 `JermesToolSource`)은 건너뛴다
    - 안 그러면 발견이 발견을 낳는 순환이 생긴다.
    """

    def name(self) -> str:
        return "xgen-harness-tools"

    def discover(self) -> list:
        import asyncio

        from ..discovery import KIND_HOST, Capability

        sources = _harness_tools_module().get_tool_sources()
        out = []
        for source in sources:
            source_id = getattr(source, "source_id", type(source).__name__)
            if source_id == "jermes":
                continue
            tools = _run_coro(source.list_tools()) or []
            for tool in tools:
                if not isinstance(tool, dict) or not tool.get("name"):
                    continue
                hints = tool.get("annotations") or {}
                out.append(Capability(
                    name=f"xgen:{source_id}:{tool['name']}",
                    kind=KIND_HOST,
                    description=str(tool.get("description") or "")[:1024],
                    source=f"xgen-harness:{source_id}",
                    invoke={"via": "xgen_tool_source", "source_id": source_id,
                            "tool": tool["name"],
                            "input_schema": tool.get("input_schema") or {}},
                    # 서버가 말한 것만 옮긴다 - MCP 소스와 같은 규율
                    # (discovery.capability_from_mcp_tool 참조).
                    read_only=bool(hints.get("readOnlyHint", False)),
                    destructive=bool(hints.get("destructiveHint", False)),
                    idempotent=bool(hints.get("idempotentHint", True)),
                    open_world=bool(hints.get("openWorldHint", False)),
                    annotated=bool(hints),
                ))
        return out


def capability_source_factory() -> HarnessCapabilitySource:
    return HarnessCapabilitySource()


def call_harness_tool(args, choice) -> int:
    """`jermes ask` 가 하네스 소스의 도구를 고르면 실제로 부른다.

    입력 뽑기·위험 게이팅·이력 기록은 `_ask_mcp` 와 **같은 함수**를 그대로
    쓴다 - MCP 든 하네스 자체 소스든 사람이 겪는 결정("이거 부를까 말까")은
    같다. 다른 것은 실제로 부르는 방법(비동기 in-process 호출) 하나뿐이다.
    """
    import asyncio
    import json

    from .. import cli

    capability = choice.capability
    invoke = capability.invoke if isinstance(capability.invoke, dict) else {}
    source_id, tool_name = invoke.get("source_id", ""), invoke.get("tool", "")

    manifest = {"name": capability.name, "description": capability.description,
               "input_schema": invoke.get("input_schema") or {}}
    payload, why = cli._payload_for(args, manifest, args.query,
                                    cli._recall_for(args.query))
    if payload is None:
        print(f"\n입력을 뽑지 못했습니다: {why}")
        return 1

    if not (capability.read_only and capability.annotated) and not args.risky:
        reason = ("읽기전용이라고 하네스가 말하지 않았습니다"
                  if capability.annotated else "주석이 없어 성질을 모릅니다")
        print(f"\n부르지 않았습니다: {reason} [{capability.risk()}]")
        print(f"  부르려던 것: {source_id}:{tool_name} "
              f"{json.dumps(payload, ensure_ascii=False)}")
        print("  그래도 부르려면 --risky 를 붙이세요.")
        return 1

    print(f"입력: {json.dumps(payload, ensure_ascii=False)}")
    sources = {getattr(s, "source_id", type(s).__name__): s
              for s in _harness_tools_module().get_tool_sources()}
    source = sources.get(source_id)
    if source is None:
        print(f"\n실패: 그 소스가 지금은 없습니다: {source_id}")
        return 1
    try:
        result = _run_coro(source.call_tool(tool_name, payload))
    except Exception as exc:                          # noqa: BLE001
        print(f"\n실패: {type(exc).__name__}: {exc}")
        return 1
    ok = not result.get("isError")
    text = str(result.get("content", ""))
    print()
    print(text[:4000])
    cli._log_own(args.query, f"{source_id}:{tool_name}", payload, ok, detail=text[:200])
    if ok:
        cli._record_if_confident(capability.name, choice, args.query)
    return 0 if ok else 1

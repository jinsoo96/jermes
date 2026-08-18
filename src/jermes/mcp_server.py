"""SF12 - 단조한 툴을 **MCP 로 내준다**. 그러면 아무 에이전트나 부를 수 있다.

여태 툴은 파일로 내보낼 수만 있었다(`jermes export`). 받는 쪽이 직접 실행해야 하니
"플러그인"은 아니었다. MCP 서버가 되면 플랫폼·Claude Code·Cursor 무엇이든 **이미 가진
클라이언트로** 부른다. 플랫폼에 노드로 편입할 때도 이 길이 가장 깔끔하다 -
플랫폼은 `MCPClient` 를 이미 갖고 있어서 우리 쪽 코드가 필요 없다.

**규율이 통합에도 그대로 나타난다.**
- 기본은 **검증된 툴만** 내준다. 못 본 문제로 확인 안 된 것을 남에게 부르게 하는 것은
  우리가 하지 말자고 한 바로 그것이다. 미검증까지 내주려면 명시적으로 켜야 한다.
- 도구 주석은 그 툴의 `ToolPolicy` 에서 나온다. 권한을 선언하고 허락받은 그대로가
  `readOnlyHint`/`destructiveHint`/... 로 나간다. 받는 쪽이 미리 안다.
- 설명에 **검증 근거를 같이 싣는다**(`dev 9/9 · holdout 3/3`). 남의 에이전트가
  고를 때도 우리 신호를 쓸 수 있다.

프로토콜은 JSON-RPC 2.0 over stdio(줄바꿈 구분). 의존성 0.

    jermes serve                 # 검증된 툴만
    jermes serve --include-staged  # 미검증까지(직접 켤 때만)
"""

from __future__ import annotations

import json
import sys
from typing import Any, Iterable

from .ledger import SkillLedger
from .tools import ToolPolicy, run_tool

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "jermes", "version": "0.1"}


def _manifest(record) -> dict:
    try:
        return json.loads(record.skill.body)
    except ValueError:
        return {}


def servable(ledger: SkillLedger, include_staged: bool = False) -> dict[str, Any]:
    """내줄 툴들. **기본은 검증된 것만** - 확인 안 된 것을 남이 부르게 두지 않는다."""
    found = {}
    for record in ledger.list():
        if record.skill.kind != "tool" or record.status not in ("active", "staged"):
            continue
        if not include_staged and not record.skill.verified:
            continue
        manifest = _manifest(record)
        if manifest.get("script"):
            found[record.name] = (record, manifest)
    return found


_JSON_TYPES = {bool: "boolean", int: "integer", float: "number",
               str: "string", list: "array", dict: "object"}


def schema_from_cases(manifest: dict) -> dict:
    """검증 케이스의 payload 에서 입력 스키마를 만든다.

    예전에는 `{"type":"object","additionalProperties":true}` 만 내줬다. properties
    가 없으니 받는 쪽이 인자 이름을 모르고, 소비 실험에서 4/4 KeyError 가 났다.
    우리 도구를 남이 못 쓰면 내주는 의미가 없다.

    지어내지 않는다. 이 툴은 그 케이스들로 **검증을 통과했으므로** 케이스의 키가
    곧 이 툴이 받는 인자다. 통과한 사실에서 읽는다.
    """
    payloads = [case.get("payload") for case in (manifest.get("cases") or [])
                if isinstance(case.get("payload"), dict)]
    if not payloads:
        # 케이스가 없으면 모른다. 아는 척하지 않고 예전 모양을 그대로 둔다.
        return {"type": "object", "description": "툴이 받는 JSON 그대로",
                "additionalProperties": True}

    seen: dict[str, set] = {}
    for payload in payloads:
        for key, value in payload.items():
            seen.setdefault(key, set()).add(type(value))

    properties = {}
    for key, kinds in seen.items():
        entry: dict = {}
        names = {_JSON_TYPES.get(kind) for kind in kinds}
        # 타입이 섞였으면 안 적는다. 하나로 우기면 받는 쪽이 틀린 검증을 한다.
        if len(names) == 1 and None not in names:
            entry["type"] = names.pop()
        example = next((p[key] for p in payloads if key in p), None)
        if isinstance(example, (str, int, float, bool)):
            entry["examples"] = [example]
        properties[key] = entry

    required = sorted(key for key in seen
                      if all(key in payload for payload in payloads))
    schema = {"type": "object", "properties": properties,
              "additionalProperties": False}
    if required:
        schema["required"] = required
    return schema


def describe(record, manifest: dict) -> dict:
    """MCP `tools/list` 한 항목. 주석은 그 툴이 허락받은 권한에서 나온다."""
    policy = ToolPolicy.from_dict(manifest.get("policy"))
    annotations = policy.annotations()
    # 증명 못 했으면 **읽기전용이라고 말하지 않는다.** 남에게 하는 주장이
    # 제일 무겁다 - 받는 쪽은 그 말을 믿고 승인 없이 부른다.
    # `annotations()` 는 snake_case 로 낸다(`read_only`). camelCase 로 덮으면
    # 엉뚱한 열쇠가 하나 늘 뿐 원래 값은 그대로 나간다 - 한 번 그렇게 틀렸다.
    if manifest.get("purity") and not any(
            v for k, v in (manifest.get("policy") or {}).items()
            if k.startswith("allow_")):
        annotations = dict(annotations, read_only=False)
    evidence = manifest.get("verification", "")
    description = manifest.get("description") or record.skill.description
    if evidence:
        # 검증 근거를 설명에 싣는다 - 남의 에이전트가 고를 때도 이 신호를 쓴다.
        description = f"{description}\n[검증: {evidence}]"
    return {
        "name": record.name,
        "description": description[:1024],
        "inputSchema": manifest.get("input_schema")
        or schema_from_cases(manifest),
        "annotations": {"readOnlyHint": annotations["read_only"],
                        "destructiveHint": annotations["destructive"],
                        "idempotentHint": annotations["idempotent"],
                        "openWorldHint": annotations["open_world"]},
    }


def execute(ledger: SkillLedger, name: str, arguments: dict,
            include_staged: bool = False, timeout: float | None = None
            ) -> tuple[bool, str]:
    """검증된 툴 하나를 안전하게 실행한다. **호스트마다 다른 응답 모양은 여기서 안 만든다.**

    MCP(``tools/call``)와 다른 호출 규약(예: 플랫폼의 ``ToolSource.call_tool``)이
    같은 조회+실행 로직을 각자 다시 쓰면, 한쪽만 정책 복원을 고치고 다른 쪽은 예전
    그대로 남는 일이 생긴다. 그래서 "찾고 - 권한 그대로 돌리고 - 결과를 문자열로"는
    이 한 곳에서만 하고, 응답을 감싸는 모양(MCP 의 content 블록 vs 평평한 문자열)만
    부르는 쪽이 정한다.
    """
    entry = servable(ledger, include_staged).get(name)
    if entry is None:
        # 없는 것을 조용히 빈 결과로 주지 않는다 - 부른 쪽이 원인을 알아야 한다.
        return False, f"그런 툴이 없거나 내주지 않습니다: {name}"
    record, manifest = entry
    # 만들 때 허락한 권한 그대로 돌린다. 부르는 쪽이 넓힐 수 없다.
    policy = ToolPolicy.from_dict(manifest.get("policy"))
    if timeout:
        policy.timeout = timeout
    result = run_tool(manifest.get("script", ""), arguments or {}, policy=policy)
    if not result.ok:
        return False, result.error
    return True, json.dumps(result.output, ensure_ascii=False, default=str)


class JermesMcpServer:
    """stdio MCP 서버. 한 번에 한 요청씩 - 툴 실행이 짧고 결정적이라 그걸로 충분하다."""

    def __init__(self, ledger: SkillLedger, include_staged: bool = False,
                 timeout: float | None = None) -> None:
        self.ledger = ledger
        self.include_staged = include_staged
        self.timeout = timeout

    # ── 메서드들 ────────────────────────────────────────────────────────
    def initialize(self, params: dict) -> dict:
        return {"protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO}

    def list_tools(self, params: dict) -> dict:
        return {"tools": [describe(record, manifest) for record, manifest
                          in servable(self.ledger, self.include_staged).values()]}

    def call_tool(self, params: dict) -> dict:
        name = str(params.get("name") or "")
        ok, text = execute(self.ledger, name, params.get("arguments") or {},
                           self.include_staged, self.timeout)
        return self._text(text, error=not ok)

    @staticmethod
    def _text(text: str, error: bool = False) -> dict:
        return {"content": [{"type": "text", "text": text}], "isError": error}

    # ── 배관 ────────────────────────────────────────────────────────────
    def handle(self, message: dict) -> dict | None:
        """요청 하나 -> 응답 하나. 통지(id 없음)에는 답하지 않는 것이 프로토콜이다."""
        method = message.get("method", "")
        request_id = message.get("id")
        routes = {"initialize": self.initialize,
                  "tools/list": self.list_tools,
                  "tools/call": self.call_tool,
                  "ping": lambda params: {}}
        if request_id is None:
            return None
        handler = routes.get(method)
        if handler is None:
            return {"jsonrpc": "2.0", "id": request_id,
                    "error": {"code": -32601, "message": f"모르는 메서드: {method}"}}
        try:
            return {"jsonrpc": "2.0", "id": request_id,
                    "result": handler(message.get("params") or {})}
        except Exception as exc:
            # 서버가 죽으면 부른 쪽은 "연결 문제"로 오진한다. 오류도 응답으로 준다.
            return {"jsonrpc": "2.0", "id": request_id,
                    "error": {"code": -32603, "message": f"{type(exc).__name__}: {exc}"[:300]}}

    def serve(self, lines: Iterable[str] | None = None, out=None) -> None:
        stream = lines if lines is not None else sys.stdin
        sink = out or sys.stdout
        for line in stream:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                continue          # 깨진 줄은 버린다 - 추측해서 답하지 않는다
            response = self.handle(message)
            if response is None:
                continue
            sink.write(json.dumps(response, ensure_ascii=False) + "\n")
            sink.flush()

"""MCP 서버에 실제로 붙어 도구를 받아오고 부른다. stdio 와 HTTP 둘 다. 의존성 0.

왜 제품에 있는가: 이 클라이언트는 원래 `experiments/e10_mcp_live.py` 안에만
있었다. 그래서 `McpLiveSource` 는 테스트와 실험에서만 살아 있고 **CLI 에서는 한
번도 안 불렸다** - 설정에 적힌 MCP 서버는 영원히 "미해결"로 남았고, `route`·`ask`
는 MCP 도구를 하나도 고를 수 없었다. 근처의 도구를 다 쓰겠다고 해놓고 이름만
읽고 있었던 셈이다.

전송은 두 가지다. `command` 가 있으면 stdio, `url` 이면 HTTP. **고르는 자리는
`session_for` 한 곳**이라 나머지는 어느 쪽인지 몰라도 된다.

프로토콜(JSON-RPC 2.0. stdio 는 줄바꿈 구분):
    -> {"jsonrpc":"2.0","id":1,"method":"initialize","params":{...}}
    <- {"jsonrpc":"2.0","id":1,"result":{"capabilities":{...}}}
    -> {"jsonrpc":"2.0","method":"notifications/initialized"}
    -> {"jsonrpc":"2.0","id":2,"method":"tools/list"}
    <- {"jsonrpc":"2.0","id":2,"result":{"tools":[{name,description,inputSchema,...}]}}

[주의] 서버를 띄우는 것은 **임의 명령 실행**이다. 그래서 사용자의 MCP 설정에 적힌
것만 띄우고, 띄운 것은 반드시 내린다. 그리고 기본으로는 안 띄운다 - 능력 목록을
볼 때마다 남의 프로세스를 여는 것은 사용자가 시켜야 하는 일이다(`--live`).
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

PROTOCOL_VERSION = "2025-06-18"
CLIENT = {"name": "jermes", "version": "0.1"}

# 자식 서버에 물려줄 환경변수. **여기 적힌 것만** 간다.
#
# 예전에는 `{**os.environ, **spec_env}` 였다. 설정의 env 는 os.environ 을
# 대체하는 게 아니라 위에 얹는 것이라, 사용자가 env 를 명시해도 나머지가 전부
# 따라갔다. 실측: 심어 둔 가짜 비밀 3개가 서드파티 서버에 원문으로 도착했고
# 환경변수 96개가 넘어갔다(SSH_CLIENT 의 내부망 IP, 살아 있는 VSCODE IPC
# 파이프 핸들 포함).
#
# 우리는 **우리가 만든 툴**에는 이미 이 방어를 걸고 있다(`tools.run_tool`).
# 남이 쓴 코드인 MCP 서버에만 안 걸었으니 신뢰 경계가 거꾸로였다.
# MCP 공식 SDK 도 기본이 allowlist 다.
_INHERITED = (
    "PATH", "HOME", "USERPROFILE", "TEMP", "TMP", "TMPDIR",
    "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT",
    "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES",
    "LANG", "LC_ALL", "TZ", "PYTHONIOENCODING", "PYTHONUTF8",
)


def child_env(extra: dict | None = None) -> dict:
    """자식 서버가 받을 환경. 물려줄 것만 물려주고 나머지는 설정이 준 것만.

    서버가 정말 키가 필요하면 사용자가 MCP 설정의 `env` 에 **그 이름만** 적는다.
    적지 않은 것이 새어 나가지 않는 것이 요점이다.
    """
    base = {name: os.environ[name] for name in _INHERITED if name in os.environ}
    base.update(extra or {})
    return base


class StdioMcp:
    """stdio MCP 서버 하나와의 대화. 최소한만 한다 - initialize 와 tools/list."""

    def __init__(self, command: str, args: list[str], env: dict | None = None,
                 timeout: float = 30.0) -> None:
        self.command, self.args = command, list(args or [])
        self.env = child_env(env)
        self.timeout = timeout
        self.process: subprocess.Popen | None = None
        self._next_id = 0
        self._stderr: list[str] = []

    def __enter__(self) -> "StdioMcp":
        self.process = subprocess.Popen(
            [self.command, *self.args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
            bufsize=1, env=self.env,
            # .cmd 는 셸을 거쳐야 뜬다. 설정에 적힌 것만 띄우므로 여기서만 허용한다.
            shell=self.command.lower().endswith((".cmd", ".bat")))
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        return self

    def __exit__(self, *exc) -> None:
        """띄운 것은 반드시 내린다 - 좀비 프로세스를 남기지 않는다."""
        if not self.process:
            return
        try:
            self.process.stdin.close()
        except Exception:
            pass
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill()

    def _drain_stderr(self) -> None:
        for line in self.process.stderr:
            self._stderr.append(line.rstrip())
            del self._stderr[:-40]

    def _send(self, payload: dict) -> None:
        self.process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def call(self, method: str, params: dict | None = None) -> dict:
        self._next_id += 1
        request_id = self._next_id
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method,
                    "params": params or {}})
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            line = self.process.stdout.readline()
            if not line:
                raise ConnectionError("서버가 stdout 을 닫았습니다: "
                                      + " | ".join(self._stderr[-3:]))
            try:
                message = json.loads(line)
            except ValueError:
                continue          # 서버가 흘린 로그 줄은 무시한다
            if message.get("id") == request_id:
                if "error" in message:
                    raise ConnectionError(str(message["error"])[:200])
                return message.get("result") or {}
        raise TimeoutError(f"{method} 응답 없음({self.timeout:g}s)")

    def notify(self, method: str) -> None:
        self._send({"jsonrpc": "2.0", "method": method})

    def handshake(self) -> None:
        self.call("initialize", {"protocolVersion": PROTOCOL_VERSION,
                                 "capabilities": {}, "clientInfo": CLIENT})
        self.notify("notifications/initialized")

    def list_tools(self) -> list[dict]:
        self.handshake()
        return self.call("tools/list").get("tools") or []

    def call_tool(self, name: str, arguments: dict | None = None) -> dict:
        """도구를 **실제로 부른다**. 결과는 서버가 준 그대로.

        오래 이 자리가 비어 있었다. 도구를 찾아서 카드로 보여 주기만 하면 "근처의
        도구를 다 쓸 수 있다"는 말이 성립하지 않는다 - 찾은 것과 쓴 것은 다르다.
        """
        self.handshake()
        return self.call("tools/call", {"name": name,
                                        "arguments": dict(arguments or {})})


class HttpMcp:
    """Streamable HTTP MCP 서버 하나와의 대화. stdio 와 같은 두 가지만 한다.

    왜 필요한가: 설정을 실제로 읽어 보니 `command` 가 없는 원격 서버가 4곳 있었고,
    stdio 만 말할 줄 알아서 **한 곳도 못 봤다.** 목록에 뜨지도 않으니 없는 것과
    같았다.

    응답은 JSON 이거나 SSE(`text/event-stream`)다. 서버가 고르는 것이라 둘 다 읽는다.
    세션 헤더(`Mcp-Session-Id`)는 서버가 주면 그대로 돌려준다.
    """

    def __init__(self, url: str, headers: dict | None = None,
                 timeout: float = 30.0) -> None:
        self.url = url
        self.headers = dict(headers or {})
        self.timeout = timeout
        self.session_id = ""
        self._next_id = 0

    def __enter__(self) -> "HttpMcp":
        return self

    def __exit__(self, *exc) -> None:
        return None

    def _post(self, payload: dict) -> tuple[dict, dict]:
        headers = {
            "Content-Type": "application/json",
            # 서버가 JSON 으로 줄지 SSE 로 줄지 고르게 둘 다 받는다고 말한다.
            "Accept": "application/json, text/event-stream",
            **self.headers,
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        request = urllib.request.Request(
            self.url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                got = response.headers.get("Mcp-Session-Id")
                if got:
                    self.session_id = got
                body = response.read().decode("utf-8", "replace")
                kind = (response.headers.get("Content-Type") or "").lower()
        except urllib.error.HTTPError as exc:
            # "HTTPError" 한 낱말로는 고칠 수 없다. 서버는 보통 JSON-RPC 오류로
            # 이유를 말해 준다(실측: 401 `unauthenticated` - 쿠키가 만료된 것).
            # 그 말을 그대로 올려 보낸다.
            detail = exc.read().decode("utf-8", "replace")[:300]
            try:
                said = json.loads(detail).get("error", {}).get("message", "")
            except ValueError:
                said = detail.strip()
            raise ConnectionError(
                f"HTTP {exc.code}" + (f" {said}" if said else "")) from None
        if "text/event-stream" in kind:
            # SSE: `data:` 줄만 모은다. 마지막 JSON 이 이 요청의 답이다.
            messages = []
            for line in body.splitlines():
                if line.startswith("data:"):
                    try:
                        messages.append(json.loads(line[5:].strip()))
                    except ValueError:
                        continue
            return (messages[-1] if messages else {}), {}
        if not body.strip():
            return {}, {}
        return json.loads(body), {}

    def call(self, method: str, params: dict | None = None) -> dict:
        self._next_id += 1
        message, _ = self._post({"jsonrpc": "2.0", "id": self._next_id,
                                 "method": method, "params": params or {}})
        if "error" in message:
            raise ConnectionError(str(message["error"])[:200])
        return message.get("result") or {}

    def notify(self, method: str) -> None:
        self._post({"jsonrpc": "2.0", "method": method})

    def handshake(self) -> None:
        self.call("initialize", {"protocolVersion": PROTOCOL_VERSION,
                                 "capabilities": {}, "clientInfo": CLIENT})
        self.notify("notifications/initialized")

    def list_tools(self) -> list[dict]:
        self.handshake()
        return self.call("tools/list").get("tools") or []

    def call_tool(self, name: str, arguments: dict | None = None) -> dict:
        self.handshake()
        return self.call("tools/call", {"name": name,
                                        "arguments": dict(arguments or {})})


def session_for(spec: dict, timeout: float = 30.0):
    """설정 한 줄 -> 말이 통하는 세션. **전송 방식은 여기서만 고른다.**

    호출측이 stdio 인지 http 인지 알아야 하면 언젠가 한 경로에서 한쪽만 다루고,
    그 경로에서는 그 서버가 조용히 사라진다.
    """
    if spec.get("command"):
        return StdioMcp(spec.get("command", ""), spec.get("args") or [],
                        spec.get("env"), timeout=timeout)
    url = spec.get("url") or ""
    if not url:
        raise ValueError("command 도 url 도 없는 MCP 설정입니다")
    return HttpMcp(url, spec.get("headers"), timeout=timeout)


def list_tools_for(spec: dict, timeout: float = 30.0):
    """설정 한 줄 -> `tools/list` 를 부르는 콜러블. stdio 든 http 든.

    `McpLiveSource` 는 받아온 모양만 다루고 접속 방식은 모른다. 그 경계를 지키려고
    붙이는 일은 전부 여기서 한다.
    """
    def list_tools() -> list[dict]:
        with session_for(spec, timeout) as session:
            return session.list_tools()
    return list_tools


# 옛 이름. 전송이 stdio 뿐이던 시절의 호출자가 남아 있을 수 있다.
stdio_list_tools = list_tools_for


def call_tool(spec: dict, tool: str, arguments: dict,
              timeout: float = 60.0) -> tuple[bool, str]:
    """도구 하나를 부르고 끝낸다(띄웠으면 내린다). 반환 (성공, 사람이 읽을 결과).

    MCP 결과는 `content` 블록 목록이다. 텍스트만 뽑아 붙인다 - 우리가 할 일은
    보여 주는 것이지 해석하는 것이 아니다. `isError` 는 서버가 말한 그대로 옮긴다.
    """
    with session_for(spec, timeout) as session:
        result = session.call_tool(tool, arguments)
    blocks = result.get("content") or []
    text = "\n".join(str(b.get("text", "")) for b in blocks
                     if isinstance(b, dict) and b.get("type") == "text").strip()
    if not text:
        # 텍스트가 없다고 실패는 아니다(이미지·리소스일 수 있다). 모양을 알려 준다.
        kinds = sorted({b.get("type", "?") for b in blocks if isinstance(b, dict)})
        text = f"(텍스트 아닌 결과 {len(blocks)}블록: {', '.join(kinds) or '없음'})"
    return not result.get("isError", False), text


# 옛 이름.
call_stdio_tool = call_tool


def load_servers(paths) -> dict[str, dict]:
    """MCP 설정 파일들에서 stdio 서버 정의를 모은다. 먼저 나온 것이 이긴다."""
    servers: dict[str, dict] = {}
    for path in paths:
        path = Path(path)
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        found = dict(data.get("mcpServers") or {})
        for project in (data.get("projects") or {}).values():
            found.update(project.get("mcpServers") or {})
        for name, spec in found.items():
            # stdio(`command`) 든 원격(`url`) 이든 받는다. 예전에는 명령이 있는
            # 것만 받아서, 설정에 있던 원격 서버 4곳을 **한 곳도 못 봤다.**
            if not isinstance(spec, dict) or name in servers:
                continue
            if spec.get("command") or spec.get("url"):
                servers[name] = spec
    return servers

"""Claude Code 세션 기록 -> `RunTrace`. **플랫폼 없이 도는 첫 번째 원천.**

왜 이게 중요한가: 지금까지 Jermes 는 플랫폼 spine 이 있어야만 배울 수 있었다. 그래서
"플랫폼 부속"이었다. Claude Code 는 세션마다 `~/.claude/projects/<프로젝트>/<세션>.jsonl`
에 **도구 호출·결과·오류**를 그대로 남긴다 - 즉 이미 로컬에 진짜 에이전트 실행 기록이
쌓여 있다. 이걸 읽으면 Jermes 는 **혼자서** 배울 수 있다.

기록 형식(실측):
    {"type": "assistant", "message": {"content": [{"type": "tool_use",
                                                   "name": "Bash", "input": {...}}]}}
    {"type": "user",      "message": {"content": [{"type": "tool_result",
                                                   "tool_use_id": "...",
                                                   "content": "...",
                                                   "is_error": true}]}}
    {"type": "user",      "message": {"content": [{"type": "text", "text": "사용자 지시"}]}}

매핑 규칙(엔진의 신호 추출기가 보는 모양에 맞춘다):
- tool_use + 짝지어진 tool_result -> `tool_call` (ok = not is_error)
- 실패한 도구 호출 뒤에 **같은 도구가 성공**하면 그 지점에 `recovery` 를 끼운다
  (엔진 `recovery` 추출기가 error -> recovery 순서를 본다)
- 도구 결과가 아닌 사용자 텍스트 중 교정으로 보이는 것 -> `user_correction`

[주의] 추측하지 않는다: 판단할 수 없는 줄은 버린다. 없는 신호를 지어내면 그 위의 검증이
전부 무의미해진다.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from ..model import RunTrace, TraceEvent

# 사용자가 방향을 되돌리는 말 - 있으면 그 자리가 배울 거리다.
# 교정을 알아보는 낱말들. **목록으로 두는 것이 맞는지 재 봤다.**
#
# 구조로 대신할 수 있을까 싶어 "도구를 쓰던 중에 사람이 끼어들었다" 를 대안으로
# 놓고 실세션 12개에서 견줬다:
#     낱말 규칙 193건 · 구조 규칙 53건 · 둘 다 20건
# 구조 규칙이 잡은 53건 중 서른셋은 교정이 아니었다 - 시스템 알림, task
# 알림, "Continue from where you left off". 낱말 목록이 실제로 더 나은 도구다.
#
# 다만 이 목록은 우리말과 영어뿐이다. 다른 말로 일하는 사람은 교정이 **조용히**
# 0건이 된다 - 토크나이저에서 이미 한 번 겪은 실패다. 그래서 목록의 주인을
# 사용자에게 넘긴다: `JERMES_CORRECTION_HINTS` 에 쉼표로 적으면 더해진다.
_BUILTIN_CORRECTION_HINTS = (
    "아니", "그게 아니", "다시", "틀렸", "하지 마", "말고", "잘못",
    "no,", "not that", "instead", "revert", "undo", "wrong",
)


def _correction_hints() -> tuple[str, ...]:
    extra = os.environ.get("JERMES_CORRECTION_HINTS", "")
    added = tuple(h.strip().lower() for h in extra.split(",") if h.strip())
    return _BUILTIN_CORRECTION_HINTS + added
# 교정으로 보기엔 너무 짧거나 단순한 것은 제외(잡음).
_MIN_CORRECTION_CHARS = 6


def default_root() -> Path:
    """Claude Code 프로젝트 기록의 기본 위치. 환경변수로 덮어쓸 수 있다."""
    override = os.environ.get("JERMES_CLAUDE_PROJECTS")
    if override:
        return Path(override)
    return Path.home() / ".claude" / "projects"


def _newest_first(base, suffix: str = ".jsonl", prefix: str = "",
                  min_size: int = 0) -> list[Path]:
    """하위 폴더까지 훑어 **최근 순**으로. `os.scandir` 로 한 번에 읽는다.

    예전에는 `rglob` 로 목록을 만든 뒤 파일마다 `Path.stat()` 을 불렀다. 세션이
    14,320개인 기계에서 3.39초가 들었고, 그게 `status` 8.07초와 `doctor` 11초의
    절반이었다. `scandir` 은 디렉터리를 읽을 때 stat 정보를 같이 받아 오므로 그
    호출이 통째로 없어진다.

    실측: 3.39초 -> 0.84초(4.0배). 파일 집합 동일, 상위 50개 순서 동일.
    """
    found: list[tuple[float, str]] = []
    stack = [str(base)]
    while stack:
        try:
            with os.scandir(stack.pop()) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                            continue
                        if not entry.name.endswith(suffix):
                            continue
                        if prefix and not entry.name.startswith(prefix):
                            continue
                        stat = entry.stat()
                        if min_size and stat.st_size < min_size:
                            continue      # 빈 파일은 세션이 아니다
                        found.append((stat.st_mtime, entry.path))
                    except OSError:
                        continue      # 사라졌거나 못 읽는 항목 하나로 멈추지 않는다
        except OSError:
            continue
    found.sort(reverse=True)
    return [Path(p) for _, p in found]


def project_key(path) -> str:
    """작업 폴더 -> 세션이 쓰는 폴더 이름. 기억을 프로젝트별로 가르는 열쇠다.

    Claude Code 는 작업 폴더를 이렇게 인코딩해 세션을 담는다:

        C:\\Users\\wlstn  ->  C--Users-wlstn
        D:\\               ->  d--

    `:` 와 경로 구분자를 `-` 로 바꾸는 것뿐이라 우리도 같은 열쇠를 만든다.

    **틀려도 안전한 쪽으로 틀린다.** 인코딩이 짐작과 다르면 그 사실이 지금
    프로젝트에서 안 불릴 뿐이고(회상이 줄어든다), 남의 프로젝트 사실이 딸려 오는
    일은 안 생긴다.
    """
    text = str(path)
    for bad in (":", "\\", "/"):
        text = text.replace(bad, "-")
    return text


def scope_of_session(path: Path) -> str:
    """세션 파일 -> 그 세션이 돈 프로젝트의 스코프.

    루트 바로 아래 폴더가 프로젝트다. 그 아래로 더 들어간 것(`subagents` 등)은
    같은 프로젝트의 하위라 위로 올라가 찾는다.
    """
    root = default_root().resolve()
    try:
        here = Path(path).resolve()
        rel = here.relative_to(root)
    except (OSError, ValueError):
        return "user"          # 어디 것인지 모르면 가르지 않는다
    return f"project:{rel.parts[0]}" if rel.parts else "user"


def iter_session_files(root: Path | None = None) -> list[Path]:
    """세션 기록 파일들 - 최근 수정 순."""
    base = Path(root) if root else default_root()
    if not base.exists():
        return []
    return _newest_first(base, min_size=1)


def _iter_records(path: Path, limit_lines: int = 0) -> Iterator[dict]:
    with Path(path).open(encoding="utf-8", errors="replace") as handle:
        for index, line in enumerate(handle):
            if limit_lines and index >= limit_lines:
                return
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue          # 깨진 줄은 버린다 - 추측해서 채우지 않는다
            if isinstance(record, dict):
                yield record


def _content_blocks(record: dict) -> list[dict]:
    message = record.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if isinstance(content, list):
        return [c for c in content if isinstance(c, dict)]
    return []


def _result_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [str(c.get("text") or "") for c in content if isinstance(c, dict)]
        return " ".join(p for p in parts if p)
    return str(content or "")


def _short_input(value) -> str:
    """도구 입력을 한 줄로. 길면 자른다 - 파일 전문이 들어오는 경우가 있다."""
    if not isinstance(value, dict):
        return ""
    parts = []
    for key, item in value.items():
        text = str(item).replace(chr(10), " ")
        parts.append(f"{key}={text[:200]}")
    return " ".join(parts)[:600]


def _looks_like_correction(text: str) -> bool:
    stripped = (text or "").strip()
    if len(stripped) < _MIN_CORRECTION_CHARS:
        return False
    low = stripped.lower()
    return any(hint in low for hint in _correction_hints())


def load_trace(path: Path | str, *, scope: str = "", scope_key: str = "",
               max_lines: int = 0) -> RunTrace:
    """세션 파일 하나를 `RunTrace` 로 읽는다.

    `max_lines` 는 아주 긴 세션(수만 줄)에서 앞부분만 볼 때 쓴다(0 = 전체).
    """
    path = Path(path)
    # tool_use_id -> (도구 이름, 입력 한 줄).
    # 입력까지 들고 오는 이유: 실패한 호출과 성공한 재시도의 입력을 견주면
    # **무엇을 바꿔서 통과했는가**가 나오고, 그게 재현벤치의 진짜 요구조건이다.
    # 이름만 들고 오던 동안에는 벤치가 복구 문구 템플릿에서 낱말을 뽑아
    # "도구 이름을 적었는가"라는 서식을 쟀다.
    pending: dict[str, tuple[str, str]] = {}
    events: list[TraceEvent] = []
    failed_tools: set[str] = set()
    lessons: list[str] = []      # 이 원천은 정제된 사실을 주지 않는다 - 비워 둔다

    for record in _iter_records(path, max_lines):
        for block in _content_blocks(record):
            kind = block.get("type")
            if kind == "tool_use":
                use_id = str(block.get("id") or "")
                if use_id:
                    pending[use_id] = (str(block.get("name") or "tool"),
                                       _short_input(block.get("input")))
            elif kind == "tool_result":
                use_id = str(block.get("tool_use_id") or "")
                name, sent = pending.pop(use_id, ("tool", ""))
                is_error = bool(block.get("is_error"))
                detail = _result_text(block)[:300]
                if is_error:
                    failed_tools.add(name)
                elif name in failed_tools:
                    # 같은 도구가 실패 뒤에 성공했다 = 우회로를 찾은 지점.
                    failed_tools.discard(name)
                    events.append(TraceEvent(type="recovery", name=name,
                                             detail=f"{name} succeeded after failing"))
                # 입력도 싣는다. 실패한 호출과 성공한 재시도의 입력을 견주면
                # **무엇을 바꿔서 통과했는가**가 나오고, 그게 재현벤치의 진짜
                # 요구조건이 된다. 예전에는 결과만 실어서, 벤치가 복구 문구
                # 템플릿("<도구> succeeded after failing")에서 낱말을 뽑았고
                # 결국 "도구 이름을 적었는가"라는 서식을 쟀다.
                events.append(TraceEvent(
                    type="tool_call", name=name, ok=not is_error, detail=detail,
                    meta={"input": sent}))
            elif kind == "text" and record.get("type") == "user":
                text = str(block.get("text") or "")
                if _looks_like_correction(text):
                    # 교정은 **신호**로만 쓴다(초안 작성의 재료). `lessons` 에는 넣지
                    # 않는다 - lessons 는 기억으로 적재되는데, 대화 원문은 사실이
                    # 아니라 그때의 지시다. 라이브에서 확인: "계속하고 있지? 그냥…"
                    # 같은 문장이 사실처럼 기억에 쌓였고, 그게 나중에 프롬프트로
                    # 되돌아가면 지난 지시가 되살아난다.
                    events.append(TraceEvent(type="user_correction", name="user",
                                             detail=text.strip()[:300]))

    calls = [e for e in events if e.type == "tool_call"]
    success = bool(calls) and calls[-1].ok
    return RunTrace(
        run_id=f"claude-code:{path.stem}",
        scope=scope or "user",
        # 프로젝트는 **여기** 담는다. `scope` 는 플랫폼 열거형(session/workflow/
        # user/platform)이라 프로젝트를 담을 자리가 아니다 - 넣어 봤더니 모델이
        # 거부했다. `scope_key` 가 자유 문자열이고 원래 이 용도다.
        #
        # 안 주면 **세션이 돈 프로젝트**에서 낸다. 예전에는 배운 사실이 전부
        # 전역이라 "이 저장소의 기본 브랜치는 develop" 이 모든 프로젝트에 딸려 갔다.
        scope_key=scope_key or scope_of_session(path),
        events=events,
        lessons=lessons[:5],
        refined_memory="",
        success=success,
    )


@dataclass
class SessionSummary:
    """세션 하나를 학습 재료로 볼 때의 요약 - 고르기 전에 보는 값."""

    path: Path
    run_id: str
    tool_calls: int = 0
    errors: int = 0
    recoveries: int = 0
    corrections: int = 0
    success: bool = False
    signals: list[str] = field(default_factory=list)

    @property
    def worth_learning(self) -> bool:
        """배울 거리가 있는가 - 신호 추출기가 실제로 뭔가 잡았을 때만."""
        return bool(self.signals)

    def line(self) -> str:
        # **같은 낱말을 서른 번 늘어놓지 않는다.** 신호를 그대로 이어 붙이니
        # `user_correction` 이 34번 반복돼 한 줄이 화면을 넘어갔고, 세션을 훑어
        # 고르라고 만든 화면에서 정작 훑을 수가 없었다. 종류와 개수가 알고 싶은
        # 전부다 - 어느 것이 몇 번인지.
        if self.signals:
            counted: dict[str, int] = {}
            for name in self.signals:
                counted[name] = counted.get(name, 0) + 1
            marks = ", ".join(f"{k}x{v}" if v > 1 else k
                              for k, v in sorted(counted.items(),
                                                 key=lambda kv: -kv[1]))
        else:
            marks = "신호 없음"
        return (f"{self.path.name[:12]}… 도구 {self.tool_calls:>4} · 오류 {self.errors:>3} · "
                f"복구 {self.recoveries:>2} · 교정 {self.corrections:>2} → {marks}")


def summarize_session(path: Path | str, *, max_lines: int = 0) -> SessionSummary:
    from ..signals import extract_signals

    trace = load_trace(path, max_lines=max_lines)
    calls = [e for e in trace.events if e.type == "tool_call"]
    return SessionSummary(
        path=Path(path),
        run_id=trace.run_id,
        tool_calls=len(calls),
        errors=sum(1 for e in calls if not e.ok),
        recoveries=sum(1 for e in trace.events if e.type == "recovery"),
        corrections=sum(1 for e in trace.events if e.type == "user_correction"),
        success=trace.success,
        signals=[hit.signal for hit in extract_signals(trace)],
    )

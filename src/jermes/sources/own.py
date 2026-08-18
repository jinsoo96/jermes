"""Jermes 가 **자기가 한 일**을 배울 거리로 읽는다.

지적(재현함): `ask` 와 `run` 은 원장에 성공/실패 불리언 하나만 남기고 끝났다.
`RunTrace` 를 만들지 않으니 학습 루프에 아무것도 들어가지 않는다. 그래서 이 물건은
Claude Code 와 Codex 세션에서는 배우면서 **정작 사용자가 자기한테 시킨 일에서는
아무것도 배우지 않았다.** 개인화 에이전트인데 자기 자신만 안 보고 있었던 셈이다.

특히 아까운 것이 실패다. 학습 재료 중 제일 값진 것은 실패-복구 쌍인데(재현벤치가
통째로 그걸로 만들어진다), `ask` 의 실패는 `record_outcome(..., False)` 한 줄로
버려졌다. 무엇을 넣었다가 왜 깨졌는지가 사라진다.

다른 원천과 **같은 모양**으로 적는다. 그래야 엔진도 CLI 도 이 원천을 특별 취급하지
않는다 - 원천 하나를 붙이는 방법은 모듈 하나와 `ADAPTERS` 한 줄이라고 이 폴더가
스스로 말해 뒀고, 그 말을 지킨다.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from ..model import RunTrace, TraceEvent
from .claude_code import SessionSummary

__all__ = ["SessionSummary", "default_root", "iter_session_files", "load_trace",
           "record", "summarize_session"]

PREFIX = "jermes-"

# 기록에 실패한 이유. 삼키되 남긴다 - 통째로 조용하면 기록기가 영영 망가져도
# 아무도 모르고, 사용자는 자기가 시킨 일에서 아무것도 안 배워지는 이유를 알
# 길이 없다. `Registry.load_errors` 와 같은 방식이고 `doctor` 가 꺼내 본다.
record_errors: list[str] = []


def default_root() -> Path:
    override = os.environ.get("JERMES_OWN_SESSIONS")
    if override:
        return Path(override)
    home = os.environ.get("JERMES_HOME")
    base = Path(home) if home else Path.home() / ".jermes"
    return base / "sessions"


def iter_session_files(root: Path | None = None) -> list[Path]:
    base = Path(root) if root else default_root()
    if not base.is_dir():
        return []
    from .claude_code import _newest_first

    return _newest_first(base, prefix=PREFIX)


def record(query: str, tool: str, payload, ok: bool, detail: str = "",
           seconds: float = 0.0, root: Path | None = None) -> Path:
    """`ask`/`run` 한 번을 하루치 파일에 한 줄로 덧붙인다.

    하루 한 파일인 이유: 한 호출당 한 파일이면 파일 수천 개가 쌓이고, 통째로 한
    파일이면 오래된 것을 버릴 수가 없다. 다른 원천도 세션 단위로 끊는다.

    **덧붙이기만 한다.** 읽고-고쳐-쓰기를 하면 두 프로세스가 겹칠 때 앞의 줄이
    사라진다. 한 줄 append 는 그 위험이 없다.
    """
    base = Path(root) if root else default_root()
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"{PREFIX}{time.strftime('%Y%m%d')}.jsonl"
    row = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "query": query,
        "tool": tool,
        "input": _short(payload),
        "ok": bool(ok),
        "detail": detail,
        "seconds": round(float(seconds), 3),
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


def _short(value) -> str:
    """입력을 한 줄로. 재현벤치가 실패와 성공한 재시도의 입력을 견주므로 모양을
    다른 원천과 맞춘다."""
    if isinstance(value, dict):
        parts = [f"{k}={str(v).replace(chr(10), ' ')[:200]}" for k, v in value.items()]
        return " ".join(parts)[:600]
    return str(value)[:600] if value is not None else ""


def load_trace(path: Path | str, *, scope: str = "user", scope_key: str = "",
               max_lines: int = 0) -> RunTrace:
    path = Path(path)
    events: list[TraceEvent] = []
    rows: list[dict] = []
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if max_lines and number > max_lines:
                break
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue      # 반쯤 쓰인 줄 하나 때문에 그날치를 통째로 버리지 않는다
            if isinstance(row, dict):
                rows.append(row)

    failed_at: dict[str, int] = {}
    for row in rows:
        tool = str(row.get("tool") or "tool")
        ok = bool(row.get("ok"))
        events.append(TraceEvent(
            type="tool_call", name=tool, ok=ok,
            detail=str(row.get("detail") or ""),
            meta={"input": str(row.get("input") or ""),
                  "query": str(row.get("query") or "")}))
        # 실패한 뒤 같은 도구가 통과하면 복구다. 학습 재료 중 제일 값진 쌍이고,
        # 예전에는 `ask` 가 이걸 불리언 하나로 버렸다.
        if not ok:
            failed_at[tool] = len(events) - 1
        elif tool in failed_at:
            events.append(TraceEvent(
                type="recovery", name=tool, ok=True,
                detail=str(row.get("detail") or ""),
                meta={"input": str(row.get("input") or "")}))
            failed_at.pop(tool, None)

    return RunTrace(
        run_id=path.stem,
        scope=scope,
        scope_key=scope_key,
        events=events,
        lessons=[],
        refined_memory="",
        judge_score=None,
        success=all(e.ok for e in events) if events else True,
    )


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
        corrections=0,      # 자기 기록에는 사용자 교정이라는 개념이 없다
        success=trace.success,
        signals=[hit.signal for hit in extract_signals(trace)],
    )

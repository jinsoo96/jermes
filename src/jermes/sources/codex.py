"""Codex CLI 세션 기록(`~/.codex/sessions/**/rollout-*.jsonl`) -> `RunTrace`.

Claude Code 하나만 읽던 것을 넓힌다. 사람이 쓰는 에이전트는 하나가 아니고, 배울
거리는 **어디서 일했든** 거기 있다. 엔진은 이 파일이 있는지도 모른다 - `RunTrace`
로만 들어오면 같은 루프가 돈다.

Codex 의 줄 모양(2026-08 확인, cli 0.144):
    {"type":"session_meta", "payload":{...}}
    {"type":"response_item","payload":{"type":"function_call","name":...,"arguments":...}}
    {"type":"response_item","payload":{"type":"function_call_output","output":...}}
    {"type":"response_item","payload":{"type":"custom_tool_call","name":...,"input":...}}
    {"type":"response_item","payload":{"type":"message","role":"user","content":[...]}}

`reasoning` 은 안 읽는다. 모델이 혼자 생각한 것은 **일어난 일이 아니다** -
그것으로 스킬을 만들면 하지도 않은 절차를 배운다.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from ..model import RunTrace, TraceEvent
from .claude_code import (
    _MIN_CORRECTION_CHARS,
    SessionSummary,
    _looks_like_correction,
)

# 실패로 볼 출력. Codex 는 성공/실패 플래그를 안 주므로 출력에서 읽어야 한다.
_FAILURE_HINTS = ("error", "traceback", "exit code 1", "exit code 2",
                  "not found", "failed", "permission denied", "no such file")


def default_root() -> Path:
    override = os.environ.get("JERMES_CODEX_SESSIONS")
    if override:
        return Path(override)
    return Path.home() / ".codex" / "sessions"


def iter_session_files(root: Path | None = None) -> list[Path]:
    base = Path(root) if root else default_root()
    if not base.is_dir():
        return []
    from .claude_code import _newest_first

    return _newest_first(base, prefix="rollout-")


def _content_text(payload: dict) -> str:
    """message payload 의 본문. content 는 문자열이거나 블록 목록이다."""
    content = payload.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [str(b.get("text", "")) for b in content
                 if isinstance(b, dict) and b.get("text")]
        return "\n".join(parts)
    return ""


def load_trace(path: Path, max_lines: int = 8000) -> RunTrace:
    """세션 하나 -> 트레이스. 도구 호출과 그 결과를 짝지어 성공/실패를 매긴다."""
    events: list[TraceEvent] = []
    pending: dict[str, int] = {}          # call_id -> events 안 위치
    lines = 0
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        lines += 1
        if max_lines and lines > max_lines:
            break
        raw = raw.strip()
        if not raw:
            continue
        try:
            record = json.loads(raw)
        except ValueError:
            continue
        if record.get("type") != "response_item":
            continue
        payload = record.get("payload") or {}
        kind = payload.get("type")

        if kind in ("function_call", "custom_tool_call"):
            detail = str(payload.get("arguments") or payload.get("input") or "")
            events.append(TraceEvent(type="tool_call",
                                     name=str(payload.get("name") or "tool"),
                                     ok=True, detail=detail[:2000]))
            call_id = str(payload.get("call_id") or "")
            if call_id:
                pending[call_id] = len(events) - 1
        elif kind in ("function_call_output", "custom_tool_call_output"):
            output = str(payload.get("output") or "")
            low = output.lower()
            failed = any(hint in low for hint in _FAILURE_HINTS)
            index = pending.pop(str(payload.get("call_id") or ""), None)
            if index is None:
                continue
            # 결과는 그 호출에 붙인다. 별도 이벤트로 두면 도구 수가 두 배로 세어져
            # "도구 1800건" 같은 거짓 숫자가 나온다.
            call = events[index]
            events[index] = TraceEvent(type=call.type, name=call.name,
                                       ok=not failed,
                                       detail=(output[:2000] if failed
                                               else call.detail))
            if not failed and index + 1 <= len(events):
                # 실패 뒤 성공은 복구다. 재현벤치의 재료가 되는 자리.
                previous = next((e for e in reversed(events[:index])
                                 if e.type == "tool_call"), None)
                if previous is not None and not previous.ok:
                    events.append(TraceEvent(type="recovery", name=call.name,
                                             ok=True,
                                             detail=output[:400]))
        elif kind == "message" and payload.get("role") == "user":
            text = _content_text(payload).strip()
            if len(text) >= _MIN_CORRECTION_CHARS and _looks_like_correction(text):
                events.append(TraceEvent(type="user_correction", name="user",
                                         ok=True, detail=text[:1000]))

    return RunTrace(run_id=f"codex:{path.stem}", scope="user",
                    events=events, success=True,
                    lessons=[],          # 이 원천도 정제 기억을 주지 않는다
                    refined_memory="")


def summarize_session(path: Path | str, *, max_lines: int = 0) -> SessionSummary:
    """Claude Code 쪽과 **같은 계약**을 낸다. 호출측이 원천을 구분할 필요가 없다."""
    from ..signals import extract_signals

    trace = load_trace(Path(path), max_lines=max_lines)
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

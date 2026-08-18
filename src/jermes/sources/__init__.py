"""트레이스 원천 - 끝난 에이전트 실행을 `RunTrace` 로 읽어오는 어댑터들.

엔진은 어디서 실행이 일어났는지 모른다. 플랫폼 spine 이든 Claude Code 세션이든
Codex 세션이든 **Jermes 자신이 한 일**이든 `RunTrace` 로만 들어오면 같은 루프가 돈다. 새 원천을 붙이려면
여기에 모듈 하나를 추가하고 `ADAPTERS` 에 한 줄 적으면 된다 - 엔진 본체도, CLI 도
건드리지 않는다.

**어느 원천에서 왔는지는 파일 경로가 정한다.** 호출측이 "이건 Codex 야" 하고
알려 줄 필요가 없다. 알려 주게 만들면 언젠가 한 경로에서 안 알려 주고, 그
경로에서는 그 원천이 조용히 사라진다.
"""

from __future__ import annotations

import os
from pathlib import Path

from . import claude_code, codex, own
from .claude_code import SessionSummary

# (이름, 모듈) - 목록이 곧 지원하는 원천이다.
ADAPTERS = (("claude-code", claude_code), ("codex", codex), ("own", own))

__all__ = ["ADAPTERS", "SessionSummary", "adapter_for", "enabled_sources",
           "iter_session_files", "load_trace", "source_of", "summarize_session"]


def enabled_sources() -> list[str]:
    """읽을 원천 목록. `JERMES_SOURCES` 로 좁힌다(쉼표 구분), 기본은 전부.

    왜 스위치가 하나여야 하나: 원천이 하나였을 때는 `JERMES_CLAUDE_PROJECTS` 하나로
    격리가 됐다. 원천을 늘리자 그 환경변수를 세워 둔 시험이 **남의 진짜 세션까지**
    읽기 시작했다. 원천을 추가할 때마다 격리 장치를 하나씩 더 세워야 한다면 언젠가
    빠뜨리고, 그때 사용자 기록이 시험에 섞인다.
    """
    raw = os.environ.get("JERMES_SOURCES", "").strip()
    if not raw:
        return [name for name, _ in ADAPTERS]
    wanted = {part.strip() for part in raw.split(",") if part.strip()}
    return [name for name, _ in ADAPTERS if name in wanted]


def iter_session_files(root: Path | None = None) -> list[Path]:
    """켜져 있는 원천의 세션을 **한 목록으로**, 최근 순.

    `root` 를 주면 그것만 본다(옛 계약 유지). 안 주면 붙어 있는 원천을 전부 훑는다 -
    사람이 Claude Code 로 일하다 Codex 로 옮겨도 배울 거리는 이어져야 한다.
    """
    if root is not None:
        return claude_code.iter_session_files(root)
    on = set(enabled_sources())
    found: list[Path] = []
    for name, module in ADAPTERS:
        if name in on:
            found.extend(module.iter_session_files(None))
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)


def source_of(path: Path | str) -> str:
    """이 파일이 어느 원천 것인지. 모르면 기본 원천으로 본다."""
    name = Path(path).name
    if name.startswith("rollout-"):
        return "codex"
    if name.startswith(own.PREFIX):
        return "own"
    return "claude-code"


def adapter_for(path: Path | str):
    name = source_of(path)
    return dict(ADAPTERS)[name]


def load_trace(path: Path | str, max_lines: int = 0):
    return adapter_for(path).load_trace(Path(path), max_lines=max_lines)


def summarize_session(path: Path | str, *, max_lines: int = 0) -> SessionSummary:
    return adapter_for(path).summarize_session(Path(path), max_lines=max_lines)

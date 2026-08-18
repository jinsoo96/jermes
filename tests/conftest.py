"""테스트가 `pytest` 한 줄로 돌게 경로를 잡는다.

- `src/` : 패키지 본체(jermes)
- 레포 루트 : absorb.py(흡수 매니페스트) - 드리프트 테스트가 읽는다
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for path in (ROOT / "src", ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


@pytest.fixture(autouse=True)
def _isolate_session_sources(monkeypatch):
    """시험은 **사용자의 진짜 세션을 읽지 않는다.**

    원천이 하나였을 때는 `JERMES_CLAUDE_PROJECTS` 하나를 세우면 격리가 됐다.
    Codex 원천을 붙이자 그 환경변수를 세워 둔 시험이 사용자의 진짜 Codex 기록까지
    읽기 시작했고, "배울 거리 0개" 를 기대하던 시험이 4개를 봤다. 원천을 늘릴
    때마다 격리 장치를 하나씩 더 세우게 두면 언젠가 빠뜨린다. 여기서 한 번에 막는다.
    """
    monkeypatch.setenv("JERMES_SOURCES", "claude-code")


@pytest.fixture(autouse=True)
def _no_ambient_llm(monkeypatch):
    """시험은 **떠 있는 LLM 을 우연히 물지 않는다.**

    `build_completer` 는 localhost 다섯 포트를 훑어 살아 있는 것에 붙는다. 사람이
    쓸 때는 그게 친절이지만, 시험에서는 초록불의 뜻이 "코드가 맞다"에서 "내 기계에
    뭐가 떠 있다"로 바뀐다. 실측: 로컬 141초 초록 · CI 27초 빨강. 그 차이가 결함을
    하나 감추고 있었다(얇은 근거 시험이 CI 에서만 깨졌다).

    LLM 이 필요한 시험은 `cli.build_completer` 를 직접 세운다. 세우는 편이 낫다 -
    무엇을 답하게 했는지가 시험 안에 보인다.
    """
    monkeypatch.delenv("JERMES_BASE_URL", raising=False)
    monkeypatch.delenv("JERMES_MODEL", raising=False)
    monkeypatch.setattr("jermes.cli.discover_endpoint", lambda *a, **k: ("", ""))

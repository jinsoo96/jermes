"""초개인화 - **이 사람의 이 프로젝트**에 대해 배우고, 그것만 꺼낸다.

두 결함이 있었다. 둘 다 "장치는 있는데 아무도 안 쓴다" 부류라 시험도 통과하고
코드도 있어서 된다고 믿게 만든다.

1. **신뢰가 한 번도 안 움직였다.** 실측: 실세션 학습에서 `기억 +6 · 측정 6(↑0 ↓0)`.
   여섯 건이 전부 0.50 에 머물렀다. "메모리 기반 자가개선"이라고 해놓고 자가개선의
   결과가 0 이다. 원인은 스킬 게이트와 같은 뿌리였다 - 사실을 **무관한 실패**에
   대고 재니 이득이 희석돼 0 이 되고, 0 은 neutral 이라 신뢰가 안 움직인다.

2. **프로젝트가 한 번도 안 갈렸다.** `recall(scope=...)` 은 제대로 도는데
   모든 사실이 `scope="user"` 로 박히고 `_recall_for` 는 스코프를 안 넘겼다.
   프로젝트 A 의 "기본 브랜치는 develop" 이 B 의 질문에 딸려 온다.
"""

import pytest

from jermes import cli
from jermes.agent import JermesAgent
from jermes.gate import BenchCase, ForgeGate
from jermes.ledger import JsonlSkillLedger
from jermes.memory import MemoryItem, recall


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("JERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JERMES_SKILL_PATH", str(tmp_path / "none"))
    monkeypatch.setenv("JERMES_SOURCES", "own")
    return tmp_path


# --- 신뢰가 측정으로 움직인다 --------------------------------------------------

def test_trust_moves_when_a_fact_actually_helps(tmp_path):
    """실측: 여섯 건을 재고 ↑0 ↓0 이었다. 관계된 실패로만 재면 이득이 안 희석된다."""
    git = [BenchCase(case_id=f"g{i}",
                     payload={"tool": "Bash",
                              "error_detail": "warning: LF will be replaced by CRLF"})
           for i in range(6)]
    unrelated = [BenchCase(case_id=f"o{i}",
                           payload={"tool": "TodoWrite",
                                    "error_detail": "InputValidationError: JSON"})
                 for i in range(6)]

    fact = MemoryItem(item_id="m1", scope="user", source_run_ids=[],
                      text="윈도우에서 git 은 LF 를 CRLF 로 바꾼다는 경고를 낸다")

    def score(case, item):
        # 그 사실은 관계된 케이스에서만 돕는다(현실을 흉내낸다).
        return 1.0 if item is not None and case.case_id.startswith("g") else 0.0

    ledger = JsonlSkillLedger(str(tmp_path / "skills.jsonl"))
    agent = JermesAgent(ledger, ForgeGate(lambda c, s: 0.0), memory=[fact])
    before = fact.trust
    measured, up, down = agent.measure_memory(score, git + unrelated)

    assert measured == 1 and up == 1 and down == 0
    assert fact.trust > before, "재고도 신뢰가 안 움직이면 자가개선이 아니다"


def test_an_unrelated_fact_is_not_measured_as_useless(tmp_path):
    """못 잰 것과 도움이 안 된 것은 다르다. 관계된 케이스가 없으면 안 잰다."""
    cases = [BenchCase(case_id=f"o{i}",
                       payload={"tool": "TodoWrite", "error_detail": "JSON 파싱"})
             for i in range(8)]
    fact = MemoryItem(item_id="m1", scope="user", source_run_ids=[],
                      text="윈도우에서 git 은 줄바꿈 경고를 낸다")

    ledger = JsonlSkillLedger(str(tmp_path / "skills.jsonl"))
    agent = JermesAgent(ledger, ForgeGate(lambda c, s: 0.0), memory=[fact])
    measured, up, down = agent.measure_memory(lambda c, i: 0.0, cases)

    assert (measured, up, down) == (0, 0, 0)
    assert fact.trust == 0.5, "안 쟀는데 신뢰가 움직였다"


# --- 프로젝트가 갈린다 ---------------------------------------------------------

def test_a_project_fact_does_not_follow_you_elsewhere(home, monkeypatch):
    """실측: 모든 사실이 user 로 박히고 회상이 스코프를 안 넘겨서 한 번도 안 갈렸다."""
    a = home / "projA"
    b = home / "projB"
    a.mkdir()
    b.mkdir()

    monkeypatch.chdir(a)
    cli.main(["memory", "--add", "이 저장소의 기본 브랜치는 develop 이다"])
    cli.main(["memory", "--add", "나는 답을 한국어로 받는다", "--global"])

    here = cli._recall_for("브랜치 어디에 올려")
    assert any("develop" in i.text for i in here), "제 프로젝트에서 안 나온다"

    monkeypatch.chdir(b)
    there = cli._recall_for("브랜치 어디에 올려")
    assert not any("develop" in i.text for i in there), "남의 프로젝트로 샜다"


def test_a_fact_about_the_person_follows_everywhere(home, monkeypatch):
    """사람 자신에 대한 사실은 어느 프로젝트에서나 참이다."""
    a = home / "projA"
    b = home / "projB"
    a.mkdir()
    b.mkdir()

    monkeypatch.chdir(a)
    cli.main(["memory", "--add", "나는 답을 한국어로 받는다", "--global"])

    monkeypatch.chdir(b)
    got = recall(cli.load_memory(), limit=5, task="한국어로 답해",
                 scope=cli.current_scope())
    assert any("한국어" in i.text for i in got)


def test_the_scope_key_is_the_same_one_the_sessions_use():
    """다른 열쇠를 내면 손으로 넣은 사실과 배운 사실이 영영 안 만난다 - 둘 다
    있는데 서로 못 보는 상태가 제일 나쁘다."""
    from pathlib import Path

    from jermes.sources.claude_code import project_key

    assert project_key(r"C:\Users\wlstn") == "C--Users-wlstn"
    assert cli.current_scope() == f"project:{project_key(Path.cwd().resolve())}"


def test_a_learned_fact_belongs_to_the_session_project(tmp_path):
    """배운 사실이 전부 전역이면 "이 저장소는" 으로 시작하는 사실이 다 샌다."""
    from jermes.sources.claude_code import scope_of_session

    root = tmp_path / "projects"
    (root / "C--work-repo" / "subagents").mkdir(parents=True)
    session = root / "C--work-repo" / "subagents" / "s.jsonl"
    session.write_text("{}\n", encoding="utf-8")

    import os
    os.environ["JERMES_CLAUDE_PROJECTS"] = str(root)
    try:
        assert scope_of_session(session) == "project:C--work-repo"
    finally:
        os.environ.pop("JERMES_CLAUDE_PROJECTS", None)


def test_an_override_wins_when_one_repo_is_many_projects(monkeypatch):
    monkeypatch.setenv("JERMES_SCOPE", "project:내가-정한-것")
    assert cli.current_scope() == "project:내가-정한-것"

"""규약 - Hermes 의 SOUL.md 에 대응하되 문장이 아니라 집행이다.

Hermes 는 "배우지 말 것"을 스킬 프롬프트 안의 문장으로 두고, 페르소나 파일은
에이전트가 스스로 고친다 → 조용히 표류하고 그걸 알 방법이 없다. 여기서 고정하는 계약:
① 금지는 게이트가 막는다(모델의 선의에 기대지 않음) ② 에이전트는 자기 규약을 못 고친다
③ 표류는 diff 로 보인다.
"""

import pytest

from jermes.agent import JermesAgent
from jermes.constitution import Constitution
from jermes.gate import BenchCase, ForgeGate
from jermes.ledger import InMemorySkillLedger
from jermes.model import Provenance, RunTrace, SkillCandidate, TraceEvent


def candidate(**kw):
    base = dict(name="some-skill", kind="guide", scope="user", action="create",
                rationale="이유", when_to_use="언제", procedure=["a", "b"],
                verification=["v"], provenance=Provenance(origin="llm_drafter"))
    base.update(kw)
    return SkillCandidate(**base)


# ------------------------------------------------- ① 집행

def test_forbidden_content_is_rejected_by_the_constitution():
    law = Constitution()
    assert law.check_candidate(candidate(procedure=["api_key 를 로그에 남긴다"]))
    assert law.check_candidate(candidate(rationale="검증을 건너뛰고 바로 적용한다"))
    assert law.check_candidate(candidate(when_to_use="사람 승인 없이 배포할 때"))
    assert law.check_candidate(candidate()) is None


def test_the_gate_enforces_it_before_running_the_bench():
    """배우면 안 되는 것은 성능이 좋아도 안 된다 - 벤치를 돌리기 전에 막는다."""
    scored = []

    def score(case, skill):
        scored.append(case.case_id)
        return 0.9 if skill else 0.1

    gate = ForgeGate(score, constitution=Constitution())
    bad = candidate(procedure=["skip the verification gate"])
    from jermes.synthesis import synthesize
    result = gate.verify(bad, synthesize(bad), [BenchCase(case_id=f"c{i}") for i in range(8)])
    assert result.verdict == "rejected"
    assert "never_learn" in result.reasons[0]
    assert scored == []          # 점수를 아예 매기지 않았다


def test_a_gate_without_a_constitution_still_works():
    """규약은 선택 - 없다고 게이트가 깨지면 기존 호출부가 다 죽는다."""
    gate = ForgeGate(lambda case, skill: 0.5)
    from jermes.synthesis import synthesize
    good = candidate()
    assert gate.verify(good, synthesize(good), []).verdict == "staged"


def test_a_broken_rule_does_not_disable_enforcement():
    """규칙 하나가 잘못된 정규식이어도 나머지는 계속 집행돼야 한다."""
    law = Constitution(never_learn=[r"([unclosed", r"api[_-]?key"])
    assert law.check_candidate(candidate(procedure=["api_key 유출"]))


def test_agent_wires_the_constitution_into_the_gate_automatically():
    gate = ForgeGate(lambda case, skill: 0.9 if skill else 0.1)
    agent = JermesAgent(InMemorySkillLedger(), gate)
    assert gate.constitution is agent.constitution

    events = [TraceEvent(type="tool_call", name=f"s{i}") for i in range(6)]
    events.append(TraceEvent(type="error", name="s2", detail="404"))
    trace = RunTrace(run_id="r1", scope="user", events=events, success=True)
    report = agent.cycle(trace, bench_cases=[BenchCase(case_id=f"c{i}") for i in range(8)],
                         drafted=[candidate(name="leaky",
                                            procedure=["password 를 저장한다"])])
    assert report.rejected == ["leaky"] and report.promoted == []


# ------------------------------------------------- ② 에이전트는 못 고친다

def test_propose_reports_the_change_but_does_not_apply_it():
    law = Constitution()
    before = list(law.never_learn)
    lines = law.propose(never_learn=["아무거나"])
    assert lines and law.never_learn == before      # 그대로다


def test_adopt_requires_a_human_approver():
    with pytest.raises(ValueError):
        Constitution().adopt({"principles": ["새 원칙"]}, approved_by="")


def test_adopt_applies_bumps_version_and_records_who():
    law = Constitution()
    law.adopt({"principles": ["새 원칙"]}, approved_by="김진수")
    assert law.principles == ["새 원칙"]
    assert law.version != "1.0.0"
    assert "김진수" in law.history[0]


def test_version_and_history_cannot_be_proposed():
    """이력을 고칠 수 있으면 표류를 숨길 수 있다."""
    lines = Constitution().propose(version="9.9.9", history=["지웠음"])
    assert all(line.startswith("거부:") for line in lines)


# ------------------------------------------------- ③ 표류가 보인다

def test_diff_shows_what_moved():
    old = Constitution()
    new = Constitution(identity="Drifted", never_learn=["api[_-]?key"])
    lines = old.diff(new)
    assert any("identity" in line for line in lines)
    assert any("never_learn 삭제" in line for line in lines)


def test_markdown_roundtrip_keeps_the_rules():
    law = Constitution()
    law.adopt({"principles": ["증거 없이 지우지 않는다"]}, approved_by="김진수")
    restored = Constitution.from_markdown(law.to_markdown())
    assert restored.identity == law.identity
    assert restored.never_learn == law.never_learn
    assert restored.principles == law.principles
    assert restored.version == law.version
    assert restored.diff(law) == []


def test_markdown_is_frontmatter_shaped_so_other_tools_can_read_it():
    text = Constitution().to_markdown()
    assert text.startswith("---\n") and text.count("---") >= 2
    assert "## Never learn" in text


def test_scope_approval_policy_is_declared_not_scattered():
    law = Constitution()
    assert law.needs_human_approval("org") and not law.needs_human_approval("user")


# --- 자격증명은 규약보다 아래 규칙으로 막는다 --------------------------------
# 전수조사에서 나왔다. `never_learn` 은 사람이 고치는 **낱말** 목록이라 모양이
# 비밀인 것을 못 잡았고, 같은 판단을 `curator._SECRET_PATTERNS` 가 따로 하고
# 있어서 **스킬은 두 겹, 기억은 한 겹**이었다. 기억이 약한 쪽만 거쳤다는 뜻이다.

# 토큰 **모양**을 시험하려면 그 모양이 필요한데, 파일에 그대로 적으면 공개본
# 금칙어 검사가 진짜와 구분할 수 없어 막는다(실제로 막혔고, 그게 맞는 동작이다).
# 그래서 조각을 붙여 만든다 - 시험은 진짜 모양을 보고, 파일에는 그 모양이 없다.
_FAKE_GH = "ghp_" + "z" * 32
_FAKE_SK = "sk-" + "z" * 26
_FAKE_AWS = "AKIA" + "Z" * 16

SECRETS = {
    "영어 대입": "the admin credential=hunter2xyz",
    "한글 암호": "관리자 암호는 hunter2 이다",
    "비밀번호": "로그인 비밀번호 abcd1234",
    "JWT": "Bearer eyJhbGciOiJIUzI1NiJ9.abcdefghij.klmnop 로 인증",
    "AWS 키": _FAKE_AWS + " 를 쓴다",
    "GitHub 토큰": _FAKE_GH,
    "OpenAI 키": _FAKE_SK,
    "개인키": "-----BEGIN RSA PRIVATE KEY-----",
    "주민등록번호": "주민등록번호 900101-1234567",
    "카드번호": "카드 1234-5678-9012-3456",
}

INNOCENT = {
    "절차": "배포 전에는 스테이징에서 먼저 돌린다",
    "도구 사용법": "Edit 도구는 파일을 먼저 읽어야 한다",
    "비용 조언": "프롬프트를 짧게 쓰면 비용이 준다",
    "버전과 포트": "버전은 1.2.3 이고 포트는 8000-8003 을 쓴다",
    "날짜와 커밋": "날짜는 2026-08-11 이고 커밋은 4f8748b 이다",
    "암호화 이야기": "암호화 알고리즘은 AES 를 쓴다",
}


def test_credential_shapes_are_blocked():
    from jermes.constitution import Constitution

    law = Constitution()
    leaked = [name for name, text in SECRETS.items() if not law.check_text(text)]
    assert not leaked, f"자격증명이 통과했다: {leaked}"


def test_ordinary_facts_are_not_blocked():
    """다 막으면 배울 게 없다. 낱말을 논하는 문장까지 막으면 안 된다."""
    from jermes.constitution import Constitution

    law = Constitution()
    blocked = [name for name, text in INNOCENT.items() if law.check_text(text)]
    assert not blocked, f"멀쩡한 사실이 막혔다: {blocked}"


def test_the_floor_survives_a_rewritten_constitution():
    """규약은 사람이 고치는 것이지만 자격증명을 안 배우는 것은 정할 문제가 아니다."""
    from jermes.constitution import Constitution

    law = Constitution(never_learn=[])          # 사용자가 목록을 통째로 비웠다
    assert law.check_text(_FAKE_AWS + " 를 쓴다"), "바닥이 꺼졌다"


def test_skills_and_memory_share_one_secret_detector():
    """두 벌이면 한쪽만 고쳐지고 그 한쪽으로 샌다."""
    from jermes import curator
    from jermes.constitution import secret_shape

    assert not hasattr(curator, "_SECRET_PATTERNS"), "옛 목록이 남아 있다"
    assert curator.secret_shape is secret_shape


@pytest.fixture
def jermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("JERMES_HOME", str(tmp_path))
    return tmp_path


def test_narrowing_never_learn_says_what_it_turns_off(jermes_home, capsys):
    """`--adopt` 는 목록을 갈아치운다. 규칙 하나를 더하려고 `never_learn` 에 한 줄을
    주면 기본으로 있던 비밀 보호(비밀번호·API 키·토큰 정규식)가 통째로 사라진다.
    바뀐 값을 나란히 찍기는 했지만 그건 "적용" 으로 읽히지 "방금 보호를 껐다" 로
    읽히지 않는다. 막지는 않는다 - 규약의 주인은 사람이다."""
    from jermes import cli

    assert cli.main(["law", "--adopt", '{"never_learn": ["고객사 실명"]}',
                     "--by", "김진수"]) == 0
    out = capsys.readouterr().out
    assert "never_learn 에서" in out and "걸러지지 않습니다" in out, out
    assert "api" in out.lower(), out          # 무엇이 빠지는지 이름이 나와야 한다


def test_approval_governance_is_enforced_not_merely_declared(jermes_home):
    """규약에 `approval_required_scopes` 가 있고 `needs_human_approval()` 도
    있었는데 **부르는 곳이 없었다.** 정의만 있고 집행이 없으면 규약이 아니라
    장식이다. 게다가 앞자리로 견주지 않아서, 실제 스코프가 `project:d--` 처럼
    열쇠를 달고 오면 규약에 뭘 적어도 절대 안 걸렸다."""
    from jermes.constitution import Constitution

    law = Constitution()
    assert not law.needs_human_approval("user")
    assert not law.needs_human_approval("project:d--")   # 내 것은 내가 책임진다
    assert law.needs_human_approval("platform")          # 여럿이 쓰는 자리
    assert law.needs_human_approval("org:acme")       # 열쇠가 붙어도 걸린다


def test_a_shared_scope_skill_waits_for_a_person(jermes_home, capsys):
    """게이트를 통과한 것과 남들이 쓰는 자리에 올려도 되는 것은 다르다 -
    전자는 기계가 정하고 후자는 사람이 정한다."""
    from jermes import cli
    from jermes.model import SkillDef

    cli.open_ledger().commit(SkillDef(name="shared-thing", kind="guide",
                                      scope="platform", description="여럿이 쓰는 것",
                                      body="## Procedure\n- 하나\n"))
    assert cli.main(["approve", "shared-thing"]) == 1
    assert "--by" in capsys.readouterr().out          # 에이전트가 스스로 못 올린다

    assert cli.main(["approve", "shared-thing", "--by", "김진수"]) == 0
    out = capsys.readouterr().out
    assert "올렸습니다" in out and "승인은 검증이 아닙니다" in out, out
    assert cli.open_ledger().get("shared-thing").status == "active"

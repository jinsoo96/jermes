"""홀드아웃 한 건으로 `검증됨` 이 붙지 않는다.

실측한 결함: `min_cases=4` 면 dev 3 · holdout 1 이다. 운 좋게 그 한 건만 맞은
스킬이 `promoted` 를 받았고, 같은 스킬이 케이스 8건에서는 `rejected` 였다.
`jermes tool --cases`(4줄 csv)로도 그대로 재현됐다.

게이트 자신의 주석이 "홀드아웃이 1개뿐이라 잡음 하나가 판정을 정했다"를 문제로
적어 두고 있는데, 최소 설정이 정확히 그 상황을 만들고 있었다.

`min_cases` 를 올리지 않는다 - 그러면 케이스가 적은 사람은 **재보지도** 못한다.
승격에만 문턱을 둔다. `staged` 는 이미 "못 쟀다"는 뜻이고, 홀드아웃 한 건은
재긴 쟀지만 근거가 얇은 것이다. 얇은 근거로 `검증됨` 을 붙이지 않는 것이 이
물건의 전부다.
"""

import pytest

from jermes.gate import BenchCase, ForgeGate, GateConfig, split_holdout
from jermes.model import Provenance, SkillCandidate, SkillDef

PROV = Provenance(origin="t", source_run_ids=["r"])
SKILL = SkillDef(name="a-skill", kind="guide", scope="user", description="d",
                 body="body", provenance=PROV)
CAND = SkillCandidate(name="a-skill", kind="guide", scope="user", action="create",
                      rationale="r", procedure=["p"], provenance=PROV)


def _cases(n):
    return [BenchCase(case_id=f"c{i}") for i in range(n)]


def _always_helps(_case, skill):
    return 1.0 if skill is not None else 0.0


@pytest.mark.parametrize("n,expected", [(4, "staged"), (8, "promoted"),
                                        (12, "promoted")])
def test_a_thin_holdout_stages_instead_of_promoting(n, expected):
    """케이스 4건이면 홀드아웃이 1건이다. 완벽한 스킬이어도 그걸로 `검증됨` 을
    붙이지 않는다 - 한 건이 맞았다는 것과 그 스킬이 돕는다는 것은 다르다."""
    assert ForgeGate(_always_helps).verify(
        CAND, SKILL, _cases(n)).verdict == expected


def test_the_reason_says_what_to_do():
    """왜 안 붙었는지 사람이 짐작할 수 없다. 무엇을 하면 되는지까지 말한다."""
    result = ForgeGate(_always_helps).verify(CAND, SKILL, _cases(4))
    joined = " ".join(result.reasons)
    assert "홀드아웃이 1건" in joined and "8건 이상" in joined


def test_one_lucky_holdout_case_no_longer_promotes():
    """원래 재현: dev 는 다 오르고 홀드아웃은 운 좋게 한 건만 맞은 스킬."""
    cases = _cases(4)
    dev, hold = split_holdout(cases)
    assert len(hold) == 1, "이 시험의 전제"
    lucky, dev_ids = hold[0].case_id, {c.case_id for c in dev}

    def scores(case, skill):
        if skill is None:
            return 0.0
        return 1.0 if (case.case_id in dev_ids or case.case_id == lucky) else 0.0

    assert ForgeGate(scores).verify(CAND, SKILL, cases).verdict == "staged"


def test_the_threshold_is_one_number_in_one_place():
    """스킬과 툴이 서로 다른 문턱을 쓰면 `검증됨` 의 뜻이 두 개가 된다."""
    assert GateConfig().min_holdout_to_promote == 2


# --- 툴 경로도 같은 규율 -------------------------------------------------------

def test_a_tool_with_one_holdout_case_is_not_verified(tmp_path, monkeypatch):
    """재는 방식이 달라도 그 세 낱말의 뜻은 두 경로에서 같아야 한다."""
    monkeypatch.setenv("JERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JERMES_SOURCES", "own")
    from jermes.tools import ToolCase, verify_tool

    script = "def run(payload):\n    return payload['a'] + payload['b']\n"
    four = [ToolCase(case_id=f"c{i}", payload={"a": i, "b": 1}, expect=i + 1)
            for i in range(4)]
    eight = [ToolCase(case_id=f"c{i}", payload={"a": i, "b": 1}, expect=i + 1)
             for i in range(8)]

    assert verify_tool(script, four).verdict == "staged"
    assert verify_tool(script, eight).verdict == "promoted"


def test_a_forbid_only_case_waits_its_turn():
    """금지 조건만 있는 케이스는 스킬이 무엇을 내놓든 만점이다 - 답변에 오류
    문구를 그대로 옮겨 적는 일은 없으니까. 가릴 수 없는데 자리는 차지해서 평균을
    눅인다(실측: 관계된 12건 중 5건이 그랬고 진짜 이득이 12로 나뉘었다). 버리지는
    않는다 - 스킬이 실패를 되풀이하게 만드는지는 그 케이스만 잡아낸다."""
    from jermes.bench import Expectation, ReplayCase
    from jermes.gate import asks_for_something

    asks = ReplayCase(case_id="a", payload={"tool": "Bash"},
                      expect=Expectation(require=["PYTHONUTF8"],
                                         forbid=["Exit code 1"])).as_bench_case()
    guard = ReplayCase(case_id="b", payload={"tool": "Bash"},
                       expect=Expectation(forbid=["Exit code 1"])).as_bench_case()
    assert asks_for_something(asks)
    assert not asks_for_something(guard)


def test_a_budget_that_ran_out_is_not_a_verdict():
    """예산이 떨어지면 재생이 전부 빈 답이 되고, 그러면 스킬을 넣으나 빼나 같은
    점수라 `+0.000` 이 나온다. 그걸 "도움이 안 된다"로 내면 **한 번도 재 보지 않고
    확신에 찬 판정**을 내는 것이고, 이 게이트가 막으려는 것이 정확히 그것이다.

    실측: 큰 세션 하나에서 LLM 호출 5회로 후보 5건을 전부 그렇게 거절했다.
    """
    from jermes.bench import Expectation, ReplayCase, ReproReplayRunner
    from jermes.gate import ForgeGate, GateConfig
    from jermes.model import SkillCandidate, SkillDef, Unmeasurable

    def dry(payload, skill):
        raise Unmeasurable("시간 상한에 닿았습니다")

    cases = [ReplayCase(f"c{i}", {"tool": "Bash", "error_detail": "cp949 인코딩 실패"},
                        Expectation(require=["PYTHONUTF8"], forbid=["cp949"]))
             for i in range(8)]
    gate = ForgeGate(ReproReplayRunner(dry, cases), GateConfig())
    verdict = gate.verify(
        SkillCandidate(name="enc", kind="guide", scope="user", action="create",
                       rationale="r", when_to_use="cp949 인코딩 실패"),
        SkillDef(name="enc", kind="guide", scope="user",
                 description="cp949 인코딩 실패", body="PYTHONUTF8=1"),
        [c.as_bench_case() for c in cases])
    assert verdict.verdict == "staged", verdict.reasons
    assert "못 쟀" in verdict.reasons[0]


def test_one_failed_call_is_still_counted_conservatively():
    """한 번의 호출 실패는 다르다. 그건 그 케이스가 0점인 것으로 보수적으로 세면
    된다 - 전부가 실패하는 사정과 섞으면 흔한 잡음에 판정이 멈춘다."""
    from jermes.bench import Expectation, ReplayCase, ReproReplayRunner

    case = ReplayCase("c", {"tool": "Bash"}, Expectation(require=["x"]))
    runner = ReproReplayRunner(lambda p, s: (_ for _ in ()).throw(TimeoutError()),
                               [case])
    assert runner.score(case.as_bench_case(), None) == 0.0


def test_a_difference_smaller_than_one_case_says_so():
    """dev 가 9건이면 한 케이스가 0.111 을 움직인다. 그보다 작은 `+0.056` 을 이득
    이라고만 적으면 숫자가 있으니 잰 것처럼 보이는데, 실은 어느 케이스가 뽑혔느냐만
    말한다. 실측: 같은 스킬을 12건으로 재면 +0.056, 28건으로 재면 -0.016 이었다."""
    from jermes.bench import Expectation, ReplayCase, ReproReplayRunner
    from jermes.gate import ForgeGate, GateConfig
    from jermes.model import SkillCandidate, SkillDef

    cases = [ReplayCase(f"c{i}", {"tool": "Bash", "error_detail": "cp949 인코딩 실패"},
                        Expectation(require=["utf-8"], forbid=["cp949"]))
             for i in range(12)]
    hits = {"c0"}          # 열두 건 중 한 건만 달라진다 - 한 케이스어치 미만

    def replay(payload, skill):
        if skill is None:
            return "다시 해본다"
        return "utf-8 로 다시 한다" if payload.get("case") in hits else "다시 해본다"

    for case in cases:
        case.payload["case"] = case.case_id
    gate = ForgeGate(ReproReplayRunner(replay, cases), GateConfig())
    got = gate.verify(
        SkillCandidate(name="enc", kind="guide", scope="user", action="create",
                       rationale="r", when_to_use="cp949 인코딩 실패"),
        SkillDef(name="enc", kind="guide", scope="user",
                 description="cp949 인코딩 실패", body="utf-8"),
        [c.as_bench_case() for c in cases])
    assert any("자가 거칩니다" in r for r in got.reasons), got.reasons


# --- 부호검정: 평균이 못 하는 말을 한다 ---------------------------------

def test_sign_test_calls_one_flip_out_of_nine_a_coin_toss():
    """평균 차이는 "얼마나" 는 말해도 "우연인가" 는 못 말한다. 실측: 같은 스킬이
    케이스 12건에서 `+0.056`, 28건에서 `-0.016` 이었다 - 부호조차 안 정해진
    상태를 이득이라고 적고 있었다."""
    from jermes.gate import sign_test

    assert sign_test([(0, 1)] + [(0, 0)] * 8) == (1, 0, 0.5)
    better, worse, p = sign_test([(0, 1)] * 6)
    assert (better, worse) == (6, 0) and p < 0.02
    # 비긴 것은 버린다 - 그게 부호검정의 요점이다
    assert sign_test([(1, 1)] * 6) == (0, 0, 1.0)


def test_a_coin_flip_on_the_holdout_is_not_a_promotion():
    """**평균은 크기에 휘둘린다.** 한 건이 크게 좋아지고 다른 한 건이 조금
    나빠지면 평균은 양수인데, 갈린 방향으로 보면 1↑/1↓ 즉 동전 던지기다.

    실측(고치기 전): `holdout 0.500->0.600 (+0.100)` 으로 **promoted** 가
    나왔다. 부호검정 p=0.750 - 동전보다 못한 근거로 `검증됨` 딱지가 붙고
    남의 에이전트에 MCP 로 나갈 뻔했다.
    """
    from jermes.gate import BenchCase, ForgeGate, GateConfig, split_holdout
    from jermes.model import SkillCandidate, SkillDef

    cases = [BenchCase(case_id=f"c{i}",
                       payload={"about": "인코딩", "tool": "Bash",
                               "error_detail": "인코딩 실패"}) for i in range(12)]
    dev, hold = split_holdout(cases, GateConfig().holdout_ratio)
    d = [c.case_id for c in dev]
    h = [c.case_id for c in hold]
    # 홀드아웃: 한 건 0.5->1.0(+0.5) · 한 건 0.5->0.3(-0.2) => 평균 +0.1, 갈림 1↑/1↓
    table = {d[0]: 1.0, d[1]: 1.0, d[2]: 1.0, h[0]: 1.0, h[1]: 0.3}

    def score(case, skill):
        return 0.5 if skill is None else table.get(case.case_id, 0.5)

    got = ForgeGate(score, GateConfig()).verify(
        SkillCandidate(name="enc-skill", kind="guide", scope="user",
                       action="create", rationale="r", when_to_use="인코딩"),
        SkillDef(name="enc-skill", kind="guide", scope="user",
                 description="인코딩", body="b"),
        cases)
    assert got.verdict == "staged", got.reasons
    assert any("좋아진 건수가 나빠진 건수를 못 넘었습니다" in r for r in got.reasons)


def test_a_real_improvement_still_promotes():
    """문턱은 **가장 약한 형태**(다수결)로만 걸었다 - 진짜 개선이라면 당연히
    넘는다. 오늘 이미 한 번 배웠다: 케이스가 적을 때 엄격한 문턱은 진짜로
    작은 개선까지 같이 죽인다."""
    from jermes.gate import BenchCase, ForgeGate, GateConfig, split_holdout
    from jermes.model import SkillCandidate, SkillDef

    cases = [BenchCase(case_id=f"c{i}",
                       payload={"about": "인코딩", "tool": "Bash",
                               "error_detail": "인코딩 실패"}) for i in range(12)]
    dev, hold = split_holdout(cases, GateConfig().holdout_ratio)
    improves = {c.case_id for c in dev[:5]} | {c.case_id for c in hold}

    def score(case, skill):
        if skill is None:
            return 0.5
        return 1.0 if case.case_id in improves else 0.5

    got = ForgeGate(score, GateConfig()).verify(
        SkillCandidate(name="enc-skill", kind="guide", scope="user",
                       action="create", rationale="r", when_to_use="인코딩"),
        SkillDef(name="enc-skill", kind="guide", scope="user",
                 description="인코딩", body="b"),
        cases)
    assert got.verdict == "promoted", got.reasons

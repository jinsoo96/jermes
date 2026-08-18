"""증거로 등급을 매기는 기억.

흔한 방식은 조회·편집 같은 **대리 신호**로 trust 를 움직이는 것이다. 그러면
"보였는데 안 썼다"를 음의 신호로 쓸 수 없다 - 나쁜 질의를 애먼 노트 탓으로
돌리게 되니까. 우리는 대리 신호를 아예 안 쓴다. 스킬을 검증하는 재현벤치로
**기억 항목의 기여를 직접 잰다**. 그래서 음의 신호도 안전하게 쓸 수 있다.

이 파일이 고정하는 계약:
① trust 는 측정으로만 움직인다 ② 모순은 드러내는 데서 끝내지 않고 증거로 판정한다
③ 자동 삭제는 없다 ④ 확신은 중립으로 감쇠한다(경화 방지) ⑤ disputed 는 회상 금지.
"""

import pytest

from jermes.gate import BenchCase
from jermes.memory import (
    NEUTRAL,
    MemoryItem,
    MemoryPolicy,
    apply_measurement,
    decay_unmeasured,
    detect_contradictions,
    measure,
    recall,
    resolve,
)


def cases(n=6):
    return [BenchCase(case_id=f"c{i}") for i in range(n)]


def item(item_id="m1", text="배포는 develop 브랜치에서 한다", **kw):
    return MemoryItem(item_id=item_id, text=text, **kw)


def scorer(helpful_ids=(), harmful_ids=(), base=0.5, delta=0.3):
    """항목이 있을 때 점수가 오르거나 내리는 러너 - 게이트와 같은 계약."""
    def score(case, memory_item):
        if memory_item is None:
            return base
        if memory_item.item_id in helpful_ids:
            return base + delta
        if memory_item.item_id in harmful_ids:
            return base - delta
        return base
    return score


# ------------------------------------------------- ① 측정만이 trust 를 움직인다

def test_helpful_item_earns_trust_by_measurement():
    memory = item()
    result = measure(memory, scorer(helpful_ids={"m1"}), cases())
    apply_measurement(memory, result)
    assert result.gain == pytest.approx(0.3)
    assert memory.trust > NEUTRAL
    assert memory.measured and memory.evidence["measurements"][0]["verdict"] == "helpful"


def test_harmful_item_loses_trust():
    memory = item()
    apply_measurement(memory, measure(memory, scorer(harmful_ids={"m1"}), cases()))
    assert memory.trust < NEUTRAL


def test_no_difference_does_not_move_trust():
    """'차이가 없다'는 '나쁘다'가 아니다 - 움직이지 않는 것이 정직하다."""
    memory = item()
    apply_measurement(memory, measure(memory, scorer(), cases()))
    assert memory.trust == pytest.approx(NEUTRAL)
    assert memory.evidence["measurements"][0]["verdict"] == "neutral"


def test_too_few_cases_refuses_to_measure_instead_of_guessing():
    """스킬 게이트가 케이스 부족을 staged 로 두는 것과 같은 규율."""
    assert measure(item(), scorer(helpful_ids={"m1"}), cases(2)) is None


def test_unmeasured_item_stays_neutral_and_says_so():
    memory = item()
    assert memory.trust == NEUTRAL and not memory.measured


def test_one_measurement_does_not_max_out_trust():
    """한 방에 확신하지 않는다 - 단계로 움직인다."""
    memory = item()
    apply_measurement(memory, measure(memory, scorer(helpful_ids={"m1"}), cases()))
    assert memory.trust == pytest.approx(NEUTRAL + MemoryPolicy().step)
    assert memory.trust < 1.0


# ------------------------------------------------- ④ 경화 방지

def test_confidence_decays_toward_neutral_not_zero():
    """안 재봤다는 것이 나쁘다는 뜻은 아니다 - 0 이 아니라 중립으로 흐른다."""
    high, low = item("m1", trust=0.95), item("m2", trust=0.05)
    for _ in range(3):
        decay_unmeasured([high, low])
    assert NEUTRAL < high.trust < 0.95
    assert 0.05 < low.trust < NEUTRAL


def test_decay_never_overshoots_neutral():
    memory = item(trust=NEUTRAL + 0.01)
    decay_unmeasured([memory])
    assert memory.trust == pytest.approx(NEUTRAL)


def test_retired_items_are_left_alone_by_decay():
    memory = item(trust=0.9, status="retired")
    decay_unmeasured([memory])
    assert memory.trust == pytest.approx(0.9)


# ------------------------------------------------- ② 모순 감지와 증거 판정

def test_negation_flip_is_detected():
    pair = [item("m1", "배포는 develop 브랜치에서 한다"),
            item("m2", "배포는 develop 브랜치에서 하지 않는다")]
    found = detect_contradictions(pair)
    assert len(found) == 1 and found[0].kind == "negation_flip"


def test_numeric_conflict_is_detected():
    pair = [item("m1", "judge threshold is 0.8"),
            item("m2", "judge threshold is 0.6")]
    found = detect_contradictions(pair)
    assert len(found) == 1 and found[0].kind == "numeric_conflict"


def test_unrelated_items_are_not_flagged():
    pair = [item("m1", "배포는 develop 브랜치에서 한다"),
            item("m2", "점심은 김치찌개가 맛있다")]
    assert detect_contradictions(pair) == []


def test_different_scopes_are_not_compared():
    """다른 스코프의 사실은 서로 모순이 아니다 - 각자의 세계다."""
    pair = [item("m1", "threshold is 0.8", scope="user"),
            item("m2", "threshold is 0.6", scope="project")]
    assert detect_contradictions(pair) == []


def test_contradiction_is_decided_by_the_bench_not_by_a_vote():
    left, right = item("m1", "threshold is 0.8"), item("m2", "threshold is 0.6")
    found = detect_contradictions([left, right])[0]
    outcome = resolve(found, left, right, scorer(helpful_ids={"m1"}), cases())
    assert outcome.decided and outcome.winner == "m1" and outcome.loser == "m2"
    assert left.status == "active"


def test_the_loser_is_disputed_never_deleted():
    """자동 삭제 금지 - 우리가 틀렸을 때 되돌릴 수 있어야 한다."""
    left, right = item("m1", "threshold is 0.8"), item("m2", "threshold is 0.6")
    found = detect_contradictions([left, right])[0]
    resolve(found, left, right, scorer(helpful_ids={"m1"}), cases())
    assert right.status == "disputed"          # retired 가 아니다
    assert any("disputed" in line for line in right.history)


def test_indistinguishable_pair_goes_to_a_human():
    """변별이 안 되면 판정하지 않는다 - 억지 판정이 조용한 오답을 만든다."""
    left, right = item("m1", "threshold is 0.8"), item("m2", "threshold is 0.6")
    found = detect_contradictions([left, right])[0]
    outcome = resolve(found, left, right, scorer(), cases())
    assert not outcome.decided
    assert left.status == right.status == "disputed"
    assert "사람" in outcome.reason


def test_resolution_without_enough_cases_does_not_pretend():
    left, right = item("m1", "threshold is 0.8"), item("m2", "threshold is 0.6")
    found = detect_contradictions([left, right])[0]
    outcome = resolve(found, left, right, scorer(helpful_ids={"m1"}), cases(2))
    assert not outcome.decided and left.status == right.status == "disputed"


# ------------------------------------------------- ⑤ 회상 규율

def test_disputed_items_are_never_recalled():
    """모순이 풀리기 전에 주입하면 에이전트가 반대되는 두 사실을 동시에 믿는다."""
    items = [item("m1", trust=0.9), item("m2", trust=0.95, status="disputed")]
    assert [i.item_id for i in recall(items)] == ["m1"]


def test_measured_harmful_items_drop_below_the_threshold():
    memory = item()
    apply_measurement(memory, measure(memory, scorer(harmful_ids={"m1"}), cases()))
    assert recall([memory]) == []


def test_recall_prefers_higher_trust_and_is_bounded():
    items = [item(f"m{i}", trust=0.5 + i / 100) for i in range(8)]
    picked = recall(items, limit=3)
    assert len(picked) == 3
    assert [i.item_id for i in picked] == ["m7", "m6", "m5"]


def test_unmeasured_items_are_still_usable():
    """측정 전이라고 배제하면 새 기억이 영원히 못 쓰인다 - 중립은 통과다."""
    assert recall([item("m1")]) != []


# ------------------------------------------------- 검수에서 나온 결함(회귀 방지)

def test_noise_does_not_move_trust_in_either_direction():
    """검수에서 잡힌 편향: +0.001 은 helpful 로 세어 trust 를 올리는데 -0.001 은
    중립이었다. 잡음 구간이 비대칭이면 신뢰가 한쪽으로만 흐른다."""
    for delta in (0.001, -0.001):
        memory = item()
        apply_measurement(memory, measure(
            memory, lambda case, i: 0.5 + (delta if i else 0), cases()))
        assert memory.trust == pytest.approx(NEUTRAL), f"delta={delta}"
        assert memory.evidence["measurements"][0]["verdict"] == "neutral"


def test_real_gain_still_moves_trust():
    """잡음을 막느라 진짜 신호까지 막으면 안 된다."""
    memory = item()
    apply_measurement(memory, measure(memory, scorer(helpful_ids={"m1"}), cases()))
    assert memory.trust > NEUTRAL


def test_same_number_written_differently_is_not_a_contradiction():
    """검수에서 확인: '0.8' 과 '0.80' 이 문자열 비교로 충돌 판정됐다."""
    pair = [item("m1", "threshold is 0.8"), item("m2", "threshold is 0.80")]
    assert detect_contradictions(pair) == []


def test_genuinely_different_numbers_are_still_caught():
    pair = [item("m1", "threshold is 0.8"), item("m2", "threshold is 0.6")]
    assert len(detect_contradictions(pair)) == 1


def test_undecided_pair_records_why_on_both_sides():
    """보류만 하고 이유를 안 남기면 사람이 되돌릴 근거가 없다(오탐일 수 있다)."""
    left, right = item("m1", "threshold is 0.8"), item("m2", "threshold is 0.6")
    found = detect_contradictions([left, right])[0]
    resolve(found, left, right, scorer(), cases())
    assert any("상대=m2" in line and "변별 못 함" in line for line in left.history)
    assert any("상대=m1" in line for line in right.history)


def test_recall_does_not_spend_slots_on_the_same_fact_twice():
    """회상은 서너 칸뿐인데 같은 말을 다르게 적은 것들이 그 칸을 먹는다.
    실측(활성 기억 81건): "Edit 도구로 파일에 쓰기 전에 먼저 읽기" 계열 세 건이
    네 칸 중 세 칸을 차지해, 정작 다른 것을 말하는 사실은 못 들어왔다."""
    from jermes.memory import MemoryItem, recall

    items = [
        MemoryItem(item_id="a", text="Edit 도구로 파일에 쓰기 전에 반드시 먼저 읽어야 한다"),
        MemoryItem(item_id="b", text="Edit 도구는 파일을 수정하기 전에 반드시 먼저 읽어야 한다"),
        MemoryItem(item_id="c", text="Edit 도구는 replace_all 이 false 면 여러 개 일치할 때 실패한다"),
    ]
    got = recall(items, limit=2, task="Edit 도구로 파일을 고치기 전에 무엇을 해야 하나")
    texts = [i.text for i in got]
    assert len(texts) == 2, texts
    assert not (texts[0].startswith("Edit 도구로") and texts[1].startswith("Edit 도구는 파일")), \
        f"거의 같은 사실 둘이 두 칸을 다 먹었다: {texts}"


def test_a_measured_but_uninformative_fact_says_so():
    """재고서 `neutral` 만 나온 사실은 신뢰가 중립 그대로 남는다. 그런데 화면에는
    `신뢰 0.50` 으로 떠서 근거가 있어서 0.50 인 것처럼 보인다. 실측: 측정 기록
    78건 중 67건이 gain 정확히 0.000 이었다."""
    from jermes.memory import MemoryItem

    item = MemoryItem(item_id="a", text="사실")
    assert not item.told_us_nothing          # 안 재봤다
    item.evidence["measurements"] = [{"cases": 5, "gain": 0.0, "verdict": "neutral"}]
    assert item.told_us_nothing              # 재봤는데 못 갈랐다
    item.evidence["measurements"].append({"cases": 5, "gain": 0.2, "verdict": "helpful"})
    assert not item.told_us_nothing          # 한 번이라도 갈랐으면 아니다


def test_a_dead_budget_does_not_become_a_neutral_measurement():
    """예산이 끊기면 넣으나 빼나 0 점이라 이득이 정확히 `+0.000` 이 되고, 그건
    `neutral` 이라 신뢰가 안 움직인다 - 못 쟀다는 사실이 "재봤는데 차이 없음" 으로
    둔갑한다. 스킬 쪽은 이미 이렇게 고쳤는데 기억 쪽은 그대로였다.

    실측: 측정 기록 78건 중 67건이 gain 정확히 0.000 이었다.
    """
    from jermes.agent import JermesAgent
    from jermes.gate import BenchCase
    from jermes.memory import MemoryItem
    from jermes.model import Unmeasurable

    from jermes.gate import ForgeGate, GateConfig
    from jermes.ledger import InMemorySkillLedger

    agent = JermesAgent(ledger=InMemorySkillLedger(),
                        gate=ForgeGate(lambda case, skill: 0.0, GateConfig()))
    agent.memory = [MemoryItem(item_id="a", text="cp949 대신 utf-8 로 읽는다")]
    cases = [BenchCase(case_id=f"c{i}",
                       payload={"tool": "Bash", "error_detail": "cp949 실패"})
             for i in range(6)]

    def dead(case, item):
        raise Unmeasurable("시간 상한 189초에 닿았습니다")

    measured, up, down = agent.measure_memory(dead, cases)
    assert (measured, up, down) == (0, 0, 0)
    # 조용히 0 이 아니라, **왜** 0 인지가 남아야 한다
    assert "189초" in agent.memory_measure_stopped, agent.memory_measure_stopped


def test_the_model_sees_the_same_label_the_person_sees():
    """사람 화면만 고치고 모델 프롬프트를 안 고치면, 사람은 "못가름" 을 보는데
    모델은 "신뢰 0.50" 을 보고 둘이 다른 것을 근거로 판단한다. 딱지는 한 자리에서
    나와야 한다."""
    from jermes.agent import ContextPack, _memory_status
    from jermes.memory import MemoryItem

    blind = MemoryItem(item_id="a", text="쟀지만 못 가른 사실")
    blind.evidence["measurements"] = [{"cases": 5, "gain": 0.0, "verdict": "neutral"}]

    from jermes.cli import _known_block, _memory_mark
    assert "못가름" in _memory_mark(blind)
    assert "못가름" in _known_block([blind])           # 모델이 보는 쪽
    assert "못 가름" in _memory_status(blind)          # XML 컨텍스트 쪽

    pack = ContextPack(skills=[], memory=[blind])
    assert "측정했으나 못 가름" in pack.render(), pack.render()


def test_saving_can_actually_remove_when_the_caller_says_so(tmp_path, monkeypatch):
    """동시 쓰기를 막으려고 저장할 때 파일을 다시 읽어 병합한다. 그런데 그러면
    **저장이 지우지를 못한다** - 실측으로, 하나를 빼고 저장했더니 병합이 그대로
    되살렸다. `known` 을 준 쪽은 "내가 이만큼을 보고 시작했다" 고 말한 것이고,
    그중 안 넘긴 것은 일부러 뺀 것이다."""
    monkeypatch.setenv("JERMES_HOME", str(tmp_path))
    from jermes import cli
    from jermes.memory import MemoryItem

    cli.save_memory([MemoryItem(item_id="a", text="첫째"),
                     MemoryItem(item_id="b", text="둘째")])
    before = cli.load_memory()
    cli.save_memory([i for i in before if i.item_id != "b"],
                    known={i.item_id for i in before})
    assert [i.item_id for i in cli.load_memory()] == ["a"]

    # `known` 없이 저장하면 못 본 것을 지킨다 - 다른 프로세스가 쓴 것을 잃지 않는다
    cli.save_memory([MemoryItem(item_id="c", text="셋째")])
    assert sorted(i.item_id for i in cli.load_memory()) == ["a", "c"]


def test_a_wrong_fact_is_not_filtered_out_as_irrelevant():
    """**자가검증이 상만 주고 벌은 못 주면 자가검증이 아니다.**

    관계도는 주제의 낱말 수로 나눈다. 스킬은 주제가 이름+설명이라 짧아서 잘 맞는데
    기억은 주제가 **문장 하나**다. 그리고 그 편향은 한쪽으로만 작동한다(실측):

        "cp949 오류가 나면 PYTHONUTF8=1 을 붙인다"   0.077  통과
        "cp949 오류가 나면 그냥 무시한다"            0.034  걸러짐

    맞는 사실은 고칠 낱말을 품고 있어서 통과하고, 틀린 사실은 그 낱말이 없어서
    "관련 없음" 으로 걸러진다. 그러면 틀린 사실은 영영 재보지도 못한다.
    """
    from jermes.gate import BenchCase, relevant_cases

    cases = [BenchCase(case_id=f"c{i}", payload={
        "tool": "Bash",
        "error_detail": "UnicodeDecodeError: 'cp949' codec can't decode byte",
        "about": "PYTHONUTF8"}) for i in range(6)]
    wrong = "cp949 오류가 나면 인코딩 설정을 건드리지 말고 그냥 무시한다"
    right = "윈도우에서 cp949 디코딩 오류가 나면 PYTHONUTF8=1 을 붙여 실행한다"
    unrelated = "점심은 보통 열두시에 먹는다"

    assert len(relevant_cases(cases, wrong)) == 0          # 예전 동작
    assert len(relevant_cases(cases, wrong, symmetric=True)) == 6
    assert len(relevant_cases(cases, right, symmetric=True)) == 6
    # 무관한 것은 여전히 안 들어온다 - 문을 넓힌 것이지 없앤 것이 아니다
    assert len(relevant_cases(cases, unrelated, symmetric=True)) == 0


def test_the_loop_moves_trust_down_when_a_fact_misleads():
    """벌을 줄 수 있는가. 기준선이 **통과하는** 케이스에 오도하는 사실을 넣는다."""
    from jermes.gate import BenchCase
    from jermes.memory import MemoryItem, MemoryPolicy, apply_measurement, measure

    cases = [BenchCase(case_id=f"c{i}", payload={"about": "pull"}) for i in range(6)]

    def score(case, item):
        if item is None:
            return 1.0                      # 그냥 두면 맞힌다
        return 0.0 if "force" in item.text else 1.0

    item = MemoryItem(item_id="bad", text="rejected 되면 pull 하지 말고 --force 로 덮어써라")
    policy = MemoryPolicy()
    result = measure(item, score, cases, policy)
    assert result.gain == -1.0
    assert result.verdict(policy.min_gain, policy.harm_threshold) == "harmful"
    apply_measurement(item, result, policy)
    assert item.trust < 0.5, item.trust

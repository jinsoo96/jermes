"""기억의 시간 유효성 - 사실은 영원하지 않다.

**메운 구멍.** "배포는 stg 로 한다"(3월)와 "배포는 main 으로 한다"(8월)는 같은 시점에는
모순이지만 다른 시점에는 그냥 규칙이 바뀐 것이다. 여태 이걸 모순으로만 처리해서
**둘 다 `disputed`** 가 됐고, 그러면 멀쩡한 최신 사실까지 잃었다.

실사례에서도 같은 지점이 갈렸다 - 사실마다 "언제부터 언제까지 참"을 들고 있는 시스템이
시간 추론에서 크게 앞섰다. 우리 규율은 유지한다: **잴 수 있으면 재서 판정하고**,
잴 수 없을 때만 시간으로 가르며, 그렇게 판단했다는 사실을 이력에 남긴다.
"""

from jermes.gate import BenchCase
from jermes.memory import (
    Contradiction, MemoryItem, MemoryPolicy, from_dict, recall, resolve,
    resolve_by_time, supersede, to_dict,
)


def item(item_id, text, **kw):
    return MemoryItem(item_id=item_id, text=text, **kw)


# ------------------------------------------------- 유효창

def test_a_superseded_fact_stops_being_recalled_but_is_not_deleted():
    """지난 규칙을 최신인 양 주입하면 에이전트가 옛 규칙대로 움직인다.
    그렇다고 지우면 '그때는 무엇이 참이었나'를 못 묻는다."""
    old = item("m1", "배포는 stg 로 한다", valid_from="2026-03-01")
    new = item("m2", "배포는 main 으로 한다")
    supersede(old, new, "2026-08-01")

    assert [i.item_id for i in recall([old, new])] == ["m2"]
    assert old.status == "active"          # 은퇴가 아니다. 그때는 참이었다.
    assert old.superseded_by == "m2"
    assert any("superseded" in line for line in old.history)
    assert any("supersedes" in line for line in new.history)


def test_the_past_can_still_be_asked_about():
    old = item("m1", "배포는 stg 로 한다", valid_from="2026-03-01")
    new = item("m2", "배포는 main 으로 한다")
    supersede(old, new, "2026-08-01")

    assert [i.item_id for i in recall([old, new], at="2026-05-01")] == ["m1"]
    assert [i.item_id for i in recall([old, new], at="2026-09-01")] == ["m2"]


def test_a_fact_is_not_yet_true_before_it_starts():
    future = item("m1", "새 규칙", valid_from="2026-12-01")
    assert not future.valid_at("2026-08-01")
    assert future.valid_at("2027-01-01")


def test_an_open_window_is_valid_at_any_time():
    always = item("m1", "언제나 참")
    assert always.valid_at("1999-01-01") and always.valid_at("2099-01-01")
    assert not always.expired


def test_the_window_survives_a_save_and_reload():
    old = item("m1", "옛 규칙", valid_from="2026-03-01")
    supersede(old, item("m2", "새 규칙"), "2026-08-01")
    back = from_dict(to_dict(old))
    assert back.valid_until == "2026-08-01" and back.superseded_by == "m2"


def test_records_written_before_this_feature_still_load():
    """옛 기록이 터지면 사람들이 업그레이드를 못 한다."""
    back = from_dict({"item_id": "x", "text": "옛 기록"})
    assert back.valid_from == "" and back.valid_until == "" and not back.expired


# ------------------------------------------------- 측정이 먼저, 시간은 그다음

def test_the_bench_still_decides_when_it_can():
    """규율은 그대로다 - 잴 수 있으면 잰다. 시간은 잴 수 없을 때만 쓴다."""
    left = item("m1", "임계값은 0.8 이다", valid_from="2026-01-01")
    right = item("m2", "임계값은 0.6 이다", valid_from="2026-06-01")
    cases = [BenchCase(case_id=f"c{i}") for i in range(8)]

    def score(case, memory):
        # 인자 순서는 엔진 계약을 따른다(case, memory) - 뒤집으면 조용히 0 점이 된다.
        return 1.0 if memory is not None and "0.8" in memory.text else 0.0

    decision = resolve(Contradiction(left="m1", right="m2", kind="numeric_conflict",
                                     overlap=0.9, detail="0.8/0.6"),
                       left, right, score, cases, MemoryPolicy())
    assert decision.decided and decision.winner == "m1"     # 시간상 나중인 쪽이 아니라


def test_time_settles_what_the_bench_could_not():
    left = item("m1", "배포는 stg 로 한다", valid_from="2026-03-01")
    right = item("m2", "배포는 main 으로 한다", valid_from="2026-08-01")
    contradiction = Contradiction(left="m1", right="m2", kind="negation_flip",
                                overlap=0.8, detail="stg/main")

    decision = resolve_by_time(contradiction, left, right, when="2026-08-01")
    assert decision.decided and decision.winner == "m2"
    assert left.expired and not right.expired
    assert left.status == "active" and right.status == "active"   # 아무도 보류되지 않는다
    assert "측정 아님" in decision.reason                          # 추론임을 밝힌다


def test_time_refuses_to_guess_when_it_cannot_tell():
    """어느 쪽이 새것인지 모르면 아무것도 하지 않는다."""
    left, right = item("m1", "A 다"), item("m2", "B 다")
    decision = resolve_by_time(Contradiction(left="m1", right="m2", kind="negation_flip",
                                          overlap=0.8, detail=""),
                               left, right, when="2026-08-01")
    assert not decision.decided
    assert not left.expired and not right.expired


def test_the_same_timestamp_is_not_enough_to_decide():
    left = item("m1", "A 다", valid_from="2026-08-01")
    right = item("m2", "B 다", valid_from="2026-08-01")
    decision = resolve_by_time(Contradiction(left="m1", right="m2", kind="negation_flip",
                                          overlap=0.8, detail=""),
                               left, right, when="2026-08-02")
    assert not decision.decided and not left.expired


def test_the_run_that_produced_it_can_stand_in_for_a_timestamp():
    left = item("m1", "A 다", source_run_ids=["2026-03-01-run"])
    right = item("m2", "B 다", source_run_ids=["2026-08-01-run"])
    decision = resolve_by_time(Contradiction(left="m1", right="m2", kind="negation_flip",
                                          overlap=0.8, detail=""),
                               left, right, when="2026-08-02")
    assert decision.decided and decision.winner == "m2"


def test_the_clock_is_supplied_not_read():
    """모듈이 몰래 현재시각을 읽으면 같은 입력이 날마다 다른 답을 낸다."""
    import inspect

    from jermes import memory

    source = inspect.getsource(memory)
    assert "datetime.now" not in source and "time.time" not in source


def test_disputed_items_are_still_never_recalled():
    """새 기능이 옛 규율을 흐리면 안 된다."""
    held = item("m1", "보류된 사실")
    held.status = "disputed"
    assert recall([held]) == []
    assert recall([held], at="2026-08-01") == []


# ------------------------------------------------- 기억도 과제를 본다

def make(item_id, text, trust=0.7):
    return MemoryItem(item_id=item_id, text=text, trust=trust)


def test_memory_is_recalled_by_relevance_not_just_trust():
    """스킬은 과제를 보고 고르는데 기억만 안 보면, 기억 200건 중 늘 같은 5건이
    무슨 일을 하든 들어간다. 그건 회상이 아니라 상수다."""
    items = [make("m1", "배포는 main 브랜치로 한다", 0.8),
             make("m2", "점심은 12시", 0.99),
             make("m3", "영업일 계산은 토·일을 뺀다", 0.7)]
    assert [i.item_id for i in recall(items, limit=1, task="배포 어떻게 하지")] == ["m1"]
    assert [i.item_id for i in recall(items, limit=1, task="영업일 며칠 뒤")] == ["m3"]
    # 과제를 안 주면 예전 동작 - 기억이 적을 때는 그게 맞다
    assert [i.item_id for i in recall(items, limit=1)] == ["m2"]


def test_trust_multiplies_relevance_it_does_not_replace_it():
    """더하면 관련 없는 고신뢰 항목이 신뢰만으로 올라온다 - 라우터에서 겪은 그대로다."""
    items = [make("loud", "점심은 12시", 0.99),
             make("fit", "배포는 main 브랜치로 한다", 0.55)]
    assert [i.item_id for i in recall(items, limit=1, task="배포 브랜치")] == ["fit"]


def test_between_two_relevant_memories_the_measured_one_wins():
    items = [make("weak", "배포는 stg 로 한다", 0.55),
             make("strong", "배포는 main 으로 한다", 0.95)]
    assert [i.item_id for i in recall(items, limit=1, task="배포 어디로")] == ["strong"]


def test_when_nothing_is_relevant_nothing_is_injected():
    """이 시험은 예전에 "적게 준다"(0 < n <= 2)를 굳히고 있었다. 그 자리가 결함이다.

    적게라도 주면 사용자에게는 "이게 관련 있다"는 뜻으로 읽히고, 프롬프트에
    들어가면 모델도 그렇게 읽는다. 실측: "오늘 날씨 알려줘" 에 "Edit 도구는
    파일을 먼저 읽어야 한다" 가 딸려 나왔다. 라우터는 이미 아무것도 안 주는데
    기억만 채울 이유가 없다.
    """
    items = [make(f"m{i}", f"관계없는 사실 {i}") for i in range(6)]
    assert recall(items, limit=4, task="행렬 고윳값 분해") == []


def test_relevance_lives_in_one_place():
    """기억이 자기 관련도 계산을 따로 만들면 라우터와 조용히 어긋난다."""
    import inspect

    from jermes import memory as memory_module

    assert "from .router import relevance" in inspect.getsource(memory_module.recall)


def test_an_expired_memory_stays_out_even_when_relevant():
    """관련도가 옛 규율을 덮으면 안 된다."""
    old = make("m1", "배포는 stg 로 한다", 0.9)
    supersede(old, make("m2", "배포는 main 으로 한다", 0.9), "2026-08-01")
    assert [i.item_id for i in recall([old], limit=3, task="배포 어디로")] == []


# --- 기억도 규약을 받는가 -------------------------------------------------
# 스킬 후보는 규약을 거치는데 기억은 안 거치고 있었다. 원천이 정제 기억을 주는
# 순간(호스트 스파인, 또는 세션 증류) 비밀값이 그대로 기억에 박힌다.

def _trace_with_lessons(lessons):
    from jermes.model import RunTrace

    return RunTrace(run_id="r1", scope="user", events=[], success=True,
                    lessons=list(lessons), refined_memory="")


def test_memory_obeys_never_learn():
    from jermes.agent import JermesAgent
    from jermes.gate import ForgeGate
    from jermes.ledger import InMemorySkillLedger

    agent = JermesAgent(InMemorySkillLedger(), ForgeGate(lambda c, s: 0.0))
    added = agent.remember(_trace_with_lessons([
        "배포 전에는 스테이징에서 먼저 돌린다",
        "관리자 password 는 hunter2 이다",
    ]))
    texts = [item.text for item in added]
    assert any("스테이징" in t for t in texts), "멀쩡한 사실은 실려야 한다"
    assert not any("hunter2" in t for t in texts), "비밀값이 기억에 실렸다"
    assert agent.blocked_memories, "막았으면 막았다고 남겨야 한다"


def test_constitution_enforces_in_one_place():
    """스킬 후보든 기억이든 같은 규칙을 받아야 한다."""
    from jermes.constitution import Constitution
    from jermes.model import SkillCandidate

    law = Constitution()
    secret = "api_key 는 sk-abc 이다"
    assert law.check_text(secret) is not None
    candidate = SkillCandidate(name="leak-check", kind="guide", scope="user",
                               action="create", rationale=secret,
                               procedure=["a", "b"], verification=["v"])
    assert law.check_candidate(candidate) is not None


# --- 기억을 실제로 재는가 ---------------------------------------------------
# `기억 측정 안 함 - 점수 함수 미지정` 이 매번 찍혔다. 싣기는 하는데 신뢰도가 영영
# 중립이라 측정·감쇠·모순판정 장치가 통째로 놀았다.

def test_memory_score_uses_the_same_bench_as_skills():
    """항목을 넣었을 때와 뺐을 때를 같은 케이스로 비교해야 한다."""
    from jermes.bench import Expectation, ReplayCase
    from jermes.cli import _memory_score_with
    from jermes.gate import BenchCase
    from jermes.memory import MemoryItem

    asked: list[str] = []

    def complete(prompt: str) -> str:
        asked.append(prompt)
        return "먼저 파일을 읽는다" if "먼저 읽어" in prompt else "그냥 고친다"

    cases = [ReplayCase(case_id="c1", payload={"tool": "Edit",
                                               "error_detail": "String not found"},
                        expect=Expectation(require=["먼저"]))]
    score = _memory_score_with(complete, cases)
    case = BenchCase(case_id="c1", payload={})

    item = MemoryItem(item_id="m1", text="Edit 전에는 먼저 읽어야 한다", scope="user")
    with_item = score(case, item)
    without = score(case, None)
    assert with_item > without, "도움이 되는 사실은 점수를 올려야 한다"
    assert any("Edit 전에는" in p for p in asked), "항목 본문이 실제로 들어가야 한다"


def test_memory_score_is_zero_for_unknown_cases():
    from jermes.cli import _memory_score_with
    from jermes.gate import BenchCase

    score = _memory_score_with(lambda p: "무엇이든", [])
    assert score(BenchCase(case_id="없음", payload={}), None) == 0.0


# --- 겹치는 말이 없으면 아무것도 안 준다 --------------------------------------
# 예전에는 신뢰도 순으로 절반을 채웠다. 그러면 "오늘 날씨 알려줘" 에 "Edit 도구는
# 파일을 먼저 읽어야 한다" 가 딸려 나온다(실측). 사용자에게는 관련 있다는 뜻으로
# 읽히고 프롬프트에 들어가면 모델도 그렇게 읽는다. 라우터는 이미 같은 규율을
# 지키는데 기억만 안 지킬 이유가 없다.

def test_no_overlap_returns_nothing_not_filler():
    from jermes.memory import recall

    items = [make("m1", "Edit 도구로 파일을 고치려면 먼저 읽어야 한다", 0.9),
             make("m2", "git 저장소가 아니면 exit code 128 이 난다", 0.8)]
    assert recall(items, task="오늘 날씨 알려줘") == []
    assert recall(items, task="파일 고치기 전에 뭘 해야 하나")


def test_no_task_still_falls_back_to_trust():
    """과제를 안 주면 고를 근거가 없다. 그때는 신뢰도 순이 맞다."""
    from jermes.memory import recall

    items = [make("low", "가", 0.55), make("high", "나", 0.95)]
    assert [i.item_id for i in recall(items, limit=1)] == ["high"]


# --- 사용자가 직접 가르치고 내리고 되돌릴 수 있는가 ---------------------------
# 지적: `jermes memory` 는 인자 0개 읽기전용이었고, 기억은 오직 세션 증류로만
# 들어왔다. "이거 기억해둬" 가 아무 데도 안 남고, 틀린 기억을 내릴 방법도 없었다
# (`retired` 는 선언만 돼 있고 대입하는 곳이 없었다).

def test_a_user_can_teach_a_fact_by_hand(tmp_path, monkeypatch, capsys):
    from jermes import cli

    monkeypatch.setenv("JERMES_HOME", str(tmp_path / "home"))
    assert cli.main(["memory", "--add", "배포는 develop 에서 시작한다"]) == 0
    assert "기억했습니다" in capsys.readouterr().out
    items = cli.load_memory()
    assert len(items) == 1 and "develop" in items[0].text
    # 사람이 적었다는 것은 그 사실이 맞다는 증거가 아니다.
    assert not items[0].measured


def test_hand_written_memory_still_obeys_the_law(tmp_path, monkeypatch, capsys):
    from jermes import cli

    monkeypatch.setenv("JERMES_HOME", str(tmp_path / "home"))
    assert cli.main(["memory", "--add", "관리자 암호는 hunter2 이다"]) == 1
    assert "안 실었습니다" in capsys.readouterr().out
    assert cli.load_memory() == []


def test_retiring_hides_a_fact_without_deleting_it(tmp_path, monkeypatch, capsys):
    from jermes import cli
    from jermes.memory import recall

    monkeypatch.setenv("JERMES_HOME", str(tmp_path / "home"))
    cli.main(["memory", "--add", "배포는 develop 에서 시작한다"])
    item_id = cli.load_memory()[0].item_id
    capsys.readouterr()

    assert cli.main(["memory", "--retire", item_id]) == 0
    items = cli.load_memory()
    assert len(items) == 1, "지워지면 안 된다"
    assert items[0].status == "retired"
    assert recall(items, task="배포 브랜치") == []


def test_superseding_closes_the_old_window(tmp_path, monkeypatch, capsys):
    """`when` 이 비면 유효창이 안 닫혀 옛 사실이 계속 회상됐다(실측)."""
    from jermes import cli
    from jermes.memory import recall

    monkeypatch.setenv("JERMES_HOME", str(tmp_path / "home"))
    cli.main(["memory", "--add", "배포는 develop 에서 시작한다"])
    old_id = cli.load_memory()[0].item_id
    capsys.readouterr()

    assert cli.main(["memory", "--supersede", old_id,
                     "--add", "배포는 main 에서 시작한다"]) == 0
    items = cli.load_memory()
    assert len(items) == 2, "옛 사실은 남아야 한다"
    old = next(i for i in items if i.item_id == old_id)
    assert old.valid_until, "유효창이 안 닫혔다"
    assert old.superseded_by

    picked = recall(items, task="배포 브랜치")
    assert [i.text for i in picked] == ["배포는 main 에서 시작한다"]


# --- 스코프와 문자 예산 --------------------------------------------------------
# 지적(재현함): `MemoryItem.scope` 는 필드도 있고 채우기까지 하는데 `recall()` 이
# 안 읽어서, 저장소가 하나뿐인 상황에서 프로젝트 A 의 기억이 B 에 그대로 들어갔다.
# "없다"보다 "있는데 안 쓴다"가 나쁘다.

def test_recall_can_stay_inside_a_scope():
    from jermes.memory import recall

    items = [make("mine", "나는 두 칸 들여쓰기를 쓴다", 0.8),
             make("proj", "이 프로젝트는 stg 로 배포한다", 0.8)]
    items[0].scope, items[1].scope = "user", "project"

    assert {i.item_id for i in recall(items)} == {"mine", "proj"}
    # 좁은 스코프를 물어도 사용자 자신에 대한 사실은 남는다.
    assert {i.item_id for i in recall(items, scope="session")} == {"mine"}
    assert {i.item_id for i in recall(items, scope="project")} == {"mine", "proj"}


def test_injection_has_a_character_budget():
    """개수만 세면 긴 것 하나가 컨텍스트를 삼킨다. 실측 46,289자."""
    from jermes.agent import ContextPack
    from jermes.discovery import KIND_SKILL, Capability

    big = [Capability(name=f"s{i}", kind=KIND_SKILL, description="d" * 200,
                      body="본문 " * 3000, verified=True) for i in range(5)]
    rendered = ContextPack(skills=big).render()
    assert len(rendered) <= 12000
    assert "truncated" in rendered, "잘랐으면 잘랐다고 말해야 한다"
    # 예산을 끄면 예전 그대로.
    assert len(ContextPack(skills=big, max_chars=0).render()) > 40000


def test_truncation_happens_at_block_boundaries():
    """태그 중간에서 끊으면 다음 블록이 이전 블록 안으로 빨려 들어간다."""
    from jermes.agent import ContextPack
    from jermes.discovery import KIND_SKILL, Capability

    packs = [Capability(name=f"s{i}", kind=KIND_SKILL, description="설명 " * 50,
                        verified=True) for i in range(20)]
    rendered = ContextPack(skills=packs, max_chars=800).render()
    assert rendered.count("<skill ") == rendered.count("</skill>")

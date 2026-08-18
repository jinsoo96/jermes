"""과제 이력 - **무엇인지보다 무엇을 해왔는지가 잘 맞춘다.**

E9(실무 데이터, 문항을 지어내지 않음)에서 확인한 것:
- XGEN 실사용 질문 → 워크플로우: 이름만 top-5 43.8% · macro 25.7% · 무응답 17/48
  거기에 과거 실사용 질문을 붙이자 top-5 97.9% · macro 92.9% · 무응답 0
- GitLab MR 제목 → 레포: 이름만 top-5 10.1% (최빈 찍기 16.4%보다도 나쁘다)
  과거 MR 제목을 붙이자 top-5 93.2%
- 노드 구성을 붙이는 것은 도움이 안 됐다(48.4% → 47.9%). 도메인 어휘가 아니라서다.

그래서 이력을 쌓고 쓰는 길을 여기서 고정한다. 단 두 가지를 지켜야 한다 -
**성공한 것만** 쌓고(못 한 일을 한다고 광고하지 않는다), **지금 푸는 문제는 안 넣는다**
(정답을 보고 채점하게 된다).
"""

from jermes.discovery import LedgerSource
from jermes.ledger import MAX_EXAMPLES, InMemorySkillLedger, JsonlSkillLedger
from jermes.model import SkillDef
from jermes.router import Router, searchable


def active(name, description, **meta):
    skill = SkillDef(name=name, kind="guide", scope="user",
                     description=description, body=f"# {name}", meta=meta)
    skill.status = "active"
    skill.verified = True
    return skill


# ------------------------------------------------- 이력이 라우팅을 바꾼다

def test_history_finds_what_the_name_alone_cannot():
    """실측한 그림 그대로 - 이름에 도메인 어휘가 없으면 아무것도 못 고른다."""
    plain = [__import__("jermes.discovery", fromlist=["Capability"]).Capability(
        name="masahoe_rag", kind="skill", description="masahoe_rag")]
    assert Router(plain).route("2024년 국제경주 개최 결과에서 참가국 수").names() == []

    with_history = [__import__("jermes.discovery",
                               fromlist=["Capability"]).Capability(
        name="masahoe_rag", kind="skill", description="masahoe_rag",
        examples=["2023년 국제경주 개최 결과 보고서를 요약해줘",
                  "한국마사회 적극행정 우수사례를 알려줘"])]
    assert Router(with_history).route(
        "2024년 국제경주 개최 결과에서 참가국 수").names() == ["masahoe_rag"]


def test_a_long_history_does_not_win_by_volume_alone():
    """실적이 많은 능력이 무조건 이기면 순위가 인기투표가 된다.
    (실측에서 레포마다 MR 수가 300 대 3 으로 벌어졌다.)"""
    from jermes.discovery import Capability

    loud = Capability(name="loud", kind="skill", description="많이 쓰인 것",
                      examples=["회의록을 정리한다"] * 200)
    quiet = Capability(name="quiet", kind="skill", description="영업일 마감일을 계산한다",
                       examples=["영업일 며칠 뒤가 마감인지 알려줘"])
    assert Router([loud, quiet]).route("영업일 마감일 계산").names()[0] == "quiet"


def test_the_searchable_text_includes_name_description_and_history():
    from jermes.discovery import Capability

    text = searchable(Capability(name="n", kind="skill", description="d",
                                 examples=["e1", "e2"]))
    assert "n" in text and "d" in text and "e1" in text and "e2" in text


# ------------------------------------------------- 이력이 쌓이는 길

def test_a_successful_task_is_remembered_as_an_example():
    ledger = InMemorySkillLedger()
    ledger.commit(active("deploy-guide", "배포 절차"))
    ledger.record_outcome(["deploy-guide"], True, task="stg 에 배포하는 순서 알려줘")
    assert ledger.get("deploy-guide").skill.meta["examples"] == ["stg 에 배포하는 순서 알려줘"]


def test_a_failed_task_is_not_advertised_as_a_strength():
    """못 한 일을 한다고 광고하면 라우터가 그 실패로 다시 부른다."""
    ledger = InMemorySkillLedger()
    ledger.commit(active("deploy-guide", "배포 절차"))
    ledger.record_outcome(["deploy-guide"], False, task="쿠버네티스 파드를 재시작해줘")
    assert ledger.get("deploy-guide").skill.meta.get("examples", []) == []
    assert ledger.get("deploy-guide").usage.failures == 1


def test_the_same_task_is_not_recorded_twice():
    ledger = InMemorySkillLedger()
    ledger.commit(active("s", "설명"))
    for _ in range(3):
        ledger.record_outcome(["s"], True, task="같은 질문")
    assert ledger.get("s").skill.meta["examples"] == ["같은 질문"]


def test_the_history_is_bounded_and_drops_the_oldest():
    """무한히 쌓이면 프롬프트도 색인도 부담이다."""
    ledger = InMemorySkillLedger()
    ledger.commit(active("s", "설명"))
    for index in range(MAX_EXAMPLES + 10):
        ledger.record_outcome(["s"], True, task=f"질문 {index}")
    examples = ledger.get("s").skill.meta["examples"]
    assert len(examples) == MAX_EXAMPLES
    assert examples[-1] == f"질문 {MAX_EXAMPLES + 9}"     # 최신은 남고
    assert "질문 0" not in examples                        # 오래된 것이 빠진다


def test_recording_an_outcome_without_a_task_still_counts_usage():
    """옛 호출측이 task 를 안 줘도 깨지면 안 된다."""
    ledger = InMemorySkillLedger()
    ledger.commit(active("s", "설명"))
    ledger.record_outcome(["s"], True)
    assert ledger.get("s").usage.successes == 1
    assert ledger.get("s").skill.meta.get("examples", []) == []


def test_the_history_survives_a_restart(tmp_path):
    """재기동할 때마다 근거가 사라지면 어제 잘 찾던 스킬을 오늘 못 찾는다."""
    path = tmp_path / "skills.jsonl"
    ledger = JsonlSkillLedger(path)
    ledger.commit(active("s", "설명"))
    ledger.record_outcome(["s"], True, task="국제경주 개최 결과 요약")

    reopened = JsonlSkillLedger(path)
    assert reopened.get("s").skill.meta["examples"] == ["국제경주 개최 결과 요약"]


def test_discovery_carries_the_history_into_the_capability():
    ledger = InMemorySkillLedger()
    ledger.commit(active("s", "설명", examples=["국제경주 참가국 수"]))
    found = LedgerSource(ledger).discover()
    assert found[0].examples == ["국제경주 참가국 수"]


def test_the_history_source_can_be_supplied_by_the_host():
    """XGEN 은 execution_io 에서, 단독 실행은 스킬 메타에서 - 어디서 끌지는 호스트 몫."""
    ledger = InMemorySkillLedger()
    ledger.commit(active("s", "설명"))
    found = LedgerSource(ledger, history=lambda record: ["외부에서 온 이력"]).discover()
    assert found[0].examples == ["외부에서 온 이력"]


def test_a_skill_with_no_history_is_not_given_a_fake_one():
    ledger = InMemorySkillLedger()
    ledger.commit(active("s", "설명"))
    assert LedgerSource(ledger).discover()[0].examples == []


def test_a_skill_body_becomes_searchable_text_when_imported(tmp_path):
    """남의 SKILL.md 는 도메인 어휘가 본문에만 있는 경우가 많다."""
    from jermes.discovery import SkillDirSource

    pkg = tmp_path / "ko-rag"
    pkg.mkdir()
    (pkg / "SKILL.md").write_text(
        "---\nname: ko-rag\ndescription: 사내 문서 검색. 언제 쓰는지 설명.\n---\n\n"
        "# ko-rag\n\n## When\n- 한국마사회 국제경주 보고서를 찾을 때\n", encoding="utf-8")
    found = SkillDirSource([tmp_path]).discover()
    assert found and "국제경주" in " ".join(found[0].examples)
    assert Router(found).route("국제경주 보고서 찾아줘").names() == ["ko-rag"]


# ------------------------------------------------- 실제 경로까지 이어졌는가

def test_the_recall_feedback_path_carries_the_task():
    """여기가 안 이어지면 XGEN 실행에서는 이력이 영영 안 쌓인다."""
    from jermes.recall import LedgerSkillSource

    ledger = InMemorySkillLedger()
    ledger.commit(active("s", "설명"))
    LedgerSkillSource(ledger).record_run_outcome(["s"], True, task="국제경주 참가국 수")
    assert ledger.get("s").skill.meta["examples"] == ["국제경주 참가국 수"]


def test_the_spine_ledger_persists_and_replays_the_task():
    """XGEN 은 spine 에 적는다. 적기만 하고 못 읽으면 재기동에서 사라진다."""
    from jermes.host import SpineSkillLedger

    class FakeStore:
        def __init__(self):
            self.rows = []

        def append(self, spine_type, key, payload):
            self.rows.append({"type": spine_type, "key": key, **payload})

        def query(self, spine_type):
            return [dict(row) for row in self.rows if row["type"] == spine_type]

    store = FakeStore()
    ledger = SpineSkillLedger(store)
    ledger.commit(active("s", "설명"))
    ledger.record_outcome(["s"], True, task="국제경주 참가국 수")

    reopened = SpineSkillLedger(store)
    assert reopened.get("s").skill.meta["examples"] == ["국제경주 참가국 수"]

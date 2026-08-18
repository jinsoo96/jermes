import json
import logging

from jermes import (
    Curator,
    RunTrace,
    TraceEvent,
    extract_signals,
)
from jermes.drafter import LLMDrafter, build_prompt
from jermes.host import (
    InMemorySpineStore,
    SpineSkillLedger,
    signature_counts,
    trace_from_spine,
)
from jermes.model import SkillDef


def make_trace():
    events = [TraceEvent(type="tool_call", name=f"step_{i}") for i in range(6)]
    events.append(TraceEvent(type="error", name="step_2", detail="404 wrong branch"))
    return RunTrace(run_id="r1", scope="user", events=events,
                    lessons=["pin the ref"], success=True)


def test_build_prompt_shows_the_learnable_spot_and_the_rules():
    """예전엔 트레이스 앞 40개를 그대로 실었다. 이제는 **배울 자리**(실패·복구·교정)
    주변을 떠서 보여준다 - 긴 세션에서 정작 배울 곳이 잘려나가던 문제 때문이다."""
    trace = make_trace()
    prompt = build_prompt(trace, extract_signals(trace))
    assert "step_2" in prompt and "FAILED" not in prompt or "step_2" in prompt
    assert "404 wrong branch" in prompt          # 실패 지점이 실린다
    assert "kebab-case-name" in prompt
    assert "Return []" in prompt


def test_drafter_parses_and_sanitizes():
    payload = [{
        "name": "Repo Fetch_Ref Pin!!",
        "when_to_use": "fetching repo files",
        "rationale": "404 when branch assumed",
        "procedure": ["Resolve default branch", "Fetch with ?ref="],
        "verification": ["200 response"],
    }]
    drafter = LLMDrafter(lambda p: f"```json\n{json.dumps(payload)}\n```")
    trace = make_trace()
    candidates = drafter.draft(trace, extract_signals(trace))
    assert len(candidates) == 1
    assert candidates[0].name == "repo-fetch-ref-pin"
    assert candidates[0].provenance.origin == "llm_drafter"


def test_drafter_survives_garbage_and_errors():
    assert LLMDrafter(lambda p: "no json here").draft(make_trace(), []) == []
    assert LLMDrafter(lambda p: "[not, valid").draft(make_trace(), []) == []

    def boom(prompt):
        raise RuntimeError("provider down")
    assert LLMDrafter(boom).draft(make_trace(), []) == []


def test_zero_candidates_says_which_zero_it_was(caplog):
    """라이브에서 signals=2 drafted=0 을 만났을 때 원인을 못 갈랐다 - 모델이 빈
    배열을 준 것과, 예시 유출 가드가 전부 걷어낸 것은 다른 사건이고 대응도 다르다."""
    trace = make_trace()
    hits = extract_signals(trace)
    assert hits, "이 테스트는 신호가 있는 트레이스를 전제로 한다"

    with caplog.at_level(logging.INFO, logger="jermes.drafter"):
        LLMDrafter(lambda p: "[]").draft(trace, hits)
    empty = [r.getMessage() for r in caplog.records]
    assert any("model=0" in m and "kept=0" in m for m in empty), empty

    # 프롬프트 예시를 그대로 베껴온 경우 - 모델은 뭔가 줬는데 전부 걸러진다
    leak = json.dumps([{"name": "paginate-with-cursor", "when_to_use": "x",
                        "rationale": "y", "procedure": ["a"], "verification": ["v"]}])
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="jermes.drafter"):
        LLMDrafter(lambda p: leak).draft(trace, hits)
    leaked = [r.getMessage() for r in caplog.records]
    assert any("model=1" in m and "leak=1" in m and "kept=0" in m for m in leaked), leaked


def test_no_signals_no_log_noise(caplog):
    """할 일이 없을 때까지 떠들면 진짜 신호가 묻힌다."""
    with caplog.at_level(logging.INFO, logger="jermes.drafter"):
        LLMDrafter(lambda p: "[]").draft(make_trace(), [])
    assert not [r for r in caplog.records if "[drafter] hits=" in r.getMessage()]


def test_drafter_caps_candidates():
    payload = [{"name": f"skill-{i}", "when_to_use": "x", "rationale": "y",
                "procedure": ["a", "b"], "verification": ["v"]} for i in range(5)]
    drafter = LLMDrafter(lambda p: json.dumps(payload), max_candidates=2)
    assert len(drafter.draft(make_trace(), [])) == 2


def seed_spine(store):
    store.append("activity", "r1", {"type": "tool_call", "name": "fetch", "ok": False,
                                    "detail": "404"})
    store.append("activity", "r1", {"type": "recovery", "name": "",
                                    "detail": "used ?ref=main"})
    store.append("activity", "r1", {"type": "tool_call", "name": "fetch", "ok": True})
    store.append("lesson", "r1", {"text": "pin ref explicitly"})
    store.append("refined_memory", "r1", {"text": "repo default branch is main"})
    store.append("judge_score", "r1", {"score": 0.87})


def test_trace_from_spine():
    store = InMemorySpineStore()
    seed_spine(store)
    trace = trace_from_spine(store, "r1")
    assert len(trace.events) == 3
    assert trace.lessons == ["pin ref explicitly"]
    assert trace.judge_score == 0.87
    assert "default branch" in trace.refined_memory


def test_signature_counts():
    t1, t2 = make_trace(), make_trace()
    counts = signature_counts([t1, t2])
    assert counts[t1.signature()] == 2


def skill(name="s1"):
    return SkillDef(name=name, kind="guide", scope="user",
                    description="d", body="b")


def test_spine_ledger_roundtrip():
    store = InMemorySpineStore()
    ledger = SpineSkillLedger(store)
    committed = skill()
    committed.status = "active"
    ledger.commit(committed, note="create")
    ledger.record_outcome(["s1"], True)
    ledger.set_status("s1", "deprecated", "manual")

    reloaded = SpineSkillLedger(store)
    record = reloaded.get("s1")
    assert record.status == "deprecated"
    assert record.usage.successes == 1
    assert len(store.query("skill_def", "s1")) == 3


def test_spine_ledger_feeds_curator_patch_resolution():
    store = InMemorySpineStore()
    ledger = SpineSkillLedger(store)
    existing = skill("repo-fetch-ref-pin")
    existing.status = "active"
    ledger.commit(existing)
    curator = Curator(ledger)
    from jermes.model import Provenance, SkillCandidate
    candidate = SkillCandidate(
        name="repo-fetch-ref-pin", kind="guide", scope="user", action="create",
        rationale="dup", when_to_use="dup",
        procedure=["a", "b"], verification=["v"],
        provenance=Provenance(origin="llm_drafter"))
    result = curator.curate([candidate])
    assert result.accepted[0].action == "patch"

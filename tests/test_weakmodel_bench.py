import json

from jermes import (
    Curator,
    ForgeGate,
    InMemorySkillLedger,
    RunTrace,
    SkillForge,
    TraceEvent,
    extract_signals,
)
from jermes.bench import (
    Expectation,
    ReplayCase,
    ReproReplayRunner,
    cases_from_repro_rows,
)
from jermes.drafter import EnsembleDrafter, LLMDrafter

GOOD_ITEM = {
    "name": "resolve-ref-before-fetch",
    "when_to_use": "when fetching repository files by URL",
    "rationale": "assumed default branch causes 404",
    "procedure": ["Resolve default_branch from the repos API",
                  "Fetch with explicit ?ref= parameter"],
    "pitfalls": ["Assuming master"],
    "verification": ["Response is 200 with non-empty body"],
}


def make_trace():
    events = [TraceEvent(type="tool_call", name=f"t{i}") for i in range(6)]
    events.append(TraceEvent(type="error", name="t2", detail="404 wrong branch"))
    return RunTrace(run_id="wk-1", scope="user", events=events, success=True)


class ScriptedCompleter:
    """Plays back canned responses in order; repeats the last one after."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def __call__(self, prompt):
        self.calls += 1
        index = min(self.calls - 1, len(self.responses) - 1)
        return self.responses[index]


def test_retry_with_feedback_recovers_flaky_model():
    completer = ScriptedCompleter([
        "I think the agent did well overall!",       # garbage, no JSON
        "[]",                                        # repair of garbage -> empty
        json.dumps([GOOD_ITEM]),                     # retry succeeds
    ])
    drafter = LLMDrafter(completer, repair=True, max_retries=1)
    out = drafter.draft(make_trace(), extract_signals(make_trace()))
    assert len(out) == 1 and out[0].name == "resolve-ref-before-fetch"


def test_repair_pass_fixes_malformed_json():
    broken = '[{"name": "resolve-ref-before-fetch", "when_to_use": "x",'  # truncated
    completer = ScriptedCompleter([broken, json.dumps([GOOD_ITEM])])
    drafter = LLMDrafter(completer, repair=True, max_retries=0)
    out = drafter.draft(make_trace(), [])
    assert len(out) == 1
    assert completer.calls == 2  # second call was the repair prompt


def test_prose_wrapped_json_extracted_without_repair():
    wrapped = "Sure! Here are the skills:\n```json\n" + json.dumps([GOOD_ITEM]) + "\n```\nHope this helps!"
    drafter = LLMDrafter(ScriptedCompleter([wrapped]), repair=False)
    assert len(drafter.draft(make_trace(), [])) == 1


def test_ensemble_dedupes_and_survives_garbage_samples():
    variant = dict(GOOD_ITEM, name="pin-ref-on-fetch")  # same ground, different name
    distinct = {
        "name": "compact-before-finish",
        "when_to_use": "when the transcript grows beyond the window",
        "rationale": "long transcripts truncate context",
        "procedure": ["Call compact", "Continue from the summary"],
        "verification": ["No truncation warning"],
    }
    completer = ScriptedCompleter([
        json.dumps([GOOD_ITEM]),
        "total garbage %% not json",
        "[]",                          # repair of garbage
        json.dumps([variant, distinct]),
    ])
    ensemble = EnsembleDrafter(LLMDrafter(completer, max_retries=0), samples=3)
    out = ensemble.draft(make_trace(), [])
    names = [c.name for c in out]
    assert "resolve-ref-before-fetch" in names
    assert "compact-before-finish" in names
    assert "pin-ref-on-fetch" not in names  # near-duplicate collapsed


def test_expectation_scoring():
    expect = Expectation(require=["200"], require_regex=[r"ref=\w+"], forbid=["404"])
    assert expect.score("fetched with ?ref=main -> 200 OK") == 1.0
    assert expect.score("got 404 again") < 0.5


def replay_cases():
    return [
        ReplayCase(
            case_id=f"case-{i}",
            payload={"repo": f"r{i}"},
            expect=Expectation(require=["200"], forbid=["404"]),
        )
        for i in range(16)
    ]


def run_fn(payload, skill):
    if skill is not None and "ref" in skill.body.lower():
        return "resolved default branch, fetched ?ref=main -> 200 OK"
    return "fetch failed: 404 wrong branch"


def test_replay_runner_with_gate_promotes_helpful_skill():
    runner = ReproReplayRunner(run_fn, replay_cases())
    ledger = InMemorySkillLedger()
    forge = SkillForge(ledger, ForgeGate(runner), Curator(ledger))
    drafter = LLMDrafter(ScriptedCompleter([json.dumps([GOOD_ITEM])]))
    trace = make_trace()
    episode = forge.process_trace(trace, bench_cases=runner.bench_cases(),
                                  drafted=drafter.draft(trace, []))
    assert episode.results[0][1].verdict == "promoted"
    assert ledger.get("resolve-ref-before-fetch").skill.verified


def test_replay_runner_rejects_useless_skill():
    useless = dict(GOOD_ITEM, name="restart-and-hope",
                   rationale="restarting sometimes helps flaky fetches",
                   procedure=["Restart the client process", "Try the request again"],
                   verification=["It works this time"])
    runner = ReproReplayRunner(run_fn, replay_cases())
    ledger = InMemorySkillLedger()
    forge = SkillForge(ledger, ForgeGate(runner), Curator(ledger))
    drafter = LLMDrafter(ScriptedCompleter([json.dumps([useless])]))
    trace = make_trace()
    episode = forge.process_trace(trace, bench_cases=runner.bench_cases(),
                                  drafted=drafter.draft(trace, []))
    assert episode.results[0][1].verdict == "rejected"
    assert ledger.get("restart-and-hope") is None  # rejected candidates never land


def test_weak_model_end_to_end_matches_strong_outcome():
    """The thesis test: a badly-behaved model (garbage, prose wrap, malformed
    JSON, duplicate spam) still yields exactly one verified skill because
    recovery layers + deterministic gate carry the quality."""
    weak = ScriptedCompleter([
        "hmm let me think about this run...",                    # garbage
        "[]",                                                    # repair -> empty
        json.dumps([GOOD_ITEM]),                                 # retry OK
        "Here you go:\n```json\n" + json.dumps([GOOD_ITEM]) + "\n```",
        '[{"name": "resolve-ref-before-fetch", broken',          # malformed
        json.dumps([GOOD_ITEM]),                                 # repair OK
    ])
    ensemble = EnsembleDrafter(LLMDrafter(weak, repair=True, max_retries=1), samples=3)
    runner = ReproReplayRunner(run_fn, replay_cases())
    ledger = InMemorySkillLedger()
    forge = SkillForge(ledger, ForgeGate(runner), Curator(ledger))
    trace = make_trace()
    episode = forge.process_trace(trace, bench_cases=runner.bench_cases(),
                                  drafted=ensemble.draft(trace, extract_signals(trace)))
    promoted = [s for s, g in episode.results if g.verdict == "promoted"]
    assert len(promoted) == 1
    record = ledger.get("resolve-ref-before-fetch")
    assert record.skill.verified and record.status == "active"


def test_cases_from_repro_rows():
    rows = [{"case_id": "a", "payload": {"x": 1}, "require": ["ok"], "forbid": ["fail"]}]
    cases = cases_from_repro_rows(rows)
    assert cases[0].expect.score("all ok") == 1.0


def test_capture_repro_rows_from_trace():
    from jermes.bench import capture_repro_rows
    trace = make_trace()  # contains error "404 wrong branch", no recovery event
    rows = capture_repro_rows(trace)
    assert len(rows) == 1
    assert rows[0]["forbid"] == ["404"]
    assert rows[0]["auto_captured"] is True
    # 이 시험은 예전에 **복구 문구에서 요구조건을 뽑는** 동작을 굳히고 있었다.
    # 그 자리가 결함이었다: 실제 원천의 복구 문구는 "<도구> succeeded after
    # failing" 이라는 템플릿이라 내용이 0이고, 거기서 뽑으면 게이트가 "도구 이름을
    # 적었는가"라는 서식을 잰다. 이제는 **성공한 재시도의 입력**에서 뽑는다.
    failed = next(e for e in trace.events if e.type == "error" or
                  (e.type == "tool_call" and not e.ok))
    failed.meta = {"input": "url=https://x/repo/file"}
    trace.events.append(TraceEvent(type="tool_call", name=failed.name, ok=True,
                                   detail="200 ok",
                                   meta={"input": "url=https://x/repo/file?ref=main"}))
    rows = capture_repro_rows(trace)
    assert rows[0]["require"], "바뀐 것에서 요구조건이 나와야 한다"
    # `?ref=main` 은 토큰 경계에서 `ref` 와 `main` 으로 갈린다. 그게 맞다 -
    # `=` 를 토큰에 넣으면 `command=cd` 처럼 키까지 딸려 온다.
    assert "ref" in rows[0]["require"], rows[0]["require"]
    cases = cases_from_repro_rows(rows)
    assert cases[0].expect.score("다시 받을 때 ref=main 을 붙인다") > 0.5


def test_example_leak_guard():
    leaked = {"name": "paginate-with-cursor", "when_to_use": "orders API paging",
              "rationale": "offset pagination drops rows",
              "procedure": ["a", "b"], "verification": ["v"]}
    drafter = LLMDrafter(ScriptedCompleter([json.dumps([leaked, GOOD_ITEM])]),
                         max_candidates=2)
    out = drafter.draft(make_trace(), [])
    assert [c.name for c in out] == ["resolve-ref-before-fetch"]


def test_ensemble_varies_prompts_across_samples():
    prompts = []

    def recorder(prompt):
        prompts.append(prompt)
        return json.dumps([GOOD_ITEM])

    EnsembleDrafter(LLMDrafter(recorder, repair=False, max_retries=0),
                    samples=3).draft(make_trace(), [])
    assert len(prompts) == 3 and len(set(prompts)) == 3


def test_completion_failure_is_logged_not_silent(caplog):
    def boom(prompt):
        raise RuntimeError("provider down")
    with caplog.at_level("WARNING"):
        assert LLMDrafter(boom).draft(make_trace(), []) == []
    msgs = " ".join(str(r.getMessage()) for r in caplog.records)
    assert "completion failed" in msgs and "provider down" in msgs


def test_unparsable_output_is_logged(caplog):
    with caplog.at_level("WARNING"):
        LLMDrafter(ScriptedCompleter(["not json at all", "still not json"]),
                   repair=True, max_retries=0).draft(make_trace(), [])
    msgs = " ".join(str(r.getMessage()) for r in caplog.records)
    assert "unparsable output" in msgs and "repair failed" in msgs


class _Boom:
    """호출할 때마다 죽는 엔드포인트."""
    def __init__(self, tag="down"):
        self.tag = tag
        self.calls = 0

    def __call__(self, prompt):
        self.calls += 1
        raise RuntimeError(f"{self.tag} unreachable")


def test_failover_uses_the_next_endpoint_when_first_is_down():
    from jermes.drafter import failover_completer
    dead, alive = _Boom(), lambda p: "OK"
    complete = failover_completer([dead, alive])
    assert complete("x") == "OK"
    assert dead.calls == 1


def test_failover_sticks_to_the_healthy_endpoint():
    from jermes.drafter import failover_completer
    dead = _Boom()
    complete = failover_completer([dead, lambda p: "OK"])
    complete("a")
    complete("b")
    complete("c")
    assert dead.calls == 1, "죽은 엔드포인트를 매번 다시 때리면 안 된다"


def test_failover_raises_only_when_every_endpoint_is_down():
    from jermes.drafter import failover_completer
    complete = failover_completer([_Boom("a"), _Boom("b")])
    try:
        complete("x")
    except RuntimeError as e:
        assert "unreachable" in str(e)
    else:
        raise AssertionError("전부 죽었는데 예외가 안 났다")


def test_failover_requires_at_least_one_completer():
    from jermes.drafter import failover_completer
    try:
        failover_completer([])
    except ValueError:
        pass
    else:
        raise AssertionError("빈 목록은 거절해야 한다")


def test_drafter_survives_total_endpoint_outage():
    """전 엔드포인트 다운이어도 드래프터는 예외 대신 빈 목록을 준다(루프 생존)."""
    from jermes.drafter import failover_completer
    drafter = LLMDrafter(failover_completer([_Boom(), _Boom()]))
    assert drafter.draft(make_trace(), []) == []


# --- 벤치가 서식이 아니라 내용을 재는가 ---------------------------------------
# 지적(재현함): 실세션 요구조건이 거의 전부 `['<도구이름>','succeeded']` 였다.
# 세션 하나에서 9건 중 7건이 ('Bash','succeeded'). 그러면 게이트가 묻는 것은
# "이 스킬이 도움이 되는가"가 아니라 "답변에 도구 이름을 적었는가"라는 서식이고,
# 서식만 지시하는 쓸모없는 스킬이 검증됨을 받는다.

def _trace_with_retry():
    from jermes.model import RunTrace, TraceEvent

    return RunTrace(run_id="r1", scope="user", success=True, events=[
        TraceEvent(type="tool_call", name="Bash", ok=False,
                   detail="fatal: not a git repository",
                   meta={"input": "command=git commit -m hello"}),
        TraceEvent(type="recovery", name="Bash", ok=True,
                   detail="Bash succeeded after failing"),
        TraceEvent(type="tool_call", name="Bash", ok=True, detail="ok",
                   meta={"input": "command=cd myrepo && git commit -m hello"}),
    ])


def test_requirements_come_from_what_changed_not_the_template():
    from jermes.bench import capture_repro_rows

    rows = capture_repro_rows(_trace_with_retry())
    assert len(rows) == 1
    require = rows[0]["require"]
    assert require, "요구조건이 비었다"
    # 바뀐 것(cd, myrepo)을 요구해야 한다. 템플릿 낱말은 안 된다.
    assert any("myrepo" in token or token == "cd" for token in require), require
    assert "succeeded" not in require, "복구 문구 템플릿에서 뽑으면 서식을 재게 된다"
    assert "Bash" not in require


def test_incidental_numbers_are_not_required():
    """줄 번호나 개수 플래그는 그 순간에만 맞는 값이다."""
    from jermes.bench import _worth_requiring

    assert not _worth_requiring("470")
    assert not _worth_requiring("-25")
    assert not _worth_requiring("1.2")
    assert _worth_requiring("myrepo")
    assert _worth_requiring("--force")


def test_no_successful_retry_means_no_invented_requirement():
    """지어낸 요구조건은 없느니만 못하다. 그게 이 결함의 교훈이었다."""
    from jermes.bench import capture_repro_rows
    from jermes.model import RunTrace, TraceEvent

    trace = RunTrace(run_id="r2", scope="user", success=False, events=[
        TraceEvent(type="tool_call", name="Bash", ok=False,
                   detail="fatal: not a git repository",
                   meta={"input": "command=git commit"}),
    ])
    rows = capture_repro_rows(trace)
    assert rows and rows[0]["require"] == []
    assert rows[0]["forbid"], "금지 조건은 남아야 한다"

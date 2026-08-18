"""주제를 **측정이 정한다.** 모델이 인상으로 고르게 두지 않는다.

실측: 프롬프트에 "이런 것이 되풀이된다"를 횟수까지 적어 줬는데도 드래프터는 세 번
연속 트레이스에서 인상적이었던 것을 골랐다.

    safe-file-edit-with-anchor · schema-validation-with-empty-fallback
    detect-default-branch-before-git

셋 다 한 번 일어난 일이고 전부 dev +0.000 으로 거절됐다. 힌트는 힌트일 뿐이다.

그래서 되풀이된 해법마다 스킬을 하나씩 **시킨다.** 무엇에 대해 쓸지는 우리가 센
숫자가 정하고, 모델은 그 사실을 절차로 옮긴다.
"""

import json

import pytest

from jermes.bench import Expectation, ReplayCase, fix_examples
from jermes.drafter import TargetedDrafter
from jermes.model import RunTrace


def _trace():
    return RunTrace(run_id="r1", scope="user", scope_key="", events=[],
                    lessons=[], refined_memory="", judge_score=None, success=True)


def _case(cid, tool, error, require):
    return ReplayCase(case_id=cid,
                      payload={"tool": tool, "error_detail": error},
                      expect=Expectation(require=list(require), forbid=["boom"]))


POOL = [
    _case("c1", "Bash", "UnicodeEncodeError: cp949 codec", ["PYTHONIOENCODING"]),
    _case("c2", "Bash", "UnicodeEncodeError: illegal sequence", ["PYTHONIOENCODING"]),
    _case("c3", "Bash", "Exit code 128 not a git repository", ["git"]),
]


GOOD = json.dumps([{"name": "survive-cp949-console",
                    "when_to_use": "when a run dies on UnicodeEncodeError",
                    "rationale": "the console is cp949 and cannot draw the text",
                    "procedure": ["see that it is an encoding failure",
                                  "re-run with PYTHONIOENCODING=utf-8"],
                    "pitfalls": ["the file also needs encoding=utf-8"],
                    "verification": ["the command exits 0"]}])


def _says(answer):
    seen = []

    def complete(prompt):
        seen.append(prompt)
        return answer

    complete.prompts = seen
    return complete


def test_the_topic_comes_from_the_count_not_the_model():
    """모델은 무엇에 대해 쓸지 고르지 않는다. 그 자리는 측정이 가진다."""
    complete = _says(GOOD)
    out = TargetedDrafter(complete).draft(
        _trace(), [("PYTHONIOENCODING", 2)],
        lambda token: fix_examples(POOL, token))

    assert len(out) == 1
    assert out[0].provenance.signal == "recurring-fix:PYTHONIOENCODING"
    assert "PYTHONIOENCODING" in complete.prompts[0]


def test_the_model_is_shown_the_real_failures():
    """"이 기법을 2번 썼다"만으로는 스킬을 못 쓴다. 무엇이 어떻게 깨졌을 때
    그걸 썼는지를 봐야 "언제 쓰는가"를 적을 수 있다."""
    complete = _says(GOOD)
    TargetedDrafter(complete).draft(
        _trace(), [("PYTHONIOENCODING", 2)],
        lambda token: fix_examples(POOL, token))

    assert "UnicodeEncodeError" in complete.prompts[0]


def test_one_example_is_not_a_pattern():
    """두 번은 봐야 되풀이다. 하나로는 쓸 말이 없고, 지어내게 만들 뿐이다."""
    out = TargetedDrafter(_says(GOOD)).draft(
        _trace(), [("git", 1)], lambda token: fix_examples(POOL, token))
    assert out == []


def test_a_broken_answer_drops_that_topic_not_the_run():
    """한 주제가 깨져도 나머지는 계속 쓴다."""
    calls = []

    def flaky(prompt):
        calls.append(prompt)
        return "죄송합니다" if len(calls) == 1 else GOOD

    out = TargetedDrafter(flaky).draft(
        _trace(), [("PYTHONIOENCODING", 2), ("PYTHONIOENCODING", 2)],
        lambda token: fix_examples(POOL, token))
    assert len(out) == 1


def test_examples_are_pulled_from_the_cases_that_used_it():
    got = fix_examples(POOL, "PYTHONIOENCODING")
    assert len(got) == 2
    assert all("UnicodeEncodeError" in g for g in got)
    assert fix_examples(POOL, "git") == ["Bash: Exit code 128 not a git repository"]

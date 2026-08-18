"""벤치가 **이길 수 있는** 것을 요구한다.

실측한 결함(세션 200개, 재현 케이스 126건): 요구 낱말 148종 중 106종(72%)이 딱
한 번만 나왔다.

    harness_bridge/jermes.py · /d/xgen-maker · node_modules/typescript/bin/tsc

그때 그 자리의 경로다. 일반 스킬로는 절대 못 내고, 외워야만 통과하는데 홀드아웃은
정확히 **외운 것을 거절하려고** 있다. 이길 수 없는 벤치였고, 그래서 실세션 학습이
몇 번을 돌려도 승격 0건이었다.

이건 내가 만든 결함이다. 예전 요구조건은 `['Bash','succeeded']` 라는 **서식**이었고,
그걸 고치면서 입력 차이에서 낱말을 뽑게 했는데 너무 멀리 갔다. 서식에서 값으로
갔을 뿐, 그 사이에 있는 **기법**을 못 짚었다.

되풀이되는 것은 다르다: grep 9번 · utf-8 5번 · PYTHONIOENCODING 4번 · sed 4번.
`PYTHONIOENCODING` 이 네 번이라는 것은 이 사람이 인코딩 문제를 반복해서 겪고 매번
그걸로 고쳤다는 뜻이다. 초개인화가 배워야 할 것이 정확히 그거다.
"""

import pytest

from jermes.bench import (Expectation, ReplayCase, generalize_requirements,
                          recurring_fixes)


def _case(cid, tool, error, require, forbid=()):
    return ReplayCase(case_id=cid,
                      payload={"tool": tool, "error_detail": error},
                      expect=Expectation(require=list(require),
                                         forbid=list(forbid)))


def _pool():
    return [
        _case("c1", "Bash", "UnicodeEncodeError: cp949",
              ["PYTHONIOENCODING", "utf-8", "D:/one/off/path.py"],
              ["UnicodeEncodeError"]),
        _case("c2", "Bash", "UnicodeEncodeError: cp949",
              ["PYTHONIOENCODING", "utf-8", "/another/unique/file.txt"],
              ["UnicodeEncodeError"]),
        _case("c3", "Bash", "Exit code 128",
              ["git", "harness_bridge/jermes.py"], ["Exit code 128"]),
        _case("c4", "Bash", "Exit code 2",
              ["git", "node_modules/typescript/bin/tsc"], ["Exit code 2"]),
    ]


def test_one_off_values_are_not_required():
    """한 번뿐인 경로를 요구하면 외워야만 통과한다. 그런데 홀드아웃은 외운 것을
    거절하려고 있다 - 서로 모순이라 아무도 못 이긴다."""
    cases = generalize_requirements(_pool())
    every = {t for c in cases for t in c.expect.require}
    assert "D:/one/off/path.py" not in every
    assert "harness_bridge/jermes.py" not in every
    assert "node_modules/typescript/bin/tsc" not in every


def test_recurring_techniques_survive():
    """되풀이되는 것은 기법이다. 다음에도 쓴다."""
    cases = generalize_requirements(_pool())
    every = {t for c in cases for t in c.expect.require}
    assert {"PYTHONIOENCODING", "utf-8", "git"} <= every


def test_a_case_always_keeps_something_checkable():
    """요구가 다 걷혀도 `forbid` 는 남는다 - 그 실패 표식이 다시 나오면 안 된다.
    검사할 것이 하나도 없는 케이스는 평균만 눅이고 아무것도 못 잰다."""
    cases = generalize_requirements(_pool())
    for case in cases:
        assert case.expect.require or case.expect.forbid, case.case_id


def test_nothing_is_invented():
    """뽑을 것이 없으면 안 만든다. 지어낸 요구조건은 없느니만 못하다."""
    lonely = [_case("x1", "Bash", "boom", ["only-here"], ["boom"])]
    generalize_requirements(lonely)
    assert lonely[0].expect.require == []
    assert lonely[0].expect.forbid == ["boom"]


def test_the_drafter_is_told_what_actually_fixes_things():
    """무엇이 깨지는지만 알려 주면 모델은 좋은 **일반 조언**을 쓴다. 그건 옳지만
    벤치가 채점하는 것이 아니라, 잴 수 없는 스킬이 된다."""
    fixes = dict(recurring_fixes(generalize_requirements(_pool())))
    assert fixes.get("PYTHONIOENCODING") == 2
    assert fixes.get("git") == 2
    assert "D:/one/off/path.py" not in fixes


def test_relevance_can_see_what_a_case_demands():
    """실측: `PYTHONIOENCODING` 을 요구하는 케이스가 인코딩 스킬과 "관계없다"고
    판정돼, 6건짜리 주제가 2건으로 줄어 최소치에 못 미쳤다. 그 케이스가 무엇에
    관한 것인지는 오류 문구만큼이나 **무엇을 요구하는지**에 담겨 있다."""
    from jermes.gate import case_text, relevant_cases

    cases = generalize_requirements(_pool())
    bench = [c.as_bench_case() for c in cases]
    assert "PYTHONIOENCODING" in case_text(bench[0])

    topic = "force utf8 on windows console PYTHONIOENCODING 인코딩 실패"
    assert len(relevant_cases(bench, topic)) >= 2

"""빠져 있던 두 칸 - **비용 계정**과 **스킬 자동연동**.

능력 격자를 코드로 훑었더니 세 칸이 비어 있었다: 비용 계정, 비용 상한, 스킬 자동연동.
나머지(스킬 자동생성·테스트, 도구 자동생성·연동·테스트, 메모리 적재·자가개선)는 있었다.

**비용** - 우리는 라우팅·툴 검증·회귀검사·기억 회상에서 LLM 을 0회 부른다. 그건
자랑거리지만 부르는 자리에서 얼마나 쓰는지 모르면 "효율적"이 측정이 아니라 인상이 된다.

**자동연동** - 배운 것을 내보낼 수는 있었지만, 다른 에이전트가 **집는 자리**에 놓지는
못했다. 내보내기만으로는 사람이 옮겨야 하고, 옮기지 않으면 배운 것이 안 쓰인다.
"""

import json

import pytest

from jermes import cli
from jermes.drafter import Budget, BudgetExceeded, metered


# ------------------------------------------------- 얼마나 썼는가

def test_usage_is_counted_even_without_a_limit():
    """상한을 안 걸어도 센다 - 쓴 양을 모르는 것보다 아는 편이 늘 낫다."""
    budget = Budget()
    complete = metered(lambda prompt: "답" * 40, budget)
    complete("질문" * 30)
    assert budget.calls == 1 and budget.tokens > 0


def test_the_endpoint_usage_beats_our_guess():
    """어림(문자수/4)보다 엔드포인트가 준 숫자가 정확하다."""
    def complete(prompt):
        return "답"
    complete.last_usage = {"prompt_tokens": 123, "completion_tokens": 45}

    budget = Budget()
    metered(complete, budget)("무엇이든")
    assert budget.prompt_tokens == 123 and budget.completion_tokens == 45
    assert not budget.estimated


def test_a_guess_is_marked_as_a_guess():
    """엔드포인트가 usage 를 안 주는 경우가 있다. 어림했으면 어림했다고 말한다."""
    budget = Budget()
    metered(lambda p: "답" * 40, budget)("질문")
    assert budget.estimated and "어림" in budget.summary()


def test_money_is_unknown_not_zero_without_a_rate():
    """0 은 "안 썼다"로 읽힌다. 모르면 모른다고 해야 한다.
    그리고 요율을 코드에 박지 않는다 - 박으면 언젠가 조용히 틀린 금액을 보고한다."""
    budget = Budget()
    metered(lambda p: "답", budget)("질문")
    assert budget.usd is None and "미상" in budget.summary()

    priced = Budget(usd_per_1k=0.15)
    metered(lambda p: "답" * 400, priced)("질문" * 400)
    assert priced.usd and priced.usd > 0 and "$" in priced.summary()


@pytest.mark.parametrize("limit,expect", [
    ({"max_calls": 2}, "호출 상한"),
    ({"max_tokens": 50}, "토큰 상한"),
    ({"max_usd": 0.001, "usd_per_1k": 1.0}, "금액 상한"),
])
def test_each_limit_stops_before_the_next_call(limit, expect):
    """넘고 나서 알려주면 늦다. **부르기 전에** 멈춘다."""
    budget = Budget(**limit)
    complete = metered(lambda p: "답" * 200, budget)
    with pytest.raises(BudgetExceeded) as caught:
        for _ in range(20):
            complete("질문" * 100)
    assert expect in str(caught.value)


def test_a_money_limit_without_a_rate_cannot_fire():
    """요율이 없으면 금액을 모른다. 모르는 값으로 막으면 엉뚱하게 멈춘다."""
    budget = Budget(max_usd=0.0001)          # 요율 없음
    complete = metered(lambda p: "답" * 200, budget)
    for _ in range(5):
        complete("질문")                      # 안 터진다
    assert budget.calls == 5


def test_all_llm_paths_go_through_one_meter():
    """부르는 자리마다 세면 언젠가 한 곳을 빠뜨리고, 그 자리가 새는 자리가 된다."""
    import inspect

    source = inspect.getsource(cli.build_completer)
    assert "metered(" in source


# ------------------------------------------------- 배운 것이 쓰이는 자리로

def active_tool(ledger, name="adder", verified=True):
    from jermes.tools import (ToolCase, synthesize_tool_skill, verify_tool)

    add = "def run(payload):\n    return payload['a'] + 1\n"
    cases = [ToolCase(case_id=f"c{i}", payload={"a": i}, expect=i + 1) for i in range(12)]
    skill = synthesize_tool_skill(name, "1을 더한다", add, verify_tool(add, cases),
                                  cases=cases)
    skill.verified = verified
    skill.status = "active"
    ledger.commit(skill)
    return skill


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("JERMES_HOME", str(tmp_path / "home"))
    return tmp_path


def test_install_writes_where_other_agents_look(home, capsys):
    """내보내기만으로는 사람이 옮겨야 한다. 안 옮기면 배운 것이 안 쓰인다."""
    active_tool(cli.open_ledger())
    target = home / "skills"
    assert cli.main(["install", "--into", str(target)]) == 0
    assert (target / "adder" / "SKILL.md").exists()
    assert (target / "adder" / "scripts" / "tool.py").exists()


def test_what_was_installed_is_discovered_again(home, monkeypatch):
    """설치한 자리를 발견기가 읽어야 왕복이 닫힌다."""
    from jermes.discovery import SkillDirSource

    active_tool(cli.open_ledger())
    target = home / "skills"
    cli.main(["install", "--into", str(target)])
    found = SkillDirSource([target]).discover()
    assert [c.name for c in found] == ["adder"]
    assert found[0].kind == "tool"          # scripts/ 가 있으니 실행 가능한 것으로


def test_the_installed_script_runs_without_jermes(home):
    import subprocess
    import sys

    active_tool(cli.open_ledger())
    target = home / "skills"
    cli.main(["install", "--into", str(target)])
    done = subprocess.run([sys.executable, str(target / "adder" / "scripts" / "tool.py")],
                          input='{"a": 41}', capture_output=True, text=True, timeout=30)
    assert done.returncode == 0 and json.loads(done.stdout) == 42


def test_unverified_is_not_installed_by_default(home, capsys):
    """확인 안 된 것을 남의 도구 목록에 끼워 넣는 것은 우리가 하지 말자고 한 그것이다."""
    active_tool(cli.open_ledger(), name="unproven", verified=False)
    target = home / "skills"
    assert cli.main(["install", "--into", str(target)]) == 1
    assert not (target / "unproven").exists()
    assert "검증된 것만" in capsys.readouterr().out


def test_unverified_can_be_installed_but_it_says_so(home, capsys):
    active_tool(cli.open_ledger(), name="unproven", verified=False)
    target = home / "skills"
    assert cli.main(["install", "--into", str(target), "--all"]) == 0
    out = capsys.readouterr().out
    assert (target / "unproven").exists()
    assert "미검증" in out and "확인된 줄 압니다" in out


def test_installing_into_several_places_at_once(home):
    import os as _os

    active_tool(cli.open_ledger())
    first, second = home / "a", home / "b"
    cli.main(["install", "--into", f"{first}{_os.pathsep}{second}"])
    assert (first / "adder" / "SKILL.md").exists()
    assert (second / "adder" / "SKILL.md").exists()


def test_installing_one_by_name(home):
    ledger = cli.open_ledger()
    active_tool(ledger, name="keep")
    active_tool(ledger, name="skip")
    target = home / "skills"
    cli.main(["install", "keep", "--into", str(target)])
    assert (target / "keep").exists() and not (target / "skip").exists()


# ------------------------------------------------- 스모크에서 잡힌 것

def test_importing_your_own_export_does_not_erase_the_verification(home, capsys):
    """실측(smoke.py): 자기가 내보낸 SKILL.md 를 다시 들여왔더니 **검증된 tool 이
    미검증 guide 로 덮였다.** 들여오기는 항상 미검증으로 착지하므로, 같은 이름의
    검증된 기록 위에 얹으면 그 검증이 조용히 사라진다. 덮는 것은 사람이 정할 일이다."""
    active_tool(cli.open_ledger())
    out_dir = home / "out"
    cli.main(["export", "adder", "--out", str(out_dir)])
    capsys.readouterr()

    assert cli.main(["import", str(out_dir / "adder" / "SKILL.md")]) == 1
    message = capsys.readouterr().out
    assert "이미 있는 이름" in message and "--replace" in message

    record = cli.open_ledger().get("adder")
    assert record.skill.verified and record.skill.kind == "tool"


def test_importing_under_another_name_is_allowed(home, capsys):
    active_tool(cli.open_ledger())
    out_dir = home / "out"
    cli.main(["export", "adder", "--out", str(out_dir)])
    assert cli.main(["import", str(out_dir / "adder" / "SKILL.md"),
                     "--as", "adder-copy"]) == 0
    assert cli.open_ledger().get("adder").skill.verified          # 원본은 그대로
    assert not cli.open_ledger().get("adder-copy").skill.verified  # 사본은 미검증


def test_replace_is_possible_but_deliberate(home, capsys):
    active_tool(cli.open_ledger())
    out_dir = home / "out"
    cli.main(["export", "adder", "--out", str(out_dir)])
    assert cli.main(["import", str(out_dir / "adder" / "SKILL.md"), "--replace"]) == 0
    assert not cli.open_ledger().get("adder").skill.verified


def test_a_tool_without_a_task_warns_that_it_will_be_unfindable(home, tmp_path, capsys):
    """설명이 이름뿐인 툴은 만들어 두고 못 쓰는 물건이 된다."""
    cases_file = tmp_path / "c.csv"
    cases_file.write_text("a,b,expect\n" + "".join(f"{i},1,{i + 1}\n" for i in range(12)),
                          encoding="utf-8")
    script = tmp_path / "add.py"
    script.write_text("def run(p):\n    return p['a'] + p['b']\n", encoding="utf-8")

    cli.main(["tool", "silent", "--cases", str(cases_file), "--script", str(script)])
    assert "--task 가 없어" in capsys.readouterr().out


def test_watch_counts_only_the_rounds_that_actually_learned(home, monkeypatch, capsys):
    """`cmd_learn` 은 초안이 0건이어도 0 을 돌려준다(정상 종료다). 그걸 배움으로
    세면 화면에 `배움 1` 이 뜨는데 그 바퀴에서 원장은 한 줄도 안 늘었다.
    실측: 초안 0건으로 끝난 바퀴가 `배움 1` 로 보고됐다."""
    from jermes import cli

    seen = []

    def learn_nothing(args):
        seen.append(args.session)
        return 0                      # 정상 종료 - 그러나 배운 것은 없다

    class _Worth:
        worth_learning = True

    monkeypatch.setattr(cli, "cmd_learn", learn_nothing)
    monkeypatch.setattr(cli, "iter_session_files",
                        lambda root=None: [home / "s1.jsonl"])
    monkeypatch.setattr(cli, "summarize_session",
                        lambda path, max_lines=0: _Worth())
    (home / "s1.jsonl").write_text("{}\n", encoding="utf-8")

    cli.main(["watch", "--limit", "1", "--per-round", "1"])
    out = capsys.readouterr().out
    assert "배움 0" in out and "배운 것 없음 1" in out, out

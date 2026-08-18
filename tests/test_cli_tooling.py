"""명령줄에서 툴을 만들고 굴리는 길 - **처음 쓰는 사람이 설정 없이 결과까지 가는가.**

기능이 있어도 첫 화면에서 막히면 없는 것과 같다. 여기서 고정하는 것:
  ① 맨손 `jermes` 는 오류가 아니라 현황과 다음 한 줄
  ② 케이스 파일은 사람이 이미 가진 모양(CSV·JSONL·평평한 JSON)을 그대로 받는다
  ③ LLM 을 못 찾으면 조용히 대충 하지 않고 어디를 찾아봤는지 말한다
"""

import json

import pytest

from jermes import cli

ADD = "def run(payload):\n    return payload['a'] + payload['b']\n"


@pytest.fixture
def jhome(tmp_path, monkeypatch):
    monkeypatch.setenv("JERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JERMES_CLAUDE_PROJECTS", str(tmp_path / "no-sessions"))
    return tmp_path


def write(path, text):
    path.write_text(text, encoding="utf-8")
    return path


# ------------------------------------------------- 케이스 파일 읽기

def test_csv_is_read_as_cases_with_types_restored(tmp_path):
    """CSV 칸은 전부 문자열로 온다. "7" 을 기대값으로 두면 7 을 내는 툴이 영원히 틀린다."""
    path = write(tmp_path / "c.csv", "a,b,expect\n1,2,3\n10,20,30\n")
    cases = cli._read_cases(str(path))
    assert [c.payload for c in cases] == [{"a": 1, "b": 2}, {"a": 10, "b": 20}]
    assert [c.expect for c in cases] == [3, 30]


@pytest.mark.parametrize("column", ["expect", "expected", "정답", "결과", "RESULT"])
def test_the_expected_column_may_be_named_several_ways(tmp_path, column):
    path = write(tmp_path / "c.csv", f"a,b,{column}\n1,2,3\n")
    assert cli._read_cases(str(path))[0].expect == 3


def test_a_missing_expected_column_says_what_to_rename(tmp_path):
    path = write(tmp_path / "c.csv", "a,b,값\n1,2,3\n")
    with pytest.raises(SystemExit) as caught:
        cli._read_cases(str(path))
    assert "기대값 칸" in str(caught.value) and "expect" in str(caught.value)


def test_jsonl_and_flat_json_are_both_accepted(tmp_path):
    lines = write(tmp_path / "c.jsonl",
                  '{"a": 1, "b": 2, "expect": 3}\n{"a": 4, "b": 5, "expect": 9}\n')
    assert [c.expect for c in cli._read_cases(str(lines))] == [3, 9]
    flat = write(tmp_path / "c.json", json.dumps([{"a": 1, "b": 2, "expect": 3}]))
    assert cli._read_cases(str(flat))[0].payload == {"a": 1, "b": 2}


def test_the_two_json_forms_still_work(tmp_path):
    formal = write(tmp_path / "f.json",
                   json.dumps([{"payload": {"a": 1}, "expect": 2}]))
    assert cli._read_cases(str(formal))[0].expect == 2
    short = write(tmp_path / "s.json", json.dumps([[{"a": 1}, 2]]))
    assert cli._read_cases(str(short))[0].payload == {"a": 1}


def test_a_missing_case_file_is_named_not_traced(tmp_path):
    with pytest.raises(SystemExit) as caught:
        cli._read_cases(str(tmp_path / "nope.json"))
    assert "없습니다" in str(caught.value)


def test_a_csv_with_a_bom_still_parses(tmp_path):
    """엑셀이 저장한 CSV 는 BOM 이 붙는다 - 첫 칸 이름이 조용히 어긋난다."""
    path = tmp_path / "c.csv"
    path.write_text("a,expect\n1,2\n", encoding="utf-8-sig")
    assert cli._read_cases(str(path))[0].payload == {"a": 1}


# ------------------------------------------------- LLM 찾기

def test_a_discovered_endpoint_is_used_and_announced(monkeypatch, capsys):
    monkeypatch.delenv("JERMES_BASE_URL", raising=False)
    monkeypatch.delenv("JERMES_MODEL", raising=False)
    monkeypatch.setattr(cli, "discover_endpoint", lambda *a, **k: ("http://x/v1", "m1"))

    class Args:
        base_url = model = api_key = ""
        timeout = 5.0

    cli.build_completer(Args())
    assert "자동" in capsys.readouterr().out       # 무엇에 붙었는지 반드시 밝힌다


def test_no_endpoint_says_where_it_looked(monkeypatch):
    monkeypatch.delenv("JERMES_BASE_URL", raising=False)
    monkeypatch.delenv("JERMES_MODEL", raising=False)
    monkeypatch.setattr(cli, "discover_endpoint", lambda *a, **k: ("", ""))

    class Args:
        base_url = model = api_key = ""
        timeout = 5.0

    with pytest.raises(SystemExit) as caught:
        cli.build_completer(Args())
    assert "찾아본 곳" in str(caught.value)
    assert cli.LOCAL_ENDPOINTS[0] in str(caught.value)


def test_discovery_survives_a_dead_port(monkeypatch):
    """탐색이 예외로 죽으면 첫 실행이 traceback 으로 끝난다."""
    monkeypatch.setattr(cli, "LOCAL_ENDPOINTS", ("http://127.0.0.1:9/v1",))
    assert cli.discover_endpoint(timeout=0.2) == ("", "")


# ------------------------------------------------- 맨손 실행

def test_bare_jermes_reports_status_instead_of_erroring(jhome, capsys, monkeypatch):
    monkeypatch.setattr(cli, "discover_endpoint", lambda *a, **k: ("", ""))
    assert cli.main([]) == 0
    out = capsys.readouterr().out
    assert "원장" in out and "다음에 할 것" in out
    assert "jermes demo" in out                    # 아무것도 없을 때의 첫 한 줄


def test_status_offers_running_a_tool_once_one_exists(jhome, capsys, monkeypatch):
    monkeypatch.setattr(cli, "discover_endpoint", lambda *a, **k: ("", ""))
    cases = write(jhome / "c.csv", "a,b,expect\n" + "".join(
        f"{i},1,{i + 1}\n" for i in range(12)))
    script = write(jhome / "t.py", ADD)
    assert cli.main(["tool", "adder", "--cases", str(cases), "--script", str(script)]) == 0
    capsys.readouterr()
    cli.main([])
    assert "jermes run adder" in capsys.readouterr().out


# ------------------------------------------------- 툴 만들기·굴리기 (LLM 없이)

def test_an_existing_script_can_be_verified_without_an_llm(jhome, capsys):
    cases = write(jhome / "c.csv", "a,b,expect\n" + "".join(
        f"{i},1,{i + 1}\n" for i in range(12)))
    script = write(jhome / "t.py", ADD)
    assert cli.main(["tool", "adder", "--cases", str(cases), "--script", str(script)]) == 0
    out = capsys.readouterr().out
    assert "promoted" in out and "검증 True" in out


def test_a_failing_script_exits_nonzero_and_is_not_marked_verified(jhome, capsys):
    cases = write(jhome / "c.csv", "a,b,expect\n" + "".join(
        f"{i},1,{i + 2}\n" for i in range(12)))          # 기대값이 어긋난다
    script = write(jhome / "t.py", ADD)
    assert cli.main(["tool", "adder", "--cases", str(cases), "--script", str(script)]) == 1
    out = capsys.readouterr().out
    assert "rejected" in out and "검증 False" in out
    assert "입력" in out                                  # 무엇이 틀렸는지 보여준다


def test_no_holdout_is_honest_about_being_only_staged(jhome, capsys):
    """예시가 명세 전부라고 선언해도 '검증됨'이 되지는 않는다."""
    cases = write(jhome / "c.csv", "a,b,expect\n" + "".join(
        f"{i},1,{i + 1}\n" for i in range(12)))
    script = write(jhome / "t.py", ADD)
    cli.main(["tool", "adder", "--cases", str(cases), "--script", str(script),
              "--no-holdout"])
    out = capsys.readouterr().out
    assert "staged" in out and "감춘 검증 0" in out


def test_a_generalisation_failure_tells_the_user_what_to_do(jhome, capsys):
    from jermes.tools import split_cases, ToolCase

    rows = [(i, 1) for i in range(12)]
    cases_all = [ToolCase(case_id=f"case-{i}") for i in range(12)]
    _, held = split_cases(cases_all)
    held_index = {int(c.case_id.split("-")[1]) for c in held}
    lines = "".join(f"{a},{b},{a + b + (100 if i in held_index else 0)}\n"
                    for i, (a, b) in enumerate(rows))
    cases = write(jhome / "c.csv", "a,b,expect\n" + lines)
    script = write(jhome / "t.py", ADD)
    cli.main(["tool", "adder", "--cases", str(cases), "--script", str(script)])
    out = capsys.readouterr().out
    assert "감춰둔 것에서 틀렸습니다" in out and "--no-holdout" in out


def test_a_forged_tool_can_be_run_by_name(jhome, capsys):
    cases = write(jhome / "c.csv", "a,b,expect\n" + "".join(
        f"{i},1,{i + 1}\n" for i in range(12)))
    script = write(jhome / "t.py", ADD)
    cli.main(["tool", "adder", "--cases", str(cases), "--script", str(script)])
    capsys.readouterr()
    assert cli.main(["run", "adder", "--payload", '{"a": 40, "b": 2}']) == 0
    assert "42" in capsys.readouterr().out


def test_running_something_that_is_not_a_tool_says_so(jhome, capsys):
    assert cli.main(["run", "없는-것", "--payload", "{}"]) == 1
    assert "툴이 아니거나 없습니다" in capsys.readouterr().out


def test_the_exported_tool_package_carries_the_evidence(jhome, capsys):
    cases = write(jhome / "c.csv", "a,b,expect\n" + "".join(
        f"{i},1,{i + 1}\n" for i in range(12)))
    script = write(jhome / "t.py", ADD)
    out_dir = jhome / "pkg"
    cli.main(["tool", "adder", "--cases", str(cases), "--script", str(script),
              "--out", str(out_dir)])
    skill_md = (out_dir / "adder" / "SKILL.md").read_text(encoding="utf-8")
    assert "dev" in skill_md and "holdout" in skill_md      # 근거가 같이 나간다
    assert (out_dir / "adder" / "scripts" / "tool.py").exists()


# 빈 파일은 세션이 아니다(원천이 크기 0을 거른다).
JSON_LINE = "{}" + chr(10)


# ------------------------------------------------- watch: 계속 배우기

def test_watch_never_learns_the_same_session_twice(tmp_path, monkeypatch, capsys):
    """자동으로 도는 것은 규율이 없으면 사고다. 첫째 규율: 두 번 안 배운다."""
    import json as _json

    from jermes import cli

    home = tmp_path / "home"
    monkeypatch.setenv("JERMES_HOME", str(home))
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    # 빈 파일은 세션이 아니다(원천이 크기 0을 거른다).
    (sessions / "a.jsonl").write_text(JSON_LINE, encoding="utf-8")
    (sessions / "b.jsonl").write_text(JSON_LINE, encoding="utf-8")
    monkeypatch.setenv("JERMES_CLAUDE_PROJECTS", str(sessions))

    learned: list[str] = []
    monkeypatch.setattr(cli, "cmd_learn", lambda args: learned.append(args.session))

    class Worth:
        worth_learning = True

    monkeypatch.setattr(cli, "summarize_session", lambda p, **k: Worth())

    assert cli.main(["watch", "--per-round", "5"]) == 0
    first = list(learned)
    assert len(first) == 2, "새 세션은 배워야 한다"

    assert cli.main(["watch", "--per-round", "5"]) == 0
    assert learned == first, "이미 본 세션을 다시 배우면 안 된다"
    seen = _json.loads((home / "watched.json").read_text(encoding="utf-8"))
    assert len(seen) == 2


def test_watch_stops_at_the_budget(tmp_path, monkeypatch):
    """예산에 닿으면 조용히 계속하지 않는다."""
    from jermes import cli
    from jermes.drafter import BudgetExceeded

    monkeypatch.setenv("JERMES_HOME", str(tmp_path / "home"))
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "a.jsonl").write_text(JSON_LINE, encoding="utf-8")
    monkeypatch.setenv("JERMES_CLAUDE_PROJECTS", str(sessions))

    class Worth:
        worth_learning = True

    monkeypatch.setattr(cli, "summarize_session", lambda p, **k: Worth())

    def broke(args):
        raise BudgetExceeded("상한 도달")

    monkeypatch.setattr(cli, "cmd_learn", broke)
    assert cli.main(["watch"]) == 1

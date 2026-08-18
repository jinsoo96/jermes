"""요구조건은 **명령의 자리**에서 나온다.

도구 입력 한 줄에는 `content=` 로 파일 전문 앞머리가 같이 실린다. 그걸 통째로
낱말로 쪼개면 그때 쓴 파일 내용이 요구조건이 된다. 실측(세션 40개)으로 올라온
"되풀이된 해법" 상위가 이랬다:

    grep 17 · **def 12** · **src 11** · echo 10 · **node 8** · **class 5**
    · **pos 5** · **import 4** · C:/Users/.../scratchpad 7

굵은 것은 그때 쓴 파이썬 파일과 경로 조각이다. 다음에 또 쓸 기법이 아니라서, 이걸
주제로 받은 드래프터는 잴 수 없는 스킬을 쓴다 - 초안 6건이 전부 dev +0.000 으로
거절됐다. 같은 세션에서 명령의 자리만 보면:

    head 20 · grep 17 · python 13 · PYTHONIOENCODING 11 · echo 10 · docker 9
    · utf-8 9 · sed 7
"""

from jermes.bench import _operative_tokens


def test_file_content_is_not_a_technique():
    """`Write` 로 옮긴 짐은 한 일이 아니다. 그 파일에 `def` 가 있었다는 사실은
    다음에 또 쓸 기법이 아니라 그때의 내용이다. 자리 이름은 남지만(무엇을 골랐는지
    는 뜻이 있다) 실패·성공 양쪽에 다 있으므로 차집합에서 지워진다."""
    written = ("file_path=D:/x/y.py content=import re\n"
               "def go(): pass\nclass A:\n" + "x = 1\n" * 20)
    got = _operative_tokens(written)
    assert not ({"def", "class", "import"} & set(got)), got
    assert not [t for t in got if "/" in t], got


def test_command_head_and_env_survive():
    """기법이 앉는 자리는 환경변수 이름과 명령 머리다."""
    got = _operative_tokens("command=cd repo && PYTHONIOENCODING=utf-8 python -m pytest -q")
    assert "PYTHONIOENCODING" in got and "python" in got


def test_powershell_env_is_the_same_act():
    """`$env:NAME='v'` 와 `NAME=v` 는 같은 일이다. 한쪽만 읽으면 윈도우에서 쓴
    인코딩 대처가 통째로 안 잡힌다(실측: PYTHONIOENCODING 8 -> 0)."""
    got = _operative_tokens("command=$env:PYTHONIOENCODING='utf-8'; grep -rn foo src/")
    assert got[:3] == ["PYTHONIOENCODING", "utf-8", "grep"]


def test_a_path_valued_env_is_not_a_technique():
    """환경변수 **값**도 뜻을 가질 때가 있지만(`utf-8`), 경로는 그때 그 자리의
    값이다. 실측: `JERMES_HOME=C:/Users/.../scratchpad` 가 7번 올라와 요구조건
    행세를 했다 - 외워야만 통과하는데 홀드아웃은 외운 것을 거절한다."""
    got = _operative_tokens("command=JERMES_HOME=C:/Users/w/tmp python x.py")
    assert got == ["JERMES_HOME", "python"]


def test_short_flags_are_the_moment_not_the_method():
    """`-n 20` 의 `-n` 은 그때 몇 줄을 봤는가다. 이름 있는 플래그만 남긴다."""
    got = _operative_tokens("command=head -20 out.log timeout=5000 description=본다")
    assert got[0] == "head" and "-20" not in got, got
    assert "--profile" in _operative_tokens("command=docker compose --profile dev up -d")


def test_an_env_assignment_does_not_cut_the_command():
    """도구 파라미터 이름은 소문자고 환경변수는 대문자다. 안 가르면 명령 안의
    `PYTHONIOENCODING=` 이 새 필드로 보여 명령이 그 앞에서 잘린다."""
    both = _operative_tokens(
        "command=PYTHONUTF8=1 python -m jermes.cli status description=상태")
    assert both[:2] == ["PYTHONUTF8", "python"]


def test_a_parameter_you_added_is_a_choice():
    """명령이 아닌 자리에서도 **고른 것**은 나온다. `Edit` 가 실패해서 먼저 읽도록
    바꿨다면 그 파라미터가 해법이다 - 다음에도 쓴다."""
    got = _operative_tokens("old_string=missing read_first=yes")
    assert "read_first" in got, got


def test_a_two_letter_command_is_still_an_act():
    """세 글자 규칙은 줄 번호 같은 **값**을 걸러내려던 것이다. `cd` 는 값이 아니다.
    실측: `git commit` 이 실패하고 `cd myrepo && git commit` 으로 통과한 자리에서
    요구조건이 통째로 비었다."""
    assert "cd" in _operative_tokens("command=cd myrepo && git commit -m x")


def test_a_description_sentence_is_not_a_choice():
    """명령이 아닌 자리의 값이라도 **띄어쓰기가 있으면 글이다.** 실측: Bash 의
    `description=` 에 적은 설명문이 쪼개져 `Run` `across` `case` `Verify` 가
    요구조건으로 올라왔고, 그런 케이스는 어떤 스킬로도 못 이긴다."""
    got = _operative_tokens("command=ls description=Run tests across the case")
    assert not ({"Run", "across", "case", "tests"} & set(got)), got
    assert "ls" in got

"""안전 등급은 **증명**에서 나온다. 금지 목록이 비었다는 사실에서 나오지 않는다.

실측한 결함: `os.open`+`os.write` 로 파일을 쓰는 툴이 `--policy strict`(권한 0)로
`safe · 검증됨` 을 받고, MCP 로 `readOnlyHint: true` 라고 광고되고, 다단계 루프에서
승인 없이 실행되어 임시 디렉터리 **밖** 절대경로에 파일을 썼다.

목록에 `os.open` 을 추가하는 것은 답이 아니었다. `Path.write_text`, `shutil` 별칭,
`open(mode=)` 을 이미 그렇게 하나씩 막아 왔고 이번이 네 번째다. 목록으로는 끝나지
않는다. 그래서 질문을 뒤집었다: "나쁜 이름이 있는가"(끝없다) 대신 **"순수 계산뿐임을
증명할 수 있는가"**(닫혀 있다).

우리는 남의 MCP 도구에 "주석 없는 것은 위험한 게 아니라 **모르는 것**" 이라는
원칙을 쓴다. 우리 툴에만 "모르면 안전" 을 쓰고 있었다. 그 비대칭이 결함이었다.
"""

import json

import pytest

from jermes import cli
from jermes.tools import ToolCase, purity_scan, synthesize_tool_skill, verify_tool

SNEAKY = ("import os\n"
          "def run(payload):\n"
          "    fd = os.open(payload['path'], os.O_WRONLY | os.O_CREAT | os.O_TRUNC)\n"
          "    os.write(fd, payload['text'].encode('utf-8'))\n"
          "    os.close(fd)\n"
          "    return {'n': len(payload['text'])}\n")
CLEAN = "def run(payload):\n    return payload['a'] + payload['b']\n"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("JERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JERMES_SKILL_PATH", str(tmp_path / "none"))
    monkeypatch.setenv("JERMES_SOURCES", "own")
    return tmp_path


# --- 증명할 수 있는가 ---------------------------------------------------------

@pytest.mark.parametrize("label,script", [
    ("순수 덧셈", CLEAN),
    ("math", "import math\ndef run(p):\n    return math.sqrt(p['x'])\n"),
    ("json+re", "import json, re\ndef run(p):\n    return re.findall('x', p['s'])\n"),
])
def test_pure_computation_is_provable(label, script):
    assert purity_scan(script) == "", label


@pytest.mark.parametrize("label,script", [
    ("os.open 우회", SNEAKY),
    ("Path.write_text", "from pathlib import Path\ndef run(p):\n    Path(p['x']).write_text('y')\n"),
    ("shutil 별칭", "import shutil as sh\ndef run(p):\n    sh.copy(p['a'], p['b'])\n"),
    ("open()", "def run(p):\n    open(p['f'], 'w').write('x')\n"),
    ("eval", "def run(p):\n    return eval(p['code'])\n"),
    ("dunder 우회", "def run(p):\n    return ().__class__.__bases__[0].__subclasses__()\n"),
    ("socket", "import socket\ndef run(p):\n    socket.socket()\n"),
    ("subprocess", "import subprocess\ndef run(p):\n    subprocess.run(p['c'])\n"),
])
def test_anything_we_cannot_prove_says_so(label, script):
    """빠뜨려도 **안전한 쪽으로** 틀린다. 금지 목록은 빠뜨리면 위험한 쪽으로
    틀렸다 - 그게 os.open 이 통과한 이유다."""
    assert purity_scan(script), label


def test_unparsable_source_is_unknown_not_safe():
    assert purity_scan("def run(:\n  ???") != ""


# --- 그 판정이 실제로 흐르는가 -------------------------------------------------

def _forge(name, script, payloads, task):
    cases = [ToolCase(case_id=f"c{i}", payload=p, expect=None)
             for i, p in enumerate(payloads, 1)]
    skill = synthesize_tool_skill(name, task, script,
                                  verify_tool(script, cases), cases=cases)
    cli.open_ledger().commit(skill)
    return skill


def test_an_unproven_tool_is_not_called_safe(home, tmp_path):
    """등급이 거짓이면 그 아래 모든 판단이 조용히 거짓이 된다."""
    from jermes.discovery import LedgerSource

    _forge("file-writer", SNEAKY,
           [{"path": str(tmp_path / f"f{i}.txt"), "text": "x" * i} for i in range(1, 7)],
           "텍스트를 파일에 쓴다")
    _forge("adder", CLEAN, [{"a": i, "b": 1} for i in range(1, 7)], "두 수를 더한다")

    caps = {c.name: c for c in LedgerSource(cli.open_ledger()).discover()}
    assert caps["adder"].risk() == "safe", "증명된 것까지 깎으면 안 된다"
    assert caps["file-writer"].risk() == "caution"
    assert caps["file-writer"].annotated is False, "모른다고 말해야 한다"


def test_an_unproven_tool_is_not_advertised_read_only(home, tmp_path):
    """남에게 하는 주장이 제일 무겁다 - 받는 쪽은 그 말을 믿고 승인 없이 부른다."""
    from jermes.mcp_server import describe, servable

    _forge("file-writer", SNEAKY,
           [{"path": str(tmp_path / f"f{i}.txt"), "text": "x" * i} for i in range(1, 7)],
           "텍스트를 파일에 쓴다")
    _forge("adder", CLEAN, [{"a": i, "b": 1} for i in range(1, 7)], "두 수를 더한다")

    served = {n: describe(rec, man)
              for n, (rec, man) in servable(cli.open_ledger(), True).items()}
    assert served["adder"]["annotations"]["readOnlyHint"] is True
    assert served["file-writer"]["annotations"]["readOnlyHint"] is False


def test_an_unproven_tool_asks_before_running(home, tmp_path):
    """다단계 루프에서 승인을 건너뛰는 것이 제일 아픈 결과였다."""
    _forge("file-writer", SNEAKY,
           [{"path": str(tmp_path / f"f{i}.txt"), "text": "x" * i} for i in range(1, 7)],
           "텍스트를 파일에 쓴다")
    _forge("adder", CLEAN, [{"a": i, "b": 1} for i in range(1, 7)], "두 수를 더한다")

    args = cli.build_parser().parse_args(["ask", "무엇이든"])
    offers = {o.name: o for o in cli._offers_for(args, cli._registry(args))}
    assert offers["adder"].read_only is True, "증명된 것은 안 묻는다"
    assert offers["file-writer"].read_only is False, "모르는 것은 묻는다"


def test_a_declared_permission_is_still_knowledge(home, tmp_path):
    """권한을 **선언한** 툴은 영향이 없어야 한다. 선언이 곧 지식이다."""
    from jermes.discovery import LedgerSource
    from jermes.tools import ToolPolicy

    script = ("from pathlib import Path\n"
              "def run(payload):\n"
              "    Path(payload['path']).write_text(payload['text'], encoding='utf-8')\n"
              "    return {'ok': True}\n")
    cases = [ToolCase(case_id=f"c{i}",
                      payload={"path": str(tmp_path / f"d{i}.txt"), "text": "x"},
                      expect=None) for i in range(1, 7)]
    policy = ToolPolicy(allow_write=True)
    cli.open_ledger().commit(synthesize_tool_skill(
        "writer", "파일에 쓴다", script, verify_tool(script, cases, policy=policy),
        cases=cases, policy=policy))

    cap = {c.name: c for c in LedgerSource(cli.open_ledger()).discover()}["writer"]
    assert cap.annotated is True, "선언했으면 우리가 아는 것이다"
    assert cap.read_only is False and cap.risk() == "caution"


def test_the_manifest_carries_the_verdict(home, tmp_path):
    """나중에 코드를 다시 파싱하지 않아도 되고, 패키지로 나갈 때 받는 쪽도 본다."""
    _forge("file-writer", SNEAKY,
           [{"path": str(tmp_path / f"f{i}.txt"), "text": "x"} for i in range(1, 7)],
           "텍스트를 파일에 쓴다")
    manifest = json.loads(cli.open_ledger().get("file-writer").skill.body)
    assert "os" in manifest["purity"]


# --- 실행 중에도 막는다 -------------------------------------------------------
# 등급을 바로잡아도 **파일이 실제로 써지는 것**은 정적 검사로 못 막는다. 이름을
# 어떻게 쓰든 파이썬 런타임에서는 결국 같은 함수에 닿기 때문이다. 그래서 닿는
# 그 지점에서 막는다.

def test_a_zero_permission_tool_cannot_write_outside_at_runtime(tmp_path):
    """실측: `os.open`+`os.write` 가 정적 검사를 통과해 임시 디렉터리 밖 절대경로에
    파일을 만들었다. 이제 런타임이 막는다."""
    from jermes.tools import ToolPolicy, run_tool

    target = tmp_path / "ESCAPED.txt"
    outcome = run_tool(SNEAKY, {"path": str(target), "text": "escaped"},
                       policy=ToolPolicy())
    assert not outcome.ok
    assert "허락받은 곳 밖" in (outcome.error or "")
    assert not target.exists(), "막았다면서 파일이 생겼다"


def test_the_guard_does_not_break_ordinary_tools():
    """막는 것이 목적이 아니라 **모르는 채로 통과시키지 않는 것**이 목적이다."""
    from jermes.tools import run_tool

    assert run_tool(CLEAN, {"a": 1, "b": 2}).output == 3
    script = ("import json, re\n"
              "def run(p):\n"
              "    return re.findall('x', p['s'])\n")
    got = run_tool(script, {"s": "axbx"})
    assert got.ok and got.output == ["x", "x"]


def test_reading_outside_is_not_blocked(tmp_path):
    """막는 것은 **쓰기**다. 표준 라이브러리가 자기 모듈을 읽어야 돌고, 읽기는
    `allow_write`·`allow_delete` 라는 이름의 관심사가 아니다."""
    from jermes.tools import run_tool

    source = tmp_path / "in.txt"
    source.write_text("hello", encoding="utf-8")
    script = ("def run(p):\n"
              "    return {'n': len(open(p['f'], encoding='utf-8').read())}\n")
    got = run_tool(script, {"f": str(source)})
    assert got.ok and got.output == {"n": 5}


def test_a_declared_write_is_not_confined(tmp_path):
    """허락은 허락이다. 정산서를 사용자가 고른 경로에 떨구는 것이 그 툴의 목적일
    수 있다. 여기서 지키는 것은 허락 안 한 툴이 몰래 쓰지 않는 것이다."""
    from jermes.tools import ToolPolicy, run_tool

    target = tmp_path / "report.txt"
    script = ("from pathlib import Path\n"
              "def run(p):\n"
              "    Path(p['f']).write_text('hi', encoding='utf-8')\n"
              "    return {'ok': True}\n")
    got = run_tool(script, {"f": str(target)}, policy=ToolPolicy(allow_write=True))
    assert got.ok and target.read_text(encoding="utf-8") == "hi"


def test_a_common_word_does_not_ban_a_skill():
    """`token` 은 자격증명 낱말이기도 하지만 토크나이저·JSON 토큰·편집 토큰처럼
    무관한 자리에 훨씬 자주 나온다. 맨낱말로 막으면 멀쩡한 스킬이 조용히 죽는다
    (실측: `read-before-edit` 이 본문에 "token" 한 번 나왔다고 거절됐다).

    **막는 일은 바닥이 이미 한다** - 값이 대입된 모양은 규약 파일로도 못 끈다."""
    from jermes.constitution import Constitution

    law = Constitution()
    assert law.check_text("Edit 의 old_string 토큰이 안 맞으면 먼저 읽는다") is None
    assert law.check_text("count the tokens before sending") is None
    assert law.check_text("access token 을 기억해 둔다") is not None
    assert law.check_text("api_key: sk-abc12345 를 쓴다") is not None
    assert law.check_text("password 는 hunter2 다") is not None


def test_a_timeout_does_not_burn_the_remaining_attempts():
    """일시적인 호출 실패는 재시도가 있어야 할 자리다. 실측: 도구를 단조하는 중에
    타임아웃이 한 번 나서 `--attempts 3` 을 준 도구가 1회로 끝나고 staged 가 됐다 -
    모델이 못 쓴 것이 아니라 물어보지도 못한 것이다."""
    from jermes.tools import ToolCase, draft_tool

    tries = []

    def flaky(prompt):
        tries.append(prompt)
        if len(tries) == 1:
            raise TimeoutError("느림")
        return "def run(payload):\n    return payload['a'] + payload['b']\n"

    cases = [ToolCase(case_id=f"c{i}", payload={"a": i, "b": 1}, expect=i + 1)
             for i in range(8)]
    _, report, attempts = draft_tool(flaky, "두 수를 더한다", cases, max_attempts=3)
    assert len(tries) == 2, attempts
    assert report.verdict == "promoted", (report.verdict, attempts)

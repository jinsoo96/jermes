"""권한·오라클 검증·개선 루프 - **한계를 없앤 자리들**이 실제로 열렸는가.

예전 구조의 한계 셋을 여기서 하나씩 깬다.
  ① 파일 쓰기·네트워크를 무조건 막아서 쓸모 있는 툴의 절반을 못 만들었다 → 권한 선언
  ② 정답이 하나로 떨어지는 절차만 툴이 될 수 있었다 → 모양·성질 검사
  ③ 한 번 만든 툴은 그대로 굳었다 → 회귀검사 + 고쳐쓰기

단 열되 헐겁게 열지 않는다. 허락하지 않은 것은 여전히 막히고, 아무것도 주장하지 않는
케이스는 통과하지 못하며, 고친 결과가 나쁘면 버린다.
"""

import json

import pytest

from jermes.tools import (
    ToolCase, ToolPolicy, ToolReport, improve_tool, load_cases, run_tool,
    safety_scan, synthesize_tool_skill, tool_package, verify_tool,
)

ADD = "def run(payload):\n    return payload['a'] + payload['b']\n"


def cases(n=12, offset=0):
    return [ToolCase(case_id=f"case-{i}", payload={"a": i, "b": offset},
                     expect=i + offset) for i in range(n)]


# ------------------------------------------------- 권한 (금지가 아니라 동의)

def test_a_forbidden_action_becomes_allowed_when_granted():
    """예전엔 파일 쓰기를 무조건 막아서 결과를 파일로 떨구는 툴을 아예 못 만들었다.
    막는 대신 선언하게 하고 허락한다 - 그게 한계를 없애는 방식이다."""
    script = "def run(p):\n    open('out.txt', 'w').write('x')\n    return 'ok'\n"
    assert "파일 쓰기" in safety_scan(script)                       # 기본은 여전히 막힘
    assert safety_scan(script, ToolPolicy(allow_write=True)) == ""  # 허락하면 통과


def test_granting_one_permission_does_not_grant_the_others():
    net = "import requests\ndef run(p): return 1\n"
    assert safety_scan(net, ToolPolicy(allow_write=True)) != ""
    assert safety_scan(net, ToolPolicy(allow_network=True)) == ""


def test_the_refusal_says_how_to_allow_it():
    """막기만 하고 방법을 안 알려주면 사용자는 포기한다."""
    reason = safety_scan("import socket\ndef run(p): return 1\n")
    assert "allow_network=True" in reason


def test_a_write_permitted_tool_writes_but_only_in_the_scratch_dir(tmp_path, monkeypatch):
    """허락은 써도 된다는 뜻이지 아무 데나 써도 된다는 뜻이 아니다."""
    monkeypatch.chdir(tmp_path)
    script = ("def run(p):\n    open('made.txt', 'w').write('hi')\n"
              "    return open('made.txt').read()\n")
    result = run_tool(script, {}, policy=ToolPolicy(allow_write=True))
    assert result.ok and result.output == "hi"
    assert not (tmp_path / "made.txt").exists()      # 프로젝트에는 안 남는다


def test_secrets_are_withheld_unless_named_explicitly(monkeypatch):
    monkeypatch.setenv("MY_TOKEN", "secret-value")
    monkeypatch.setenv("OTHER_TOKEN", "should-not-leak")
    script = ("import os\ndef run(p):\n"
              "    return [os.environ.get('MY_TOKEN', ''), "
              "os.environ.get('OTHER_TOKEN', '')]\n")
    result = run_tool(script, {}, policy=ToolPolicy(env_allowlist=("MY_TOKEN",)))
    assert result.ok and result.output == ["secret-value", ""]


def test_an_unknown_policy_name_is_refused_with_the_options():
    with pytest.raises(ValueError) as caught:
        ToolPolicy.preset("무적모드")
    assert "strict" in str(caught.value)


def test_the_policy_survives_a_save_and_reload():
    """실행할 때 권한을 몰래 넓히거나 좁히면 안 된다."""
    policy = ToolPolicy.preset("files", env_allowlist=("K",))
    back = ToolPolicy.from_dict(policy.to_dict())
    assert back.granted() == policy.granted() and back.env_allowlist == ("K",)
    # 나중에 필드가 늘어도 옛 기록이 터지지 않아야 한다
    assert ToolPolicy.from_dict({"allow_write": True, "미래필드": 1}).allow_write
    assert ToolPolicy.from_dict(None).granted() == []


def test_the_permissions_travel_with_the_tool():
    policy = ToolPolicy.preset("network")
    skill = synthesize_tool_skill("fetcher", "환율을 받아온다", ADD,
                                  verify_tool(ADD, cases()), policy=policy)
    manifest = json.loads(skill.body)
    assert manifest["annotations"]["open_world"] is True
    assert "allow_network" in manifest["permissions"]
    assert "allow_network" in tool_package(skill)["fetcher/SKILL.md"]


# ------------------------------------------------- 정답이 하나가 아닌 절차

def test_a_case_can_demand_a_shape_instead_of_an_exact_value():
    """요약·포맷팅처럼 정답이 하나로 안 떨어지는 절차도 툴이 될 수 있어야 한다."""
    script = "def run(p):\n    return f\"[{p['tag']}] {p['text'][:10]}\"\n"
    shaped = [ToolCase(case_id=f"case-{i}", payload={"tag": "INFO", "text": "가" * 30},
                       match=r"^\[INFO\] ") for i in range(12)]
    assert verify_tool(script, shaped).verdict == "promoted"


def test_a_shape_that_does_not_match_still_fails():
    script = "def run(p):\n    return '엉뚱한 출력'\n"
    shaped = [ToolCase(case_id=f"case-{i}", payload={}, match=r"^\[INFO\] ")
              for i in range(12)]
    report = verify_tool(script, shaped)
    assert report.verdict == "rejected" and "정규식" in report.failures[0]


def test_a_case_can_demand_a_property():
    script = "def run(p):\n    return sorted(p['items'])\n"

    def sorted_ascending(output):
        """정렬되어 있을 것"""
        return output == sorted(output)

    props = [ToolCase(case_id=f"case-{i}", payload={"items": [3, 1, 2, i]},
                      check=sorted_ascending) for i in range(12)]
    assert verify_tool(script, props).verdict == "promoted"


def test_a_case_that_asserts_nothing_fails_instead_of_passing_for_free():
    """통과를 공짜로 주면 검증 전체가 무의미해진다."""
    empty = ToolCase(case_id="c", payload={})
    empty._has_expect = False
    passed, why = empty.judge("아무거나")
    assert not passed and "아무것도 주장하지 않습니다" in why


def test_expect_none_is_still_a_real_expectation():
    """null 을 내야 한다는 것은 정당한 기대다. 기대 없음과 헷갈리면 안 된다."""
    case = ToolCase(case_id="c", payload={}, expect=None)
    assert case.judge(None)[0] and not case.judge("무언가")[0]


def test_a_property_check_that_explodes_is_a_failure_not_a_crash():
    def broken(output):
        raise ValueError("판정 불가")

    passed, why = ToolCase(case_id="c", check=broken).judge(1)
    assert not passed and "터짐" in why


def test_expect_and_shape_can_both_be_demanded():
    """둘 다 걸면 둘 다 만족해야 한다 - 약한 쪽으로 새지 않는다."""
    case = ToolCase(case_id="c", expect="ABC", match=r"^A")
    assert case.judge("ABC")[0]
    assert not case.judge("ABD")[0]


# ------------------------------------------------- 회귀와 개선

def test_a_tool_carries_its_own_cases_so_it_can_be_rechecked_later():
    """증거가 따라다녀야 나중에 아무 때나 회귀검사를 할 수 있다."""
    skill = synthesize_tool_skill("adder", "더한다", ADD, verify_tool(ADD, cases()),
                                  cases=cases())
    loaded = load_cases(skill)
    assert len(loaded) == 12
    assert verify_tool(ADD, loaded).verdict == "promoted"


def test_a_property_case_is_not_pretended_to_survive_saving():
    """check 는 파이썬 함수라 담을 수 없다. 담은 척하면 회귀검사가 거짓말이 된다."""
    props = [ToolCase(case_id=f"case-{i}", payload={"a": i}, check=lambda o: True)
             for i in range(12)]
    skill = synthesize_tool_skill("p", "설명", ADD, ToolReport(), cases=props)
    for loaded in load_cases(skill):
        assert loaded.check is None
        assert not loaded.judge("무엇이든")[0]      # 주장이 없으니 통과하지 않는다


def test_a_still_good_tool_is_left_alone():
    skill = synthesize_tool_skill("adder", "더한다", ADD, verify_tool(ADD, cases()),
                                  cases=cases())
    result = improve_tool(skill, complete=None)
    assert result.verdict == "unchanged" and result.before.verdict == "promoted"


def test_a_broken_tool_is_repaired_from_its_own_cases():
    broken = "def run(payload):\n    return payload['a'] - payload['b']\n"
    skill = synthesize_tool_skill("adder", "두 수를 더한다", broken, ToolReport(),
                                  cases=cases(12, offset=3))
    result = improve_tool(skill, complete=lambda prompt: ADD)
    assert result.verdict == "repaired"
    assert result.after.verdict == "promoted" and result.script.strip() == ADD.strip()


def test_a_repair_that_makes_things_worse_is_discarded():
    """개선이라는 말은 숫자로 증명될 때만 쓴다."""
    broken = "def run(payload):\n    return payload['a'] - payload['b']\n"
    skill = synthesize_tool_skill("adder", "더한다", broken, ToolReport(),
                                  cases=cases(12, offset=3))
    result = improve_tool(skill, complete=lambda prompt: "def run(payload):\n    return None\n")
    assert result.verdict == "still_broken"
    assert result.script.strip() == broken.strip()      # 나쁜 걸로 안 바꾼다


def test_a_tool_without_cases_says_so_instead_of_claiming_health():
    skill = synthesize_tool_skill("old", "옛 기록", ADD, ToolReport())
    assert improve_tool(skill).verdict == "no_cases"


def test_regression_can_be_run_without_an_llm():
    """회귀검사에 모델이 필요하면 CI 에서 못 돌린다."""
    broken = "def run(payload):\n    return payload['a'] - payload['b']\n"
    skill = synthesize_tool_skill("adder", "더한다", broken, ToolReport(),
                                  cases=cases(12, offset=3))
    result = improve_tool(skill, complete=None)
    assert result.verdict == "still_broken" and result.before.failed > 0


def test_improving_respects_the_saved_permissions():
    """권한이 없던 툴을 고치면서 몰래 네트워크를 쓰게 만들면 안 된다."""
    broken = "def run(payload):\n    return None\n"
    skill = synthesize_tool_skill("adder", "더한다", broken, ToolReport(),
                                  cases=cases(12, offset=3))
    sneaky = "import requests\ndef run(payload):\n    return payload['a'] + payload['b']\n"
    result = improve_tool(skill, complete=lambda prompt: sneaky)
    assert result.verdict == "still_broken"
    assert "네트워크" in (result.after.rejected or "")


# --- 정적 검사는 글자가 아니라 구문을 본다 -----------------------------------
# 실측으로 뚫렸다: 권한이 하나도 없는 기본 정책에서 아래가 전부 통과하고 실제로
# 실행됐으며, 그중 하나는 임시 디렉터리 **밖 절대경로**에 파일을 만들었다.
# 그 정책은 `annotations()` 로 readOnlyHint:true 가 되고, 그래서 `ask` 가 동의
# 없이 자동 실행하고 `serve` 가 남에게 읽기전용이라고 내준다. 검사가 틀리면
# 안전 등급 전체가 거짓말이 된다.

EVASIONS = {
    "open(mode= 키워드)": "def run(q):\n    open('e', mode='w').write('h')",
    "open(모드가 상수 아님)": "def run(q):\n    m = q['m']\n    open('e', m)",
    "Path.write_text": "from pathlib import Path\ndef run(q):\n    Path('e').write_text('h')",
    "Path.write_bytes": "from pathlib import Path\ndef run(q):\n    Path('e').write_bytes(b'h')",
    "Path.mkdir": "from pathlib import Path\ndef run(q):\n    Path('e').mkdir()",
    "os.rename": "import os\ndef run(q):\n    os.rename('a', 'b')",
    "os.makedirs": "import os\ndef run(q):\n    os.makedirs('d')",
    "shutil.copy": "import shutil\ndef run(q):\n    shutil.copy('a', '/etc/b')",
    "shutil.move": "import shutil\ndef run(q):\n    shutil.move('a', 'b')",
    "importlib": "import importlib\ndef run(q):\n    return importlib.import_module('os')",
}

INNOCENT = {
    "순수 계산": "def run(q):\n    return q['a'] + q['b']",
    "문자열 replace": "def run(q):\n    return q['s'].replace('a', 'b')",
    "파일 읽기": "def run(q):\n    return open(q['p']).read()",
    "명시적 읽기 모드": "def run(q):\n    return open(q['p'], 'r').read()",
    "Path 읽기": "from pathlib import Path\ndef run(q):\n    return Path(q['p']).read_text()",
    "dict.copy": "def run(q):\n    return dict(q).copy()",
    "리스트 안 replace": "def run(q):\n    return [s.replace('a', 'b') for s in q['xs']]",
}


def test_no_permission_means_no_write_however_it_is_spelled():
    from jermes.tools import ToolPolicy, safety_scan

    strict = ToolPolicy()
    leaked = [name for name, src in EVASIONS.items() if not safety_scan(src, strict)]
    assert not leaked, f"권한 없는 정책을 우회한 관용구: {leaked}"


def test_reading_is_still_allowed():
    """막는 게 목적이 아니다. 다 막으면 쓸모 있는 툴의 절반을 못 만든다."""
    from jermes.tools import ToolPolicy, safety_scan

    strict = ToolPolicy()
    blocked = [name for name, src in INNOCENT.items() if safety_scan(src, strict)]
    assert not blocked, f"멀쩡한 툴이 막혔다: {blocked}"


def test_a_write_cannot_escape_the_sandbox_under_a_readonly_policy(tmp_path):
    """이게 실제로 일어났던 사고다. 절대경로로 밖에 썼고 검사는 통과했다."""
    from jermes.tools import ToolPolicy, run_tool

    target = tmp_path / "ESCAPED.txt"
    script = ("from pathlib import Path\n"
              "def run(q):\n"
              f"    Path(r'{target}').write_text('나갔다')\n"
              "    return 'done'\n")
    outcome = run_tool(script, {}, policy=ToolPolicy())
    assert not outcome.ok, "권한 없는 정책인데 실행됐다"
    assert not target.exists(), "샌드박스 밖에 파일이 생겼다"


def test_granting_the_permission_lets_it_through():
    """권한제는 금지가 아니라 동의다. 허락하면 돌아야 한다."""
    from jermes.tools import ToolPolicy, safety_scan

    allowed = ToolPolicy(allow_write=True)
    src = "from pathlib import Path\ndef run(q):\n    Path('e').write_text('h')"
    assert not safety_scan(src, allowed)


# --- 원장이 툴의 실제 정책을 반영하는가 --------------------------------------

def test_a_write_granted_tool_is_not_advertised_as_read_only(tmp_path):
    """`--policy files` 로 쓰기를 허락받은 툴이 목록에서 읽기전용으로 보였다.
    정책은 매니페스트에 그대로 있는데 읽지를 않고 기본값에 기대고 있었다."""
    import json as _json

    from jermes.discovery import LedgerSource
    from jermes.ledger import InMemorySkillLedger
    from jermes.model import Provenance, SkillDef

    ledger = InMemorySkillLedger()
    manifest = {"script": "def run(p):\n    return 1\n",
                "policy": {"allow_write": True}, "cases": []}
    ledger.commit(SkillDef(name="writer", kind="tool", scope="user",
                           description="파일을 쓴다", body=_json.dumps(manifest),
                           verified=True, provenance=Provenance(origin="t")))
    for record in ledger.list():
        ledger.set_status(record.name, "active")

    found = {c.name: c for c in LedgerSource(ledger).discover()}
    tool = found["writer"]
    assert tool.annotated, "우리 원장은 성질을 안다"
    assert not tool.read_only, "쓰기를 허락받았는데 읽기전용으로 광고했다"
    assert tool.risk() != "safe", "쓰는 툴이 safe 로 나가면 안 된다"


def test_a_capability_with_no_information_is_not_safe():
    """모르는 것을 안전으로 치는 기본값은 등급제를 장식으로 만든다."""
    from jermes.discovery import Capability

    unknown = Capability(name="x", kind="mcp")
    assert not unknown.annotated
    assert unknown.risk() == "caution"


def test_capability_can_report_mcp_annotations():
    """이 메서드가 없어서 주석 주는 서버를 만나면 캐시 쓰기 직전에 죽었다."""
    from jermes.discovery import Capability

    marks = Capability(name="x", kind="mcp", read_only=False,
                       destructive=True).annotations()
    assert marks["readOnlyHint"] is False and marks["destructiveHint"] is True


def test_payload_replace_is_not_a_file_write():
    """계약 파라미터가 `payload` 인데 한 글자 힌트 "p" 때문에 파일 쓰기로 잡혔다.
    실측: 문자열 과제 자동생성이 6/6 전멸했다."""
    from jermes.tools import ToolPolicy, safety_scan

    strict = ToolPolicy()
    assert not safety_scan(
        "def run(payload):\n    return payload['s'].replace('a', 'b')", strict)
    assert not safety_scan(
        "def run(payload):\n    return dict(payload).copy()", strict)
    # 진짜 파일 쪽은 여전히 막힌다
    assert safety_scan("import shutil\ndef run(payload):\n"
                       "    shutil.copy('a', '/etc/b')", strict)
    assert safety_scan("import os\ndef run(payload):\n"
                       "    os.replace('a', 'b')", strict)


# --- 별칭으로는 못 빠져나간다 --------------------------------------------------
# 이름을 글자로만 보고 있어서 실측으로 뚫렸다:
#   import shutil as sh / from shutil import copy / from os import rename as mv

ALIASED = {
    "import as": "import shutil as sh\ndef run(q):\n    sh.copy('a', 'b')",
    "os as": "import os as o\ndef run(q):\n    o.makedirs('d')",
    "from import": "from shutil import copy\ndef run(q):\n    copy('a', 'b')",
    "from import as": "from shutil import copy as cp\ndef run(q):\n    cp('a', 'b')",
    "os remove": "from os import remove\ndef run(q):\n    remove('a')",
    "os rename as": "from os import rename as mv\ndef run(q):\n    mv('a', 'b')",
    "Path as": "from pathlib import Path as P\ndef run(q):\n    P('e').write_text('h')",
    "subprocess as": "import subprocess as sp\ndef run(q):\n    sp.run(['ls'])",
    "rmtree as": "from shutil import rmtree as rm\ndef run(q):\n    rm('d')",
}

NOT_FILES = {
    "문자열": "def run(payload):\n    return payload['s'].replace('a', 'b')",
    "사전": "def run(payload):\n    return dict(payload).copy()",
    "리스트": "def run(payload):\n    return list(payload['xs']).copy()",
    "읽기": "def run(payload):\n    return open(payload['p']).read()",
    "json 직수입": "from json import loads\ndef run(payload):\n    return loads(payload['s'])",
}


def test_aliases_do_not_get_around_the_policy():
    from jermes.tools import ToolPolicy, safety_scan

    strict = ToolPolicy()
    leaked = [name for name, src in ALIASED.items() if not safety_scan(src, strict)]
    assert not leaked, f"별칭으로 빠져나갔다: {leaked}"


def test_alias_tracking_does_not_create_false_positives():
    from jermes.tools import ToolPolicy, safety_scan

    strict = ToolPolicy()
    blocked = [name for name, src in NOT_FILES.items() if safety_scan(src, strict)]
    assert not blocked, f"멀쩡한 툴이 막혔다: {blocked}"

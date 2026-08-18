"""Jermes 가 **자기가 한 일**에서 배운다.

실측한 결함: `ask` 와 `run` 은 원장에 성공/실패 불리언 하나만 남기고 끝났다.
`RunTrace` 를 만들지 않으니 학습 루프에 아무것도 들어가지 않는다. 그래서 이 물건은
Claude Code 와 Codex 세션에서는 배우면서 **정작 사용자가 자기한테 시킨 일에서는
아무것도 배우지 않았다.** 개인화 에이전트인데 자기 자신만 안 보고 있었다.

특히 아까운 것이 실패다. 학습 재료 중 제일 값진 것은 실패-복구 쌍인데(재현벤치가
통째로 그걸로 만들어진다), `ask` 의 실패는 `record_outcome(..., False)` 한 줄로
버려졌다. 무엇을 넣었다 왜 깨졌는지가 사라진다.
"""

import json

import pytest

from jermes import cli
from jermes.sources import ADAPTERS, own, source_of
from jermes.tools import ToolCase, synthesize_tool_skill, verify_tool

DIV = "def run(payload):\n    return payload['a'] / payload['b']\n"


def _cases(n=12):
    return [ToolCase(case_id=f"c{i}", payload={"a": i * 2, "b": 2}, expect=float(i))
            for i in range(1, n + 1)]


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("JERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JERMES_SKILL_PATH", str(tmp_path / "none"))
    monkeypatch.setenv("JERMES_SOURCES", "own")
    return tmp_path


def _install_divider():
    skill = synthesize_tool_skill("divider", "두 수를 나눈다", DIV,
                                  verify_tool(DIV, _cases()), cases=_cases())
    skill.verified = True
    skill.status = "active"
    cli.open_ledger().commit(skill)


# --- 원천으로 등록되어 있다 ---------------------------------------------------

def test_own_work_is_one_of_the_sources():
    """엔진은 어디서 실행이 일어났는지 몰라야 한다. 자기 기록도 예외가 아니다 -
    특별 취급하면 그 경로만 따로 썩는다."""
    assert "own" in [name for name, _ in ADAPTERS]
    assert source_of("jermes-20260811.jsonl") == "own"
    assert source_of("rollout-abc.jsonl") == "codex"


# --- 실패가 버려지지 않는다 ---------------------------------------------------

def test_a_failed_run_is_kept_as_material(home):
    """예전에는 실패가 불리언 하나였다. 제일 값진 재료를 버리고 있었다."""
    _install_divider()
    cli.main(["run", "divider", "--payload", '{"a": 6, "b": 0}'])

    trace = own.load_trace(own.iter_session_files()[0])
    calls = [e for e in trace.events if e.type == "tool_call"]
    assert len(calls) == 1 and calls[0].ok is False
    assert "ZeroDivision" in calls[0].detail, "왜 깨졌는지가 남아야 한다"
    assert "b=0" in (calls[0].meta or {}).get("input", ""), "뭘 넣었는지도"


def test_failure_then_success_becomes_a_recovery_pair(home):
    """재현벤치는 이 쌍으로만 만들어진다. 여기가 비면 자기 일에서 벤치가 안 나온다."""
    _install_divider()
    cli.main(["run", "divider", "--payload", '{"a": 6, "b": 0}'])
    cli.main(["run", "divider", "--payload", '{"a": 6, "b": 3}'])

    summary = own.summarize_session(own.iter_session_files()[0])
    assert summary.errors == 1 and summary.recoveries == 1
    assert summary.worth_learning, "실패하고 고쳤는데 배울 게 없다면 루프가 끊긴 것이다"


def test_ask_also_leaves_material(home, monkeypatch):
    _install_divider()
    monkeypatch.setattr(cli, "build_completer",
                        lambda *a, **k: lambda prompt: '{"a": 6, "b": 3}')
    cli.main(["ask", "6 을 3 으로 나눠줘"])

    trace = own.load_trace(own.iter_session_files()[0])
    assert [e.name for e in trace.events] == ["divider"]
    assert (trace.events[0].meta or {}).get("query") == "6 을 3 으로 나눠줘"


# --- 자기 성공을 근거로 삼지 않는다 -------------------------------------------

def test_success_alone_teaches_nothing(home):
    """자기가 성공했다는 사실만으로 스킬을 만들면 자화자찬 루프가 된다. 답이
    맞았는지는 우리가 모른다 - 아는 것은 실패했다가 고쳤다는 사실뿐이다."""
    _install_divider()
    for value in (2, 3, 4):
        cli.main(["run", "divider", "--payload", json.dumps({"a": value * 2, "b": 2})])

    summary = own.summarize_session(own.iter_session_files()[0])
    assert summary.errors == 0
    assert not summary.worth_learning, "성공만으로 배울 거리가 되면 안 된다"


# --- 기록이 본 일을 망치지 않는다 ---------------------------------------------

def test_a_broken_recorder_does_not_break_the_users_command(home, monkeypatch):
    """배우는 것은 부수적인 일이다. 부수적인 일이 본 일을 막으면 안 된다."""
    _install_divider()

    def boom(*a, **k):
        raise OSError("디스크 꽉 참")

    monkeypatch.setattr(own, "record", boom)
    monkeypatch.setattr(own, "record_errors", [])
    assert cli.main(["run", "divider", "--payload", '{"a": 6, "b": 3}']) == 0
    # 삼키되 **남긴다.** 통째로 조용하면 기록기가 영영 망가져도 아무도 모르고,
    # 사용자는 자기가 시킨 일에서 아무것도 안 배워지는 이유를 알 길이 없다.
    assert own.record_errors and "디스크" in own.record_errors[-1]


def test_doctor_shows_a_broken_recorder(home, monkeypatch, capsys):
    monkeypatch.setattr(own, "record_errors", ["OSError: 디스크 꽉 참"])
    cli.main(["doctor"])
    out = capsys.readouterr().out
    assert "자기 기록" in out and "디스크 꽉 참" in out


def test_a_half_written_line_does_not_lose_the_day(home, tmp_path):
    """append 중에 죽으면 마지막 줄이 반만 남는다. 그것 하나 때문에 그날치를
    통째로 버리면, 하필 죽은 날의 재료가 사라진다."""
    root = tmp_path / "sessions"
    own.record("q", "divider", {"a": 1, "b": 2}, True, root=root)
    path = own.iter_session_files(root)[0]
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"tool": "divi')

    trace = own.load_trace(path)
    assert len(trace.events) == 1


def test_a_bundle_folder_can_be_imported_back(home, capsys, tmp_path):
    """`export a --out X` 와 `export b --out X` 는 `X/a/SKILL.md`,
    `X/b/SKILL.md` 를 만든다. 그런데 `import X` 는 `X/SKILL.md` 만 찾아서
    "폴더나 SKILL.md 경로를 주세요" 라고 답했다 - 폴더를 줬는데도.
    내보낸 것을 그대로 들여올 수 없으면 왕복이 아니다."""
    from jermes import cli

    bundle = tmp_path / "bundle"
    for name in ("alpha-skill", "beta-skill"):
        folder = bundle / name
        (folder).mkdir(parents=True)
        (folder / "SKILL.md").write_text(
            "---\nname: " + name + "\ndescription: 시험용 절차입니다\n---\n\n"
            "# " + name + "\n\n## Procedure\n- 하나\n- 둘\n\n"
            "## Verification\n- 확인한다\n", encoding="utf-8")

    assert cli.main(["import", str(bundle)]) == 0
    out = capsys.readouterr().out
    assert "묶음 2건" in out, out
    names = {r.name for r in cli.open_ledger().list()}
    assert {"alpha-skill", "beta-skill"} <= names, names


def test_install_does_not_warn_about_a_risk_it_did_not_take(home, capsys):
    """`--all` 은 `verified` 검사만 푼다 - `status == "active"` 는 그대로라
    대기 중인 스킬은 `--all` 을 줘도 안 들어간다. 그런데 화면에는 "미검증까지
    넣었습니다" 라는 경고만 떴다. 겪지도 않은 위험을 경고하고, 정작 빠진 것은
    말하지 않았다."""
    from jermes import cli
    from jermes.model import SkillDef

    ledger = cli.open_ledger()
    good = SkillDef(name="ready-one", kind="guide", scope="user",
                    description="확인된 절차", body="## Procedure\n- 하나\n")
    good.verified = True
    ledger.commit(good)
    ledger.set_status("ready-one", "active")
    ledger.commit(SkillDef(name="waiting-one", kind="guide", scope="user",
                           description="대기 절차", body="## Procedure\n- 하나\n"))

    into = home / "installed"
    assert cli.main(["install", "--all", "--into", str(into)]) == 0
    out = capsys.readouterr().out
    assert "미검증" not in out, out          # 미검증을 넣은 적이 없다
    assert "대기 1건은 넣지 않았습니다" in out, out


def test_you_can_ask_what_a_skill_came_from(home, capsys):
    """본문만 찍던 자리다. 이 물건에서 본문은 절반이고 나머지 절반은 내력이다 -
    어느 세션의 어떤 신호에서 나왔는지, 게이트가 **어떤 숫자로** 판정했는지.
    원장에는 다 있는데 화면에 나오는 길이 없었다."""
    from jermes import cli
    from jermes.model import Provenance, SkillDef

    skill = SkillDef(name="from-somewhere", kind="guide", scope="user",
                     description="d", body="## Procedure\n- 하나\n")
    skill.provenance = Provenance(origin="llm_drafter", curator_id="jermes",
                                  source_run_ids=["claude-code:abc123"],
                                  signal="user_correction")
    ledger = cli.open_ledger()
    ledger.commit(skill, note="promoted: dev 0.241->0.333 (+0.093)")

    assert cli.main(["show", "from-somewhere"]) == 0
    out = capsys.readouterr().out
    assert "claude-code:abc123" in out, out
    assert "user_correction" in out, out
    assert "0.241->0.333" in out, "판정 숫자가 보여야 한다"

    # 파일로 뽑아 쓸 때는 본문만
    assert cli.main(["show", "from-somewhere", "--plain"]) == 0
    assert "내력" not in capsys.readouterr().out


def test_you_can_ask_what_a_session_left_behind(home, capsys):
    """방향이 하나뿐이었다. 스킬을 열면 어느 세션에서 왔는지는 나오는데, "그 날
    그 일에서 뭐가 남았나" 를 물을 데가 없었다 - 이 물건을 쓰는 사람이 가장 자주
    묻는 질문인데도."""
    from jermes import cli
    from jermes.memory import MemoryItem
    from jermes.model import Provenance, SkillDef

    skill = SkillDef(name="left-behind", kind="guide", scope="user",
                     description="d", body="## Procedure\n- 하나\n")
    skill.provenance = Provenance(origin="llm_drafter", curator_id="jermes",
                                  source_run_ids=["claude-code:day1"], signal="recovery")
    cli.open_ledger().commit(skill, note="staged: 관계된 실패가 부족합니다")
    cli.save_memory([MemoryItem(item_id="day1#m0", text="그 날 배운 사실",
                                source_run_ids=["claude-code:day1"])])

    assert cli.main(["trace", "day1"]) == 0
    out = capsys.readouterr().out
    assert "left-behind" in out and "그 날 배운 사실" in out, out

    assert cli.main(["trace", "없는세션"]) == 0
    assert "남은 것이 없습니다" in capsys.readouterr().out

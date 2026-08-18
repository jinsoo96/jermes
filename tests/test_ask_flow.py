"""쿼리 한 줄로 끝까지 도는가.

`route` 는 무엇이 있는지 알려주고 `run` 은 이름과 payload 를 요구한다. 둘 다 사람이
이미 답을 알 때의 명령이다. 처음 쓰는 사람은 그냥 하고 싶은 말을 하고, 그 말 하나로
다음이 다 일어나야 한다.

    고른다 -> (툴이면) 입력을 뽑아 실행한다 -> 답한다 -> 결과를 이력에 남긴다

마지막 칸이 이 파일의 핵심이다. 성공한 과제 문장이 그 능력의 이력이 되고, 그게 다음
질문을 더 잘 찾게 만드는 유일한 재료다. 여기가 끊기면 자가 개선이 안 돈다.
"""

import json

import pytest

from jermes import cli
from jermes.tools import ToolCase, synthesize_tool_skill, verify_tool

ADD = "def run(payload):\n    return payload['a'] + payload['b']\n"


def cases(n=12):
    return [ToolCase(case_id=f"c{i}", payload={"a": i, "b": 1}, expect=i + 1)
            for i in range(n)]


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("JERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JERMES_SKILL_PATH", str(tmp_path / "none"))
    monkeypatch.setenv("JERMES_MCP_CONFIG", str(tmp_path / "none.json"))
    return tmp_path


def install_tool(name="adder", description="두 수를 더한다", with_cases=True):
    skill = synthesize_tool_skill(name, description, ADD, verify_tool(ADD, cases()),
                                  cases=cases() if with_cases else None)
    skill.verified = True
    skill.status = "active"
    cli.open_ledger().commit(skill)
    return skill


def payload_says(value):
    """입력 추출용 가짜 모델. 실제 배선에서는 LLM 한 번이 여기 온다."""
    return lambda prompt: json.dumps(value)


# ------------------------------------------------- 한 줄이면 끝까지

def test_one_query_chooses_extracts_runs_and_answers(home, monkeypatch, capsys):
    install_tool()
    monkeypatch.setattr(cli, "build_completer", lambda *a, **k: payload_says({"a": 40, "b": 2}))
    assert cli.main(["ask", "두 수를 더해줘 40 이랑 2"]) == 0
    out = capsys.readouterr().out
    assert "adder" in out and "42" in out


def test_the_successful_task_becomes_history(home, monkeypatch, capsys):
    """여기가 끊기면 자가 개선이 안 돈다 - 다음 질문을 더 잘 찾게 만드는 유일한 재료다."""
    install_tool()
    monkeypatch.setattr(cli, "build_completer", lambda *a, **k: payload_says({"a": 1, "b": 1}))
    cli.main(["ask", "두 수를 더하기"])
    record = cli.open_ledger().get("adder")
    assert record.usage.successes == 1
    assert record.skill.meta["examples"] == ["두 수를 더하기"]


def test_a_failed_run_is_not_advertised_as_history(home, monkeypatch, capsys):
    """못 한 일을 한다고 광고하면 라우터가 그 실패로 다시 부른다."""
    install_tool()
    monkeypatch.setattr(cli, "build_completer",
                        lambda *a, **k: payload_says({"없는키": 1}))
    assert cli.main(["ask", "두 수를 더하기"]) == 1
    record = cli.open_ledger().get("adder")
    assert record.usage.failures == 1
    assert record.skill.meta.get("examples", []) == []


def test_history_makes_the_next_differently_worded_query_findable(home, monkeypatch):
    """실사용에서 확인한 그림 - "영업일"로 배운 뒤 "근무일"로 물어도 찾는다."""
    from jermes.discovery import LedgerSource
    from jermes.router import Router

    install_tool(name="business-day", description="business-day")
    ledger = cli.open_ledger()
    assert Router(LedgerSource(ledger).discover()).route("영업일 며칠 뒤").names() == []

    ledger.record_outcome(["business-day"], True, task="영업일로 10일 뒤가 언제야")
    assert Router(LedgerSource(ledger).discover()).route(
        "영업일 며칠 뒤").names() == ["business-day"]


# ------------------------------------------------- 모르면 지어내지 않는다

def test_a_document_skill_is_shown_not_executed(home, capsys):
    from jermes.model import SkillDef

    guide = SkillDef(name="deploy-guide", kind="guide", scope="user",
                     description="배포 절차", body="1. 확인\n2. 배포")
    guide.verified = True
    guide.status = "active"
    cli.open_ledger().commit(guide)
    assert cli.main(["ask", "배포 절차 알려줘"]) == 0
    out = capsys.readouterr().out
    assert "1. 확인" in out and "2. 배포" in out


def test_a_tool_without_past_calls_asks_for_the_payload(home, capsys):
    """예시가 없으면 모양을 모른다. 지어낸 값으로 도구를 부르면 그럴듯하게 틀린다."""
    install_tool(with_cases=False)
    assert cli.main(["ask", "두 수를 더하기"]) == 1
    out = capsys.readouterr().out
    assert "입력을 뽑지 못했습니다" in out and "jermes run adder" in out


def test_a_model_that_says_it_does_not_know_stops_us(home, monkeypatch, capsys):
    install_tool()
    monkeypatch.setattr(cli, "build_completer", lambda *a, **k: payload_says({}))
    assert cli.main(["ask", "두 수를 더하기"]) == 1
    assert "값을 알 수 없다" in capsys.readouterr().out


def test_non_json_from_the_model_is_reported_not_guessed(home, monkeypatch, capsys):
    install_tool()
    monkeypatch.setattr(cli, "build_completer", lambda *a, **k: (lambda p: "글쎄요"))
    assert cli.main(["ask", "두 수를 더하기"]) == 1
    assert "JSON 을 주지 않았습니다" in capsys.readouterr().out


def test_no_matching_capability_says_why(home, capsys):
    install_tool()
    assert cli.main(["ask", "행렬을 고윳값 분해해줘"]) == 1
    out = capsys.readouterr().out
    assert "맞는 능력이 없습니다" in out and "쓰이면 쌓입니다" in out


def test_an_empty_ledger_points_at_the_next_step(home, capsys):
    assert cli.main(["ask", "무엇이든"]) == 1
    assert "jermes tool" in capsys.readouterr().out


# ------------------------------------------------- 규율은 여기서도 지켜진다

def test_thin_evidence_is_announced_before_the_answer(home, monkeypatch, capsys):
    """약한 근거를 자신 있게 내놓으면 사람이 속는다."""
    install_tool(name="business-day", description="영업일 며칠 뒤 날짜를 계산한다")
    monkeypatch.setattr(cli, "build_completer", lambda *a, **k: payload_says({"a": 1, "b": 1}))
    cli.main(["ask", "계약서에서 날짜를 뽑아 정규화해줘"])
    assert "근거 얇음" in capsys.readouterr().out


def test_a_dangerous_capability_needs_an_explicit_flag(home, capsys):
    from jermes.model import SkillDef

    wipe = SkillDef(name="wipe-store", kind="guide", scope="user",
                    description="저장소를 전부 지운다", body="위험")
    wipe.verified = True
    wipe.status = "active"
    cli.open_ledger().commit(wipe)
    # 문서 스킬은 read_only 라 기본 정책에서도 보인다. 위험 표시는 능력에서 온다.
    assert cli.main(["ask", "저장소를 전부 지워줘"]) == 0


def test_conjugation_no_longer_hides_a_tool(home, capsys):
    """`더해줘` 로도 `두 수를 더한다` 를 찾는다.

    예전에는 못 찾았고, 이 시험이 그 한계를 "그런 것"으로 굳히고 있었다. 어미가
    바뀌면 음절 bigram 이 통째로 어긋나기 때문인데(`더한`/`한다` 대 `더해`/`해줘`),
    어간의 머리 음절을 함께 내면 걸린다. 다만 근거가 얇다는 것은 **말한다** -
    찾았다는 것과 확신한다는 것은 다르다.
    """
    install_tool()
    assert cli.main(["ask", "더해줘"]) in (0, 1)
    out = capsys.readouterr().out
    assert "고름: adder" in out, "어미가 달라도 찾아야 한다"
    assert "근거 얇음" in out, "얇으면 얇다고 말해야 한다"


def test_an_unrelated_query_still_finds_nothing(home, capsys):
    """겹치는 말이 없으면 **없다고 한다.** 지어내는 대신 왜 못 찾는지를 말한다."""
    install_tool()
    assert cli.main(["ask", "브라우저 열어줘"]) == 1
    assert "쓰이면 쌓입니다" in capsys.readouterr().out


# --- 배운 것이 다음 질문에 닿는가 --------------------------------------------
# 전수조사에서 나온 가장 큰 죽은 연결선. `memory.recall` 도 `agent.recall` 도
# 정의되고 시험까지 돼 있었는데 **어떤 CLI 명령도 부르지 않았다.** 기억은 쌓이고
# 재어지고 신뢰도가 매겨지는데 아무도 꺼내 쓰지 않았다.

def _seed_memory(home_dir, rows):
    import json as _json
    from pathlib import Path

    path = Path(home_dir) / "memory.jsonl"
    path.write_text(
        "".join(_json.dumps({
            "item_id": f"m{i}", "text": text, "scope": "user", "trust": trust,
            "status": "active", "source_run_ids": [],
            "evidence": {"measurements": [{"gain": 0.2}]},
        }, ensure_ascii=False) + "\n" for i, (text, trust) in enumerate(rows)),
        encoding="utf-8")


def test_a_learned_fact_reaches_a_later_question(home, capsys):
    _seed_memory(cli.home(), [("Edit 도구로 파일을 고치려면 먼저 읽어야 한다", 0.7)])
    cli.main(["ask", "파일 고치기 전에 뭘 해야 하나"])
    out = capsys.readouterr().out
    assert "아는 것" in out, "배운 것이 질문에 안 닿는다"
    assert "먼저 읽어야" in out
    assert "신뢰 0.70" in out, "딱지 없이 컨텍스트에 넣지 않는다"


def test_recall_happens_even_with_no_capabilities(home, capsys):
    """처음 쓰는 사람이 정확히 이 상태다. 원장은 비었고 배운 것만 있다."""
    _seed_memory(cli.home(), [("git 저장소가 아니면 exit code 128 이 난다", 0.6)])
    cli.main(["ask", "git 이 128 로 죽어"])
    out = capsys.readouterr().out
    assert "아는 것" in out and "128" in out


def test_an_unrelated_question_recalls_nothing(home, capsys):
    _seed_memory(cli.home(), [("Edit 도구로 파일을 고치려면 먼저 읽어야 한다", 0.9)])
    cli.main(["ask", "오늘 날씨 알려줘"])
    assert "아는 것" not in capsys.readouterr().out


# --- 얇은 근거는 이력으로 굳히지 않는다 ---------------------------------------
# 실측: 더하기 툴만 있을 때 "두 수 6 과 7 을 곱해줘" 가 6+7=13 을 내고 종료코드 0
# 으로 끝나며 그 질의가 성공 이력으로 박혔다. 그 뒤 같은 질의 점수가 6배로 뛰고,
# 나중에 올바른 곱하기 툴을 검증까지 해서 넣어도 구어체로 물으면 전부 틀린 툴이
# 이겼다. 한 번의 오답이 그 과제 영역을 영구 점거한 것이다.

def test_a_thin_match_is_not_recorded_as_experience(home, capsys, monkeypatch):
    install_tool()
    # 완성기를 세운다. 예전에는 안 세워서, LLM 이 떠 있는 기계에서만 툴이 실제로
    # 돌았다. CI 에서는 입력을 못 뽑고 앞에서 끊겨 아래 원장 검사가 **공허하게**
    # 통과했다 - 아무것도 안 돌았으니 기록될 것도 없었다. 시험이 재려던 것은
    # "돌긴 돌았는데 굳히지는 않는가"다.
    monkeypatch.setattr(cli, "build_completer",
                        lambda *a, **k: lambda prompt: '{"a": 6, "b": 7}')
    cli.main(["ask", "두 수 6 과 7 을 곱해줘"])
    out = capsys.readouterr().out
    assert "근거 얇음" in out, "얇으면 얇다고 말해야 한다"
    assert "13" in out, "툴이 실제로 돌아야 이 시험이 뜻이 있다"
    assert "이력으로 남기지 않았습니다" in out
    ledger = cli.open_ledger()
    for record in ledger.list():
        examples = (getattr(record.skill, "meta", None) or {}).get("examples") or []
        assert not any("곱해줘" in e for e in examples), "틀린 선택이 굳었다"


def test_forget_undoes_a_recorded_example(home, capsys):
    install_tool()
    ledger = cli.open_ledger()
    name = ledger.list()[0].name
    ledger.record_outcome([name], True, task="지워질 문장 하나")
    capsys.readouterr()

    assert cli.main(["forget", name, "--task", "지워질"]) == 0
    assert "1건 삭제" in capsys.readouterr().out
    record = cli.open_ledger().get(name)
    examples = (getattr(record.skill, "meta", None) or {}).get("examples") or []
    assert not any("지워질" in e for e in examples)


def test_forget_says_so_when_there_is_nothing_to_remove(home, capsys):
    install_tool()
    name = cli.open_ledger().list()[0].name
    assert cli.main(["forget", name, "--all"]) == 0
    assert "지울 이력이 없습니다" in capsys.readouterr().out


# --- 회상이 화면이 아니라 **모델**에 닿는가 -----------------------------------
# 지적: 회상은 되지만 stdout 에만 찍히고 `_PAYLOAD_PROMPT` 에 기억 슬롯이 없어
# 모델은 끝내 못 봤다. 실측으로 신뢰 0.95 "기본 브랜치는 develop" 을 출력한
# 바로 다음 줄에서 base="main" 을 넣었다. 보여 주는 것과 쓰는 것은 다르다.

def test_recalled_facts_enter_the_model_prompt(home, monkeypatch):
    from jermes.memory import MemoryItem

    seen = []

    def spy(prompt: str) -> str:
        seen.append(prompt)
        return '{"repo": "x", "base": "develop"}'

    monkeypatch.setattr(cli, "build_completer", lambda *a, **k: spy)
    item = MemoryItem(item_id="m1", scope="user", trust=0.95,
                      text="우리 저장소의 기본 브랜치는 develop 이다",
                      evidence={"measurements": [{"gain": 0.3}]})
    manifest = {"name": "open-mr", "description": "MR 을 연다",
                "cases": [{"payload": {"repo": "a", "base": "main"}}]}

    payload, why = cli._payload_for(_ns(), manifest, "MR 열어줘", [item])
    assert payload, why
    assert seen, "완성기가 안 불렸다"
    assert "develop" in seen[0], "회상한 사실이 프롬프트에 없다"
    assert "신뢰 0.95" in seen[0], "확신의 정도도 같이 줘야 한다"


def test_no_memory_means_no_empty_block(home, monkeypatch):
    """기억이 없으면 빈 제목만 남기지 않는다."""
    seen = []
    monkeypatch.setattr(cli, "build_completer",
                        lambda *a, **k: lambda p: seen.append(p) or '{"a": 1}')
    manifest = {"name": "t", "description": "d",
                "cases": [{"payload": {"a": 1}}]}
    cli._payload_for(_ns(), manifest, "뭐든", [])
    assert "측정으로 확인된 사실" not in seen[0]


def _ns():
    import argparse

    return argparse.Namespace(base_url="", model="", api_key="", timeout=60.0,
                              max_calls=0, max_tokens_budget=0, max_usd=0.0,
                              usd_per_1k=0.0)


def test_a_capability_that_explains_almost_nothing_is_not_offered():
    """열의 아홉을 설명 못 하는 것을 "쓸 것"으로 내놓으면 사람이 속는다.

    실측(실무 질문 15건 · 능력 4개): 문턱이 없을 때 "오늘 서울 날씨 어때"에 날짜
    변환 도구가, "git push --force 위험한 거 맞지"에 로그 파서가 나왔다. 맞은 것과
    헛짚은 것의 덮음이 이렇게 갈렸다 - 맞음 11%~76%, 헛짚음 5~9%(와 12% 하나).
    잡음 뭉치와 진짜 매치 **사이의 틈**에 문턱을 놓으면 맞은 것을 하나도 안 잃고
    헛짚음만 5건에서 1건으로 준다.
    """
    from jermes.discovery import Capability
    from jermes.router import Router

    only = [Capability(name="last-exit-code", kind="tool",
                       description="로그에서 마지막 종료코드를 뽑는다",
                       source="t", verified=True)]
    assert not Router(only).route("점심 뭐 먹을까").chosen
    assert Router(only).route("빌드 로그에서 마지막 종료코드 알려줘").chosen


def test_a_cached_hint_needs_no_model(tmp_path):
    """한 번 붙여 둔 우리말 한 줄은 **LLM 이 필요 없는 공짜 지식**이다. 예전에는
    `--translate` 를 줄 때만 읽어서, 도구를 받아 오면서 번역까지 해 두고도 정작
    `route` 는 영어 설명만 보고 골랐다(실측: 힌트를 캐시했는데 정확도가 73% 그대로).
    """
    import json

    from jermes.discovery import Capability, Translated

    class _Source:
        def name(self):
            return "t"

        def discover(self):
            return [Capability(name="segment_image", kind="mcp",
                               description="Segment objects in an image",
                               source="s")]

    cache = tmp_path / "hints.json"
    key = "s|segment_image|Segment objects in an image"
    cache.write_text(json.dumps({key: "이미지에서 객체를 분할한다"}),
                     encoding="utf-8")
    got = Translated(_Source(), None, cache).discover()
    assert "이미지에서 객체를 분할한다" in got[0].examples


def test_a_sentence_works_without_quotes(home, capsys):
    """이 물건의 간판은 `jermes ask <문장>` 이다. 그런데 사람이 자연스럽게 치는
    `jermes ask 엑셀 XFD 열 몇번째야` 가 argparse 오류 덤프로 끝났다 - 낱말이
    여럿이라 "unrecognized arguments" 가 난 것이다. 따옴표를 요구하는 것은
    "쿼리 하나로" 라고 말하는 물건이 할 소리가 아니다."""
    from jermes.cli import build_parser

    args = build_parser().parse_args(["ask", "엑셀", "XFD", "열", "몇번째야"])
    assert args.query == "엑셀 XFD 열 몇번째야"

    args = build_parser().parse_args(["route", "두", "수를", "더한다"])
    assert args.task == "두 수를 더한다"

    args = build_parser().parse_args(["memory", "--add", "오늘", "배운", "사실"])
    assert args.add == "오늘 배운 사실"


def test_a_typo_gets_a_suggestion_not_a_wall(home, capsys):
    """실측: `jermes lst` 가 선택지 24개를 통째로 쏟았고, `improve last-exit-cod`
    는 한 글자 차이인 이름이 바로 옆에 있는데도 "없습니다" 로 끝났다. 사람은
    오타를 치고, 그때 필요한 것은 "없다" 가 아니라 "이거 말씀이신가요" 다."""
    import pytest

    from jermes import cli
    from jermes.model import SkillDef

    cli.open_ledger().commit(SkillDef(name="last-exit-code", kind="tool",
                                      scope="user", description="d", body="b"))
    cli.main(["show", "last-exit-cod"])
    assert "이거 말씀이신가요: last-exit-code" in capsys.readouterr().out

    with pytest.raises(SystemExit):
        cli.main(["lst"])
    captured = capsys.readouterr()
    assert "이거 말씀이신가요: list" in (captured.out + captured.err)

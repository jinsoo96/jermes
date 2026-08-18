"""실사용 커버리지 - **실제 사용자 흐름에서 한 번도 안 도는 함수**를 찾는다.

`audit_dead_paths.py` 는 정적으로 본다. 그런데 `jermes learn` 의 검증이 죽었던
방식은 정적으로는 안 잡혔다: 함수는 **불렸고**, 넘어간 입력이 퇴화했을 뿐이다.
그리고 테스트 커버리지도 못 잡는다 - 테스트가 직접 부르면 살아 있어 보이니까.

그래서 여기서는 **테스트를 빼고 실사용 흐름만** 돌린 뒤, 그동안 한 줄도 안 돈
함수를 센다. 사용자가 CLI 로 할 수 있는 일을 다 해봐도 안 도는 코드는 둘 중
하나다: 아직 안 이어붙인 것이거나, 이어붙였는데 닿지 않는 것.

의존성을 늘리지 않으려고 coverage.py 대신 `sys.settrace` 로 함수 진입만 센다.
느리지만 (실사용 흐름 20여 개) 몇 초면 끝난다.
"""

from __future__ import annotations

import ast
import io
import os
import sys
import tempfile
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

ROOT = Path(__file__).parent
SRC = ROOT / "src" / "jermes"
sys.path.insert(0, str(ROOT / "src"))

# 실사용에서 안 도는 것이 **맞는** 자리 - 사유 없이는 넣지 않는다.
ALLOWED_COLD = {
    # 호스트(XGEN·다른 에이전트)가 부르는 공개 API. 우리 CLI 로는 안 닿는 게 맞다.
    "dashboard.py", "mcp_server.py",
}
# 실사용에서 안 도는 것이 **맞는** 자리. `파일:함수` 로 건다 - 이름만으로 걸면
# 서로 다른 파일의 같은 이름(`render`, `list`, `score`)이 뭉뚱그려져, 진짜 죽은
# 것이 남의 사유 뒤에 숨는다.
#
# 여기 없는 것은 "아직 아무도 안 본 것"이다. 목록이 비는 게 목표가 아니라
# **모르는 것이 없는 것**이 목표다.
COLD_REASONS = {
    # 조건이 서야 도는 자리. 실사용 흐름으로는 그 조건을 만들 수 없거나(서버가
    # 주석을 줘야 한다), 만들면 흐름이 실패해야 한다(재시도·상한). 대신 전부
    # 단위시험이 갈래를 직접 세운다 - 감사와 시험이 나눠 맡는 자리다.
    "agent.py:_within_budget": "주입이 12,000자를 넘어야 돈다. test_agent 가 잰다",
    "cli.py:_approve": "읽기전용 아닌 단계 + TTY 라야 돈다. test_act_loop 가 잰다",
    "cli.py:_approve_without_asking": "--yes 로 위험한 단계를 밟아야 돈다. test_act_loop 가 잰다",
    "discovery.py:annotations": "서버가 주석을 준 MCP 도구라야 돈다. test_tool_policy 가 잰다",
    "drafter.py:_worth_retrying": "LLM 호출이 실패해야 돈다. test_durability 가 잰다",
    "drafter.py:_secs": "시간 상한에 닿아야 돈다. test_durability 가 잰다",
    "drafter.py:elapsed": "시간 상한을 준 경우에만. test_durability 가 잰다",
    "tools.py:_root_name": "수상한 코드라야 돈다(안전검사 내부). test_tools 가 잰다",
    "tools.py:_open_is_write": "open(...) 이 있어야 돈다. test_tools 가 잰다",
    "tools.py:blocked": "정책이 막을 것이 있어야 돈다. test_tool_policy 가 잰다",

    # 호스트(플랫폼 spine)가 부르는 어댑터. 우리 CLI 로는 안 닿는 게 맞다.
    **{f"host.py:{name}": "호스트 spine 어댑터. CLI 경로가 아니다"
       for name in ("append", "query", "trace_from_spine", "signature_counts",
                    "_replay", "commit", "set_status", "record_outcome")},
    # 호스트의 점진공개(progressive disclosure) 경로. 프롬프트 주입은 호스트가 한다.
    **{f"recall.py:{name}": "호스트 주입 경로 공개 API"
       for name in ("list", "view", "view_annotated", "render_index",
                    "record_run_outcome")},
    # Protocol 선언과 다른 구현체. CLI 는 JSONL 원장 하나만 쓴다.
    **{f"ledger.py:{name}": "Protocol 선언 또는 CLI 가 안 쓰는 구현"
       for name in ("get", "list", "commit", "set_status", "record_outcome")},
    "ledger.py:sweep_deprecate": "오래된 스킬 정리. 호스트 유지보수 명령이다",
    # 기억은 실려야 돌고, 모순은 생겨야 판정한다. 초안이 0건이면 학습이 일찍
    # 끝나서 이 아래가 통째로 안 돈다 - 코드가 죽은 게 아니라 그날 재료가 없었던 것.
    **{f"memory.py:{name}": "기억이 실리고 모순이 생겨야 도는 자리"
       for name in ("measured", "expired", "valid_at", "to_dict", "from_dict",
                    "gain", "verdict", "measure", "apply_measurement", "_tokens",
                    "_has_negation", "_strip_negation", "resolve", "note",
                    "recall", "supersede", "resolve_by_time", "stamp")},
    **{f"discovery.py:{name}": "공개 API·Protocol 선언(호출자는 호스트)"
       for name in ("_attr", "render_all", "to_dict", "render", "name",
                    "discover", "by_risk", "_confidence")},
    "mcp_client.py:call_tool": "`ask --risky` 로 실제 호출할 때만. 감사가 남의 서버를 건드리지 않는다",
    "mcp_client.py:notify": "같은 이유",
    "mcp_client.py:_drain_stderr": "데몬 스레드에서 돈다. `sys.settrace` 는 그 스레드를 안 본다",
    **{f"registry.py:{name}": "import 시점에 돈다. 추적기를 걸기 전이라 안 잡힌다"
       for name in ("register", "names", "registry_decorator", "outer", "inner")},
    **{f"agent.py:{name}": "회상·렌더 경로는 호스트가 부른다(CLI 는 라우터를 쓴다)"
       for name in ("_attr", "_as_capability", "render", "recall")},
    **{f"drafter.py:{name}": "다른 프로바이더·장애조치 경로. 이 기계는 하나로 붙는다"
       for name in ("failover_completer", "anthropic_completer", "complete")},
    "constitution.py:needs_human_approval": "승인 필요 스코프일 때만",
    "constitution.py:diff": "규약 변경 제안을 비교할 때만",
    "constitution.py:block": "마크다운 직렬화 내부 도우미",
    **{f"router.py:{name}": "공개 API(호스트가 부른다)"
       for name in ("names", "render", "relevance")},
    "cli.py:score": "기억 채점 클로저. 기억이 실려야 불린다",
    "cli.py:cmd_serve": "MCP 서버는 별도 프로세스로 띄운다(smoke 가 확인)",
    "loop.py:on_promoted": "승격 훅. 호스트가 붙인다",
    "loop.py:summary": "공개 API",
    "model.py:signature": "중복 판정용. 같은 절차가 두 번 올 때만",
    "model.py:bump_patch": "patch 로 고칠 때만(create 경로에서는 안 돈다)",
    "synthesis.py:synthesize_config": "그 kind 의 초안이 나올 때만",
    "synthesis.py:synthesize_tool": "같은 이유",
    "curator.py:draft_from_signals": "LLM 없이 도는 폴백. 여기서는 LLM 이 있다",
    "gate.py:score": "채점기 어댑터. 러너를 직접 넘기면 안 돈다",
    "tools.py:as_bench_case": "공개 API",
    "registry.py:load_entry_points": "설치된 플러그인이 없으면 붙일 게 없다. 로더 자체는 돈다",
    "cli.py:serve": "MCP 서버는 별도 프로세스로 띄운다",
    "cli.py:main": "CLI 진입점. 여기서는 cmd_* 를 직접 부른다",
}


def functions_in_src() -> dict[tuple[str, int], str]:
    """(파일, 첫 줄) -> 함수 이름."""
    table: dict[tuple[str, int], str] = {}
    for path in SRC.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith("__"):
                    continue
                # 데코레이터가 있으면 code object 의 `co_firstlineno` 는 **데코레이터
                # 줄**을 가리킨다. def 줄로 맞춰 두면 그 함수는 아무리 돌아도 영영
                # 냉구간으로 잡힌다 - 실측: 신호 추출기 4개와 합성기 3개가 멀쩡히
                # 도는데 "안 돈다"고 나왔다. 검사기가 틀린 숫자를 내면 그 목록을
                # 아무도 안 믿는다.
                first = min([d.lineno for d in node.decorator_list] + [node.lineno])
                table[(path.name, first)] = node.name
    return table


class Tracer:
    def __init__(self) -> None:
        self.seen: set[tuple[str, int]] = set()

    def __call__(self, frame, event, arg):
        if event == "call":
            code = frame.f_code
            name = os.path.basename(code.co_filename)
            if name.endswith(".py"):
                self.seen.add((name, code.co_firstlineno))
        return None          # 줄 단위는 안 본다 (느려서)


def user_flows(home: Path) -> list[tuple[str, list[str]]]:
    """사용자가 CLI 로 실제로 하는 일. LLM 없이 도는 것만 - 판정을 모델에
    맡기지 않으려는 것과 같은 이유로, 죽은 경로 판정도 모델 없이 재현돼야 한다."""
    sessions = home / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    cases = home / "cases.csv"
    cases.write_text("a,b,expect\n1,2,3\n10,5,15\n7,7,14\n2,3,5\n8,1,9\n"
                     "4,4,8\n9,2,11\n6,3,9\n", encoding="utf-8")
    script = home / "adder.py"
    script.write_text(
        "def run(payload):\n    return payload['a'] + payload['b']\n",
        encoding="utf-8")
    out = home / "out"
    into = home / "into"
    os.environ["JERMES_AUDIT_CASES"] = str(cases)
    return [
        ("status", []),
        ("demo", ["demo"]),
        ("law", ["law"]),
        ("memory", ["memory"]),
        ("capabilities", ["capabilities"]),
        ("sessions", ["sessions"]),
        ("tool --script", ["tool", "adder", "--script", str(script),
                           "--cases", str(cases), "--task", "두 수를 더한다"]),
        ("run", ["run", "adder", "--payload", '{"a": 40, "b": 2}']),
        ("list", ["list"]),
        ("show", ["show", "adder"]),
        ("improve --check-only", ["improve", "adder", "--check-only"]),
        ("route", ["route", "두 수를 더하기"]),
        ("export", ["export", "adder", "--out", str(out)]),
        ("install", ["install", "--into", str(into)]),
        ("import", ["import", str(out / "adder"), "--as", "adder2"]),
        ("capabilities --live (MCP 실접속)", ["capabilities", "--live"]),
        ("route (캐시된 MCP 도구로)", ["route", "문서에서 검색해줘"]),
        ("doctor", ["doctor"]),
        ("law --adopt (규약 변경)", ["law", "--adopt",
                            '{"never_learn": ["내부주소"]}',
                            "--by", "audit"]),
        ("law (바뀐 규약)", ["law"]),
        ("memory (적재 뒤)", ["memory"]),
        # 사람이 **직접 가르치는** 창구. 개인화 에이전트에서 이보다 핵심인 흐름이
        # 없는데 여기 목록에 없었다. 그래서 `_memory_add`·`_memory_retire`·
        # `_memory_supersede`·`cmd_forget` 이 넷 다 미확인 냉구간으로 잡혔다.
        # 실사용 목록에 없으면 그 자리는 깨져도 아무도 모른다.
        ("memory --add (손으로 가르치기)",
         ["memory", "--add", "이 프로젝트의 기본 브랜치는 develop 이다"]),
        # id 앞부분만 준다. 사람도 그렇게 쓴다 - 전체 id(`hand-1-483920`)를
        # 타이핑하게 만들면 있는 기능이라도 안 쓰게 된다.
        ("memory --supersede (사실이 바뀌었을 때)",
         ["memory", "--supersede", "hand-1",
          "--add", "이 프로젝트의 기본 브랜치는 main 으로 바뀌었다"]),
        ("memory --retire (틀린 것을 내리기)", ["memory", "--retire", "hand-1"]),
        ("forget (잘못 쌓인 이력 지우기)", ["forget", "adder", "--all"]),
    ]


def llm_flows() -> list[tuple[str, list[str]]]:
    """LLM 이 있어야 도는 흐름. 이걸 빼면 `learn` 계열이 통째로 냉구간으로 잡혀
    판정이 무의미해진다 - 실제로 안 도는 것과 **여기서 안 돌린 것**은 다르다.

    세션은 아무거나 쓰면 안 된다. 모델이 `[]` 를 내는 세션을 고르면 초안이 0건이라
    큐레이터·게이트·벤치가 통째로 냉구간으로 잡히고, 그러면 멀쩡한 코드를 죽었다고
    읽게 된다. `--session` 은 초안이 실제로 나오는 것을 준다.
    """
    session = os.environ.get("JERMES_AUDIT_SESSION", "05dc3e35")
    return [
        ("learn (실제 세션에서 배우기)", ["learn", "--session", session,
                                 "--max-lines", "2000"]),
        ("ask (쿼리 한 줄로 끝까지)", ["ask", "두 수를 더하기 40 이랑 2"]),
        ("ask (MCP 도구로 가는가)", ["ask", "search the documents"]),
        ("capabilities --translate (우리말 힌트)", ["capabilities", "--translate"]),
        ("memory (학습 뒤 다시 읽기)", ["memory"]),
        ("tool (LLM 이 직접 작성)", ["tool", "adder-llm", "--task",
                             "a 와 b 를 더한다", "--cases",
                             str(Path(os.environ["JERMES_AUDIT_CASES"]))]),
        ("watch (새 세션만 배우기)", ["watch", "--per-round", "0",
                              "--limit", "3"]),
    ]


def main() -> int:
    print("실사용 커버리지 - 사용자가 다 해봐도 안 도는 함수를 찾는다\n")
    table = functions_in_src()
    tracer = Tracer()

    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        os.environ["JERMES_HOME"] = str(home / "jermes")
        os.environ["JERMES_CLAUDE_PROJECTS"] = str(home / "sessions")
        os.environ["JERMES_SOURCES"] = "claude-code"
        from jermes import cli

        flows = user_flows(home)
        if "--llm" in sys.argv:
            # 진짜 세션에서 배운다. 읽기만 한다.
            os.environ["JERMES_CLAUDE_PROJECTS"] = str(
                Path.home() / ".claude" / "projects")
            flows += llm_flows()
        else:
            print("  (LLM 흐름 제외 - `--llm` 을 붙이면 learn·ask 까지 잽니다)\n")
        for label, argv in flows:
            sink = io.StringIO()
            sys.settrace(tracer)
            code = 0
            try:
                with redirect_stdout(sink), redirect_stderr(sink):
                    code = cli.main(argv) or 0
            except SystemExit as exc:
                code = exc.code or 0
            except Exception as exc:
                sys.settrace(None)
                code = f"{type(exc).__name__}: {exc}"
            finally:
                sys.settrace(None)
            # 흐름이 **왜** 아무것도 안 했는지 보이지 않으면, 그 아래 냉구간을
            # 코드 탓으로 오독하게 된다. 실측: learn 이 조용히 0건을 내는 바람에
            # 학습 파이프라인 전체가 죽은 것처럼 보였다.
            tail = (sink.getvalue().strip().splitlines() or [""])[-1][:58]
            print(f"  {label:<30} [{code}] {tail}")
        print(f"흐름 {len(flows)}개 실행, 함수 진입 {len(tracer.seen)}곳\n")

    cold = []
    for (fname, lineno), func in sorted(table.items()):
        if (fname, lineno) in tracer.seen or fname in ALLOWED_COLD:
            continue
        cold.append((fname, lineno, func))

    known = [c for c in cold if f"{c[0]}:{c[2]}" in COLD_REASONS]
    unknown = [c for c in cold if f"{c[0]}:{c[2]}" not in COLD_REASONS]
    warm = len(table) - len(cold)
    print(f"실사용에서 도는 함수 {warm}/{len(table)}  "
          f"(설명된 냉구간 {len(known)}건 · **미확인 {len(unknown)}건**)\n")
    for fname, lineno, func in unknown:
        print(f"     {fname}:{lineno:<5} {func}")
    if unknown and "--llm" not in sys.argv:
        # LLM 흐름을 빼면 학습 계열이 통째로 냉구간으로 잡힌다. 그 숫자를 결함
        # 목록으로 읽으면 멀쩡한 코드를 죽었다고 오해한다.
        print("\n  (LLM 흐름을 안 돌렸습니다. 판정하려면 `--llm` 을 붙이세요)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

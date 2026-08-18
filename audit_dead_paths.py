"""죽은 경로 색출 - **명령이 성공하는데 아무 일도 안 하는 자리**를 찾는다.

`jermes learn` 이 그랬다. 명령은 성공하고 스킬은 원장에 쌓이는데, 게이트에 넘기는
채점기가 `lambda case, skill: 0.0` 이고 케이스가 `()` 라 **검증 경로가 통째로 죽어**
있었다. 숫자가 나오니까 도는 줄 알았다.

그런 자리는 눈으로 못 찾는다. 그래서 기계로 훑는다. 다섯 가지 냄새:

  A) 정의됐는데 아무도 안 부르는 함수      - 만들어 두고 안 쓰는 것
  B) 상수를 내는 가짜 콜러블을 진짜 기계에 넘김 - 계산하는 척
  C) 파싱만 하고 안 읽는 CLI 플래그        - 켜도 아무 일 없음
  D) 삼켜지는 예외                        - 실패가 성공으로 보임
  E) 늘 비어서 넘어가는 인자              - 받는 쪽이 놀고 있음

⚠️ 이 검사기는 **의심을 내놓을 뿐 판정하지 않는다.** 정당한 경우가 많다(테스트 도우미,
공개 API, 의도적 폴백). 그래서 아는 예외는 사유와 함께 적어 두고, 나머지는 사람이
하나씩 확인한다. 목록이 비는 것이 목표가 아니라 **모르는 것이 없는 것**이 목표다.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

SRC = Path(__file__).parent / "src" / "jermes"
TESTS = Path(__file__).parent / "tests"

# 이 데코레이터를 단 함수는 이름으로 불린다 - 호출자가 AST 에 안 보여도 살아 있다.
REGISTERING_DECORATORS = {"signal_extractor", "synthesizer"}

# 알고 있는 예외 - 왜 괜찮은지 적지 않은 항목은 여기 넣지 않는다.
ALLOWED_UNUSED = {
    # 공개 API. 호스트가 부르라고 만든 것이라 이 레포 안에 호출자가 없어도 맞다.
    "main", "cycle", "remember", "reconcile", "measure_memory",
    "sweep_deprecate", "record_run_outcome", "view", "view_annotated",
    "render_index", "propose", "adopt", "diff", "to_markdown", "from_markdown",
    "decay_unmeasured", "resolve_by_time", "improve_tool", "load_cases",
    "tool_package", "read_cases", "discover", "default_sources", "usable",
    "by_risk", "annotations", "preset", "granted", "describe", "to_dict",
    "from_dict", "as_bench_case", "judge", "line", "names", "summary",
    "render", "risk", "label", "expired", "valid_at", "measured",
    "add", "get", "list", "commit", "set_status", "record_outcome", "score",
    "bench_cases", "verdict", "thin", "check", "usd", "tokens",
    # BaseHTTPRequestHandler 가 이름으로 부르는 오버라이드. 우리 코드에 호출자가
    # 없는 게 맞다 - 있으면 오히려 잘못이다.
    "do_GET", "log_message",
}
# B) 상수 콜러블 예외 - 확인하고 정당하다고 판단한 자리.
REVIEWED_STUBS = {
    # 재현벤치 재료(실패→복구 쌍)가 없을 때의 폴백. 게이트는 measured=False 로
    # 가서 staged 를 내고, CLI 가 "재현벤치 0건" 이라고 **말한다**. 통과시키는
    # 가짜가 아니라 못 쟀다고 밝히는 자리다.
    "cli.py:cmd_learn",
}
ALLOWED_SWALLOW = {
    # 관측·발견은 부수 기능이다. 한 곳이 죽어도 본 기능이 멈추면 안 된다.
    "dashboard.py", "discovery.py",
}
# 확인하고 정당하다고 판단한 자리 - 사유 없이는 넣지 않는다.
# **줄번호가 아니라 함수 이름으로 건다.** 줄번호로 걸었더니 코드를 한 줄만 고쳐도
# 예외가 통째로 어긋나 "미확인"이 되살아났다. 예외가 쉽게 썩으면 아무도 안 믿는다.
REVIEWED_SWALLOW = {
    "ledger.py:_exclusive": "잠금 해제 실패는 되돌릴 것이 없다. 바로 뒤에서 fd 를 닫고, 닫으면 커널이 잠금을 푼다 - 여기서 던지면 정작 기록은 끝났는데 명령이 실패한 것처럼 보인다",
    "ledger.py:versions": "판본을 훑는 자리다. 찢어진 줄 하나 때문에 나머지 판본을 통째로 못 보면 되돌릴 수가 없다 - _replay 가 같은 줄을 이미 세고 list 가 알린다",
    "cli.py:_capability_caller": "모르는 via 는 원래 있는 일이다 - 아무도 그 경로를 안 등록했다는 뜻이지 고장이 아니다. 그러면 문서 스킬 취급으로 자연스럽게 넘어간다",
    "cli.py:load_memory": "기억 파일의 깨진 줄은 버린다. 추측해 채우면 그 위의 측정이 무의미해진다",
    "cli.py:load_constitution": "규약이 깨지면 기본 규약으로 간다. 규약 없이 도는 것보다 낫다",
    "cli.py:discover_endpoint": "엔드포인트 탐색. 안 뜨는 포트는 넘어가는 게 목적이다",
    "cli.py:replay": "재생 실패는 그 케이스 0점. 실패를 성공으로 세는 것보다 낫다",
    "drafter.py:_extract_json_array": "3층 JSON 복구의 한 층. 실패 수는 [drafter] 로그가 센다",
    "registry.py:load_entry_points": "플러그인이 깨져도 본체는 돈다. load_errors 에 남는다",
    "tools.py:load_cases": "옛 기록이나 손으로 넣은 것. improve 가 no_cases 로 보고한다",
    "tools.py:ast_scan": "문법이 깨진 초안은 트리로 못 본다. 그러면 정규식이 다시 "
                         "보고, 진입점 검사가 어차피 막는다",
    "mcp_client.py:__exit__": "stdin 을 못 닫아도 프로세스는 반드시 내려야 한다",
    "mcp_client.py:load_servers": "깨진 MCP 설정 파일 하나 때문에 나머지를 못 읽으면 안 된다",
    "mcp_client.py:call": "서버가 흘린 로그 줄은 JSON 이 아니다. 건너뛰는 게 목적이다",
    "mcp_client.py:_post": "SSE 는 event/id 줄이 섞여 온다. JSON 아닌 줄을 넘기는 게 목적이다",
    "tools.py:apply": "setsid 는 이미 세션 리더면 실패한다. 한계 자체는 이미 걸렸고 무리 묶기만 못 한 것이다",
    "tools.py:size_of": "쓰는 중에 재는 폴더다. 항목 하나가 사라지거나 잠겨 있어도 나머지를 계속 세야 한다 - 한 파일 때문에 감시가 멈추면 그게 구멍이다",
    "cli.py:_pooled_repro_cases": "세션 파일 하나가 사라졌거나 못 읽는다. 14,548개를 훑는 자리라 하나 때문에 멈추면 재료를 통째로 못 모은다",
    "cli.py:_remembered_endpoint": "기억해 둔 엔드포인트가 없거나 못 읽는다. 그러면 전체를 다시 찾으면 된다 - 이건 빠르기 위한 것이지 옳기 위한 것이 아니다",
    "cli.py:_remember_endpoint": "같은 이유. 못 남겨도 다음에 다시 찾을 뿐이다",
    "cli.py:_speak_utf8": "인코딩을 못 바꾸는 스트림이 있다(파이프로 감싼 것 등). 여기서 죽으면 본말이 뒤집힌다 - 화면 설정 때문에 명령이 안 도는 셈이다",
    "act.py:_as_json": "모델이 JSON 이 아닌 답을 냈다. 부르는 쪽이 '다음 단계를 "
                       "정하지 못했습니다'로 바꿔 화면에 띄우므로 조용하지 않다",
}


def defined_and_used() -> tuple[dict[str, str], set[str]]:
    defined: dict[str, str] = {}
    used: set[str] = set()
    for path in list(SRC.glob("*.py")) + list(TESTS.glob("*.py")) + \
            list(Path(__file__).parent.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        in_src = path.parent == SRC
        for node in ast.walk(tree):
            if in_src and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # 레지스트리에 데코레이터로 등록된 함수는 이름으로 불린다
                # (`SIGNAL_EXTRACTORS.items()`). AST 로는 호출자가 안 보이지만 살아
                # 있다. 이것을 이름 예외로 처리하면 나중에 진짜 죽은 추출기가 생겨도
                # 안 잡히므로, **등록 여부**로 판정한다.
                if any(isinstance(d, ast.Call) and getattr(d.func, "id", "")
                       in REGISTERING_DECORATORS for d in node.decorator_list):
                    used.add(node.name)
                if not node.name.startswith("__"):
                    defined.setdefault(node.name, f"{path.name}:{node.lineno}")
            if isinstance(node, ast.Name):
                used.add(node.id)
            if isinstance(node, ast.Attribute):
                used.add(node.attr)
    return defined, used


def _enclosing(tree, lineno: int) -> str:
    """그 줄을 감싸는 가장 안쪽 함수 이름. 예외를 **함수 단위로** 걸기 위한 것."""
    best, best_line = "?", -1
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        end = getattr(node, "end_lineno", node.lineno)
        if node.lineno <= lineno <= end and node.lineno > best_line:
            best, best_line = node.name, node.lineno
    return best


def stub_callables() -> list[str]:
    """상수를 내는 람다가 진짜 기계에 인자로 들어가는 자리."""
    found = []
    for path in SRC.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        for lineno, line in enumerate(text.splitlines(), 1):
            if "lambda" not in line:
                continue
            # 인자로 넘어가는 람다이면서 몸통이 상수인 것
            if re.search(r"lambda[^:]*:\s*(0(\.0)?|None|\[\]|\{\}|\"\"|''|True|False)\s*[),]",
                         line):
                found.append(f"{path.name}:{_enclosing(tree, lineno)}  "
                             f"(줄 {lineno}) {line.strip()[:60]}")
    return found


def unused_flags() -> list[str]:
    """argparse 로 받아 놓고 args.<dest> 로 안 읽는 플래그."""
    cli = (SRC / "cli.py").read_text(encoding="utf-8")
    dests = set()
    for match in re.finditer(r'add_argument\(\s*"(--)?([\w-]+)"(.*?)\)', cli, re.DOTALL):
        explicit = re.search(r'dest\s*=\s*"(\w+)"', match.group(3) or "")
        dests.add(explicit.group(1) if explicit
                  else match.group(2).replace("-", "_").lstrip("_"))
    read = set(re.findall(r"args\.(\w+)", cli)) | set(re.findall(r'"(\w+)"\s*,\s*0\)', cli))
    read |= set(re.findall(r'getattr\(args,\s*"(\w+)"', cli))
    return sorted(d for d in dests - read if d not in {"func", "command"})


def swallowed() -> list[str]:
    """예외를 먹고 아무 말도 안 하는 자리."""
    found = []
    for path in SRC.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        lines = text.splitlines()
        for index, line in enumerate(lines):
            if not re.match(r"\s*except\b", line):
                continue
            body = lines[index + 1].strip() if index + 1 < len(lines) else ""
            if body in ("pass", "continue") or re.match(r"return (\[\]|None|\"\"|0)$", body):
                if path.name in ALLOWED_SWALLOW:
                    continue
                found.append(f"{path.name}:{_enclosing(tree, index + 1)}  "
                             f"(줄 {index + 1}) except -> {body}")
    return found


def empty_arguments() -> list[str]:
    """**호출 인자로** 늘 빈 것이 넘어가는 자리 - 받는 쪽이 놀고 있을 수 있다.

    누산기 초기화(`cases = []` 뒤에 append 가 오는 것)는 정당하므로 세지 않는다.
    처음엔 그것까지 잡아 6건이 나왔는데 전부 오탐이었다 - 오탐이 많으면 목록을
    아무도 안 본다.
    """
    found = []
    for path in SRC.glob("*.py"):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.strip().startswith("#") or "def " in line:
                continue          # 주석과 기본값 정의는 정당하다
            # 키워드 인자로 빈 것이 넘어가는 자리만 (`foo(cases=(), ...)`)
            for match in re.finditer(r"[(,]\s*(\w*(?:cases|items|skills|tools|memory|"
                                     r"events|rows|examples))\s*=\s*(\(\)|\[\])", line):
                found.append(f"{path.name}:{lineno}  {match.group(0).lstrip('(, ')}")
    return found


def main() -> int:
    print("죽은 경로 색출 - 의심을 내놓을 뿐 판정하지 않는다\n")

    defined, used = defined_and_used()
    orphans = sorted((name, where) for name, where in defined.items()
                     if name not in used and name not in ALLOWED_UNUSED)
    print(f"A) 아무도 안 부르는 함수 - {len(orphans)}건")
    for name, where in orphans:
        print(f"     {where:<24}{name}")

    stubs = [s for s in stub_callables() if s.split("  ")[0] not in REVIEWED_STUBS]
    print(f"\nB) 상수를 내는 가짜 콜러블 - 미확인 {len(stubs)}건")
    for item in stubs:
        print(f"     {item}")

    flags = unused_flags()
    print(f"\nC) 파싱만 하고 안 읽는 플래그 - {len(flags)}건")
    for flag in flags:
        print(f"     --{flag.replace('_', '-')}")

    quiet = swallowed()
    unreviewed = [q for q in quiet if q.split("  ")[0] not in REVIEWED_SWALLOW]
    print(f"\nD) 삼켜지는 예외 - 확인 끝 {len(quiet) - len(unreviewed)}건 · "
          f"미확인 {len(unreviewed)}건")
    for item in unreviewed:
        print(f"     {item}")
    quiet = unreviewed

    empties = empty_arguments()
    print(f"\nE) 늘 빈 것이 넘어가는 자리 - {len(empties)}건")
    for item in empties:
        print(f"     {item}")

    total = len(orphans) + len(stubs) + len(flags) + len(quiet) + len(empties)
    print(f"\n의심 {total}건. 하나씩 확인하고, 정당하면 사유와 함께 예외에 넣는다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

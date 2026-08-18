"""`jermes` - 혼자 도는 명령줄.

한때 Jermes 는 호스트 플랫폼의 spine 이 있어야만 배울 수 있었다. 그래서 부속이었다.
이 CLI 는 그 의존을 끊는다 - 원장은 파일(JSONL), 학습 재료는 로컬 Claude Code 세션
기록, LLM 은 OpenAI 호환 엔드포인트면 무엇이든.

    jermes                           지금 이 컴퓨터에서 뭘 할 수 있는지 + 다음 한 줄
    jermes sessions                  배울 거리가 있는 세션 훑기
    jermes learn --session <id>      세션에서 스킬 초안 -> 게이트 -> 원장
    jermes tool <이름> --cases <파일>  절차를 실행 가능한 툴로 (쓰고 -> 돌려보고 -> 판정)
    jermes run <이름> --payload <JSON> 만든 툴 실행
    jermes list                      원장 보기
    jermes show <이름>                본문 보기
    jermes export <이름> [--out 디렉터리]   agentskills.io 표준 패키지로 내보내기
    jermes import <SKILL.md>         남의 스킬 들여오기(항상 staged)
    jermes memory                    기억 원장(신뢰·측정·보류) 보기
    jermes law                       규약 보기 / --adopt --by 로만 변경
    jermes install                   검증된 것을 다른 에이전트가 집는 자리에 설치
    jermes serve                     단조한 툴을 MCP 로 내주기(다른 에이전트가 호출)
    jermes demo                      LLM 없이 게이트가 실제로 가르는지 보여주기

원장 기본 위치: `~/.jermes/skills.jsonl` (`JERMES_HOME` 으로 변경).
LLM: `--base-url` / `--model` 또는 `JERMES_BASE_URL` / `JERMES_MODEL`.
쉼표로 여러 개를 주면 앞의 것이 죽었을 때 다음으로 넘어간다. 아무것도 안 주면
로컬에서 도는 것(Ollama·vLLM·LM Studio·llama.cpp)을 찾아 붙고 무엇에 붙었는지 밝힌다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from contextlib import contextmanager
import sys
import time
from pathlib import Path

from .agent import JermesAgent
from .constitution import Constitution
from .curator import Curator
from .drafter import (Budget, BudgetExceeded, EnsembleDrafter, LLMDrafter,
                      TargetedDrafter,
                      distill_facts, failover_completer, metered,
                      openai_chat_completer)
from .gate import ForgeGate, GateConfig
from .ledger import JsonlSkillLedger
from .loop import SkillForge
from .memory import from_dict as memory_from_dict
from .model import Unmeasurable, verified_mark
from .memory import to_dict as memory_to_dict
from .portable import candidate_from_skill_md, skill_package, validate_skill_md
from .bench import (ReproReplayRunner, capture_repro_rows,
                    cases_from_repro_rows, fix_examples,
                    generalize_requirements, recurring_failures,
                    recurring_fixes)
from .signals import extract_signals
from .sources import iter_session_files, load_trace, summarize_session
from .synthesis import synthesize


def _speak_utf8() -> None:
    """콘솔이 못 그리는 글자 하나에 프로그램이 죽지 않게 한다.

    실측: 한국어 윈도우 기본 콘솔(cp949)에서 `jermes --help` 와 `status` 가 첫 줄
    부터 `UnicodeEncodeError` 로 죽었다. 우리가 개발 내내 `PYTHONUTF8=1` 을 붙여
    다녀서 못 봤다. 안 켜지는 프로그램에는 다른 어떤 장점도 의미가 없다.

    남의 콘솔 기본값에 기대지 않고 우리가 세운다. 못 그리는 글자가 있어도 그
    글자만 대체 문자로 보이는 편이, 프로그램이 죽는 것보다 낫다.

    바꿀 수 없는 환경도 있다(파이프로 감싼 스트림 등). 그러면 그냥 넘어간다 -
    인코딩은 부수적인 설정이지 본 일이 아니고, 여기서 죽으면 본말이 뒤집힌다.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            # **인코딩은 안 바꾼다.** 사용자가 파이프로 넘긴 곳이 cp949 를 기대할
            # 수 있는데 우리가 UTF-8 로 덮으면 그쪽이 깨진다. 남의 환경을 바꾸는
            # 대신 우리가 못 쓰는 글자를 안 쓴다(그래서 경고표·em 대시를 다 뺐다).
            # 여기서 하는 일은 **예상 못 한 글자 하나에 죽지 않게** 하는 것뿐이다.
            stream.reconfigure(errors="replace")
        except Exception:
            pass


def home() -> Path:
    path = Path(os.environ.get("JERMES_HOME", Path.home() / ".jermes"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def open_ledger() -> JsonlSkillLedger:
    return JsonlSkillLedger(home() / "skills.jsonl")


def current_scope() -> str:
    """지금 일하는 자리. 기억을 가르는 열쇠다.

    **일하는 폴더에서 낸다.** 사용자가 따로 선언하게 하면 언젠가 안 하고, 안 한 그
    프로젝트에서 남의 기억이 섞인다. 폴더는 이미 거기 있고 틀릴 일이 없다.

    이름과 짧은 해시를 같이 쓴다. 이름만 쓰면 서로 다른 곳의 `backend` 두 개가
    같은 스코프가 되고, 해시만 쓰면 사람이 `jermes memory` 를 보고 무엇인지 모른다.

    `JERMES_SCOPE` 로 덮을 수 있다 - 한 저장소를 여러 갈래로 쓰거나, 여러 폴더를
    한 프로젝트로 묶고 싶은 경우가 있다.
    """
    override = os.environ.get("JERMES_SCOPE", "").strip()
    if override:
        return override
    # **세션이 쓰는 열쇠와 같은 것**을 낸다. 다른 열쇠를 내면 손으로 넣은 사실과
    # 배운 사실이 영영 안 만난다 - 둘 다 있는데 서로 못 보는 상태가 제일 나쁘다.
    from .sources.claude_code import project_key

    return f"project:{project_key(Path.cwd().resolve())}"


def memory_path() -> Path:
    return home() / "memory.jsonl"


def load_memory() -> list:
    """기억 불러오기. 깨진 줄은 버린다 - 추측해서 채우면 그 위의 측정이 무의미해진다."""
    path = memory_path()
    if not path.exists():
        return []
    items = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            items.append(memory_from_dict(json.loads(line)))
        except Exception:
            continue
    return items


def write_atomically(path: Path, text: str) -> None:
    """자르고-쓰지 않는다. 옆에 쓰고 **바꿔치기**한다.

    `path.write_text()` 는 파일을 먼저 0바이트로 자른 다음 쓴다. 그 사이에
    죽으면 파일이 반만 남거나 통째로 빈다. 여기서 다루는 것은 기억·규약·
    커서처럼 **다시 만들 수 없는** 것들이다. 배운 것이 날아가면 그걸 배우느라
    쓴 LLM 비용도 같이 날아간다.

    `os.replace` 는 윈도우에서도 원자적이다. 같은 폴더에 임시파일을 두는
    이유는 다른 볼륨으로 건너가면 원자성이 깨지기 때문이다.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()          # 바꿔치기가 실패했다. 쓰레기를 남기지 않는다


@contextmanager
def memory_change():
    """기억을 **읽고 고쳐 쓰는 동안** 다른 프로세스가 끼어들지 못하게 한다.

    기억 파일은 통째로 다시 쓴다(순서·대체관계가 파일 전체의 성질이라 그렇다).
    그러면 두 프로세스가 같이 고칠 때 나중에 쓴 쪽이 앞의 것을 **통째로** 덮는다.
    실측: 동시에 6건을 넣었더니 4건만 남았다. 원장(한 줄 덧붙이기)보다 나쁘다.

    `watch` 가 도는 중에 손으로 `memory --add` 를 하는 것이 문서에 적힌 사용법이라,
    이건 드문 경우가 아니다. 잠금은 `ledger._exclusive` 와 같은 것을 쓴다 - 잠그는
    법이 두 벌이면 언젠가 한쪽만 고치게 된다.
    """
    from .ledger import _exclusive

    path = memory_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = os.open(path.with_name(path.name + ".lock"),
                     os.O_WRONLY | os.O_CREAT, 0o644)
    try:
        with _exclusive(handle):
            yield
    finally:
        os.close(handle)


def save_memory(items, known=None) -> None:
    """**못 본 사실은 안 버린다.**

    기억 파일은 통째로 다시 쓴다(순서와 대체관계가 파일 전체의 성질이라 그렇다).
    그러면 두 프로세스가 같이 고칠 때 나중에 쓴 쪽이 앞의 것을 통째로 덮는다.
    실측: 동시에 6건을 넣었더니 4건만 남았다.

    잠금 안에서 **다시 읽어 병합**한다. 부르는 쪽이 들고 있는 항목은 부르는 쪽이
    이기고(방금 고친 것이니까), 부르는 쪽이 본 적 없는 항목은 그대로 둔다. 기억은
    지우는 물건이 아니라 내리는 물건이라(`retire`) 병합으로 잃을 것이 없다.
    """
    with memory_change():
        mine = {i.item_id for i in items}
        # `known` 을 준 쪽은 "내가 이만큼을 보고 시작했다" 고 말한 것이다. 그러면
        # 그중 지금 안 넘긴 것은 **일부러 뺀 것**이지 못 본 것이 아니다.
        #
        # 이 구분이 없으면 저장이 지우지를 못한다 - 실측으로, 하나를 빼고 저장했더니
        # 병합이 그대로 되살렸다. 지금은 그렇게 쓰는 코드가 없지만(내리기는 삭제가
        # 아니라 상태 변경이다), 없는 것과 못 하는 것은 다르고 그 차이는 조용히
        # 드러난다.
        removed = (set(known) - mine) if known is not None else set()
        merged = list(items)
        merged.extend(other for other in load_memory()
                      if other.item_id not in mine
                      and other.item_id not in removed)
        write_atomically(memory_path(), "".join(
            json.dumps(memory_to_dict(i), ensure_ascii=False) + chr(10)
            for i in merged))


def constitution_path() -> Path:
    return home() / "constitution.md"


def load_constitution() -> Constitution:
    """규약은 **파일**이다 - 사람이 열어보고 고칠 수 있어야 집행에 동의할 수 있다.
    에이전트는 이 파일을 스스로 고치지 않는다(`jermes law --adopt` 로만 바뀐다)."""
    path = constitution_path()
    if path.exists():
        try:
            return Constitution.from_markdown(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    law = Constitution()
    write_atomically(path, law.to_markdown())
    return law


# 흔히 쓰는 로컬 엔드포인트 - 설정 파일을 쓰게 만들기 전에 여기부터 두드려 본다.
# 목록은 넓히기만 하면 되고, 못 찾으면 예전처럼 이유를 말하고 멈춘다.
# `localhost` 가 아니라 `127.0.0.1` 이다. `localhost` 는 IPv6(::1) 를 먼저
# 푸는데 거기서 듣는 것이 없으면 그 대기시간을 통째로 문다. 실측: 죽은 포트
# 하나에 localhost 4.09초 · 127.0.0.1 2.03초. 우리가 찾는 것은 **로컬** 서버라
# IPv6 를 기다릴 이유가 없다.
LOCAL_ENDPOINTS = (
    "http://127.0.0.1:11434/v1",     # Ollama
    "http://127.0.0.1:12361/v1",     # vLLM
    "http://127.0.0.1:1234/v1",      # LM Studio
    "http://127.0.0.1:8000/v1",      # vLLM 기본
    "http://127.0.0.1:5001/v1",      # llama.cpp
)


def _remembered_endpoint() -> str:
    """지난번에 붙었던 곳. 없으면 빈 문자열."""
    try:
        return (home() / "endpoint").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _remember_endpoint(base: str) -> None:
    """다음 실행이 여기부터 보게 남긴다. 못 써도 그냥 넘어간다 - 이건 빠르기
    위한 것이지 옳기 위한 것이 아니다."""
    try:
        write_atomically(home() / "endpoint", base)
    except OSError:
        pass


def discover_endpoint(timeout: float = 3.0) -> tuple[str, str]:
    """돌고 있는 로컬 엔드포인트를 찾아 (base_url, model) 을 준다. 못 찾으면 ("","").

    설정을 요구하기 전에 찾아본다 - 첫 사용자가 다섯 줄짜리 환경변수 안내를 읽다가
    그만두면 그 도구는 없는 것과 같다. 찾은 것은 **반드시 화면에 밝힌다**(무엇에 붙었는지
    모르는 채로 도는 게 더 나쁘다).

    타임아웃이 넉넉한 이유: 1초로 뒀더니 SSH 터널 너머의 멀쩡한 엔드포인트가 잠깐
    느린 것만으로 "LLM 을 못 찾았습니다"가 떴다. 못 찾는 것보다 조금 기다리는 편이
    낫다 - 어차피 못 찾으면 어디를 봤는지 말하고 멈춘다.
    """
    import concurrent.futures
    import urllib.request

    def probe(base: str) -> tuple[str, str]:
        try:
            with urllib.request.urlopen(f"{base}/models",
                                        timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception:
            return "", ""
        models = [m.get("id") for m in payload.get("data", []) if m.get("id")]
        return (base, models[0]) if models else ("", "")

    # **동시에** 두드린다. 차례로 두드리면 앞의 것들이 죽어 있을 때 그 대기
    # 시간을 다 물고 나서야 살아 있는 것을 본다. 실측: 로컬 LLM 이 아예 없는
    # 사람이 16.5초를 기다린 뒤에야 "못 찾았습니다" 를 봤다 -> 2.13초.
    #
    # 다만 **목록 순서는 지킨다.** 빨리 답한 쪽이 이기게 하면 실행할 때마다
    # 다른 모델에 붙어, 어제와 오늘의 판정이 달라진다.
    # **지난번에 찾은 곳을 먼저 본다.** 살아 있으면 0.01초에 끝난다. 목록 순서를
    # 지키느라 앞의 죽은 포트를 매번 2초씩 기다릴 이유가 없다 - 어제 붙은 곳이
    # 오늘도 붙는 것이 보통이다.
    #
    # TTL 을 두지 않는다. 그 대신 **안 되면 그때 다시 찾는다**(아래). 시간으로
    # 만료시키면 멀쩡히 붙어 있는데도 주기적으로 느려지고, 정작 서버가 죽은
    # 순간에는 TTL 이 남아 있어 못 알아챈다.
    remembered = _remembered_endpoint()
    if remembered:
        base, model = probe(remembered)
        if base:
            return base, model

    with concurrent.futures.ThreadPoolExecutor(
            max_workers=len(LOCAL_ENDPOINTS)) as pool:
        for base, model in pool.map(probe, LOCAL_ENDPOINTS):
            if base:
                _remember_endpoint(base)
                return base, model
    return "", ""


def build_completer(args, budget=None, fast: bool = False):
    """LLM 연결. 없으면 **조용히 대충 하지 않고** 이유를 말하고 멈춘다.

    사용량은 `Budget` 하나를 지나간다 - 부르는 자리마다 세면 언젠가 한 곳을
    빠뜨리고, 빠뜨린 그 자리가 정확히 비용이 새는 자리가 된다.

    `fast` 는 **짧은 답만 필요한 자리**를 위한 것이다. 재생 벤치는 한 세션에서
    수십 번 도는데 거기까지 사고를 켜면 학습 한 번이 십 분을 넘긴다. 생각이 값을
    하는 자리(초안·증류)와 아닌 자리를 가르는 것이지, 아껴 쓰는 시늉이 아니다.
    """
    base = args.base_url or os.environ.get("JERMES_BASE_URL", "")
    model = args.model or os.environ.get("JERMES_MODEL", "")
    if not base or not model:
        found_base, found_model = discover_endpoint()
        if found_base:
            base, model = base or found_base, model or found_model
            print(f"[자동] LLM 을 찾았습니다: {base} · {model}")
    if not base or not model:
        raise SystemExit(
            "LLM 을 찾지 못했습니다. 로컬에서 도는 것이 없으면 직접 알려주세요.\n"
            "  --base-url 과 --model, 또는 JERMES_BASE_URL / JERMES_MODEL\n"
            "  (쉼표로 여러 개를 주면 장애조치가 됩니다)\n"
            f"  찾아본 곳: {', '.join(LOCAL_ENDPOINTS)}\n"
            "  예: jermes learn --base-url http://localhost:12361/v1 --model Qwen/Qwen3.6-35B-A3B-FP8")
    # 사고를 **켜 둔다**. 예전에는 껐고 상한도 900 이었는데, 그 조합이 품질을 깎고
    # 있었다. 실측(실세션 2개, 같은 모델): 사고끔·900 초안 2 · 사실 6 → 사고켬·4000
    # 초안 3 · 사실 10. 추론 모델에게 생각하지 말라고 하면 못 하는 게 당연하다.
    # 사고가 상한에 걸려 본문이 비면 `openai_chat_completer` 가 사고를 끄고 한 번 더
    # 묻는다 - 그래서 켜 두는 쪽이 위험하지 않다. 토큰은 계량기가 센다.
    extra = ({"chat_template_kwargs": {"enable_thinking": False}, "max_tokens": 400}
             if fast else {"max_tokens": 4000})
    bases = [b.strip() for b in base.split(",") if b.strip()]
    models = [m.strip() for m in model.split(",") if m.strip()]
    budget = budget if budget is not None else budget_from(args)
    # 요청 상한을 **남은 예산으로 조인다.** 안 조이면 상한을 지나서도 요청이 살아
    # 있고, 재시도·자리넓힘까지 곱해져 25분을 아무 말 없이 먹는다(실측).
    left = (lambda: budget.remaining_seconds) if budget else None
    if len(bases) == 1:
        complete = openai_chat_completer(base_url=bases[0], model=models[0],
                                         api_key=args.api_key, temperature=0.0,
                                         timeout=args.timeout, extra=extra,
                                         remaining=left)
    else:
        # `failover_completer` 는 **완성기 목록**을 받는다. 여기서 엔드포인트와
        # 모델을 짝지어 하나씩 만들어 넘긴다. 예전에는 `(bases, models, ...)` 를
        # 그대로 넘겨서 `--base-url a,b` 가 늘 TypeError 로 죽었다 - 문서에는
        # "쉼표로 여러 개를 주면 장애조치가 됩니다"라고 적혀 있는데도.
        pairs = [(b, models[i] if i < len(models) else models[-1])
                 for i, b in enumerate(bases)]
        complete = failover_completer(
            [openai_chat_completer(base_url=b, model=m, api_key=args.api_key,
                                   temperature=0.0, timeout=args.timeout,
                                   extra=extra, remaining=left)
             for b, m in pairs])
    return metered(complete, budget) if budget else complete


def budget_from(args):
    """플래그에서 예산을 만든다. 아무것도 안 주면 **상한 없이 세기만** 한다 -
    쓴 양을 모르는 것보다는 아는 편이 늘 낫다."""
    return Budget(max_calls=getattr(args, "max_calls", 0) or 0,
                  max_tokens=getattr(args, "max_tokens_budget", 0) or 0,
                  max_usd=getattr(args, "max_usd", 0.0) or 0.0,
                  max_seconds=getattr(args, "max_seconds", 0.0) or 0.0,
                  usd_per_1k=getattr(args, "usd_per_1k", 0.0) or 0.0)


# ───────────────────────────────────────────────────────── 명령들

def cmd_sessions(args) -> int:
    files = iter_session_files(args.root)
    if not files:
        print("세션 기록을 찾지 못했습니다. JERMES_CLAUDE_PROJECTS 로 경로를 지정하세요.")
        return 1
    print(f"세션 {len(files)}개 (최근순, 상위 {args.limit}개만 검사)\n")
    learnable = 0
    for path in files[:args.limit]:
        summary = summarize_session(path, max_lines=args.max_lines)
        if summary.worth_learning:
            learnable += 1
            print(f"  [배울거리] {summary.line()}")
        elif args.all:
            print(f"             {summary.line()}")
    print(f"\n배울 거리가 있는 세션: {learnable}/{min(args.limit, len(files))}")
    if not learnable:
        print("신호가 없는 이유는 대개 도구 호출이 적거나 오류·교정이 없어서입니다.")
    return 0


def _resolve_session(args) -> Path | None:
    files = iter_session_files(args.root)
    if args.session:
        for path in files:
            if args.session in path.stem:
                return path
        print(f"세션을 못 찾았습니다: {args.session}")
        return None
    for path in files[:args.limit]:
        if summarize_session(path, max_lines=args.max_lines).worth_learning:
            return path
    # 본 것까지만 말한다. 창 밖을 안 보고 "없다"고 하면 `sessions` 가 찾아낸
    # 것과 모순된다 - 실측: `learn --limit 1` 이 "없다"라는데 `sessions` 는 2건을 내놨다.
    if len(files) > args.limit:
        args._window_notice = True
        print(f"최근 {args.limit}개만 봤고 거기엔 없었습니다 "
              f"(전체 {len(files)}개). --limit 을 늘리거나 --session 으로 지정하세요.")
    return None


# 초안 만들기가 쓸 수 있는 예산의 몫. 나머지는 **재는 데** 쓴다 - 게이트
# 측정과 사실 증류가 그 뒤에 오고, 그 둘이 값을 내는 자리다.
_DRAFTING_SHARE = 0.45


def _replay_with(complete):
    """벤치가 부르는 재생 함수. 스킬을 넣었을 때와 뺐을 때 같은 상황을 다시 묻는다.

    채점은 **정규식**이 한다(`bench.Expectation`) - LLM 판정이 아니다. 그래서 판정
    품질이 모델 지능에 딸리지 않는다. 약한 모델이 쓴 스킬도 재생에서 실제로 도움이
    되면 살아남고, 좋아 보이기만 하면 죽는다.

    호출이 **한 번** 터지면 빈 문자열을 준다. 그러면 그 케이스는 0점이 되고 게이트는
    그만큼 보수적으로 판단한다 - 실패를 성공으로 세는 것보다 낫다.

    예산이 떨어진 것은 다르다. 그건 한 케이스가 아니라 **전부**가 실패하는 사정이라,
    0 점으로 세면 넣으나 빼나 같은 점수가 나와 `+0.000` 이 되고 게이트가 "도움이 안
    된다"고 확신에 차서 거절한다. 실측: 큰 세션 하나에서 LLM 호출 5회로 후보 5건을
    전부 그렇게 거절했다 - 한 번도 재 보지 않고서. 그건 못 잰 것이므로 그렇게 말한다.
    """
    from .drafter import BudgetExceeded
    def replay(payload: dict, skill) -> str:
        guide = ""
        if skill is not None:
            guide = "\n\n참고할 절차:\n" + skill.body[:1200] + "\n"
        try:
            return complete(_replay_prompt(payload, guide))
        except BudgetExceeded as why:
            raise Unmeasurable(str(why)) from why
        except Exception:
            return ""
    return replay


def _replay_prompt(payload: dict, extra: str = "") -> str:
    """재생할 때 모델에게 주는 말. **이 자리가 하나뿐이라야 한다.**

    예전에는 스킬을 잴 때와 기억을 잴 때가 각자 같은 프롬프트를 지어 썼다. 한쪽
    문구를 다듬으면 다른 쪽은 예전 문구로 남고, 그러면 두 숫자가 더 이상 같은
    자로 잰 값이 아니다 - 그런 어긋남은 터지지 않고 조용히 결과만 바꾼다.

    `extra` 는 넣어 보는 것(스킬 절차 또는 기억 한 줄)이다. 없으면 기준선이다.
    """
    return ("에이전트가 `" + str(payload.get("tool", "도구")) + "` 를 쓰다가 "
            "이렇게 실패했다:" + chr(10)
            + str(payload.get("error_detail", ""))[:400] + chr(10) + extra
            + chr(10) + "다음에 무엇을 할지 한두 문장으로 적어라. "
            "실패를 되풀이하지 마라.")


def _memory_score_with(complete, cases):
    """기억 항목 하나의 **기여를 직접 잰다** - 스킬을 재는 그 벤치로.

    이게 없어서 `기억 측정 안 함 - 점수 함수 미지정` 이 매번 찍혔다. 기억을 싣기는
    하는데 신뢰도가 영영 중립에 머물렀고, 측정·감쇠·모순판정 장치가 통째로 놀았다.
    "메모리 기반 자가개선" 이라고 해놓고 자가개선이 안 돌고 있었던 셈이다.

    스킬을 잴 때와 **똑같이** 한다: 그 항목을 넣었을 때와 뺐을 때 같은 상황을 다시
    묻고, 정규식으로 채점한다. 조회 횟수 같은 대리 신호는 쓰지 않는다.
    """
    by_id = {case.case_id: case for case in cases}

    def score(case, item) -> float:
        replay = by_id.get(case.case_id)
        if replay is None:
            return 0.0
        payload = replay.payload
        note = ""
        if item is not None:
            note = "\n\n알고 있는 사실:\n- " + item.text[:400] + "\n"
        try:
            return replay.expect.score(complete(_replay_prompt(payload, note)))
        except BudgetExceeded as why:
            # **예산이 떨어진 것은 0 점이 아니다.** 스킬 쪽은 이미 이렇게 고쳤는데
            # 기억 쪽은 그대로였다 - 예산이 끊기면 넣으나 빼나 0 점이라 이득이
            # 정확히 `+0.000` 이 되고, 그건 `neutral` 이라 신뢰가 안 움직인다.
            # 못 잰 것이 '재봤는데 차이 없음' 으로 둔갑한다. 실측: 측정 기록
            # 78건 중 67건이 gain 정확히 0.000 이었다.
            raise Unmeasurable(str(why)) from why
        except Exception:
            return 0.0

    return score


def _pooled_repro_cases(trace, args) -> tuple[list, int]:
    """재현 케이스를 **여러 세션에서** 모은다. 반환 (케이스, 세션 수).

    한 세션의 실패는 서로 무관한 잡탕이라, 스킬 하나가 관계된 케이스가 0~1건뿐이다
    (실측). 게이트가 관계된 것으로만 재기 시작하면 잴 거리가 아예 없다.

    실측: 세션 60개를 모으니 케이스 9건 -> 100건, `normalize-line-endings-for-git`
    의 관계된 케이스가 1건 -> 6건이 됐다. 재료가 없어서 못 재는 것과 재 봤더니
    안 도운 것은 다르다. 앞의 것은 세션을 더 보면 풀린다.

    배우는 대상 세션이 **먼저** 온다 - 지금 사람이 겪은 실패가 가장 관련 높다.
    모으는 것은 싸다(파싱뿐). 비싼 LLM 채점은 관계된 것만 고르고 `--max-bench` 로
    자르므로 오히려 줄어든다.
    """
    cases = cases_from_repro_rows(capture_repro_rows(trace))
    seen = {c.case_id for c in cases}
    want = getattr(args, "bench_cases", 0) or 0
    scan = getattr(args, "bench_sessions", 0) or 0
    sessions = 1
    if want > len(cases) and scan > 1:
        # **세션 수가 아니라 재료 수**를 목표로 삼는다. 요즘 세션은 대부분 실패가
        # 없는 짧은 서브에이전트 실행이라, 최근 80개를 훑어도 재료를 낸 것은 3개
        # 뿐이었다(케이스 15건). 파일을 세면 있는 재료를 놓친다.
        #
        # 그리고 **작은 세션은 건너뛴다.** 이 기계에서 실측: 세션 14,548개 중
        # 100KB 를 넘는 것이 618개인데, 최근 200개 안에는 14개뿐이었다. 최근순
        # 으로만 훑으면 진짜 재료의 97.7% 를 못 본다. 618개를 다 읽으면 케이스가
        # 639건이고 되풀이 해법이 PYTHONIOENCODING 22번·grep 23번으로 두껍다 -
        # 얇았던 것은 재료가 아니라 내가 본 창이었다.
        #
        # 크기는 이미 알고 있다(`scandir` 이 준다). 읽지 않고 거르므로 공짜다.
        floor = getattr(args, "bench_min_bytes", 0) or 0
        for path in iter_session_files(args.root)[:scan]:
            if len(cases) >= want:
                break
            try:
                if floor and path.stat().st_size < floor:
                    continue      # 도구 몇 번 부르고 끝난 세션이다
            except OSError:
                continue
            if path.stem == trace.run_id or str(path.stem) in trace.run_id:
                continue
            try:
                more = cases_from_repro_rows(
                    capture_repro_rows(load_trace(path, max_lines=args.max_lines)))
            except Exception:
                continue      # 세션 하나가 깨졌다고 나머지를 못 쓰면 안 된다
            fresh = [c for c in more if c.case_id not in seen]
            if not fresh:
                continue
            sessions += 1
            seen.update(c.case_id for c in fresh)
            cases.extend(fresh)
    return cases, sessions


def cmd_learn(args) -> int:
    path = _resolve_session(args)
    if path is None:
        if not getattr(args, "_window_notice", False):
            print("배울 거리가 있는 세션이 없습니다. `jermes sessions` 로 확인하세요.")
        return 1
    trace = load_trace(path, max_lines=args.max_lines)
    hits = extract_signals(trace)
    print(f"세션 {path.name}")
    print(f"  도구 {len([e for e in trace.events if e.type == 'tool_call'])}건 · "
          f"신호 {len(hits)}건 ({', '.join(sorted({h.signal for h in hits})) or '없음'})")
    if not hits:
        print("  신호가 없어 배울 것이 없습니다(추측해서 만들지 않습니다).")
        return 0

    budget = budget_from(args)
    # **초안 만들기에 앞 몫만 준다.** 초안은 재료일 뿐이고 재 보지 않은 초안은
    # 값이 0 이다. 실측: 큰 세션에서 초안 만들기가 시간 상한 420초를 통째로 먹고,
    # 그 뒤의 사실 증류가 0건, 게이트는 한 번도 재 보지 못한 채 후보 5건을
    # 내보냈다. 여섯 개를 쓰고 하나도 못 재느니 두 개를 쓰고 재는 편이 낫다.
    complete = build_completer(args, budget.spending_at_most(_DRAFTING_SHARE))
    # 재생 벤치는 케이스마다 두 번(스킬 넣고/빼고) 도니까 금세 수십 회가 된다.
    # 답도 한두 문장이라 생각이 값을 하지 않는다. 같은 계량기를 지나간다.
    quick = build_completer(args, budget, fast=True)
    # **초안보다 먼저** 모은다. 되풀이되는 실패가 무엇인지 알아야 드래프터가
    # 잴 수 있는 주제를 고른다. 모으는 것은 파싱뿐이라 싸다.
    cases, pooled_from = _pooled_repro_cases(trace, args)
    # **한 번뿐인 값을 요구조건에서 걷어낸다.** 그때 그 자리의 경로를 요구하면
    # 외워야만 통과하는데, 홀드아웃은 정확히 외운 것을 거절하려고 있다.
    # 되풀이되는 기법(grep·utf-8·PYTHONIOENCODING)만 남긴다 - 그게 배울 거리다.
    cases = generalize_requirements(cases)
    repeats = recurring_failures(cases)
    # 벤치가 채점하는 것은 **해법 쪽**이다. 무엇이 깨지는지만 알려 주면 모델은
    # 좋은 일반 조언을 쓰고, 그건 옳지만 잴 수가 없다(실측: 그런 스킬이 관계된
    # 케이스 2건에 그쳐 최소치에 못 미쳤다).
    tips = recurring_fixes(cases)
    drafter = EnsembleDrafter(LLMDrafter(complete, recurring=repeats, fixes=tips),
                              samples=args.samples)
    drafted = drafter.draft(trace, hits)
    # **되풀이된 해법마다 하나씩 더 쓰게 한다.** 프롬프트에 목록을 적어 줘도
    # 모델은 트레이스에 끌려 한 번 일어난 일을 고른다(실측: 세 번 연속 그랬고
    # 전부 dev +0.000 으로 거절). 주제를 측정이 정하게 한다.
    drafted = drafted + TargetedDrafter(complete).draft(
        trace, tips, lambda token: fix_examples(cases, token))
    # **같은 이름이 두 번 오면 한 번만 잰다.** 앙상블과 겨냥 드래프터가 같은
    # 결론에 닿는 일은 흔하다. 실측: 한 회차에서 같은 스킬을 두 번 재고 두 번
    # 거절했다 - 관계된 케이스 12건 × 2회 = 재생 24회를 그냥 버린 셈이다.
    by_name = {}
    for item in drafted:
        by_name.setdefault(item.name, item)
    if len(by_name) < len(drafted):
        print(f"  초안 {len(drafted)}건 중 이름이 겹치는 "
              f"{len(drafted) - len(by_name)}건은 한 번만 잽니다")
    drafted = list(by_name.values())
    # 0건이면 여기서 세지 않는다 - 바로 아래에서 한 번 더 써 보고, 그 결과를
    # 센다. 예전에는 `초안 0건` 을 찍고 다시 쓴 뒤 또 찍어서 같은 줄이 두 번
    # 나왔다(그리고 첫 줄은 이미 틀린 값이었다).
    if drafted:
        print(f"  초안 {len(drafted)}건")
    if not drafted and budget.remaining_seconds > 30:
        # **잴 것이 없으면 앞 몫을 아낄 이유가 없다.** 초안 몫(45%)은 뒤에 오는
        # 측정과 증류에 자리를 남기려고 있는 것인데, 초안이 0건이면 남겨 둔 자리를
        # 쓸 일이 애초에 없다. 실측: 757KB 세션에서 겨냥 드래프터가 189초 몫을
        # 다 쓰고 0건으로 끝났고, 남은 230초를 그대로 버린 채 그 세션에서 아무것도
        # 안 배웠다.
        print(f"  초안 0건 - 남은 {budget.remaining_seconds:.0f}초로 한 번 더 씁니다")
        drafted = EnsembleDrafter(
            LLMDrafter(build_completer(args, budget), recurring=repeats,
                       fixes=tips), samples=1).draft(trace, hits)
        if drafted:
            print(f"  초안 {len(drafted)}건")
    if not drafted:
        # 왜 0건인지 말한다. "못 찾았다"와 "호출이 실패했다"는 다른 사건이고,
        # 뭉뚱그리면 사용자가 모델을 탓하면서 정작 타임아웃을 못 본다.
        why = getattr(LLMDrafter, "last_reason", "") or "모델이 쓸 만한 것을 못 찾음"
        print(f"  초안 0건 - {why}")
        print("  (빈 결과를 폴백으로 채우지 않습니다.)")
        return 0

    ledger = open_ledger()
    law = load_constitution()
    memory = load_memory()
    before_memory = len(memory)
    # 에이전트 사이클: 기억 적재 -> 학습 -> 화해 -> 보고. 규약은 게이트가 집행한다
    # (프롬프트로 부탁하는 게 아니라 벤치 앞단에서 막는다).
    # 재현벤치를 세션에서 직접 만든다. 이게 없으면 게이트는 케이스 부족으로 늘
    # staged 를 내고, 단독 실행에서는 **무엇도 검증될 수 없다** - 그러면 "검증하고
    # 배운다"는 말이 단독 경로에서 성립하지 않는다. 실패에서 복구한 자리를 기계가
    # 채점할 수 있는 기대로 바꾼다: 그때의 실패 표식은 안 나와야 하고, 복구의 낱말은
    # 나와야 한다.
    # 벤치는 케이스마다 두 번, 초안마다 다시 돈다. 실측: Codex 세션 하나에서
    # 케이스 38건 · 초안 3건이 나와 LLM 232회 · 108k 토큰을 썼다. 재료가 많은
    # 것은 좋은 일이지만 전부 돌릴 이유는 없다 - 최소치(4건)의 몇 배면 판정이
    # 흔들리지 않는다. **고르게 솎고, 몇 건을 뺐는지 말한다.**
    # **여기서 자르지 않는다.** 자르면 관계도 필터가 볼 때 남은 게 없다.
    # 실측: 68건을 모아 56건을 버린 다음 필터를 걸었더니 관계된 것이 다시
    # 0~1건이 됐다. 상한은 게이트가 **후보마다** 건다(GateConfig.max_cases).
    #
    # 기억 측정은 다르다. 사실은 특정 도구에 관한 것이 아니라 그날 일 전반에
    # 관한 것이라 관계도로 좁힐 수 없다. 그쪽만 고르게 솎은 표본을 쓴다.
    memory_cases = cases
    if args.max_bench and len(cases) > args.max_bench:
        step = len(cases) / args.max_bench
        memory_cases = [cases[int(i * step)] for i in range(args.max_bench)]
    if pooled_from > 1:
        print(f"  재현 재료를 세션 {pooled_from}개에서 모았습니다 "
              f"(한 세션의 실패는 서로 무관해서, 같은 실패가 여러 번 나와야 잽니다)")
    if cases:
        runner = ReproReplayRunner(_replay_with(quick), cases)
        # 예산은 **여기서** 정한다. 게이트는 준 것을 잴 뿐이다.
        gate = ForgeGate(runner, GateConfig(max_cases=args.max_bench))
        bench_cases = runner.bench_cases()
        need = gate.config.min_cases
        note = ""
        if len(bench_cases) < need:
            # "벤치 1건" 이라고만 하면 잴 것처럼 들린다. 실제로는 최소치에 못 미쳐
            # 게이트가 한 번도 판정하지 않고 전부 대기로 간다 - 그 사실을 미리 말한다.
            note = f" - 최소 {need}건에 못 미쳐 이번엔 못 잽니다(전부 대기)"
        print(f"  재현벤치 {len(bench_cases)}건 (실패에서 복구한 자리를 자동 포착)"
              + f" · 후보마다 관계된 것 최대 {gate.config.max_cases}건으로 잽니다"
              + note)
        # **되풀이 신호가 얼마나 두꺼운지 말한다.** 아무것도 승격 안 됐을 때
        # 사용자는 파이프라인이 고장난 건지 재료가 얇은 건지 알 수 없다.
        # 실측: 같은 기계에서 세션 구성이 바뀌자 제일 흔한 기법이 4번에서
        # 3번으로 떨어졌다 - 이건 코드로 못 고치는 재료량 문제다.
        if tips:
            best = ", ".join(f"{label}({n}번)" for label, n in tips[:3])
            print(f"  되풀이된 해법: {best}")
            if tips[0][1] < 4:
                print("    (제일 흔한 것도 3번 이하입니다. 같은 실패가 더 쌓여야"
                      " 잴 수 있는 스킬이 나옵니다 - 지금은 재료가 얇습니다.)")
        else:
            print("  되풀이된 해법 없음 - 같은 방법으로 두 번 이상 고친 적이"
                  " 없습니다. 스킬로 굳힐 거리가 아직 없다는 뜻입니다.")
    else:
        # 재료가 없으면 없다고 말한다. 0.0 을 내는 가짜 채점기로 통과시키지 않는다.
        gate, bench_cases = ForgeGate(lambda case, skill: 0.0), ()
        print("  재현벤치 0건 - 실패·복구 쌍이 없어 검증은 못 하고 대기로만 남깁니다.")
    # 이 원천은 정제 기억을 주지 않는다(대화 원문은 사실이 아니므로 안 주는 것이
    # 맞다). 그래서 기억 적재가 늘 0건이었고 측정·신뢰도·유효창 장치가 통째로
    # 놀았다. 원문을 복사하지 않고 **사실만 증류해** 그 자리를 채운다.
    if not trace.lessons and not trace.refined_memory:
        # **증류는 초안이 아니다.** 앞 몫으로 잘린 완성기를 주면 초안이 그
        # 몫을 다 쓴 뒤라 증류가 늘 0건이 된다(실측: 큰 세션에서 그랬다).
        # 기억은 스킬과 나란한 산출물이지 초안의 부산물이 아니다.
        trace.lessons = distill_facts(build_completer(args, budget), trace)
        if trace.lessons:
            print(f"  사실 증류 {len(trace.lessons)}건 (규약을 통과한 것만 실립니다)")
        else:
            # 0 건이면 **왜** 0 건인지 말한다. 조용한 0 은 결과가 아니라 결함이다.
            # 실측: 엔드포인트가 느릴 때 6회 중 6회가 TimeoutError 였는데 화면에는
            # 아무 말도 안 나오고 "기억 +0" 으로만 끝났다.
            why = getattr(distill_facts, "last_error", "") or "뽑을 사실이 없음"
            print(f"  사실 증류 0건 - {why}")
    agent = JermesAgent(ledger, gate,
                        memory=memory, constitution=law)
    # 기억도 **잰다**. 스킬을 재는 그 벤치로, 항목을 넣고/빼고 재생해서.
    # 안 넘기면 `기억 측정 안 함 - 점수 함수 미지정` 이 찍히고 신뢰도가 영영
    # 중립에 머문다. 재현 케이스가 없으면 잴 것이 없으므로 그대로 둔다.
    memory_score = _memory_score_with(quick, memory_cases) if cases else None
    report = agent.cycle(trace, bench_cases=bench_cases, drafted=drafted,
                         memory_score=memory_score,
                         memory_measure_limit=args.max_memory_measure)
    save_memory(agent.memory)

    print(f"  {report.summary()}")
    for name in report.rejected:
        print(f"  · {name}: rejected(규약/중복)")
    for text, why in agent.blocked_memories:
        print(f"  · 기억 차단: {why} - {text[:60]}")
    added = len(agent.memory) - before_memory
    if added:
        print(f"  기억 +{added}건 → {memory_path()}")
    if report.disputed:
        print(f"  [주의] 모순 보류 {len(report.disputed)}건 - `jermes memory` 로 확인하세요.")
    print(f"\n원장: {home() / 'skills.jsonl'}")
    print(f"  {budget.summary()}")
    return 0


def _memory_add(args, law) -> int:
    """손으로 사실 하나를 넣는다. **규약을 거치고, 미측정으로 남는다.**

    사람이 적었다는 것은 그 사실이 맞다는 증거가 아니다. 그래서 신뢰도는 중립에서
    시작하고 측정으로만 움직인다 - 이 레포의 규율을 손으로 넣었다고 비켜 갈 수는
    없다.
    """
    from .memory import MemoryItem

    text = args.add.strip()
    if not text:
        print("무엇을 기억할지 적어 주세요.")
        return 1
    blocked = law.check_text(text)
    if blocked:
        print(f"안 실었습니다: {blocked}")
        return 1

    items = load_memory()
    if any(item.text.strip() == text for item in items):
        print("이미 같은 사실이 있습니다.")
        return 0
    # 기본은 **이 프로젝트**다. 사람 자신에 대한 사실은 `--global` 로 넣는다 -
    # 그건 어느 프로젝트에서나 참이라 스코프를 가릴 이유가 없다.
    scope = "user" if getattr(args, "global_scope", False) else current_scope()
    item = MemoryItem(item_id=f"hand-{len(items) + 1}-{abs(hash(text)) % 10**6}",
                      text=text, scope=scope, source_run_ids=[])
    items.append(item)
    save_memory(items)
    print(f"기억했습니다: {item.item_id} [{item.scope}]")
    print("  (손으로 넣은 것도 미측정입니다. 신뢰는 재봐야 움직입니다.)")
    return 0


def _short_id(item_id: str, width: int = 14) -> str:
    """목록에 넣을 만큼만. 앞부분이 곧 찾는 열쇠다(`_find_memory` 참고)."""
    return item_id if len(item_id) <= width else item_id[:width - 1] + "…"


def _find_memory(items, wanted: str):
    """id 를 **앞부분만** 쳐도 찾는다. 반환 `(항목, 안내문)`.

    id 는 `hand-1-483920` 처럼 생겼다. 그걸 통째로 타이핑하게 두면 있는 기능이라도
    안 쓰게 된다. 다만 애매하면 고르지 않는다 - 여러 개에 걸리는데 하나를 골라
    내리면, 사람이 의도하지 않은 기억이 조용히 사라진다.
    """
    wanted = (wanted or "").strip()
    if not wanted:
        return None, "어느 것인지 적어 주세요."
    exact = [i for i in items if i.item_id == wanted]
    if exact:
        return exact[0], ""
    hits = [i for i in items if i.item_id.startswith(wanted)]
    if not hits:
        return None, f"그런 id 가 없습니다: {wanted}  (`jermes memory` 가 id 를 보여 줍니다)"
    if len(hits) > 1:
        names = ", ".join(i.item_id for i in hits[:4])
        return None, f"'{wanted}' 로 시작하는 것이 {len(hits)}개입니다: {names}"
    return hits[0], ""


def _memory_retire(args) -> int:
    """틀린 기억을 내린다. **지우지 않는다** - 무엇을 믿었었는지가 남아야 한다."""
    items = load_memory()
    item, why = _find_memory(items, args.retire)
    if item is None:
        print(why)
        return 1
    item.status = "retired"
    save_memory(items)
    print(f"내렸습니다: {item.item_id} (지워지지 않았고 회상에서만 빠집니다)")
    print(f"  {item.text[:80]}")
    return 0


def _memory_supersede(args, law) -> int:
    """옛 사실을 새 사실로 대체한다. 옛것은 남고 회상에서만 빠진다."""
    from .memory import MemoryItem, supersede

    text = (args.add or "").strip()
    if not text:
        print("--supersede 에는 --add '<새 사실>' 이 필요합니다.")
        return 1
    blocked = law.check_text(text)
    if blocked:
        print(f"안 실었습니다: {blocked}")
        return 1

    items = load_memory()
    old, why = _find_memory(items, args.supersede)
    if old is None:
        print(why)
        return 1
    new = MemoryItem(item_id=f"hand-{abs(hash(text)) % 10**6}", text=text,
                     scope=old.scope, source_run_ids=[])
    # `memory.supersede` 는 일부러 시계를 안 읽는다(같은 입력이 날마다 다른 답을
    # 내면 시험도 못 한다). 그래서 **부르는 쪽이** 시각을 준다. 빈 문자열을 주면
    # 유효창이 안 닫혀서 옛 사실이 계속 회상된다 - 실측으로 그렇게 됐다.
    from datetime import datetime, timezone

    when = args.at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    supersede(old, new, when=when, reason="사용자가 직접 대체")
    items.append(new)
    save_memory(items)
    print(f"대체했습니다: {old.item_id} -> {new.item_id}")
    print("  (옛 사실은 남습니다. \"그때는 무엇이 참이었나\" 를 물을 수 있어야 합니다.)")
    return 0


def _memory_story(item) -> str:
    """이 사실 하나의 **전부** - 어디서 왔고, 뭐라고 판단했고, 그래서 지금 뭔가.

    없던 화면이다. 목록은 한 줄씩만 보여 주고 `--retire`·`--supersede` 는 손잡이일
    뿐이라, "이 사실을 왜 0.50 으로 보고 있나" 를 물을 데가 없었다. 신뢰가
    측정으로만 움직인다고 말하는 물건이면 그 측정을 볼 수 있어야 한다.
    """
    shots = (item.evidence or {}).get("measurements") or []
    lines = [f"기억 {item.item_id}  [{_memory_mark(item)}]  scope={item.scope}",
             f"  사실    {item.text}"]
    if item.source_run_ids:
        lines.append(f"  원천    {', '.join(item.source_run_ids[:2])}")
    lines.append(f"  상태    {item.status}"
                 + (f" · {item.valid_from}~{item.valid_until}"
                    if item.valid_from or item.valid_until else ""))
    if item.superseded_by:
        lines.append(f"  대체됨  -> {item.superseded_by}")
    if shots:
        lines.append(f"  측정    {len(shots)}회")
        for shot in shots[-3:]:
            lines.append(f"    · 케이스 {shot.get('cases')}건 · "
                         f"이득 {shot.get('gain', 0):+.3f} · {shot.get('verdict')}")
    else:
        lines.append("  측정    없음 - 아직 재본 적이 없습니다"
                     " (신뢰 0.50 은 중립이지 근거가 아닙니다)")
    for entry in (item.history or [])[-3:]:
        lines.append(f"  이력    {entry}")
    return chr(10).join(lines)


def cmd_memory(args) -> int:
    law = load_constitution()
    if args.add and not args.supersede:
        return _memory_add(args, law)
    if args.retire:
        return _memory_retire(args)
    if args.supersede:
        return _memory_supersede(args, law)

    if getattr(args, "show", ""):
        wanted = args.show.strip()
        found = [i for i in load_memory()
                 if i.item_id == wanted or wanted in i.item_id]
        if not found:
            print(f"그런 기억이 없습니다: {wanted}")
            return 1
        for item in found[:5]:
            print(_memory_story(item))
            print()
        return 0
    items = load_memory()
    if not items:
        print("기억이 비어 있습니다. `jermes learn` 이 교훈·정제기억을 적재합니다.")
        print("  손으로 가르치려면: jermes memory --add \"<사실>\"")
        return 0
    # **id 를 같이 낸다.** 예전에는 안 냈는데, `--retire`/`--supersede` 는 정확한
    # id 를 요구한다. 그러면 사람은 넣을 수는 있어도 고치거나 내릴 방법이 없다 -
    # 화면에 없는 값을 타이핑할 수는 없으니 JSONL 을 직접 열어야 했다. 가르치는
    # 창구를 열어 두고 되돌리는 문을 잠가 둔 셈이었다.
    print(f"{'id':<16}{'항목':<38}{'신뢰':<8}{'상태':<10}측정")
    for item in items:
        measured = "측정됨" if item.measured else "미측정"
        # 대체된 것은 `status` 가 그대로 `active` 다(일부러 - 지우지 않는다는 뜻).
        # 그러나 화면에 `active` 로만 찍으면 서로 반대되는 두 사실이 똑같이 살아
        # 있는 것처럼 보인다. 회상은 이미 새것만 내주는데 목록만 거짓말을 하는 꼴이라,
        # 사람이 무엇을 믿을지 화면에서 못 고른다.
        state = "대체됨" if item.superseded_by \
            else ("만료" if item.expired else item.status)
        print(f"{_short_id(item.item_id):<16}{item.text[:36]:<38}"
              f"{item.trust:<8.2f}{state:<10}{measured}")
    replaced = [i for i in items if i.superseded_by]
    if replaced:
        # 보류와 같은 자리에 같은 모양으로. 표 안에 넣으면 칸이 붙어 읽히지 않는다.
        print(f"\n대체됨 {len(replaced)}건 - 옛 사실은 남고 회상에서만 빠집니다:")
        for item in replaced:
            print(f"  · {item.item_id} -> {item.superseded_by}")
    disputed = [i for i in items if i.status == "disputed"]
    if disputed:
        print(f"\n보류 {len(disputed)}건 - 서로 반대되는 사실이라 회상에서 제외됩니다:")
        for item in disputed:
            for line in item.history[-1:]:
                print(f"  · {item.item_id}: {line}")
    print(f"\n{memory_path()}")
    print("신뢰는 **측정으로만** 움직입니다(조회·편집 같은 대리 신호를 쓰지 않습니다).")
    return 0


def cmd_law(args) -> int:
    law = load_constitution()
    if args.adopt:
        try:
            changes = json.loads(args.adopt)
        except ValueError:
            print("--adopt 는 JSON 이어야 합니다. 예: --adopt '{\"principles\": [\"...\"]}'")
            return 1
        if not args.by:
            print("규약 변경에는 --by <승인자> 가 필요합니다(에이전트가 스스로 못 바꿉니다).")
            return 1
        # **무엇을 껐는지 먼저 말한다.** `--adopt` 는 목록을 갈아치운다. 규칙
        # 하나를 더하려고 `never_learn` 에 두 줄을 주면 기본으로 있던 비밀
        # 보호(비밀번호·API 키·토큰 정규식)가 통째로 사라진다. 바뀐 값을 나란히
        # 찍기는 했지만, 그건 "적용" 으로 읽히지 "방금 보호를 껐다" 로 읽히지
        # 않는다. 막지는 않는다 - 규약의 주인은 사람이다. 다만 모르고 지나가지
        # 않게 한다.
        dropped = [rule for rule in law.never_learn
                   if rule not in (changes.get("never_learn") or law.never_learn)]
        applied = law.adopt(changes, approved_by=args.by)
        write_atomically(constitution_path(), law.to_markdown())
        if dropped:
            print(f"[주의] never_learn 에서 {len(dropped)}줄이 빠집니다 - "
                  "이제 이것들은 걸러지지 않습니다:")
            for rule in dropped:
                print(f"    - {rule[:88]}")
        print("적용:" if applied else "바뀐 것이 없습니다.")
        for line in applied:
            print(f"  · {line}")
        return 0
    print(law.to_markdown())
    print(f"\n{constitution_path()}")
    print("`never_learn` 은 게이트가 벤치 앞단에서 집행합니다 - 프롬프트 부탁이 아닙니다.")
    print("변경은 `jermes law --adopt '<json>' --by <이름>` 으로만 가능합니다.")
    return 0


def cmd_list(args) -> int:
    ledger = open_ledger()
    records = ledger.list()
    # 못 읽은 줄이 있으면 **먼저** 말한다. 목록이 짧은 이유가 "안 배웠다" 인지
    # "못 읽었다" 인지는 사람이 할 일을 가른다.
    torn = getattr(ledger, "skipped_lines", 0)
    if torn:
        print(f"[주의] 원장에서 못 읽은 줄 {torn}개를 건너뛰었습니다 "
              f"({home()}). 아래 목록에 그만큼이 빠져 있을 수 있습니다.")
    if not records:
        print("원장이 비어 있습니다. `jermes learn` 으로 배우거나 `jermes import` 로 들여오세요.")
        return 0
    print(f"{'스킬':<34}{'종류':<8}{'상태':<10}{'검증':<8}성공/실패")
    for record in records:
        mark = verified_mark(record.skill.verified)
        print(f"{record.name:<34}{record.skill.kind:<8}{record.status:<10}{mark:<8}"
              f"{record.usage.successes}/{record.usage.failures}")
    return 0


def _did_you_mean(name: str, choices) -> str:
    """가까운 이름 한둘. **없는 이름을 알려 주는 것만으로는 부족하다.**

    실측: `jermes improve last-exit-cod` 가 "툴이 아니거나 없습니다" 로 끝났다 -
    한 글자 차이인 `last-exit-code` 가 바로 옆에 있는데도. 사람은 오타를 치고,
    그때 필요한 것은 "없다" 가 아니라 "이거 말씀이신가요" 다.
    """
    import difflib

    close = difflib.get_close_matches(name, list(choices), n=2, cutoff=0.6)
    return f"  이거 말씀이신가요: {', '.join(close)}" if close else ""


def cmd_rollback(args) -> int:
    """지난 판본으로 되돌린다 - **이력을 지우지 않고** 하나의 사건으로 남기면서.

    계획서가 원장의 기둥으로 적어 둔 셋 중 하나다(provenance · 버전 · 롤백).
    앞의 둘은 있었는데 되돌리는 길이 없었다 - 판본은 쌓이는데 쓸 수가 없었다.
    """
    ledger = open_ledger()
    if ledger.get(args.name) is None:
        print(f"없는 스킬: {args.name}")
        print(_did_you_mean(args.name, (r.name for r in open_ledger().list())) or
              "  `jermes list` 로 무엇이 있는지 봅니다.")
        return 1
    past = ledger.versions(args.name)
    if args.list:
        print(f"{args.name} 의 판본 {len(past)}개 (오래된 순)")
        for version, data in past:
            print(f"  · {version:<10}{str(data.get('description', ''))[:60]}")
        return 0
    if len(past) < 2:
        print(f"되돌릴 판본이 없습니다: {args.name} (판본 {len(past)}개)")
        print("  `--list` 로 무엇이 있는지 봅니다.")
        return 1
    record = ledger.rollback(args.name, args.to)
    if record is None:
        print(f"그런 판본이 없습니다: {args.to}")
        return 1
    print(f"되돌렸습니다: {args.name} -> {record.skill.version}"
          f" ({record.history[-1] if record.history else ''})")
    print("  이력은 지워지지 않았습니다 - 되돌리기도 하나의 사건으로 남습니다.")
    print(f"  상태 {record.status} · {verified_mark(record.skill.verified)}")
    return 0


def cmd_approve(args) -> int:
    """사람이 승인해 `staged` 를 올린다. **에이전트는 이 문을 스스로 못 연다.**

    규약의 `approval_required_scopes` 는 여럿이 쓰는 자리를 뜻한다. 게이트를
    통과한 것과 남들이 쓰는 자리에 올려도 되는 것은 다르다 - 전자는 기계가
    정하고 후자는 사람이 정한다.
    """
    ledger = open_ledger()
    record = ledger.get(args.name)
    if record is None:
        print(f"없는 스킬: {args.name}")
        print(_did_you_mean(args.name, (r.name for r in open_ledger().list())) or
              "  `jermes list` 로 무엇이 있는지 봅니다.")
        return 1
    if not args.by:
        print("승인에는 --by <승인자> 가 필요합니다(에이전트가 스스로 못 올립니다).")
        return 1
    if record.status == "active":
        print(f"이미 활성입니다: {args.name}")
        return 0
    ledger.set_status(args.name, "active", note=f"사람 승인: {args.by}")
    print(f"올렸습니다: {args.name} · 승인 {args.by}")
    print(f"  {verified_mark(record.skill.verified)} · 스코프 {record.skill.scope}")
    if not record.skill.verified:
        print("  [주의] 이건 미검증입니다 - 승인은 검증이 아닙니다.")
    return 0


def cmd_trace(args) -> int:
    """한 세션이 **무엇을 낳았는지** 되짚는다 - 교훈에서 실제 작업까지.

    지금까지는 방향이 하나뿐이었다. 스킬을 열면 어느 세션에서 왔는지는 나오는데,
    "그 날 그 일에서 뭐가 남았나" 를 물을 데가 없었다. 그건 이 물건을 쓰는 사람이
    가장 자주 묻는 질문이다 - 어제 하루가 무엇으로 남았나.

    원장과 기억이 이미 `source_run_ids` 를 들고 있으므로 새로 기록할 것은 없다.
    거꾸로 훑기만 하면 된다.
    """
    key = (args.run or "").strip()
    if not key:
        print("어느 세션인지 알려 주세요: jermes trace <세션id 일부>")
        return 1

    def touches(runs) -> bool:
        return any(key in str(run) for run in (runs or []))

    skills = [r for r in open_ledger().list()
              if r.skill.provenance is not None
              and touches(r.skill.provenance.source_run_ids)]
    facts = [i for i in load_memory() if touches(i.source_run_ids)]
    if not skills and not facts:
        print(f"그 세션에서 남은 것이 없습니다: {key}")
        print("  세션 목록은 `jermes sessions` 로 봅니다.")
        return 0

    print(f"세션 {key} 에서 남은 것")
    if skills:
        print(f"{chr(10)}  스킬 {len(skills)}건")
        for record in skills:
            mark = verified_mark(record.skill.verified)
            verdict = record.history[-1] if record.history else ""
            print(f"    · {record.name}  [{record.status} · {mark}]")
            if verdict:
                print(f"        {verdict}")
    if facts:
        measured = sum(1 for i in facts if i.measured)
        print(f"{chr(10)}  사실 {len(facts)}건 (측정된 것 {measured}건)")
        for item in facts[:8]:
            print(f"    · [{_memory_mark(item)}] {item.text[:64]}")
        if len(facts) > 8:
            print(f"    ... 그리고 {len(facts) - 8}건 더 "
                  f"(`jermes memory --show {key}` 로 하나씩 봅니다)")
    return 0


def cmd_show(args) -> int:
    """스킬 본문과 **그것이 어디서 왔고 무엇을 했는지**.

    본문만 찍던 자리다. 그런데 이 물건에서 본문은 절반이고 나머지 절반은 내력이다
    - 어느 세션의 어떤 신호에서 나왔는지, 게이트가 어떤 숫자로 판정했는지, 그 뒤로
    실제로 무슨 과제에 쓰였는지. 원장에는 다 있는데 화면에 나오는 길이 없었다.
    """
    record = open_ledger().get(args.name)
    if record is None:
        print(f"없는 스킬: {args.name}")
        print(_did_you_mean(args.name, (r.name for r in open_ledger().list())) or
              "  `jermes list` 로 무엇이 있는지 봅니다.")
        return 1
    print(record.skill.body)
    if getattr(args, "plain", False):
        return 0
    print(_provenance_block(record))
    return 0


def _provenance_block(record) -> str:
    """이 스킬의 내력 - 원천 · 판정 · 쓰임 · 실제로 맡았던 과제."""
    skill = record.skill
    lines = ["", f"── 내력 ({record.name})"]
    origin = skill.provenance
    if origin is not None:
        # 세션이 없는 것과 **모르는 것**은 다르다. 손으로 단조한 툴은 원천이
        # 세션이 아니라 케이스 파일이다 - 그걸 "(모름)" 이라고 적으면 기록이
        # 빠진 것처럼 보인다.
        runs = ", ".join(origin.source_run_ids[:2])
        if not runs:
            runs = ("사람이 케이스로 단조" if origin.origin == "cli-tool"
                    else "(기록 없음)")
        lines.append(f"  원천    {runs}")
        if origin.signal:
            lines.append(f"  신호    {origin.signal}")
        lines.append(f"  만든이  {origin.origin} / {origin.curator_id}")
    else:
        lines.append("  원천    (기록 없음 - 손으로 들여온 것일 수 있습니다)")
    mark = verified_mark(skill.verified)
    lines.append(f"  상태    {record.status} · {mark}")
    # **판정은 숫자까지 보여 준다.** `검증됨` 이라는 낱말만으로는 얼마나
    # 좋아졌는지 알 수 없고, 이 물건의 주장은 정확히 그 숫자다.
    for entry in record.history[-3:]:
        lines.append(f"  판정    {entry}")
    usage = record.usage
    lines.append(f"  쓰임    성공 {usage.successes} · 실패 {usage.failures}")
    tasks = (getattr(skill, "meta", None) or {}).get("examples") or []
    if tasks:
        lines.append(f"  맡았던 과제 {len(tasks)}건:")
        lines.extend(f"    · {task[:70]}" for task in tasks[-3:])
    return chr(10).join(lines)


def cmd_export(args) -> int:
    record = open_ledger().get(args.name)
    if record is None:
        print(f"없는 스킬: {args.name}")
        print(_did_you_mean(args.name, (r.name for r in open_ledger().list())) or
              "  `jermes list` 로 무엇이 있는지 봅니다.")
        return 1
    usage = record.usage
    evidence = {"ledger-status": record.status, "successes": usage.successes,
                "failures": usage.failures, "exported-from": "jermes-cli"}
    try:
        files = skill_package(record.skill, evidence=evidence)
    except ValueError as exc:
        print(f"스펙 위반으로 내보낼 수 없습니다: {exc}")
        return 1
    out = Path(args.out or (home() / "export"))
    for relative, content in files.items():
        target = out / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        print(f"  {target}")
    print("\nagentskills.io 표준 패키지입니다 - Claude Code·Cursor 등에 그대로 넣을 수 있습니다.")
    return 0


def cmd_import(args) -> int:
    source = Path(args.file)
    # `export` 가 내놓는 것은 폴더다. 그 폴더를 그대로 넣는 게 자연한데,
    # 예전엔 폴더를 파일처럼 읽다가 PermissionError 로 그냥 터졌다.
    if source.is_dir():
        if (source / "SKILL.md").is_file():
            source = source / "SKILL.md"
        else:
            # **`--out` 은 묶음 폴더다.** `export a --out X` 와 `export b --out X`
            # 를 하면 `X/a/SKILL.md`, `X/b/SKILL.md` 가 된다. 그런데 `import X` 는
            # `X/SKILL.md` 만 찾아서 "폴더나 SKILL.md 경로를 주세요" 라고 답했다 -
            # 폴더를 줬는데도. 내보낸 것을 그대로 들여올 수 없으면 왕복이 아니다.
            inside = sorted(d for d in source.iterdir()
                            if (d / "SKILL.md").is_file())
            if not inside:
                print(f"읽을 수 없습니다: {args.file}")
                print("  SKILL.md 가 있는 폴더나 그 파일 경로를 주세요"
                      " (묶음 폴더면 그 안의 스킬들을 전부 들여옵니다).")
                return 1
            if len(inside) > 1 and (args.as_name or args.replace):
                # 여러 개를 한 이름으로 들여올 수는 없다. 조용히 마지막 것만
                # 남기는 대신 무엇이 문제인지 말한다.
                print(f"묶음에 스킬이 {len(inside)}개 있습니다 - "
                      "--as/--replace 는 하나를 들여올 때만 쓸 수 있습니다.")
                return 1
            worst = 0
            for folder in inside:
                one = argparse.Namespace(**vars(args))
                one.file = str(folder)
                worst = max(worst, cmd_import(one))
            print(f"묶음 {len(inside)}건을 처리했습니다.")
            return worst
    if not source.is_file():
        print(f"읽을 수 없습니다: {args.file}")
        print("  폴더나 SKILL.md 경로를 주세요.")
        return 1
    text = source.read_text(encoding="utf-8")
    problems = validate_skill_md(text)
    if problems:
        print("스펙 위반으로 들여올 수 없습니다:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    candidate = candidate_from_skill_md(text)
    name = args.as_name or candidate.name
    existing = open_ledger().get(name)
    if existing is not None and not args.replace:
        # 들여오기는 항상 미검증으로 착지한다. 같은 이름의 검증된 기록 위에
        # 얹으면 그 검증이 조용히 사라진다 - 실측에서 자기 export 를
        # 다시 들여오니 검증된 tool 이 미검증 guide 로 덮였다.
        mark = verified_mark(existing.skill.verified)
        print(f"이미 있는 이름입니다: {name} ({existing.skill.kind} · {mark})")
        print("  들여오기는 항상 미검증으로 착지하므로, 덮으면 그 검증이 사라집니다.")
        print(f"  다른 이름으로: jermes import <파일> --as {name}-imported")
        print("  그래도 덮으려면: --replace")
        return 1
    candidate.name = name
    skill = synthesize(candidate)
    skill.verified = False          # 남이 붙인 verified 는 믿지 않는다
    skill.status = "staged"
    record = open_ledger().commit(skill, note="cli import (unverified until benched)")
    claimed = candidate.payload.get("claimed_verified", "")
    print(f"들여왔습니다: {record.name} · 상태 {record.status} · 검증 {record.skill.verified}")
    if claimed:
        print(f"  (파일에 적힌 verified={claimed!r} 은 기록만 하고 믿지 않습니다)")
    # **두고 온 것이 있으면 말한다.** 패키지에 실행 스크립트가 같이 왔는데 우리는
    # `SKILL.md` 만 읽는다. 그래서 툴로 내보낸 것이 문서로 들어온다 - 조용히.
    # 남의 스크립트를 자동으로 실행하지 않는 것은 옳지만, 말없이 잃는 것은 다르다.
    script = source.parent / "scripts" / "tool.py"
    if script.is_file():
        print(f"  실행 스크립트는 두고 왔습니다: {script}")
        print("  (남의 코드는 사람이 보고 나서 씁니다. 쓰려면 케이스를 붙여"
              f" `jermes tool {record.name} --cases <파일>` 로 다시 단조하세요.)")
    return 0


def _read_cases(path: str) -> list:
    """케이스 읽기는 `tools.read_cases` 한 자리에 있다 - CLI 는 오류를 사람 말로
    바꾸기만 한다. 호스트가 늘어도 파일 형식 처리가 갈라지지 않는다."""
    from .tools import read_cases

    try:
        return read_cases(path)
    except (ValueError, OSError) as exc:
        raise SystemExit(str(exc))


def _say_what_was_granted(policy) -> None:
    """`--policy` 한 낱말이 **무엇을 열었는지** 말한다.

    권한을 준 것 자체는 사람의 결정이고 막을 일이 아니다("허락은 허락이다" -
    런타임 관문은 허락 **안 한** 툴이 몰래 하는 것을 막는 물건이다). 다만 그
    허락의 범위가 임시 폴더가 아니라 **디스크 전체**라는 것은 낱말만 봐서는
    모른다.

    범위도 같이 말한다. 허락은 "이 툴이 파일을 쓴다"는 뜻이지 "어디든 쓴다"는
    뜻이 아니다 - 어디에 쓸지는 부르는 쪽이 페이로드로 정한다.
    """
    granted = policy.granted()
    if not granted:
        return
    names = {"allow_write": "파일 쓰기", "allow_delete": "파일 삭제·이동",
          "allow_network": "네트워크", "allow_process": "프로세스 실행",
          "allow_dynamic": "네이티브 코드 호출"}
    print(f"  권한을 줬습니다: {', '.join(names.get(g, g) for g in granted)}")
    print("  쓰는 곳은 임시 폴더와 **부르는 쪽이 지목한 경로**뿐입니다 - "
          "툴이 스스로 고른 곳에는 못 씁니다.")
    if policy.env_allowlist:
        print(f"  환경변수도 넘깁니다: {', '.join(policy.env_allowlist)}")


def cmd_tool(args) -> int:
    """반복 절차를 **실행 가능한 툴**로 만든다 - 그리고 진짜로 실행해서 검증한다.

    문서 스킬은 LLM 을 다시 돌려야 검증되지만 툴은 입력·출력 비교로 끝난다. 그래서
    채점이 결정적이고 싸다. 통과 못 하면 원장에 `staged` 로만 들어간다 - 실패한 것을
    성공처럼 적지 않는다.
    """
    from .gate import GateConfig
    from .tools import (ToolPolicy, draft_tool, split_cases, synthesize_tool_skill,
                        tool_package, verify_tool)
    from .model import Provenance

    try:
        policy = ToolPolicy.preset(args.policy, timeout=args.tool_timeout,
                                   env_allowlist=tuple(
                                       k for k in args.env.split(",") if k.strip()))
    except ValueError as exc:
        print(exc)
        return 1
    _say_what_was_granted(policy)
    cases = _read_cases(args.cases)
    # holdout_ratio=0 이면 전부 dev 다. 그러면 `verdict` 가 스스로 `staged` 가 된다
    # - 못 본 문제로 확인한 적이 없으니 "검증됨"이라고 말하지 않는 게 맞다.
    config = GateConfig(holdout_ratio=0.0) if args.no_holdout else GateConfig()
    dev, held = split_cases(cases, config)
    print(f"툴 단조: {args.name} · 케이스 {len(cases)}개 "
          f"(보여줄 예시 {len(dev)} · 감춘 검증 {len(held)}) · {policy.describe()}")
    if not args.task:
        # 설명이 이름뿐이면 라우터가 영영 못 찾는다. 스모크에서 걸린 자리다.
        print("  [주의] --task 가 없어 설명이 이름뿐입니다. ask·route 가 못 찾습니다.")
        print("    무슨 일을 하는지 한 줄 적어 주세요.")

    if args.script:                      # 이미 스크립트가 있으면 검증만 한다
        script = Path(args.script).read_text(encoding="utf-8")
        report = verify_tool(script, cases, config=config, timeout=args.tool_timeout,
                             policy=policy)
        attempts = [f"주어진 스크립트: {report.summary()}"]
    else:
        budget = budget_from(args)
        complete = build_completer(args, budget)
        script, report, attempts = draft_tool(
            complete, args.task or args.name, cases, config=config,
            max_attempts=args.attempts, timeout=args.tool_timeout, policy=policy)

    for line in attempts:
        print(f"  {line}")
    if not args.script:
        print(f"  {budget.summary()}")
    print(f"\n판정: {report.verdict} - {report.summary()}")
    for failure in report.failures[:5]:
        print(f"  실패 · {failure}")
    need_holdout = GateConfig().min_holdout_to_promote
    if report.verdict == "staged" and 0 < report.holdout_total < need_holdout \
            and report.holdout_pass == report.holdout_total:
        # 다 맞았는데 `검증됨` 이 안 붙는 이유는 사람이 짐작할 수 없다. 무엇을
        # 하면 되는지까지 말한다 - 케이스를 몇 개 더 넣으면 되는 일이다.
        print(f"\n다 맞았지만 감춰둔 것이 {report.holdout_total}건뿐이라 `검증됨` 을 안 붙였습니다.\n"
              f"  한 건이 맞았다는 것과 그 툴이 맞다는 것은 다릅니다.\n"
              f"  케이스를 {GateConfig().min_cases * 2}개 이상으로 늘리면 판정합니다"
              f" (지금 {report.dev_total + report.holdout_total}개).")
    if not script:
        print("스크립트를 만들지 못했습니다.")
        return 1
    if report.verdict == "rejected" and report.dev_pass == report.dev_total and held:
        print("\n보여준 예시는 다 맞았지만 감춰둔 것에서 틀렸습니다 - 예시가 다루지 못한\n"
              "형태가 있다는 뜻입니다. 위 실패 입력과 같은 형태를 케이스 파일에 더\n"
              "넣고 다시 도세요. (예시가 곧 명세 전부라면 --no-holdout)")

    skill = synthesize_tool_skill(args.name, args.task or args.name, script, report,
                                  # **내력을 비워 두지 않는다.** 단조한 툴은
                                  # 세션에서 온 것이 아니라 사람이 케이스 파일을
                                  # 들고 와서 만든 것이다. 그러면 그 사실을 적어야
                                  # `show` 가 "원천 (모름)" 이라고 말하지 않는다 -
                                  # 모르는 것이 아니라 우리가 안 적었던 것이다.
                                  provenance=Provenance(
                                      origin="cli-tool",
                                      curator_id=f"cases:{Path(args.cases).name}",
                                      signal=(args.task or "")[:120]),
                                  policy=policy, cases=cases)
    record = open_ledger().commit(skill, note=f"tool forge · {report.summary()}")
    print(f"원장 기록: {record.name} · 상태 {record.status} · 검증 {record.skill.verified}")

    if args.out:
        out = Path(args.out)
        for relative, content in tool_package(skill).items():
            target = out / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            print(f"  {target}")
        print("scripts/tool.py 는 그대로 실행됩니다: "
              'echo \'{"...": "..."}\' | python scripts/tool.py')
    return 0 if report.verdict == "promoted" else 1


def cmd_run(args) -> int:
    """만든 툴을 실제로 돌려본다 - 원장에 있는 것을 이름으로 부른다."""
    from .tools import run_tool

    record = open_ledger().get(args.name)
    if record is None or record.skill.kind != "tool":
        print(f"툴이 아니거나 없습니다: {args.name}")
        print(_did_you_mean(args.name, (r.name for r in open_ledger().list()
                                        if r.skill.kind == "tool")) or
              "  `jermes list` 로 무엇이 있는지 봅니다.")
        return 1
    from .tools import ToolPolicy

    manifest = json.loads(record.skill.body)
    script = manifest.get("script", "")
    # 만들 때 허락한 권한 그대로 돌린다 - 실행할 때 몰래 넓히지 않는다.
    policy = ToolPolicy.from_dict(manifest.get("policy"), timeout=args.tool_timeout)
    raw = args.payload if args.payload else (sys.stdin.read() or "{}")
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        # 사람이 손으로 넣는 자리다. 파이썬 트레이스백을 그대로 뱉으면 무엇을
        # 어떻게 고쳐야 하는지가 안 보인다. 흔한 실수 하나는 짚어 준다 - 셸에서
        # 줄바꿈이나 탭을 생으로 넣으면 JSON 문법 위반이다.
        print(f"입력 JSON 을 못 읽었습니다: {exc}")
        if any(ch in raw for ch in "\r\n\t") and "\\" not in raw:
            print("  줄바꿈·탭은 JSON 안에서 `\\n` `\\r` `\\t` 로 적어야 합니다.")
        print(f"  받은 것: {raw[:120]!r}")
        return 1
    result = run_tool(script, payload, timeout=args.tool_timeout, policy=policy)
    _log_own(f"run {args.name}", args.name, payload, result.ok,
             detail=result.error or "", seconds=result.seconds)
    if not result.ok:
        print(f"실패: {result.error}")
        return 1
    print(json.dumps(result.output, ensure_ascii=False))
    print(f"({result.seconds * 1000:.0f}ms)", file=sys.stderr)
    return 0


def _mcp_config_paths() -> list[Path]:
    home_dir = Path.home()
    paths = [home_dir / ".claude.json", Path.cwd() / ".mcp.json",
             home_dir / ".cursor" / "mcp.json"]
    extra = os.environ.get("JERMES_MCP_CONFIG", "")
    return [Path(p) for p in extra.split(os.pathsep) if p.strip()] + paths


def _live_mcp_sources(timeout: float = 30.0) -> list:
    """설정에 적힌 stdio MCP 서버마다 **실제로 붙는** 출처를 만든다.

    이게 없던 동안 `McpConfigSource` 가 서버 **이름**만 읽어 왔고, 그 항목은
    `resolved=False` 라 `usable()` 에서 빠졌다. 그래서 `route`·`ask` 는 MCP
    도구를 단 하나도 고를 수 없었다 - 목록에는 보이는데 못 쓰는 상태였다.
    """
    from concurrent.futures import ThreadPoolExecutor

    from .discovery import McpLiveSource
    from .mcp_client import load_servers, stdio_list_tools

    servers = load_servers(_mcp_config_paths())
    if not servers:
        return []

    # **동시에 붙고, 붙는 대로 말한다.** `stdio_list_tools` 는 지연 콜러블이라
    # 실제 접속은 나중에 `discover()` 안에서 **한 곳씩 차례로** 일어난다. 서버
    # 하나에 30초를 주고 9곳을 도니 최악 4분 30초인데, 그동안 화면에는 한 글자도
    # 안 나온다(실측: 10분을 기다렸고 출력이 비어 있었다). 남의 프로세스를 여는
    # 일이라 더더욱, 지금 무엇에 붙고 있는지는 보여야 한다.
    #
    # 미리 받아 두고 이미 받은 것을 돌려주는 콜러블로 감싼다. `McpLiveSource` 의
    # 계약(콜러블을 받는다)은 그대로 두고 접속 시점만 앞당긴다.
    print(f"  MCP 서버 {len(servers)}곳에 동시에 붙어 봅니다 "
          f"(한 곳당 {timeout:.0f}초)")

    def reach(item):
        name, spec = item
        try:
            tools = stdio_list_tools(spec, timeout)() or []
            print(f"    {name}: 도구 {len(tools)}개")
        except Exception as exc:
            # 못 붙는 서버는 흔하다(인증 필요, 명령 없음, 원격 전용). 그건 이
            # 명령의 실패가 아니라 그 서버의 사정이고, 왜인지는 말해 준다.
            print(f"    {name}: 못 붙음 - {type(exc).__name__}: {str(exc)[:80]}")
            tools = []
        return McpLiveSource(name, lambda got=tools: got)

    with ThreadPoolExecutor(max_workers=min(8, len(servers))) as pool:
        return list(pool.map(reach, servers.items()))


def _found_nothing(result) -> bool:
    """**하나도 못 골랐는가.**

    라우터는 어휘 겹침으로 고른다. 그래서 우리말로 물었는데 능력 설명이 영어면
    겹칠 낱말이 아예 없다 - 모델이 약해서가 아니라 구조적으로 못 찾는다. 실측:
    검증까지 마친 `normalize-line-endings` 를 두고 "CRLF 섞인 스크립트를 고쳐야
    한다" 라고 물었더니 엉뚱한 MCP 검색 도구가 겹침 "스" 하나로 0.42 에 뽑혔다.

    `--translate` 가 그 답인데, 사용자가 그 플래그를 알아야만 한다는 건 답이
    아니다. 그렇다고 "설명이 전부 영어인가"로 대신 재면 안 된다 - 한국어로 쓰인
    MCP 서버가 하나만 있어도 조건이 깨지는데, 정작 우리 스킬은 여전히 안 잡힌다
    (실측으로 그렇게 헛짚었다).

    **얇을 때가 아니라 없을 때만 다시 찾는다.** 얇은 것까지 다시 찾게 했더니 두
    가지가 걸렸다. 하나는 값이다 - 얇은 질문은 흔한데 번역은 능력마다 한 번씩
    LLM 을 부른다. 다른 하나가 더 나쁘다: 다시 찾은 결과가 얇지 않게 나오면
    **"근거가 얇다"는 사실이 화면에서 사라진다.** 얇은 것을 자신 있게 내놓으면
    사람이 속고, 그건 못 찾는 것보다 나쁘다. 못 고른 경우에는 잃을 것이 없다.
    """
    return not result.chosen


def _registry(args=None, translate: bool = False, live: bool = False):
    """근처 능력 장부. `translate` 면 LLM 으로 사용자 말 한 줄을 덧붙인다 -
    실측에서 영어 설명 도구를 한국어로 물으면 하나도 못 찾았다.

    `live` 면 MCP 서버에 실제로 붙어 도구까지 받아온다. 기본은 안 붙는다 -
    남의 프로세스를 여는 일은 사용자가 시켜야 한다. 한 번 붙으면 결과를
    캐시에 남겨 `route`·`ask` 가 재접속 없이 쓴다.
    """
    from .discovery import (CachedMcpSource, Translated, default_sources,
                            discover)

    sources = default_sources(ledger=open_ledger())
    if live:
        sources += _live_mcp_sources()
    else:
        # 지난번에 붙어서 받아 둔 것. 없으면 빈 목록이라 아무 해가 없다.
        sources.append(CachedMcpSource(home() / "mcp-tools.json"))
    # **캐시된 힌트는 늘 쓴다.** 한 번 붙여 둔 우리말 한 줄은 LLM 이 필요 없는
    # 공짜 지식인데, 예전에는 `--translate` 를 줄 때만 읽었다. 그래서 도구를
    # 받아 오면서 번역까지 해 두고도 정작 `route`·`ask` 는 영어 설명만 보고
    # 골랐다(실측: 힌트를 캐시했는데 정확도가 73% 그대로였다).
    #
    # 모델은 `translate` 일 때만 붙인다 - 없는 힌트를 새로 만드는 데만 쓴다.
    complete = None
    if translate:
        try:
            # 능력마다 한 번씩 부른다. 답은 **한 줄**이라 생각이 값을 하지 않는데,
            # 사고를 켠 완성기로 돌리니 능력 23개에 몇 분이 걸렸다. 벤치와 같은
            # 자리다 - 짧은 답은 빠른 쪽으로 보낸다.
            complete = build_completer(args, budget_from(args), fast=True)
        except SystemExit as exc:
            print(f"{exc}")
    cache = home() / "capability-hints.json"
    sources = [Translated(s, complete, cache) for s in sources]
    return discover(sources)


def _cache_live_tools(registry) -> None:
    """라이브로 받아온 MCP 도구를 남긴다. 다음부터는 접속 없이 쓴다."""
    by_server: dict[str, list] = {}
    for item in registry.items:
        via = item.invoke.get("via") if isinstance(item.invoke, dict) else ""
        if via != "mcp" or not item.resolved:
            continue
        by_server.setdefault(item.invoke.get("server", ""), []).append({
            "name": item.invoke.get("tool", ""),
            "description": item.description,
            "inputSchema": item.invoke.get("input_schema") or {},
            **({"annotations": item.annotations()} if item.annotated else {}),
        })
    if not by_server:
        return
    path = home() / "mcp-tools.json"
    write_atomically(path, json.dumps(by_server, ensure_ascii=False, indent=1))
    total = sum(len(v) for v in by_server.values())
    print(f"  서버 {len(by_server)}곳에서 도구 {total}개를 받아 두었습니다 "
          f"(다음부터는 접속 없이 씁니다).")


def cmd_capabilities(args) -> int:
    """근처에서 쓸 수 있는 능력을 전부 세어 보인다 - 우리가 만든 것만이 아니라."""
    registry = _registry(args, translate=args.translate, live=args.live)
    if args.live:
        _cache_live_tools(registry)
        # **받아 온 자리에서 한 번만 우리말을 붙인다.** 라우터는 어휘 겹침으로
        # 고르는데 MCP 도구 설명은 대개 영어라, 우리말로 물으면 구조적으로 못
        # 찾는다. 질의마다 번역하는 것은 답이 아니다(값도 값이지만, 다시 찾은
        # 결과가 얇지 않게 나오면 "근거가 얇다"는 사실이 화면에서 사라진다).
        # 도구를 받아 오는 이 자리가 한 번 치르고 끝나는 자리다 - 캐시된다.
        #
        # 실측(실무 질문 18건 · 후보 26개): 정확도 **73% -> 87%**.
        #   "이미지에서 객체 분할해줘" 가 영어 설명인 `segment_image` 를 놔두고
        #   한국어 설명이 붙은 `confirm_money_image` 로 갔다. 힌트를 붙이니 바로
        #   잡힌다(26% -> 51%).
        if not args.translate:
            registry = _registry(args, translate=True)   # 캐시에서 읽는다
    print(registry.summary())
    for note in registry.notes:
        print(f"  · {note}")
    if not registry.items:
        print("\n찾은 것이 없습니다. JERMES_SKILL_PATH / JERMES_MCP_CONFIG 로 경로를 더할 수 있습니다.")
        return 0
    print(f"\n{'이름':<38}{'종류':<8}{'위험':<11}상태")
    for item in sorted(registry.items, key=lambda c: (c.kind, c.name)):
        print(f"{item.name[:37]:<38}{item.kind:<8}{item.risk():<11}{item.label() or '-'}")
    return 0


def cmd_route(args) -> int:
    """과제를 주면 **쓸 것을 골라 준다**. 모델이 스스로 검색하기를 기다리지 않는다."""
    from .router import Router

    # `route` 는 "무엇으로 할 수 있나"를 묻는 명령이다. 아는 것도 그 답의
    # 일부다 - 능력만 세고 기억을 빼면 반쪽만 보여 주는 셈이다.
    _print_recalled(_recall_for(args.task))

    registry = _registry(args, translate=args.translate)
    pool = registry.items if args.include_unresolved else registry.usable()
    allowed = tuple(args.risk.split(",")) if args.risk else ("safe", "caution")
    router = Router(pool, allowed_risk=allowed)
    result = router.route(args.task, limit=args.limit)
    if not args.translate and _found_nothing(result):
        print("  겹치는 말이 없습니다 - 능력 설명이 다른 언어일 수 있어 설명에"
              " 한 줄을 붙이고 다시 찾습니다(능력마다 한 번, 캐시됨).")
        hinted = _registry(args, translate=True)
        pool = hinted.items if args.include_unresolved else hinted.usable()
        result = Router(pool, allowed_risk=allowed).route(args.task,
                                                          limit=args.limit)
    print(f"과제: {args.task}")
    print(f"  {result.summary()}")
    if not result.chosen:
        print("  맞는 능력이 없습니다(겹치는 말이 없거나 정책에 걸렸습니다).")
        if result.blocked:
            print(f"  정책상 제외: {', '.join(result.blocked[:5])}")
        return 1
    for choice in result.chosen:
        print(f"  · {choice.line()}")
    if args.render:
        print("\n--- 프롬프트에 들어갈 조각 ---")
        print(result.render())
    return 0


def cmd_improve(args) -> int:
    """이미 있는 툴을 다시 재고, 깨졌으면 고친다. 나빠지면 안 바꾼다."""
    from .tools import ToolPolicy, improve_tool

    record = open_ledger().get(args.name)
    if record is None or record.skill.kind != "tool":
        print(f"툴이 아니거나 없습니다: {args.name}")
        print(_did_you_mean(args.name, (r.name for r in open_ledger().list()
                                        if r.skill.kind == "tool")) or
              "  `jermes list` 로 무엇이 있는지 봅니다.")
        return 1
    extra = _read_cases(args.cases) if args.cases else None
    complete = None
    if not args.check_only:
        try:
            complete = build_completer(args)
        except SystemExit as exc:
            print(f"{exc}\n(회귀검사만 하려면 --check-only)")
            return 1

    manifest = json.loads(record.skill.body)
    policy = ToolPolicy.from_dict(manifest.get("policy"))
    result = improve_tool(record.skill, complete, extra_cases=extra, policy=policy)
    print(f"{args.name}: {result.summary()}")
    for line in result.attempts:
        print(f"  {line}")
    for failure in (result.after or result.before).failures[:5]:
        print(f"  실패 · {failure}")

    if result.verdict == "repaired":
        from .tools import load_cases, synthesize_tool_skill
        from .model import Provenance
        cases = load_cases(record.skill) + list(extra or [])
        fixed = synthesize_tool_skill(args.name, manifest.get("description", args.name),
                                      result.script, result.after,
                                      provenance=Provenance(origin="cli-improve"),
                                      policy=policy, cases=cases)
        saved = open_ledger().commit(fixed, note=f"improve · {result.after.summary()}")
        print(f"고쳐서 올렸습니다: {saved.name} v{saved.skill.version} · 상태 {saved.status}")
    return 0 if result.verdict in ("unchanged", "repaired") else 1


def cmd_install(args) -> int:
    """검증된 것을 **다른 에이전트가 집는 자리**에 놓는다 - 배운 것이 쓰이려면
    내보내는 것으로는 부족하다.

    표준(agentskills.io)을 쓰는 제품들은 정해진 디렉터리를 읽는다. 거기에 놓으면
    Claude Code·Cursor 같은 것들이 **다음 실행부터 그냥 본다.** 붙이는 코드가 필요 없다.

    기본은 **검증된 것만** 설치한다 - 못 본 문제로 확인 안 된 것을 남의 도구 목록에
    끼워 넣는 것은 우리가 하지 말자고 한 그것이다.
    """
    from .portable import skill_package

    targets = [Path(p) for p in (args.into or "").split(os.pathsep) if p.strip()]
    if not targets:
        targets = [Path.home() / ".claude" / "skills"]

    records = [r for r in open_ledger().list()
               if r.status == "active" and (args.all or r.skill.verified)]
    if args.name:
        records = [r for r in records if r.name == args.name]
    if not records:
        print("설치할 것이 없습니다."
              + ("" if args.all else " (검증된 것만 설치합니다 - 전부 넣으려면 --all)"))
        return 1

    written = 0
    for record in records:
        try:
            files = skill_package(record.skill, evidence={
                "ledger-status": record.status,
                "installed-by": "jermes-install"})
        except ValueError as exc:
            print(f"  건너뜀 {record.name}: {exc}")
            continue
        for target in targets:
            for relative, content in files.items():
                path = target / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            written += 1
            mark = verified_mark(record.skill.verified)
            print(f"  {target / record.name}  [{mark}]")
    print(f"\n{written}건 설치. 표준을 읽는 에이전트는 다음 실행부터 그냥 봅니다.")
    # **겪지도 않은 위험을 경고하지 않고, 정작 빠진 것은 말한다.**
    # `--all` 은 `verified` 검사만 푼다 - `status == "active"` 는 그대로다.
    # 그래서 대기 중인 스킬은 `--all` 을 줘도 안 들어가는데, 화면에는 "미검증까지
    # 넣었습니다" 라는 경고만 떴다. 실측: 원장 6건(활성 3 · 대기 3)에서 `--all`
    # 로 3건이 설치됐고, 빠진 3건은 아무 말도 없었다. 경고는 틀렸고 안내는 없었다.
    unverified = sum(1 for r in records if not r.skill.verified)
    if unverified:
        print(f"[주의] --all 로 미검증 {unverified}건까지 넣었습니다. "
              "받는 쪽은 이게 확인된 줄 압니다.")
    staged = [r.name for r in open_ledger().list() if r.status == "staged"]
    if staged:
        print(f"  대기 {len(staged)}건은 넣지 않았습니다 - 아직 활성이 아닙니다"
              f" ({', '.join(staged[:3])}{'...' if len(staged) > 3 else ''}).")
    return 0


def cmd_serve(args) -> int:
    """단조한 툴을 MCP 로 내준다 - 아무 에이전트나 부를 수 있게."""
    from .mcp_server import JermesMcpServer, servable

    ledger = open_ledger()
    tools = servable(ledger, args.include_staged)
    # 안내는 stderr 로. stdout 은 프로토콜 전용이라 한 줄이라도 섞이면 대화가 깨진다.
    print(f"[jermes] MCP stdio · 툴 {len(tools)}개"
          + ("" if args.include_staged else " (검증된 것만)"), file=sys.stderr)
    for name in tools:
        print(f"  · {name}", file=sys.stderr)
    JermesMcpServer(ledger, args.include_staged).serve()
    return 0


def _recall_for(task: str, limit: int = 3):
    """이 과제에 관련된 **측정된 기억**을 꺼낸다. 없으면 빈 목록.

    오래 비어 있던 자리다. 기억을 쌓고 재기만 하고 꺼내 쓰는 곳이 없으면
    "기억 기반"이라는 말이 성립하지 않는다. `memory.recall` 도 `agent.recall` 도
    정의되고 시험까지 돼 있었는데 **CLI 어디에서도 안 불렸다.**

    관련도 x 신뢰도로 고르고, 다툼 중인 것은 애초에 안 나온다. 나온 것은
    **신뢰도를 달고** 보여 준다 - 딱지 없이 컨텍스트에 넣지 않는 규율이 기억에도
    걸린다.
    """
    from .memory import recall as recall_memory

    # `load_memory` 가 없는 파일과 깨진 줄을 이미 처리한다. 여기서 한 겹 더
    # 삼키면 진짜 문제(권한·디스크)까지 조용히 빈 목록이 된다.
    # **스코프를 넘긴다.** 안 넘기면 프로젝트 A 에서 배운 것이 B 의 질문에
    # 딸려 온다. `recall` 이 `user` 는 통과시키므로 사람 자신에 대한 사실은
    # 어디서나 나온다.
    return recall_memory(load_memory(), limit=limit, task=task,
                         scope=current_scope())


def _print_recalled(items) -> None:
    if not items:
        return
    print(f"\n아는 것 {len(items)}건:")
    for item in items:
        # 재긴 쟀는데 못 갈랐다면 그렇게 말한다. `신뢰 0.50` 만 보이면 근거가
        # 있어서 0.50 인 것처럼 읽힌다 - 실은 재고도 알게 된 것이 없다는 뜻이다.
        mark = _memory_mark(item)
        print(f"  · [{mark}] {item.text[:100]}")


def _log_own(query: str, tool: str, payload, ok: bool,
             detail: str = "", seconds: float = 0.0) -> None:
    """자기가 한 일을 원천 파일에 남긴다. **성공에도 실패에도.**

    원장 이력과 목적이 다르다. 원장은 "다음에 이걸 고를까"라 얇거나 틀린 것을
    넣으면 안 되지만(`_record_if_confident`), 학습 재료는 실패일수록 값지다.
    예전에는 `ask` 의 실패가 불리언 하나로 버려져서, 무엇을 넣었다 왜 깨졌는지가
    사라졌다. 그 결과 이 물건은 남의 세션에서는 배우면서 정작 사용자가 자기한테
    시킨 일에서는 아무것도 배우지 않았다.

    기록이 안 되는 것 때문에 사용자의 명령이 실패하지는 않는다. 배우는 것은
    부수적인 일이고, 부수적인 일이 본 일을 망치면 안 된다.
    """
    from .sources import own

    try:
        own.record(query, tool, payload, ok, detail=detail, seconds=seconds)
    except Exception as exc:
        # 삼키되 **남긴다.** 통째로 조용하면 기록기가 영영 망가져도 아무도
        # 모른다. `jermes doctor` 가 이걸 꺼내 보여 준다.
        own.record_errors.append(f"{type(exc).__name__}: {exc}"[:160])


def _record_if_confident(name: str, choice, task: str) -> None:
    """성공을 이력으로 남긴다 - **근거가 얇지 않았을 때만.**

    실행이 성공한 것과 답이 맞은 것은 다르다. `run_tool` 은 종료코드와 JSON 파싱만
    본다. 그 상태에서 질의문을 이력으로 쌓으면 틀린 선택이 다음 선택의 근거가 된다.

    실측: 더하기 툴만 있을 때 "두 수 6 과 7 을 곱해줘" 가 6+7=13 을 내고 성공으로
    기록됐다. 그 뒤 같은 질의 점수가 6배로 뛰고, 나중에 올바른 곱하기 툴을 검증까지
    해서 넣어도 구어체로 물으면 전부 틀린 툴이 이겼다. 한 번의 오답이 그 과제
    영역을 영구 점거한 것이다.

    답의 정확성은 우리가 모른다. 그러나 **근거가 얇았다는 것은 안다**(이미 계산해
    화면에도 찍는다). 얇은 근거로 고른 것은 굳히지 않는다.
    """
    if getattr(choice, "thin", False):
        print("  (근거가 얇어 이력으로 남기지 않았습니다. "
              "맞았다면 같은 말로 한 번 더 물어보세요.)")
        return
    open_ledger().record_outcome([name], True, task=task)


def cmd_forget(args) -> int:
    """잘못 쌓인 이력을 지운다.

    이력은 라우팅의 근거라서, 한 번 잘못 들어가면 그 과제 영역을 점거한다.
    되돌릴 방법이 없으면 사용자는 원장을 손으로 고치거나 통째로 버려야 했다.
    """
    ledger = open_ledger()
    record = ledger.get(args.name)
    if record is None:
        print(f"그런 이름이 없습니다: {args.name}")
        print(_did_you_mean(args.name, (r.name for r in open_ledger().list())) or
              "  `jermes list` 로 무엇이 있는지 봅니다.")
        return 1
    meta = dict(getattr(record.skill, "meta", None) or {})
    examples = list(meta.get("examples") or [])
    if not examples:
        print(f"{args.name}: 지울 이력이 없습니다.")
        return 0

    if args.all:
        keep, dropped = [], examples
    else:
        needle = args.task.strip()
        if not needle:
            print("무엇을 지울지 주세요: --task '<문장>' 또는 --all")
            return 1
        keep = [e for e in examples if needle not in e]
        dropped = [e for e in examples if needle in e]
    if not dropped:
        print(f"{args.name}: '{args.task}' 에 걸리는 이력이 없습니다.")
        return 0

    meta["examples"] = keep
    record.skill.meta = meta
    ledger.commit(record.skill)
    print(f"{args.name}: 이력 {len(dropped)}건 삭제 (남은 {len(keep)}건)")
    for text in dropped[:5]:
        print(f"  - {text[:80]}")
    return 0


def _offers_for(args, registry) -> list:
    """지금 쓸 수 있는 것. **단계마다 다시 묻는다** - 앞 단계가 새 능력을 만들었을
    수 있고, 목록을 한 번만 뜨면 그걸 놓친다."""
    from .act import Offer

    allowed = ("safe", "caution", "dangerous") if args.risky else ("safe", "caution")
    return [Offer(name=c.name, description=c.description, risk=c.risk(),
                  read_only=bool(c.read_only and c.annotated))
            for c in registry.usable() if c.risk() in allowed]


def _approve(name: str, payload, offer) -> bool:
    """읽기 전용이 아닌 단계는 **그때그때** 묻는다.

    미리 다 받아 두는 승인은 승인이 아니다. 여러 단계의 본질이 "사람이 미리 전부를
    볼 수 없다"는 것이라, 무엇을 하려는지 보여 준 다음에 물어야 뜻이 있다.

    대화형이 아니면(파이프·크론) 거절로 본다. 물어볼 수 없는데 그냥 하는 것은
    승인을 건너뛴 것이지 받은 것이 아니다.
    """
    print(f"\n  다음: {name} [{offer.risk}] {json.dumps(payload, ensure_ascii=False)[:160]}")
    if not sys.stdin.isatty():
        print("  대화형이 아니라 물어볼 수 없습니다. --yes 를 주면 진행합니다.")
        return False
    try:
        answer = input("  진행할까요? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in ("y", "yes", "ㅇ")


def _approve_without_asking(name: str, payload, offer) -> bool:
    """`--yes` 를 준 사람. 묻지는 않되 **무엇을 했는지는 보여 준다.**

    동의는 "물어보지 마"이지 "숨겨"가 아니다. 조용히 통과시키면 위험 등급 자체가
    장식이 되고, 나중에 무엇이 일어났는지 화면에서 되짚을 수가 없다.
    """
    print(f"\n  [--yes] {name} [{offer.risk}] "
          f"{json.dumps(payload, ensure_ascii=False)[:160]}")
    return True


def _execute_step(args, registry, name: str, payload: dict):
    """한 단계를 실제로 한다. `ask` 의 한 단계와 **같은 문을 지난다** - 정책도
    기록도 두 벌이 되면 언젠가 한쪽만 느슨해진다."""
    from .act import Step
    from .tools import ToolPolicy, run_tool

    record = open_ledger().get(name)
    if record is not None and record.skill.kind == "tool":
        manifest = json.loads(record.skill.body)
        outcome = run_tool(manifest.get("script", ""), payload,
                           policy=ToolPolicy.from_dict(manifest.get("policy")))
        _log_own(args.query, name, payload, outcome.ok,
                 detail=outcome.error or "", seconds=outcome.seconds)
        return Step(name=name, payload=payload, ok=outcome.ok,
                    output=outcome.output, error=outcome.error or "",
                    seconds=outcome.seconds)

    capability = next((c for c in registry.usable() if c.name == name), None)
    if capability is not None and capability.kind == "mcp":
        from .mcp_client import call_stdio_tool, load_servers

        invoke = capability.invoke if isinstance(capability.invoke, dict) else {}
        spec = load_servers(_mcp_config_paths()).get(invoke.get("server", ""))
        if not spec:
            return Step(name=name, payload=payload, ok=False,
                        error="서버 설정을 못 찾았습니다")
        try:
            ok, text = call_stdio_tool(spec, invoke.get("tool", ""), payload,
                                       timeout=getattr(args, "tool_timeout", 60.0))
        except Exception as exc:
            return Step(name=name, payload=payload, ok=False,
                        error=f"{type(exc).__name__}: {exc}"[:400])
        _log_own(args.query, name, payload, ok, detail=text[:200])
        return Step(name=name, payload=payload, ok=ok,
                    output=text[:2000] if ok else None,
                    error="" if ok else text[:400])

    if capability is not None:
        # 문서 스킬은 실행할 것이 아니라 읽을 것이다. 본문이 곧 결과다.
        return Step(name=name, payload=payload, ok=True,
                    output=capability.render("full")[:2000])
    return Step(name=name, payload=payload, ok=False, error="그런 능력이 없습니다")


def _continue_episode(args, registry, first) -> int:
    """첫 단계로 안 끝났을 때만 이어간다.

    첫 단계는 지금 그대로다 - 라우터가 고르고 LLM 을 입력 뽑는 데 한 번만 쓴다.
    도구 하나로 끝나는 일에서 비용이 늘면 이 물건의 값이 깎인다. 여기는 **그것으로
    안 끝났을 때** 결과를 보고 다음을 정하는 자리다.
    """
    from .act import Episode, next_step, run_episode

    budget = budget_from(args)
    try:
        complete = build_completer(args, budget)
    except SystemExit:
        print("\n  더 할 수도 있지만 LLM 이 없어 여기서 멈춥니다.")
        return 0 if first.ok else 1

    episode = Episode(query=args.query, steps=[first])
    print(f"\n[이어서] 남은 단계 최대 {args.steps - 1}회")
    result = run_episode(
        args.query,
        decide=lambda ep, offers: next_step(complete, args.query, ep, offers),
        execute=lambda name, payload: _execute_step(args, registry, name, payload),
        offers_for=lambda ep: _offers_for(args, registry),
        approve=_approve_without_asking if args.yes else _approve,
        max_steps=args.steps - 1,
        on_step=lambda s: print(f"  {s.line()}"),
        check_budget=budget.check,
    )
    episode.steps.extend(result.steps)
    # 마지막에 나온 답을 먼저 보여 준다. 멈춘 사유("단계 상한에 닿았습니다")만
    # 찍으면 성공한 결과가 그 문구에 묻혀서, 잘 끝난 일이 실패처럼 읽힌다.
    if episode.ok:
        print(f"\n{json.dumps(episode.steps[-1].output, ensure_ascii=False)[:2000]}")
    print(f"\n{result.stopped}")
    print(f"  단계 {len(episode.steps)}회"
          + (f" · {budget.summary()}" if budget.calls else ""))
    return 0 if episode.ok else 1


def cmd_ask(args) -> int:
    """쿼리 한 줄로 끝까지. **어느 하위명령을 써야 하는지 몰라도 되어야 한다.**

    `route` 는 무엇이 있는지 알려주고 `run` 은 이름과 payload 를 요구한다. 둘 다
    사람이 이미 답을 알고 있을 때의 명령이다. 처음 쓰는 사람은 그냥 하고 싶은 말을
    한다. 그 말 하나로 다음이 다 일어나야 한다.

        고른다 -> (툴이면) 입력을 뽑아 실행한다 -> 답한다 -> 결과를 이력에 남긴다

    마지막이 중요하다. 성공한 과제 문장이 그 능력의 이력이 되고, 그게 다음 질문을
    더 잘 찾게 만드는 유일한 재료다(E11: 첫 38건에서 5.3% -> 71.1%).

    LLM 은 **입력을 뽑을 때만** 한 번 쓴다. 스킬을 골랐으면 0회다. 못 뽑으면
    지어내지 않고 어떻게 부르면 되는지 알려주고 멈춘다.

    `--steps` 를 2 이상으로 주면 그때부터 단계마다 한 번씩 더 쓴다(다음에 무엇을
    할지 정하는 데). 기본이 1 인 이유가 그것이다 - 한동안 기본을 3 으로 뒀더니
    도구 하나로 끝나는 일에도 두 번을 썼고, 두 번째는 이미 나온 답을 보고
    "끝났다"를 듣는 데만 쓰였다. 여러 도구가 필요한 사람만 값을 내면 된다.
    """
    from .router import Router
    from .tools import ToolPolicy, run_tool

    # **아는 것을 먼저 말한다.** 능력이 하나도 없어도 배운 사실은 있을 수
    # 있고, 처음 쓰는 사람이 정확히 그 상태다. 여기가 아니라 능력을 고른
    # 뒤에 회상하면, 능력이 없을 때 그 앞에서 끝나 영영 안 닿는다.
    known = _recall_for(args.query)
    _print_recalled(known)

    registry = _registry(args, translate=False)
    pool = registry.usable()
    if not pool:
        print("\n쓸 수 있는 능력이 없습니다. `jermes tool` 로 만들거나 `jermes learn` 으로 배우세요.")
        return 1

    allowed = ("safe", "caution", "dangerous") if args.risky else ("safe", "caution")
    result = Router(pool, allowed_risk=allowed).route(args.query, limit=3)
    if _found_nothing(result):
        # 쿼리 하나로 끝나야 하는 명령이다. "플래그를 붙여 다시 부르세요"는 답이
        # 아니다 - 하나도 못 골랐으면 여기서 한 번 더 찾아 본다.
        print("  겹치는 말이 없습니다 - 능력 설명에 한 줄을 붙이고 다시 찾습니다"
              "(능력마다 한 번, 캐시됨).")
        result = Router(_registry(args, translate=True).usable(),
                        allowed_risk=allowed).route(args.query, limit=3)
    if not result.chosen:
        print(f"'{args.query}' 에 맞는 능력이 없습니다.")
        print(f"  후보 {result.considered}개를 봤지만 겹치는 말이 없습니다.")
        if result.blocked:
            print(f"  정책상 제외 {len(result.blocked)}건 (위험한 것까지 보려면 --risky)")
        print("  그 능력이 무엇을 해왔는지가 장부에 없으면 못 찾습니다. 쓰이면 쌓입니다.")
        return 1

    best = result.chosen[0]
    print(f"고름: {best.capability.name} [{best.capability.label()}] · {best.score:.2f}")
    if best.thin:
        others = ", ".join(c.capability.name for c in result.chosen[1:])
        print(f"  근거 얇음: 과제의 {best.coverage:.0%} 만 설명됩니다"
              + (f" · 다른 후보 {others}" if others else " · 다른 후보 없음"))

    record = open_ledger().get(best.capability.name)
    if record is None and best.capability.kind == "mcp":
        # 남의 MCP 도구도 **부른다**. 오래 이 자리가 비어 있어서, 찾아 놓고
        # 카드만 보여 주고 끝났다 - 찾은 것과 쓴 것은 다르다.
        return _ask_mcp(args, best)
    if record is None:
        # MCP 말고 **호스트가 등록한** 다른 부르는 법도 있을 수 있다
        # (`jermes.capability_callers` - integrations/xgen.py 참조). 여기서는
        # `via` 값으로 고르기만 한다. 아무도 등록 안 했으면 그냥 지나간다 -
        # 문서 스킬로 착각해 본문을 찍는 것보다야 아무 일도 안 하는 게 낫다.
        via = (best.capability.invoke or {}).get("via", "") \
            if isinstance(best.capability.invoke, dict) else ""
        caller = _capability_caller(via)
        if caller is not None:
            return caller(args, best)
    if record is None or record.skill.kind != "tool":
        # 문서 스킬은 실행할 것이 아니라 읽을 것이다. 본문을 그대로 준다.
        print()
        print(best.capability.render("full"))
        return 0

    manifest = json.loads(record.skill.body)
    payload, why = _payload_for(args, manifest, args.query, known,
                                budget=budget_from(args))
    if payload is None:
        print(f"\n입력을 뽑지 못했습니다: {why}")
        print(f"  직접 주세요: jermes run {record.name} --payload '{{...}}'")
        print(f"  계약: {manifest.get('contract', '')}")
        return 1

    print(f"입력: {json.dumps(payload, ensure_ascii=False)}")
    outcome = run_tool(manifest.get("script", ""), payload,
                       policy=ToolPolicy.from_dict(manifest.get("policy")))
    _log_own(args.query, record.name, payload, outcome.ok,
             detail=outcome.error or "", seconds=outcome.seconds)
    if not outcome.ok:
        # 실패는 이력에 안 남긴다. 못 한 일을 한다고 광고하는 꼴이다.
        open_ledger().record_outcome([record.name], False, task=args.query)
        print(f"\n실패: {outcome.error}")
        if args.steps > 1:
            # **실패했을 때야말로** 다른 길을 찾아야 한다. 여기서 끝내면, 무엇을
            # 넣었다 왜 깨졌는지를 다음 판단에 넘기려고 만든 장치가 정작 제일
            # 필요한 순간에 안 돈다. 라우터가 얇은 근거로 엉뚱한 것을 골랐을 때가
            # 정확히 이 경우다.
            from .act import Step

            return _continue_episode(args, registry, Step(
                name=record.name, payload=payload, ok=False,
                error=outcome.error or "", seconds=outcome.seconds))
        return 1

    print(f"\n{json.dumps(outcome.output, ensure_ascii=False)}")
    print(f"({outcome.seconds * 1000:.0f}ms)")
    _record_if_confident(record.name, best, args.query)
    if args.steps > 1:
        from .act import Step

        return _continue_episode(args, registry, Step(
            name=record.name, payload=payload, ok=True,
            output=outcome.output, seconds=outcome.seconds))
    return 0


_PAYLOAD_PROMPT = """이 도구를 부르려면 어떤 JSON 을 넣어야 하는지 정하세요.

도구: {name}
설명: {description}
지난 호출 예시(모양을 그대로 따르세요):
{examples}
{known}
사용자가 한 말: {query}

JSON 객체 하나만 출력하세요. 설명 금지. 값을 알 수 없으면 빈 객체를 내세요."""

# 회상한 사실을 모델이 읽는 자리로 보내는 조각.
#
# 왜 필요한가(실측): 회상은 되는데 화면에만 찍혔다. 신뢰 0.95 인 "기본 브랜치는
# develop 이다" 를 출력한 **바로 다음 줄**에서 `base: "main"` 을 넣었다. 보여 주는
# 것과 쓰는 것은 다르다.
#
# 신뢰도를 함께 준다. 딱지 없이 컨텍스트에 넣지 않는 규율이 여기에도 걸리고,
# 모델이 확신의 정도를 알고 쓰는 편이 낫다.
_KNOWN_BLOCK = """
이 사용자에 대해 **측정으로 확인된 사실**(신뢰도 순, 어길 이유가 없으면 따르세요):
{facts}
"""


def _memory_mark(item) -> str:
    """이 사실을 **얼마나 믿어야 하는지** 한 마디로. 사람 화면과 모델 프롬프트가
    같은 말을 써야 한다 - 한쪽만 고치면 사람은 "못가름" 을 보는데 모델은
    "신뢰 0.50" 을 보고, 둘이 다른 것을 근거로 판단하게 된다.
    """
    if item.told_us_nothing:
        return f"{item.trust:.2f} 못가름"
    return f"신뢰 {item.trust:.2f}" if item.measured else "미측정"


def _known_block(items) -> str:
    if not items:
        return ""
    facts = chr(10).join(f"- [{_memory_mark(item)}] {item.text[:200]}"
                         for item in items)
    return _KNOWN_BLOCK.format(facts=facts)


def _capability_caller(via: str):
    """`via` 값 하나를 부르는 법으로 바꾼다. MCP 는 core 가 직접 안다(가장 흔하고
    항상 있으니 왕복 하나 아끼려고). 그 밖의 것은 `jermes.capability_callers`
    entry_point 에서 찾는다 - 이 파일은 누가 등록했는지 모른다."""
    if not via:
        return None
    from .registry import GROUP_CAPABILITY_CALLERS, Registry

    try:
        return Registry(GROUP_CAPABILITY_CALLERS).get(via)
    except KeyError:
        return None


def _ask_mcp(args, choice) -> int:
    """고른 것이 남의 MCP 도구일 때. 입력을 뽑아 **실제로 부른다.**

    부르기 전에 위험을 따진다. 서버가 `readOnlyHint` 로 읽기전용이라고 **말한**
    도구만 바로 부르고, 그렇지 않으면 무엇을 부를지 보여 주고 멈춘다. 주석이 없는
    도구는 위험한 것이 아니라 **모르는 것**이라 자동으로 부르지 않는다 - 모르는
    것을 안전하다고 치는 순간 이 등급제는 장식이 된다. `--risky` 가 동의다.
    """
    from .mcp_client import call_stdio_tool, load_servers

    capability = choice.capability
    invoke = capability.invoke if isinstance(capability.invoke, dict) else {}
    server, tool = invoke.get("server", ""), invoke.get("tool", "")
    servers = load_servers(_mcp_config_paths())
    spec = servers.get(server)
    if not spec:
        print(f"\n서버 설정을 못 찾았습니다: {server}")
        print("  `jermes capabilities --live` 로 다시 받아오세요.")
        return 1

    manifest = {"name": capability.name, "description": capability.description,
                "input_schema": invoke.get("input_schema") or {}}
    payload, why = _payload_for(args, manifest, args.query,
                                _recall_for(args.query))
    if payload is None:
        print(f"\n입력을 뽑지 못했습니다: {why}")
        print(f"  입력 형식: {json.dumps(invoke.get('input_schema') or {}, ensure_ascii=False)[:200]}")
        return 1

    if not (capability.read_only and capability.annotated) and not args.risky:
        # 왜 안 부르는지와 무엇을 부르려 했는지를 같이 보여 준다. 그래야 사람이
        # 한 번 보고 동의할 수 있다.
        reason = ("읽기전용이라고 서버가 말하지 않았습니다"
                  if capability.annotated else "서버가 주석을 주지 않아 성질을 모릅니다")
        print(f"\n부르지 않았습니다: {reason} [{capability.risk()}]")
        print(f"  부르려던 것: {server}:{tool} {json.dumps(payload, ensure_ascii=False)}")
        print("  그래도 부르려면 --risky 를 붙이세요.")
        return 1

    print(f"입력: {json.dumps(payload, ensure_ascii=False)}")
    try:
        ok, text = call_stdio_tool(spec, tool, payload,
                                   timeout=getattr(args, "tool_timeout", 60.0))
    except Exception as exc:
        print(f"\n실패: {type(exc).__name__}: {exc}")
        return 1
    print()
    print(text[:4000])
    _log_own(args.query, f"{server}:{tool}", payload, ok, detail=text[:200])
    # 성공한 과제 문장만 이력이 된다. 실패를 남기면 다음에 또 이리로 온다.
    if ok:
        _record_if_confident(capability.name, choice, args.query)
    return 0 if ok else 1


def _payload_for(args, manifest: dict, query: str, known=None, budget=None):
    """사용자 말에서 툴 입력을 뽑는다. 반환 (payload, 실패사유).

    지난 케이스의 payload 모양을 예시로 준다. 스키마를 지어내는 것보다 **실제로
    통과한 모양**을 보여주는 편이 정확하다. 값을 모르면 빈 객체를 내게 하고 우리가
    멈춘다. 지어낸 값으로 도구를 부르면 그럴듯하게 틀린 답이 나온다.
    """
    cases = manifest.get("cases") or []
    schema = manifest.get("input_schema") or {}
    if cases:
        examples = "\n".join(json.dumps(c.get("payload") or {}, ensure_ascii=False)
                             for c in cases[:4])
    elif schema:
        # 우리가 만든 툴은 지난 케이스가 있지만, 남의 MCP 도구는 없다. 대신 서버가
        # **입력 스키마**를 준다 - 예시보다 더 정확한 계약이다. 이걸 안 보고 "예시가
        # 없다"고 멈추는 바람에, 붙여 놓은 도구를 부르지 못하고 있었다.
        examples = ("예시는 없습니다. 서버가 준 입력 스키마를 그대로 따르세요:\n"
                    + json.dumps(schema, ensure_ascii=False)[:1200])
    else:
        return None, "지난 호출 예시도 입력 스키마도 없습니다"
    try:
        # 예산을 **받아서** 쓴다. 예전에는 여기서 새 `Budget` 을 만들어,
        # 이 호출이 사용자의 상한을 지나가지 않고 화면의 사용량에도 안 잡혔다.
        # 여기는 `ask` 의 핫패스다.
        complete = build_completer(args, budget)
    except SystemExit:
        return None, "LLM 이 없어 입력을 못 뽑습니다"
    prompt = _PAYLOAD_PROMPT.format(name=manifest.get("name", ""),
                                    description=manifest.get("description", ""),
                                    examples=examples,
                                    known=_known_block(known or []),
                                    query=query)
    try:
        raw = complete(prompt).strip()
    except Exception as exc:
        return None, type(exc).__name__
    if "{" in raw:
        raw = raw[raw.find("{"):raw.rfind("}") + 1]
    try:
        payload = json.loads(raw)
    except ValueError:
        return None, f"모델이 JSON 을 주지 않았습니다: {raw[:80]}"
    if not isinstance(payload, dict):
        return None, "객체가 아닙니다"
    if not payload:
        # 빈 객체는 보통 "모르겠다"는 뜻이라 멈춘다. 다만 스키마에 **필수 항목이
        # 없으면** 빈 객체가 정답이다 - 그걸 모른다고 읽으면 인자 없는 도구는
        # 영영 못 부른다(실측: `docgraph_topics` 가 그랬다).
        if schema and not (schema.get("required") or []):
            return {}, ""
        return None, "값을 알 수 없다고 했습니다(빈 객체)"
    return payload, ""


def cmd_watch(args) -> int:
    """새로 끝난 세션이 생기면 **알아서 배운다.**

    여태는 사람이 `jermes learn` 을 쳐야만 배웠다. 그러면 배우는 일이 기억나는
    날에만 일어나고, 정작 배울 것이 많은 바쁜 주에 안 일어난다.

    규율 세 가지. 이게 없으면 자동은 사고다.
    - **같은 세션을 두 번 안 배운다.** 무엇을 봤는지 `watched.json` 에 남긴다.
    - **예산 안에서만 돈다.** `--max-usd`/`--max-tokens` 는 여기서도 그대로다.
      넘으면 조용히 계속하지 않고 멈춘다.
    - **한 바퀴에 몇 개까지만.** 한 번에 다 배우려다 밤새 도는 것보다, 조금씩
      자주 도는 편이 낫다.

    한 번만 돌고 끝내려면 `--once`. 그게 기본이다 - 상주 프로세스는 사용자가
    시켜야 하는 일이라 `--interval` 을 줘야 계속 돈다.
    """
    state_path = home() / "watched.json"
    try:
        seen = set(json.loads(state_path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        seen = set()

    budget = budget_from(args)
    rounds = 0
    def remember_seen() -> None:
        """어떻게 죽든 **본 것은 본 것으로 남긴다.**

        예전에는 저장이 한 바퀴 끝에만 있었다. 예산 초과는 따로 잡아 저장했지만
        그 밖의 예외나 Ctrl+C 는 저장 없이 빠져나간다. 열 개를 배우고 열한
        번째에서 죽으면 다음에 열 개를 다시 배운다 - 그만큼 LLM 비용을 두 번
        낸다. 오래 도는 물건에서는 이게 제일 비싼 실수다.
        """
        try:
            write_atomically(state_path, json.dumps(sorted(seen)))
        except OSError as exc:
            print(f"[watch] 커서를 못 남겼습니다: {exc}")

    try:
        return _watch_rounds(args, budget, seen, remember_seen)
    finally:
        remember_seen()


def _watch_rounds(args, budget, seen, remember_seen) -> int:
    rounds = 0
    while True:
        rounds += 1
        # `--root` 를 무시하면 격리한 것처를 안 보고 진짜 기록을 읽게 된다.
        files = iter_session_files(args.root)
        fresh = [p for p in files[:args.limit] if str(p) not in seen]
        learned = skipped = nothing = 0
        for path in fresh[:args.per_round]:
            summary = summarize_session(path, max_lines=args.max_lines)
            seen.add(str(path))
            if not summary.worth_learning:
                skipped += 1
                continue
            print(f"\n[watch] {path.name}")
            # 같은 인자로 `learn` 을 부른다. 배우는 규칙이 두 벌이 되면 언젠가
            # 갈라지고, 자동으로 배운 것만 검증이 헐거워진다.
            once = argparse.Namespace(**vars(args))
            once.session = path.stem
            try:
                # **"배움" 은 실제로 배운 것만 센다.** `cmd_learn` 은 초안이 0건
                # 이어도 0 을 돌려준다(그건 정상 종료다). 그걸 배움으로 세면
                # 화면에 `배움 1` 이 뜨는데 그 바퀴에서 원장은 한 줄도 안 늘었다.
                # 실측: 초안 0건으로 끝난 바퀴가 `배움 1` 로 보고됐다.
                before = len(open_ledger().list())
                cmd_learn(once)
                if len(open_ledger().list()) > before:
                    learned += 1
                else:
                    nothing += 1
            except BudgetExceeded as exc:
                print(f"[watch] 예산에 닿아 멈춥니다: {exc}")
                return 1          # 커서는 바깥 finally 가 남긴다
            except SystemExit as exc:
                print(f"[watch] {exc}")
                break
        remember_seen()
        print(f"{chr(10)}[watch] {rounds}바퀴 · 배움 {learned} · "
              f"배운 것 없음 {nothing} · 건너뜀 {skipped} · "
              f"이미 본 것 {len(seen)}개"
              + (f" · 토큰 {budget.tokens:,}" if budget else ""))

        if not args.interval:
            return 0
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\n[watch] 멈춥니다.")
            return 0


def cmd_doctor(args) -> int:
    """이 컴퓨터에서 Jermes 가 **왜 그것밖에 못 하는지** 한 번에 짚는다.

    기능이 많아질수록 "안 되는데요"의 원인 후보가 늘어난다. LLM 을 못 찾았는지,
    세션 경로가 비었는지, MCP 서버가 안 뜨는지, 원장이 깨졌는지. 하나씩 물어보게
    하면 사람이 지친다. 그래서 **되는 것과 안 되는 것을 다 세어서**, 안 되는 것마다
    다음 한 줄을 준다.

    [주의] 여기서 고쳐 주지 않는다. 진단은 진단이고 조치는 사람이 정한다.
    """
    from .mcp_client import load_servers

    checks: list[tuple[str, bool | None, str]] = []

    # ── LLM
    base = args.base_url or os.environ.get("JERMES_BASE_URL", "")
    model = args.model or os.environ.get("JERMES_MODEL", "")
    if not base or not model:
        found_base, found_model = discover_endpoint()
        base, model = base or found_base, model or found_model
    checks.append(("LLM", bool(base and model),
                   f"{base} · {model}" if base else
                   f"못 찾음 (찾아본 곳: {', '.join(LOCAL_ENDPOINTS)})"))

    # ── 배울 재료
    sessions = iter_session_files(None)
    learnable = sum(1 for p in sessions[:20]
                    if summarize_session(p, max_lines=2000).worth_learning)
    checks.append(("세션 기록", bool(sessions),
                   f"{len(sessions)}개 · 최근 20개 중 배울거리 {learnable}개"
                   if sessions else
                   "없음 (JERMES_CLAUDE_PROJECTS 로 경로를 지정하세요)"))

    # ── 자기 기록(자기가 한 일에서 배우는 재료)
    from .sources import own as _own

    own_files = _own.iter_session_files()
    if _own.record_errors:
        checks.append(("자기 기록", False,
                       f"마지막 오류: {_own.record_errors[-1]}"))
    else:
        # 아직 안 써 본 것은 **고칠 것이 없다.** `안됨` 으로 세면 갓 설치한 사람이
        # 자기 잘못이 아닌 빨간 줄을 본다.
        checks.append(("자기 기록", True if own_files else None,
                       f"{len(own_files)}일치" if own_files else
                       "아직 없음 (jermes ask/run 을 쓰면 쌓입니다)"))

    # ── 원장
    try:
        records = open_ledger().list()
        verified = sum(1 for r in records if r.skill.verified)
        tools = sum(1 for r in records if r.skill.kind == "tool")
        checks.append(("원장", True,
                       f"스킬 {len(records)}개 (검증 {verified} · 툴 {tools})"))
    except Exception as exc:
        checks.append(("원장", False, f"읽기 실패: {type(exc).__name__}: {exc}"))

    # ── 기억과 규약
    memory = load_memory()
    checks.append(("기억", True, f"{len(memory)}건 · {memory_path()}"))
    law = load_constitution()
    checks.append(("규약", True,
                   f"never_learn {len(law.never_learn)}개 · {constitution_path()}"))

    # ── 근처 능력
    registry = _registry(args, translate=False)
    usable = registry.usable()
    checks.append(("근처 능력", bool(usable),
                   f"{len(registry.items)}개 발견 · 부를 수 있는 것 {len(usable)}개"
                   if usable else
                   "부를 수 있는 것 없음 (`jermes capabilities --live` 로 MCP 도구를 받아오세요)"))

    # ── MCP 서버: 설정에 몇 개 적혀 있고 몇 개가 실제로 떴나
    servers = load_servers(_mcp_config_paths())
    cached = home() / "mcp-tools.json"
    if cached.exists():
        try:
            got = json.loads(cached.read_text(encoding="utf-8"))
            live = f"{len(got)}곳에서 도구 {sum(len(v) for v in got.values())}개를 받아 둠"
        except ValueError:
            live = "받아 둔 목록이 깨졌습니다 (`--live` 로 다시 받으세요)"
    else:
        live = "아직 붙어 본 적 없음 (`jermes capabilities --live`)"
    checks.append(("MCP 서버", bool(servers),
                   f"설정에 {len(servers)}곳 · {live}" if servers else
                   "설정에 없음 (JERMES_MCP_CONFIG 로 경로를 더할 수 있습니다)"))

    # ── 검증을 실제로 할 수 있는가. 이게 이 도구의 존재 이유라 따로 본다.
    can_verify = bool(base and model) and learnable > 0
    checks.append(("검증 가능", can_verify,
                   "세션에서 재현벤치를 만들 수 있습니다" if can_verify else
                   "LLM 과 배울 세션이 둘 다 있어야 게이트가 판정합니다"))

    width = max(len(name) for name, _, _ in checks)
    print("Jermes 진단 - 되는 것과 안 되는 것\n")
    bad = 0
    for name, ok, detail in checks:
        # 세 번째 상태 `None` = **아직**. 고칠 것이 없고 아직 안 한 것뿐이다.
        # 타입에는 `bool | None` 이라고 적혀 있었는데 렌더링이 안 쓰고 있었다.
        # 그래서 갓 설치한 사람이 "2건이 막혀 있습니다"를 보는데, 그중 하나가
        # "아직 안 써 봤습니다"였다. 헛경보가 섞이면 진단 전체를 안 믿게 된다.
        mark = "OK  " if ok else ("아직" if ok is None else "안됨")
        bad += 1 if ok is False else 0
        print(f"  {mark}  {name:<{width}}  {detail}")

    print()
    if bad:
        print(f"{bad}건이 막혀 있습니다. 위 괄호 안이 다음 한 줄입니다.")
    else:
        print("전부 준비됐습니다. `jermes learn` 으로 시작하세요.")
    return 1 if bad else 0


def cmd_status(args) -> int:
    """맨손으로 `jermes` 를 쳤을 때 - 이 컴퓨터에서 **지금 뭘 할 수 있는지** 말한다.

    설명서를 읽게 하지 않는다. 있는 재료(세션·원장·LLM)를 세어 보고 다음 한 줄을
    찍어 준다. 없는 것은 없다고 하고, 대신 뭘 하면 되는지 알려준다.
    """
    print("Jermes - 끝난 실행에서 배우고, 효과가 실측된 것만 남깁니다.\n")

    # 개수만 쓴다. 예전에는 14,320개를 mtime 으로 줄 세워 놓고 `len()` 만
    # 불렀다 - 세는 데 정렬이 필요 없다.
    sessions = iter_session_files(None)
    print(f"  세션 기록   {len(sessions)}개" if sessions else
          "  세션 기록   없음 (JERMES_CLAUDE_PROJECTS 로 경로를 지정할 수 있습니다)")

    records = open_ledger().list()
    tools = [r for r in records if r.skill.kind == "tool"]
    verified = [r for r in records if r.skill.verified]
    print(f"  원장        스킬 {len(records)}개 (검증 {len(verified)} · 툴 {len(tools)})")
    print(f"  기억        {len(load_memory())}건")
    print(f"  집          {home()}")

    base = os.environ.get("JERMES_BASE_URL", "")
    model = os.environ.get("JERMES_MODEL", "")
    if not base:
        base, model = discover_endpoint()
        found = f"{base} · {model}" if base else "찾지 못함"
        print(f"  LLM         {found} (자동 탐색)")
    else:
        print(f"  LLM         {base} · {model} (환경변수)")

    print("\n다음에 할 것:")
    if not records:
        print("  jermes demo                     게이트가 무엇을 가르는지 30초 만에 보기 (LLM 불필요)")
    if sessions:
        print("  jermes sessions                 배울 거리가 있는 세션 훑기")
        if base:
            print("  jermes learn                    가장 최근 세션에서 배우기")
    if not base:
        print("  (LLM 없이도) jermes demo / list / show / import / export 는 돕니다")
    print("  jermes tool <이름> --cases <파일>  절차를 실행 가능한 툴로 만들기")
    if tools:
        print(f"  jermes run {tools[0].name} --payload '{{...}}'   만든 툴 실행")
    # 붙일 MCP 서버가 있는데 아직 안 붙었으면, 그게 지금 가장 값싼 한 걸음이다.
    # 안 알려 주면 사용자는 근처에 도구가 있다는 사실 자체를 모른다.
    if not (home() / "mcp-tools.json").exists():
        print("  jermes capabilities --live      근처 MCP 서버의 도구까지 끌어오기")
    print("  jermes doctor                   안 되는 게 있으면 왜인지 한 번에")
    return 0


def cmd_demo(args) -> int:
    """LLM 없이, 게이트가 진짜로 가르는지 눈으로 보여준다."""
    from .gate import BenchCase, GateConfig, split_holdout
    from .model import Provenance, SkillCandidate, SkillDef

    cases = [BenchCase(case_id=f"c{i}") for i in range(12)]
    cfg = GateConfig()
    dev, _held = split_holdout(cases, cfg.holdout_ratio)
    dev_ids = {c.case_id for c in dev}

    def candidate(name):
        return SkillCandidate(name=name, kind="guide", scope="user", action="create",
                              rationale="데모", when_to_use="데모", procedure=["a"],
                              verification=["v"], provenance=Provenance(origin="demo"))

    def skill(name):
        return SkillDef(name=name, kind="guide", scope="user", description="데모", body="b")

    arms = [
        ("진짜 도움이 되는 스킬", lambda case, s: (0.9 if s else 0.1), "promoted"),
        ("아무 효과 없는 스킬", lambda case, s: 0.5, "rejected"),
        ("dev 만 외운 스킬", lambda case, s: (0.9 if (s and case.case_id in dev_ids) else 0.1),
         "rejected"),
    ]
    print("게이트가 무엇을 가르는가 - LLM 없이 점수만 흉내낸 데모\n")
    for label, scorer, expect in arms:
        result = ForgeGate(scorer).verify(candidate("demo-skill"), skill("demo-skill"), cases)
        mark = "OK" if result.verdict == expect else "불일치"
        print(f"[{label}]")
        print(f"  판정 {result.verdict} (기대 {expect}) {mark}")
        for reason in result.reasons:
            print(f"    - {reason}")
    print("\n요점: 홀드아웃에서 재현되지 않으면 승격하지 않는다. 외운 것은 거절한다.")
    return 0


# ───────────────────────────────────────────────────────── 진입점

class _Parser(argparse.ArgumentParser):
    """명령 이름을 틀렸을 때 **스물넷을 다 토하지 않는다.**

    실측: `jermes lst` 가 24개 선택지를 통째로 쏟았다. 사람이 원하는 것은 목록이
    아니라 "혹시 list?" 한 줄이다. 목록은 `--help` 가 있는 자리다.
    """

    def error(self, message: str):
        import difflib
        import re

        bad = re.search(r"invalid choice: '([^']+)'", message)
        if bad:
            names = [a for action in self._subparsers._group_actions
                     for a in getattr(action, "choices", {})] if self._subparsers else []
            close = difflib.get_close_matches(bad.group(1), names, n=2, cutoff=0.5)
            nl = chr(10)
            hint = (f"  이거 말씀이신가요: {', '.join(close)}" + nl) if close else ""
            self.exit(2, f"모르는 명령입니다: {bad.group(1)}" + nl + hint
                      + "  전체 목록은 `jermes --help` 입니다." + nl)
        super().error(message)


class _Sentence(argparse.Action):
    """따옴표 없이 친 **문장**을 그대로 받는다.

    이 물건의 간판은 `jermes ask <문장>` 인데, 사람이 자연스럽게 치는
    `jermes ask 엑셀 XFD 열 몇번째야` 가 argparse 오류 덤프로 끝났다 - 낱말이
    여럿이라 "unrecognized arguments" 가 난 것이다. 따옴표를 요구하는 것은
    "쿼리 하나로" 라고 말하는 물건이 할 소리가 아니다.

    붙일 때는 **한 칸**으로 잇는다. 셸이 이미 낱말로 쪼개 줬으므로 원래 띄어쓰기는
    복원할 수 없고, 토크나이저는 어차피 낱말 단위로 본다.
    """

    def __call__(self, parser, namespace, values, option_string=None):
        if isinstance(values, str):
            setattr(namespace, self.dest, values)
            return
        setattr(namespace, self.dest, " ".join(str(v) for v in values))


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="jermes",
        description="Jermes - 끝난 실행에서 배우고, 효과가 실측된 것만 남긴다.")
    # required=False: 맨손 `jermes` 는 오류가 아니라 **현황 + 다음 한 줄**이어야 한다.
    sub = parser.add_subparsers(dest="command")

    def add_source_flags(p):
        p.add_argument("--root", default=None, help="Claude Code 프로젝트 기록 경로")
        p.add_argument("--limit", type=int, default=20, help="검사할 세션 수")
        p.add_argument("--max-lines", type=int, default=8000,
                       help="세션당 읽을 최대 줄 수(0=전체)")

    def add_llm_flags(p):
        p.add_argument("--base-url", default="", help="OpenAI 호환 엔드포인트(쉼표=장애조치)")
        p.add_argument("--model", default="", help="모델 id(쉼표=엔드포인트와 순서 대응)")
        p.add_argument("--api-key", default=os.environ.get("JERMES_API_KEY", ""))
        p.add_argument("--timeout", type=float, default=120.0)
        # 비용. 아무것도 안 주면 상한 없이 세기만 한다.
        p.add_argument("--max-calls", type=int, default=0, help="LLM 호출 상한(0=무제한)")
        p.add_argument("--max-tokens-budget", type=int, default=0, help="토큰 상한")
        p.add_argument("--max-usd", type=float, default=0.0, help="금액 상한(요율 필요)")
        # 시간은 다른 상한과 성격이 다르다 - 토큰은 안 부르면 안 늘지만 시간은
        # 그냥 흐른다. 느린 엔드포인트에 붙으면 토큰 상한에 닿기 전에 몇 시간이
        # 지난다. 자리를 비운 사이 도는 물건이라 여기가 제일 필요한 상한이다.
        p.add_argument("--max-seconds", type=float, default=0.0,
                       help="벽시계 상한(초, 0=무제한)")
        p.add_argument("--usd-per-1k", type=float, default=0.0,
                       help="1k 토큰당 달러. 주면 금액을 낸다 - 가격은 코드에 안 박는다")

    p = sub.add_parser("sessions", help="배울 거리가 있는 세션 훑기")
    add_source_flags(p)
    p.add_argument("--all", action="store_true", help="신호 없는 세션도 표시")
    p.set_defaults(func=cmd_sessions)

    p = sub.add_parser("learn", help="세션에서 배우기")
    add_source_flags(p)
    add_llm_flags(p)
    p.add_argument("--session", default="", help="세션 id 일부(생략하면 가장 최근 것)")
    p.add_argument("--samples", type=int, default=2, help="초안 샘플 수")
    p.add_argument("--max-bench", type=int, default=12,
                   help="재현벤치 최대 케이스 수(0=전부). 최소 4건만 넘으면 판정은 선다")
    # 한 세션의 실패는 서로 무관한 잡탕이라, 스킬 하나가 관계된 케이스가 0~1건뿐
    # 이었다(실측). 여러 세션에서 모아야 같은 실패가 여러 번 모인다.
    p.add_argument("--bench-sessions", type=int, default=3000,
                   help="재료를 찾아 훑어볼 세션 수(1=이 세션만)")
    p.add_argument("--bench-cases", type=int, default=120,
                   help="모을 재현 케이스 목표 수. 채우면 훑기를 멈춘다")
    # 이보다 작은 세션은 읽지 않는다. 도구 몇 번 부르고 끝난 것이라 실패-복구
    # 쌍이 없다. 크기는 목록을 만들 때 이미 알고 있어 거르는 비용이 0 이다.
    p.add_argument("--bench-min-bytes", type=int, default=60_000,
                   help="이 크기 미만 세션은 재료로 안 본다")
    p.add_argument("--max-memory-measure", type=int, default=6,
                   help="한 사이클에 재볼 기억 개수(미측정 항목 우선)")
    p.set_defaults(func=cmd_learn)

    p = sub.add_parser("list", help="원장 보기")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("rollback",
                       help="지난 판본으로 되돌린다(이력은 남는다)")
    p.add_argument("name")
    p.add_argument("--to", default="", help="갈 판본(없으면 바로 이전)")
    p.add_argument("--list", action="store_true", help="판본만 보기")
    p.set_defaults(func=cmd_rollback)
    p = sub.add_parser("approve",
                       help="사람이 승인해 staged 를 올린다")
    p.add_argument("name")
    p.add_argument("--by", default="", help="승인자 이름")
    p.set_defaults(func=cmd_approve)
    p = sub.add_parser("trace",
                       help="한 세션이 무엇을 낳았는지 되짚는다")
    p.add_argument("run", nargs="?", default="",
                   help="세션 id 의 일부")
    p.set_defaults(func=cmd_trace)
    p = sub.add_parser("show", help="스킬 본문 보기")
    p.add_argument("name")
    p.add_argument("--plain", action="store_true",
                   help="본문만(내력 없이). 파일로 뽑아 쓸 때")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("export", help="agentskills.io 표준으로 내보내기")
    p.add_argument("name")
    p.add_argument("--out", default="", help="출력 디렉터리")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("import", help="SKILL.md 들여오기")
    p.add_argument("file")
    p.add_argument("--as", dest="as_name", default="")
    p.add_argument("--replace", action="store_true")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("memory", help="기억 보기 · 가르치기 · 내리기")
    p.add_argument("--show", default="",
                   help="그 사실 하나의 내력(원천·측정·판정)을 본다")
    p.add_argument("--add", default="", nargs="+", action=_Sentence,
                   help="사실 하나를 손으로 넣는다")
    p.add_argument("--global", dest="global_scope", action="store_true",
                   help="이 프로젝트가 아니라 **나에 대한** 사실로 넣는다")
    p.add_argument("--retire", default="", help="그 id 를 내린다(지우지 않음)")
    p.add_argument("--supersede", default="",
                   help="그 id 를 --add 로 준 새 사실로 대체한다")
    p.add_argument("--at", default="", help="대체 시점(ISO). 없으면 지금")
    p.set_defaults(func=cmd_memory)

    p = sub.add_parser("law", help="규약 보기/변경(변경은 승인자 필수)")
    p.add_argument("--adopt", default="", help="변경할 항목(JSON)")
    p.add_argument("--by", default="", help="승인자 이름")
    p.set_defaults(func=cmd_law)

    p = sub.add_parser("tool", help="절차를 실행 가능한 툴로 만들고 **실행해서** 검증")
    add_llm_flags(p)
    p.add_argument("name", help="툴 이름(kebab-case)")
    p.add_argument("--task", default="", nargs="+", action=_Sentence,
                   help="무엇을 하는 툴인지 한 줄")
    p.add_argument("--cases", required=True, help="검증 케이스 JSON 파일")
    p.add_argument("--script", default="", help="이미 있는 스크립트를 검증만 할 때")
    p.add_argument("--attempts", type=int, default=3, help="실패 시 고쳐쓸 횟수")
    p.add_argument("--policy", default="strict",
                   help="권한: strict(기본) | files | network | trusted")
    p.add_argument("--env", default="",
                   help="툴에 넘길 환경변수 이름(쉼표). 기본은 아무것도 안 넘긴다")
    p.add_argument("--no-holdout", action="store_true",
                   help="예시가 명세 전부일 때(감춘 검증 없음 → 판정은 staged 까지만)")
    p.add_argument("--tool-timeout", type=float, default=10.0, help="툴 1회 실행 제한(초)")
    p.add_argument("--out", default="", help="표준 패키지로 내보낼 디렉터리")
    p.set_defaults(func=cmd_tool)

    p = sub.add_parser("run", help="원장에 있는 툴을 실행")
    p.add_argument("name")
    p.add_argument("--payload", default="", help="입력 JSON(생략하면 stdin)")
    p.add_argument("--tool-timeout", type=float, default=10.0)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("capabilities", help="근처에서 쓸 수 있는 능력 전부 보기")
    add_llm_flags(p)
    p.add_argument("--translate", action="store_true",
                   help="설명이 다른 언어인 능력에 우리말 한 줄을 붙인다(1회, 캐시됨)")
    p.add_argument("--live", action="store_true",
                   help="MCP 서버에 실제로 붙어 도구까지 받아온다(서버를 띄웁니다)")
    p.set_defaults(func=cmd_capabilities)

    p = sub.add_parser("route", help="과제에 맞는 능력을 골라 주기(알잘딱)")
    p.add_argument("task", nargs="+", action=_Sentence,
                   help="무엇을 하려는지 한 줄")
    p.add_argument("--limit", type=int, default=5,
                   help="몇 개까지(10~15개 넘으면 모델 정확도가 떨어진다)")
    p.add_argument("--risk", default="", help="허용 위험도(쉼표): safe,caution,dangerous")
    p.add_argument("--include-unresolved", action="store_true",
                   help="내용을 모르는 것(MCP 서버 등)도 포함")
    p.add_argument("--render", action="store_true", help="프롬프트 조각까지 출력")
    p.add_argument("--translate", action="store_true",
                   help="다른 언어로 쓰인 능력도 우리말로 찾게(1회, 캐시됨)")
    add_llm_flags(p)
    p.set_defaults(func=cmd_route)

    p = sub.add_parser("improve", help="기존 툴 회귀검사 + 깨졌으면 고치기")
    add_llm_flags(p)
    p.add_argument("name")
    p.add_argument("--cases", default="", help="케이스를 더 줄 파일(회귀 강화)")
    p.add_argument("--check-only", action="store_true", help="고치지 말고 검사만")
    p.set_defaults(func=cmd_improve)

    p = sub.add_parser("ask", help="쿼리 한 줄로 끝까지")
    add_llm_flags(p)
    p.add_argument("query", nargs="+", action=_Sentence)
    p.add_argument("--risky", action="store_true")
    # 도구 하나로 끝나는 일이 대부분이고 그 경로는 LLM 을 한 번만 쓴다. 그래서
    # 기본을 크게 두지 않는다 - 안 필요한 사람에게 비용을 물리지 않는다. 두 도구가
    # 필요한 일에서만 이어진다.
    # **기본은 1 이다.** 3 으로 뒀더니 도구 하나로 끝나는 일 - 제일 흔한 경우 -
    # 에서도 LLM 을 두 번 썼다(실측 1회 -> 2회). 두 번째 호출은 이미 나온 답을
    # 보고 "끝났다"를 듣기 위한 것뿐이다. 여러 도구가 필요한 사람만 값을 내면
    # 된다 - 안 필요한 사람에게 비용을 물리지 않는다.
    p.add_argument("--steps", type=int, default=1,
                   help="최대 단계 수(기본 1. 여러 도구를 이어 쓰려면 2 이상)")
    p.add_argument("--yes", action="store_true",
                   help="읽기전용이 아닌 단계도 묻지 않고 진행")
    p.add_argument("--tool-timeout", type=float, default=60.0)
    p.set_defaults(func=cmd_ask)

    p = sub.add_parser("install", help="검증된 것을 다른 에이전트가 집는 자리에 놓기")
    p.add_argument("name", nargs="?", default="", help="하나만 설치할 때")
    p.add_argument("--into", default="",
                   help=f"설치할 디렉터리(경로구분자로 여러 개). 기본 ~/.claude/skills")
    p.add_argument("--all", action="store_true",
                   help="미검증까지 설치 - 받는 쪽은 확인된 줄 안다")
    p.set_defaults(func=cmd_install)

    p = sub.add_parser("serve", help="단조한 툴을 MCP 서버로 내주기(stdio)")
    p.add_argument("--include-staged", action="store_true",
                   help="미검증 툴까지 내준다 - 기본은 검증된 것만")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("demo", help="게이트가 무엇을 가르는지 보기(LLM 불필요)")
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("watch", help="새 세션이 생기면 알아서 배우기")
    add_llm_flags(p)
    p.add_argument("--root", default=None, help="세션 기록 경로")
    p.add_argument("--limit", type=int, default=20, help="검사할 세션 수")
    p.add_argument("--per-round", type=int, default=2,
                   help="한 바퀴에 배울 세션 수(조금씩 자주가 낫다)")
    p.add_argument("--interval", type=float, default=0.0,
                   help="초. 주면 계속 돈다(기본은 한 바퀴만)")
    p.add_argument("--max-lines", type=int, default=2000)
    p.add_argument("--samples", type=int, default=2)
    p.add_argument("--max-bench", type=int, default=12)
    # 재현 재료를 몇 세션에서 모을지. 모으는 것은 파싱뿐이라 싸고, 비싼 LLM 채점은
    # 관계된 것만 고르므로 오히려 줄어든다.
    p.add_argument("--bench-sessions", type=int, default=3000,
                   help="재료를 찾아 훑어볼 세션 수(1=이 세션만)")
    p.add_argument("--bench-cases", type=int, default=120,
                   help="모을 재현 케이스 목표 수")
    p.add_argument("--bench-min-bytes", type=int, default=60_000,
                   help="이 크기 미만 세션은 재료로 안 본다")
    p.add_argument("--max-memory-measure", type=int, default=6)
    p.add_argument("--session", default="")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("forget", help="잘못 쌓인 이력 지우기(라우팅 오염 되돌리기)")
    p.add_argument("name")
    p.add_argument("--task", default="", nargs="+", action=_Sentence,
                   help="이 말이 들어간 이력만")
    p.add_argument("--all", action="store_true", help="그 능력의 이력을 전부")
    p.set_defaults(func=cmd_forget)

    p = sub.add_parser("doctor", help="왜 그것밖에 못 하는지 한 번에 짚기")
    add_llm_flags(p)
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("status", help="지금 이 컴퓨터에서 뭘 할 수 있는지")
    p.set_defaults(func=cmd_status)

    parser.set_defaults(func=cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    # **인자를 읽기 전에** 화면부터 세운다. `--help` 는 argparse 가 곧바로 찍고
    # 나가는데, 그 전에 안 세우면 도움말 한 줄에 프로그램이 죽는다(실측: 한국어
    # 윈도우 cp949 콘솔에서 `jermes --help` 가 첫 줄에서 UnicodeEncodeError).
    _speak_utf8()
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

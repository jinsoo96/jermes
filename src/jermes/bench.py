"""Deterministic replay bench - the weak-model equalizer.

The gate's verdict must not depend on model intelligence, so the bench judges
with code, not an LLM: each ReplayCase carries machine-checkable expectations
(substrings, regexes, forbidden markers). A weak drafter's skill survives only
if replaying real historic cases with the skill injected measurably helps.
Quality comes from selection pressure, not from the drafting model.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from typing import Callable

from .gate import BenchCase
from .model import SkillDef, Unmeasurable


@dataclass
class Expectation:
    require: list[str] = field(default_factory=list)   # substrings that must appear
    require_regex: list[str] = field(default_factory=list)
    forbid: list[str] = field(default_factory=list)    # markers that must not appear

    def score(self, output: str) -> float:
        checks: list[bool] = []
        low = output.lower()
        checks += [needle.lower() in low for needle in self.require]
        checks += [re.search(pattern, output, re.IGNORECASE) is not None
                   for pattern in self.require_regex]
        checks += [marker.lower() not in low for marker in self.forbid]
        if not checks:
            return 0.0
        return sum(checks) / len(checks)


@dataclass
class ReplayCase:
    """One historic interaction worth re-running (built from a ReproBundle)."""

    case_id: str
    payload: dict
    expect: Expectation

    def as_bench_case(self) -> BenchCase:
        # **요구조건도 실어 보낸다.** 게이트는 `BenchCase` 만 보는데, 그 케이스가
        # 무엇에 관한 것인지는 오류 문구만큼이나 **무엇을 요구하는지**에 담겨 있다.
        # 실측: `PYTHONIOENCODING` 을 요구하는 케이스가 인코딩 스킬과 "관계없다"고
        # 판정돼, 6건짜리 주제가 2건으로 줄어 최소치에 못 미쳤다.
        about = " ".join(self.expect.require + self.expect.forbid)
        # 요구와 금지를 갈라서도 싣는다. 금지만 있는 케이스는 스킬을 가릴 수 없어서
        # (오류 문구를 답변에 옮겨 적는 일은 없다) 게이트가 자리를 나중에 준다.
        asks = " ".join(self.expect.require + self.expect.require_regex)
        return BenchCase(case_id=self.case_id,
                         payload=dict(self.payload, about=about, asks=asks))


RunFn = Callable[[dict, SkillDef | None], str]
"""(case payload, candidate skill or None) -> the run's final output text.
Hosts back this with a real pipeline run (skill injected via loaded_skills);
tests back it with a scripted function."""


class ReproReplayRunner:
    """BenchRunner over replay cases. Deterministic given a deterministic RunFn:
    zero LLM-judge dependence, so verification quality is model-independent."""

    def __init__(self, run_fn: RunFn, cases: list[ReplayCase]) -> None:
        self.run_fn = run_fn
        self._by_id = {case.case_id: case for case in cases}

    def bench_cases(self) -> list[BenchCase]:
        return [case.as_bench_case() for case in self._by_id.values()]

    def score(self, case: BenchCase, skill: SkillDef | None) -> float:
        replay = self._by_id.get(case.case_id)
        if replay is None:
            return 0.0
        try:
            output = self.run_fn(replay.payload, skill)
        except Unmeasurable:
            raise            # 못 쟀다. 0점으로 바꿔 세면 판정이 거짓말이 된다
        except Exception:
            return 0.0
        return replay.expect.score(output)


# 실패의 **구체적인 표식**부터 찾는다. 순서가 뜻이다 - 앞엣것일수록 그 실패에만
# 나오는 말이라 금지 조건으로 쓸 값어치가 있다. 뒤로 갈수록 흔한 말이라, 그걸
# 금지하면 어떤 답이든 걸려 점수가 스킬과 무관해진다.
#
# 예전에는 상태코드와 흔한 낱말 다섯 개만 봤다. 실측: 실패 18건짜리 실세션에서
# 2건만 걸려 케이스가 최소치(4건)에 못 미쳤고, 그래서 게이트가 **한 번도** 판정하지
# 못했다. 검증기가 있는데 재료가 없어 놀고 있었다.
_ERROR_MARKERS = [
    re.compile(r"\b[45]\d\d\b"),                        # HTTP 상태코드
    re.compile(r"\b[A-Z]\w+(?:Error|Exception)\b"),     # KeyError, SyntaxError…
    re.compile(r"\bexit code \d+", re.IGNORECASE),
    re.compile(r"\b(?:no such file|command not found|permission denied|"
               r"module ?not ?found|connection refused|already exists)\b",
               re.IGNORECASE),
    re.compile(r"\b(?:traceback|fatal|timeout|refused|denied|not found|failed)\b",
               re.IGNORECASE),
]


def _error_marker(detail: str) -> str | None:
    """가장 구체적인 표식 하나. 없으면 None."""
    for pattern in _ERROR_MARKERS:
        found = pattern.search(detail)
        if found:
            return found.group(0)
    return None


def _input_of(event) -> str:
    meta = getattr(event, "meta", None) or {}
    return str(meta.get("input") or "") if isinstance(meta, dict) else ""


_SEPARATORS = frozenset("| || && ; & ( ) { }".split())
_REDIRECTS = frozenset("> >> < 2> 2>&1 1>".split())
_ASSIGNMENT = re.compile(r"\$?(?:env:)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)")
# 필드 경계. 도구 파라미터 이름은 소문자 snake_case 이고 환경변수는 대문자다 -
# 그 차이로 가른다. 안 가르면 `command=cd x && PYTHONIOENCODING=utf-8 python`
# 에서 `PYTHONIOENCODING=` 이 새 필드로 보여 명령이 `cd x &&` 에서 잘린다.
_FIELD = re.compile(r"(\w+)=(.*?)(?=\s+[a-z][a-z0-9_]*=|$)", re.S)
_COMMAND_FIELDS = ("command", "cmd", "script")
# 이보다 긴 값은 **문서**다(`content=` 로 실려 오는 파일 전문 앞머리). 파라미터로
# 고른 값은 짧다 - `read_first=yes`, `ref=main`, `--profile dev`.
_DOCUMENT_CHARS = 64


def _operative_tokens(text: str) -> list[str]:
    """명령에서 **무엇을 했는가**에 해당하는 낱말만 뽑는다.

    도구 입력 한 줄에는 `content=` 로 파일 전문 앞머리가 같이 실린다. 그걸 통째로
    쪼개면 그때 쓴 파일 내용이 요구조건이 된다. 실측(세션 40개)으로 올라온 상위
    낱말이 이랬다:

        grep 17 · **def 12** · **src 11** · echo 10 · **node 8** · **class 5**
        · **pos 5** · **import 4** · C:/Users/.../scratchpad 7

    `def` 와 `class` 는 그때 쓴 파이썬 파일이고 `src` 와 `node` 는 경로 조각이다.
    다음에 또 쓸 기법이 아니라서, 이걸 주제로 받은 드래프터는 잴 수 없는 스킬을
    쓴다(실측: 초안 6건 전부 dev +0.000).

    기법이 앉는 자리는 문법으로 정해져 있다 - **환경변수 이름, 명령 머리,
    이름 있는 플래그**. 값이 앉는 자리(인자·경로·따옴표 안)와 갈린다. 같은 40개
    세션에서 이 규칙이 낸 상위 낱말:

        head 20 · grep 18 · docker 13 · echo 9 · python 8 · sed 7 · until 6
        · sleep 6 · tail 5 · cat 5 · git 4 · PYTHONIOENCODING 4 · utf-8 4

    세는 것뿐이라 결정적이고 LLM 이 필요 없다.
    """
    out: list[str] = []
    heads: set[str] = set()
    for field, value in _FIELD.findall(text):
        if field not in _COMMAND_FIELDS:
            # 명령이 아닌 자리에서도 **고른 것**은 나온다: `Edit` 에 `read_first=yes`
            # 를 붙였다거나 URL 에 `?ref=main` 을 달았다거나. 그건 파라미터 선택이고,
            # 다음에도 쓴다. 옮긴 짐(`content=` 로 실린 파일 전문)과는 **길이**로
            # 갈린다 - 고른 값은 짧고 문서는 길다.
            out.append(field)
            value = value.strip().strip("\"'")
            # **띄어쓰기가 있으면 글이다.** 고른 값은 낱말 하나다 - `yes`, `main`,
            # `utf-8`. 실측: `description=Run tests across the case` 같은 설명문이
            # 쪼개져 `Run` `across` `case` `Verify` 가 요구조건으로 올라왔다.
            if value.split() == [value] and len(value) <= _DOCUMENT_CHARS:
                out.extend(t for t in re.split(r"[?&=,]+", value)
                           if _worth_requiring(t) and not _looks_like_path(t))
            continue
        try:
            words = shlex.split(value, posix=False)
        except ValueError:
            # 입력 한 줄은 200자에서 잘려 오므로 따옴표가 안 닫힐 수 있다.
            words = value.split()
        head_wanted = True
        for word in words:
            bare = word.strip("\"'").rstrip(";")
            if word in _SEPARATORS or word in _REDIRECTS:
                head_wanted = True
                continue
            assigned = _ASSIGNMENT.fullmatch(bare)
            if assigned:
                # bash `NAME=v` 도 PowerShell `$env:NAME='v'` 도 같은 일이다.
                name, value_of = assigned.group(1), assigned.group(2).strip("\"'")
                out.append(name)
                # 값도 뜻을 가질 때가 있다(`PYTHONIOENCODING=utf-8`). 다만 경로는
                # 그때 그 자리의 값이다 - 실측: `JERMES_HOME=C:/Users/.../scratchpad`
                # 가 7번 올라와 요구조건 행세를 했다.
                if _worth_requiring(value_of) and not _looks_like_path(value_of):
                    out.append(value_of)
                continue
            if bare.startswith("-"):
                if _worth_requiring(bare):
                    out.append(bare.split("=", 1)[0])   # 이름 있는 플래그만
                continue                                # `-n` 은 그 순간의 값이다
            if head_wanted:
                head_wanted = False
                head = bare.replace("\\", "/").rsplit("/", 1)[-1]
                # 명령 머리에는 세 글자 규칙을 걸지 않는다. 그 규칙은 줄 번호 같은
                # **값**을 걸러내려고 있는 것이고, `cd` `ls` `mv` 는 값이 아니라 한
                # 일이다. 실측: `git commit` 이 실패하고 `cd myrepo && git commit`
                # 으로 통과한 자리에서 요구조건이 통째로 비었다.
                if _worth_as_head(head) and not _looks_like_path(bare):
                    out.append(head)
                    heads.add(head)
    return list(dict.fromkeys(
        t for t in out if t in heads or _worth_requiring(t)))


def _worth_as_head(token: str) -> bool:
    """명령 머리로 값어치가 있는가. 값보다 무르게 본다 - `cd` 는 두 글자지만 한
    일이고, 세 글자 규칙은 줄 번호 같은 값을 걸러내려던 것이다."""
    bare = token.strip("-_.:/")
    return len(bare) >= 2 and bool(re.search(r"[A-Za-z가-힣]", bare))


def _looks_like_path(token: str) -> bool:
    return "/" in token or "\\" in token or bool(re.match(r"^[A-Za-z]:", token))


def _worth_requiring(token: str) -> bool:
    """요구조건이 될 값어치가 있는 낱말인가.

    입력 차이에는 우연한 것이 섞인다. 줄 번호(`470`), 개수 플래그(`-25`) 같은 것은
    그 순간에만 맞는 값이라, 요구하면 스킬이 아니라 그때의 숫자를 외웠는지를 재게
    된다. 뜻을 가진 낱말만 남긴다.
    """
    bare = token.strip("-_.:/")
    if len(bare) < 3:
        return False
    return not bare.replace(".", "").isdigit()


def _fix_tokens(trace, index: int, failed) -> list[str]:
    """실패한 호출과 **같은 도구가 성공한 재시도**의 입력 차이에서 낱말을 뽑는다.

    실패: Bash command=git commit -m x        -> not a git repository
    성공: Bash command=cd repo && git commit  -> 바뀐 것: cd, repo

    바뀐 쪽의 낱말을 요구하면 스킬이 그 우회로를 알려 줬는지를 재게 된다.
    뽑을 것이 없으면 **빈 목록**을 준다. 지어낸 요구조건은 없느니만 못하다.
    """
    before = set(_operative_tokens(_input_of(failed)))
    for later in trace.events[index + 1:]:
        if later.type != "tool_call" or later.name != failed.name:
            continue
        if not later.ok:
            continue          # 아직 헤매는 중이다
        # 순서를 지키면서 새로 나타난 것만. 흔한 낱말은 앞뒤 양쪽에 있으므로
        # 차집합만으로 충분하다.
        fresh = [t for t in _operative_tokens(_input_of(later)) if t not in before]
        return fresh[:2]
    return []


def generalize_requirements(cases, min_seen: int = 2) -> list:
    """요구조건에서 **한 번뿐인 값**을 걷어내고 되풀이되는 기법만 남긴다.

    실측(세션 200개, 케이스 126건): 요구 낱말 148종 중 106종(72%)이 한 번만
    나왔다. `harness_bridge/jermes.py`, `/d/xgen-maker` 같은 그때 그 자리의
    경로다. 일반 스킬로는 절대 못 내고, 외워야만 통과하는데 홀드아웃은 정확히
    외운 것을 거절하려고 있다. **이길 수 없는 벤치였다.**

    되풀이되는 것은 다르다: grep 9번, utf-8 5번, PYTHONIOENCODING 4번, sed 4번.
    `PYTHONIOENCODING` 이 네 번 나온다는 것은 이 사람이 인코딩 문제를 반복해서
    겪고 매번 그걸로 고쳤다는 뜻이다 - 그게 배울 거리다. 경로는 한 번 쓰고 버리는
    값이고, 기법은 다음에도 쓴다.

    세는 자리는 **모아 놓은 풀 전체**다. 한 세션 안에서는 무엇이 되풀이되는지 알
    수 없다 - 되풀이는 여러 번의 일이다.

    남는 것이 없는 케이스는 `forbid` 만 갖는다(그 실패 표식이 다시 나오면 안
    된다). 요구를 지어내지 않는다 - 지어낸 요구조건은 없느니만 못하다.
    """
    from collections import Counter

    seen: Counter = Counter()
    for case in cases:
        for token in set(case.expect.require):
            seen[token] += 1

    for case in cases:
        keep = [t for t in case.expect.require if seen[t] >= min_seen]
        case.expect.require = keep
    return cases


def fix_examples(cases, token: str, limit: int = 4) -> list[str]:
    """그 기법으로 고친 **실제 실패들**. 겨냥 드래프터에게 줄 재료다.

    "이 기법을 3번 썼다"만으로는 스킬을 못 쓴다. 무엇이 어떻게 깨졌을 때 그걸
    썼는지를 봐야 "언제 쓰는가"를 적을 수 있다.
    """
    out: list[str] = []
    for case in cases:
        if token not in case.expect.require:
            continue
        payload = getattr(case, "payload", None) or {}
        lines = str(payload.get("error_detail") or "").strip().splitlines()
        head = lines[0][:120] if lines else ""
        tool = str(payload.get("tool") or "")
        if head or tool:
            out.append(f"{tool}: {head}" if head else tool)
        if len(out) >= limit:
            break
    return out


def recurring_fixes(cases, top: int = 8) -> list[tuple[str, int]]:
    """되풀이되는 **고치는 법**을 센다. (기법, 횟수) 목록.

    `recurring_failures` 는 무엇이 자주 깨지는지를 세고, 이쪽은 **무엇으로 고쳤는지**
    를 센다. 드래프터에게는 후자가 더 값지다 - 벤치가 채점하는 것이 정확히 그거라서,
    이걸 모르면 좋은 조언을 쓰고도 잴 수 없는 스킬이 된다.

    실측: `PYTHONIOENCODING` 4번 · `grep` 9번 · `utf-8` 5번 · `sed` 4번.
    인코딩 실패를 매번 PYTHONIOENCODING 으로 넘겼다는 뜻이고, 그게 이 사람에 대해
    배울 수 있는 사실이다.
    """
    from collections import Counter

    counts: Counter = Counter()
    for case in cases:
        for token in set(case.expect.require):
            counts[token] += 1
    return [(t, n) for t, n in counts.most_common(top) if n > 1]


def recurring_failures(cases, top: int = 6) -> list[tuple[str, int]]:
    """모아 둔 재현 케이스에서 **되풀이되는 실패**를 센다. (설명, 횟수) 목록.

    왜 필요한가: 드래프터는 한 세션만 보고 "흥미로운 것"을 고르는데, 흥미로운
    것과 되풀이되는 것은 다르다. 한 번만 일어난 일에 대한 스킬은 잴 재료가
    없어 영영 대기다. 실측: 초안이 고른 주제의 관계된 케이스가 2건이었고, 같은
    풀에 83건짜리 주제가 따로 있었다.

    세는 것은 우리가 하고 고르는 것은 모델이 한다. 없는 것을 있다고 하게
    만들지 않는다.

    묶는 기준은 **도구 + 오류의 첫 줄**이다. 오류 전문으로 묶으면 경로·줄번호가
    달라 전부 따로 세어지고, 도구만으로 묶으면 서로 다른 실패가 뭉뚱그려진다.
    """
    from collections import Counter

    counts: Counter = Counter()
    for case in cases:
        payload = getattr(case, "payload", None) or {}
        tool = str(payload.get("tool") or "").strip()
        detail = str(payload.get("error_detail") or "").strip()
        head = detail.splitlines()[0][:90] if detail else ""
        if tool or head:
            counts[f"{tool}: {head}" if head else tool] += 1
    return counts.most_common(top)


def capture_repro_rows(trace) -> list[dict]:
    """Auto-capture replay rows from a finished trace's error->recovery pairs.

    Conservative heuristic: forbid the error's distinctive marker (status code
    or failure word), require the recovery detail's distinctive tokens. Rows
    are tagged auto_captured so hosts can review before trusting them as gate
    evidence. Returns [] when the trace has no usable error/recovery signal."""
    rows: list[dict] = []
    recovery_details = [e.detail for e in trace.events
                        if e.type == "recovery" and e.detail]
    # 복구 문구가 전부 같으면 그건 내용이 아니라 **상투구**다(원천이 "X succeeded
    # after failing" 같은 문장을 지어 넣는 경우). 그걸 요구조건으로 쓰면 모든 케이스가
    # 같은 것을 요구하게 되고, 스킬을 넣든 빼든 점수가 안 움직인다.
    # 실측: 케이스 8건이 전부 `require=['Edit','succeeded']` 였고 이득이 정확히
    # +0.000 이었다 - 게이트가 도는 것처럼 보이지만 아무것도 재고 있지 않았다.
    boilerplate = len(set(recovery_details)) <= 1 and len(recovery_details) > 1
    for index, event in enumerate(trace.events):
        # 실패는 원천마다 다르게 온다. 스파인은 `error` 이벤트를 내지만 Claude Code
        # 기록은 `tool_call(ok=False)` 로 낸다. 앞의 것만 보면 재료가 있는데도 0건이
        # 나오고, 그러면 게이트가 케이스 부족으로 **영원히 staged** 를 낸다.
        # (실측: 실패한 도구 호출 57건이 있는 세션에서 케이스 0개가 나왔다.)
        failed = event.type == "error" or (event.type == "tool_call" and not event.ok)
        if not failed or not event.detail:
            continue
        marker = _error_marker(event.detail)
        if not marker:
            continue
        # 요구조건은 **무엇을 바꿔서 통과했는가**에서 온다.
        #
        # 예전에는 복구 이벤트의 detail 에서 낱말을 뽑았는데, 그 detail 은
        # 원천이 만든 템플릿("<도구> succeeded after failing")이라 내용이 0이다.
        # 실측: 세션 하나에서 케이스 9건 중 7건이 `['Bash','succeeded']` 였고,
        # 그러면 게이트가 스킬의 유용성이 아니라 "답변에 도구 이름을 적었는가"
        # 라는 **서식**을 잰다. 서식만 지시하는 쓸모없는 스킬이 검증됨을 받는다.
        require = _fix_tokens(trace, index, event)
        # require 가 비면 금지 조건만 남는다. 그것도 충분히 뜻이 있다 -
        # "그때 실패하게 만든 그것을 다시 하지 않는가"를 묻는 것이다.
        rows.append({
            "case_id": f"{trace.run_id}-repro-{index}",
            "payload": {"error_detail": event.detail, "tool": event.name,
                        "run_id": trace.run_id},
            "forbid": [marker],
            "require": require,
            "auto_captured": True,
        })
    return rows


def cases_from_repro_rows(rows: list[dict]) -> list[ReplayCase]:
    """Build replay cases from persisted repro rows. Expected row shape:
    {case_id, payload, require?, require_regex?, forbid?} - spine `repro`
    entries and ReproBundle exports both map onto this."""
    cases = []
    for row in rows:
        cases.append(
            ReplayCase(
                case_id=str(row["case_id"]),
                payload=dict(row.get("payload", {})),
                expect=Expectation(
                    require=[str(s) for s in row.get("require", [])],
                    require_regex=[str(s) for s in row.get("require_regex", [])],
                    forbid=[str(s) for s in row.get("forbid", [])],
                ),
            )
        )
    return cases

"""Jermes drafter - LLM-backed candidate drafting from a run trace.

The LLM only *proposes*; every proposal still passes the deterministic
curator filters and the forge gate. Provider-agnostic: `complete` is any
(prompt -> text) callable. A stdlib OpenAI-compatible completer is included
so the package stays dependency-free.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
import logging
import re
import time
import urllib.error
import urllib.request
from typing import Callable

logger = logging.getLogger("jermes.drafter")

from .model import Provenance, RunTrace, SkillCandidate
from .signals import SignalHit

Completer = Callable[[str], str]

_PROMPT = """You are Jermes, a skill smith. You review a \
finished agent run and propose at most {max_candidates} reusable skills.

Rules (violations are discarded by a deterministic filter, so obey them):
- Only durable, portable procedures. NO environment-specific paths, NO \
transient failures (timeouts, rate limits), NO broad bans ("never use X"), \
NO secrets or credentials.
- Prefer nothing over something marginal. Return [] when the run taught \
nothing reusable.
- Procedures need >= 2 concrete steps and a verification recipe.

Run trace:
{trace}

Failures that keep coming back (count across many past runs). A skill about
one of these can actually be measured, because there are enough real cases to
replay it against. A skill about something that happened once cannot be - it
will sit unverified forever. Prefer these unless the run taught something
clearly more important:
{recurring}

Detected signals:
{signals}

Lessons recorded by the agent:
{lessons}

Respond with ONLY a JSON array (no prose). Each item:
{{"name": "kebab-case-name", "when_to_use": "...", "rationale": "...",
  "procedure": ["step", ...], "pitfalls": ["...", ...],
  "verification": ["...", ...]}}

Example of a good response (format reference only - do not copy content):
[{{"name": "paginate-with-cursor", "when_to_use": "when listing more than one \
page from the orders API", "rationale": "offset pagination silently drops rows \
under concurrent writes", "procedure": ["Request the first page with limit and \
no cursor", "Loop passing next_cursor until it returns empty"], "pitfalls": \
["Reusing an expired cursor returns 410"], "verification": ["Total fetched \
count equals the summary endpoint count"]}}]"""

_REPAIR_PROMPT = """The following text was supposed to be a JSON array of skill \
objects but is malformed. Output ONLY the corrected JSON array, nothing else. \
If no skill objects can be recovered, output [].

{raw}"""

_RETRY_SUFFIX = """

Your previous attempt was invalid: {feedback}
Respond again with ONLY a valid JSON array."""


# 트레이스 한 줄에 실어 보내는 오류 문구 길이. 모델이 무엇이 실패했는지 알기에는
# 충분하고, 스택트레이스 전문이 실려 프롬프트를 먹지는 않는 선이다.
_DETAIL_CAP = 160
# 흥미로운 사건 앞뒤로 함께 보낼 사건 수. 실패 하나만 보내면 무엇을 하다가 그랬는지
# 모르고, 넓게 보내면 사건 하나가 프롬프트를 다 먹는다.
_WINDOW = 2


def _is_interesting(event) -> bool:
    """배울 거리가 있는 자리 - 실패, 복구, 사용자 교정."""
    return (event.type in ("error", "recovery", "user_correction")
            or (event.type == "tool_call" and not event.ok))


def _render_trace(trace: RunTrace, cap: int = 40) -> str:
    """모델에게 **배울 곳**을 보여준다.

    예전에는 `trace.events[:cap]` 으로 **앞에서 40개만** 잘랐다. 플랫폼 런은 짧아서
    그래도 됐지만 Claude Code 세션은 1000건이 넘고 오류·복구·교정이 뒤쪽에 흩어져
    있다 - 정작 배울 자리가 통째로 잘려 모델이 매번 `[]` 를 반환했다(실측: 신호
    10건인데 초안 0건). 그래서 **신호 주변을 창으로 떠서** 보여준다.

    detail 도 자른다. 도구 결과에 파일 전문이 들어와 있어 그대로 넣으면 프롬프트가
    파일 내용으로 뒤덮인다."""
    events = trace.events
    marks = [i for i, e in enumerate(events) if _is_interesting(e)]
    if marks:
        # 창을 만들어 놓고 **앞에서부터** cap 만큼 자르면, 신호가 뒤에 흩어진
        # 세션에서는 앞의 한둘만 대표되고 나머지는 통째로 안 보인다.
        # 실측: 신호 6건짜리 실세션에서 앞의 두 창만 들어가 모델이 계속 [] 를
        # 냈다. 그래서 신호마다 자리를 나눠 주고, 넘치면 **고르게 솎는다**.
        per = max(1, cap // len(marks))
        half = min(_WINDOW, (per - 1) // 2)
        keep: set[int] = set()
        for i in marks:
            keep.update(range(max(0, i - half), min(len(events), i + half + 1)))
        chosen = sorted(keep)
        if len(chosen) > cap:
            step = len(chosen) / cap
            chosen = [chosen[int(k * step)] for k in range(cap)]
    else:
        chosen = list(range(min(cap, len(events))))

    lines = []
    if len(events) > len(chosen):
        lines.append(f"- (전체 {len(events)}건 중 배울 자리 주변 {len(chosen)}건만 표시)")
    previous = -1
    for i in chosen:
        if previous >= 0 and i > previous + 1:
            lines.append(f"- … {i - previous - 1}건 생략 …")
        event = events[i]
        status = "" if event.ok else " FAILED"
        detail = (event.detail or "").strip().replace(chr(10), " ")
        if len(detail) > _DETAIL_CAP:
            detail = detail[:_DETAIL_CAP] + "…"
        suffix = f" - {detail}" if detail else ""
        lines.append(f"- {event.type}: {event.name}{status}{suffix}")
        previous = i
    lines.append(f"- outcome: {'success' if trace.success else 'failure'}")
    return chr(10).join(lines)


def build_prompt(trace: RunTrace, hits: list[SignalHit],
                 max_candidates: int = 2, recurring=None,
                 fixes=None) -> str:
    signals = "\n".join(f"- {h.signal} ({h.strength:.2f}): {h.evidence}"
                        for h in hits) or "- none"
    lessons = "\n".join(f"- {l}" for l in trace.lessons) or "- none"
    # 되풀이되는 실패를 **횟수와 함께** 보여 준다. 안 주면 모델은 이 세션에서
    # 흥미로운 것을 고르고, 그건 대개 한 번만 일어난 일이라 잴 재료가 없다.
    repeats = "\n".join(f"- {label} ({n}번)" for label, n in (recurring or []))
    # 벤치가 채점하는 것이 정확히 이 기법들이다. 안 알려 주면 모델은 좋은 일반
    # 조언을 쓰고, 그건 옳지만 **잴 수가 없다**(실측: 그렇게 쓴 스킬이 관계된
    # 케이스 2건에 그쳐 최소치에 못 미쳤다).
    tips = "\n".join(f"- {label} ({n}번)" for label, n in (fixes or []))
    return _PROMPT.format(max_candidates=max_candidates,
                          trace=_render_trace(trace),
                          recurring=repeats or "- (아직 되풀이된 것이 없습니다)",
                          fixes=tips or "- (아직 되풀이된 해법이 없습니다)",
                          signals=signals, lessons=lessons)


def _example_from_prompt() -> tuple[str, tuple[str, ...]]:
    """유출 감지에 쓸 이름과 표지를 **프롬프트의 예시에서 뽑아낸다.**

    예전에는 손으로 베껴 적어 뒀다(`("orders api", "next_cursor", ...)`). 그러면
    프롬프트의 예시를 고치는 순간 표지는 옛 낱말을 가리키고, 유출 감지가 조용히
    아무것도 못 잡는다 - 터지지도 않고 시험도 안 깨진다. 예시가 정본이면 표지도
    거기서 나와야 한다.

    표지는 그 예시에만 있을 법한 낱말이다: 밑줄이 든 식별자(`next_cursor`)와
    흔치 않은 여러 낱말 짝. 일반적인 영어 낱말은 아무 답에나 나오므로 쓰지 않는다.
    """
    import re as _re

    block = _PROMPT[_PROMPT.index("Example of a good response"):]
    try:
        payload = json.loads(block[block.index("["):block.rindex("]") + 1]
                             .replace("{{", "{").replace("}}", "}"))
    except (ValueError, IndexError):
        return "", ()
    first = payload[0] if payload and isinstance(payload[0], dict) else {}
    name = str(first.get("name", "")).strip().lower()
    text = " ".join(str(v) for value in first.values()
                    for v in (value if isinstance(value, list) else [value])).lower()
    markers = set()
    markers.update(_re.findall(r"[a-z]+_[a-z_]+", text))        # next_cursor
    for phrase in ("offset pagination", "orders api"):          # 예시에 있을 때만
        if phrase in text:
            markers.add(phrase)
    # 예시가 바뀌어 위 두 마디가 없어져도, 이름과 밑줄 식별자로 계속 잡는다.
    return name, tuple(sorted(markers))


_EXAMPLE_NAME, _EXAMPLE_MARKERS = _example_from_prompt()


def _is_example_leak(item: dict) -> bool:
    """Weak models sometimes copy the few-shot example despite instructions.
    Drop any candidate that reproduces the example's name or content."""
    name = str(item.get("name", "")).strip().lower()
    if name == _EXAMPLE_NAME:
        return True
    text = " ".join(str(item.get(k, "")) for k in
                    ("when_to_use", "rationale")).lower()
    return sum(marker in text for marker in _EXAMPLE_MARKERS) >= 2


def _sanitize_name(raw: str) -> str:
    # agentskills.io 이름 규칙을 그대로 따른다(64자·소문자 영숫자·하이픈, 앞뒤와
    # 연속 하이픈 금지). 규칙을 어기면 다른 에이전트가 스킬을 조용히 무시한다.
    from .portable import spec_name
    return spec_name(raw)


def _extract_json_array(text: str) -> list:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    start = text.find("[")
    if start == -1:
        return []
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "[":
            depth += 1
        elif text[i] == "]":
            depth -= 1
            if depth == 0:
                try:
                    parsed = json.loads(text[start:i + 1])
                    return parsed if isinstance(parsed, list) else []
                except json.JSONDecodeError:
                    return []
    return []


class LLMDrafter:
    """Weak-model-hardened drafter. Three recovery layers before giving up:
    (1) tolerant extraction (fences, surrounding prose), (2) an LLM repair pass
    on malformed output, (3) a bounded retry with explicit failure feedback.
    A weak model that CAN emit JSON some of the time therefore still drafts;
    a weak model that drafts nonsense is stopped later by curator + gate."""

    def __init__(self, complete: Completer, max_candidates: int = 2,
                 drafter_id: str = "jermes", repair: bool = True,
                 max_retries: int = 1, recurring=None,
                 fixes=None) -> None:
        self.complete = complete
        # 되풀이되는 실패 (설명, 횟수). 안 주면 모델은 이 세션에서 흥미로운
        # 것을 고르고, 그건 대개 한 번만 일어난 일이라 잴 재료가 없다.
        self.recurring = list(recurring or [])
        # 되풀이되는 **해법**. 벤치가 채점하는 것이 이것이라, 모르면
        # 좋은 조언을 쓰고도 잴 수 없는 스킬이 된다.
        self.fixes = list(fixes or [])
        self.max_candidates = max_candidates
        self.drafter_id = drafter_id
        self.repair = repair
        self.max_retries = max_retries

    def _complete_array(self, prompt: str) -> list:
        raw = self.complete(prompt)
        items = _extract_json_array(raw)
        if not items and self.repair and raw.strip() and raw.strip() != "[]":
            logger.warning("[drafter] unparsable output, repairing. head=%r",
                           raw[:200])
            repaired = self.complete(_REPAIR_PROMPT.format(raw=raw[:4000]))
            items = _extract_json_array(repaired)
            if not items:
                logger.warning("[drafter] repair failed. head=%r", repaired[:200])
        return items

    # 마지막 초안이 왜 0건이었는지. 부르는 쪽이 화면에 옮긴다.
    last_reason = ""

    def draft(self, trace: RunTrace, hits: list[SignalHit],
              variant: str = "") -> list[SkillCandidate]:
        prompt = build_prompt(trace, hits, self.max_candidates,
                              recurring=self.recurring, fixes=self.fixes)
        if variant:
            prompt += f"\n\nAttempt focus: {variant}"
        items: list = []
        feedback = ""
        for attempt in range(self.max_retries + 1):
            attempt_prompt = prompt if not feedback else (
                prompt + _RETRY_SUFFIX.format(feedback=feedback))
            try:
                items = self._complete_array(attempt_prompt)
            except Exception as exc:
                detail = ""
                read = getattr(exc, "read", None)
                if callable(read):
                    try:
                        detail = read().decode("utf-8", "replace")[:300]
                    except Exception:
                        detail = ""
                # 호출 실패와 "모델이 쓸 게 없다고 답함"은 다른 사건이다.
                # 위층이 이걸 구분 못 하면 사용자에게 "못 찾았다"고 말하는데
                # 사실은 타임아웃이었을 수 있다.
                LLMDrafter.last_reason = (
                    f"호출 실패 {type(exc).__name__}: {str(exc)[:100]}")
                logger.warning("[drafter] completion failed: %s: %s %s",
                               type(exc).__name__, str(exc)[:200], detail)
                return []
            if items:
                break
            feedback = "no parsable JSON array of skill objects was found"
        candidates: list[SkillCandidate] = []
        strongest = max((h.signal for h in sorted(hits, key=lambda h: -h.strength)),
                        default="")
        dropped_leak = dropped_invalid = 0
        for item in items[: self.max_candidates]:
            if not isinstance(item, dict) or _is_example_leak(item):
                dropped_leak += 1
                continue
            try:
                candidates.append(
                    _candidate_from(item, trace, self.drafter_id, strongest))
            except ValueError:
                dropped_invalid += 1
                continue
        if hits:
            # "0건"이 세 가지 다른 사건일 수 있다 - 모델이 빈 배열을 줬거나,
            # 예시 유출 가드가 전부 걷어냈거나, 필드가 깨져 버려졌거나.
            # 숫자로 나뉘지 않으면 약한 모델을 튜닝할 근거가 없다.
            logger.info("[drafter] hits=%d · model=%d · leak=%d · invalid=%d · kept=%d",
                        len(hits), len(items), dropped_leak, dropped_invalid,
                        len(candidates))
            if not candidates:
                LLMDrafter.last_reason = (
                    "모델이 빈 배열" if not items else
                    f"모델 {len(items)}건 중 예시유출 {dropped_leak} · "
                    f"형식오류 {dropped_invalid} 로 전부 버림")
        return candidates


_TARGETED_PROMPT = """This person keeps recovering from one kind of failure the
same way. Write ONE reusable skill that captures it.

The technique they reach for: {token}   (used {count} times)

The failures it fixed:
{examples}

Write it so that someone hitting one of these would know to reach for `{token}`.
Name the situation, not the tool. Two or more concrete steps, and a way to check
it worked. At least one step must spell `{token}` out literally - a step that
says "change the directory" instead of `cd` leaves the reader guessing which
command you meant. Do not invent anything that is not above. If these failures
share nothing beyond the technique, return [].

Respond with ONLY a JSON array holding one item:
[{{"name": "kebab-case-name", "when_to_use": "...", "rationale": "...",
  "procedure": ["step", ...], "pitfalls": ["...", ...],
  "verification": ["...", ...]}}]"""


class TargetedDrafter:
    """되풀이된 **해법**마다 스킬을 하나씩 쓰게 한다.

    프롬프트에 되풀이 목록을 적어 줘도 모델은 트레이스에 끌려 한 번 일어난 일을
    고른다. 실측으로 세 번 연속 그랬고 전부 dev +0.000 으로 거절됐다.
    힌트는 힌트일 뿐이다.

    그래서 주제를 **측정이 정한다.** 모델은 우리가 센 사실과 뽑은 예시를 절차로
    옮기는 일만 한다. 벤치에 맞춘 부정행위가 아니다 - 벤치가 재는 것은 "그때
    통했던 기법에 손을 뻗는가" 이고, 그 기법을 네 번 쓴 사람에게 값진 스킬이
    정확히 "그 상황은 이렇게 넘긴다" 이기 때문이다.
    """

    def __init__(self, complete: Completer, drafter_id: str = "jermes-targeted",
                 max_topics: int = 3) -> None:
        self.complete = complete
        self.drafter_id = drafter_id
        self.max_topics = max_topics

    def draft(self, trace: RunTrace, fixes, examples_for) -> list[SkillCandidate]:
        out: list[SkillCandidate] = []
        for token, count in list(fixes)[:self.max_topics]:
            examples = examples_for(token)
            if len(examples) < 2:
                continue      # 두 번은 봐야 되풀이다. 하나로는 쓸 말이 없다
            prompt = _TARGETED_PROMPT.format(
                token=token, count=count,
                examples=chr(10).join(f"- {e}" for e in examples))
            try:
                items = _extract_json_array(self.complete(prompt))
            except Exception as exc:
                logger.warning("[targeted] %s: %s: %s", token,
                               type(exc).__name__, str(exc)[:120])
                continue
            for item in items[:1]:
                if not isinstance(item, dict) or _is_example_leak(item):
                    continue
                # **기법 이름이 절차에 없으면 버린다.** 부탁만 해서는 안 적는다 -
                # 실측: 인코딩 초안이 "환경 변수를 설정한다"까지만 쓰고
                # `PYTHONUTF8` 을 안 적어 재생 답변에도 안 실렸고, dev +0.000 으로
                # 거절됐다. 어느 명령인지 안 적은 절차는 읽는 사람도 못 따라간다.
                spelled = " ".join(str(s) for s in item.get("procedure", []))
                if token.lower() not in spelled.lower():
                    logger.warning("[targeted] 버림 %s: 절차에 그 낱말이 없음", token)
                    continue
                try:
                    out.append(_candidate_from(
                        item, trace, self.drafter_id,
                        "recurring-fix:" + str(token)))
                except ValueError as exc:
                    logger.warning("[targeted] 버림 %s: %s", token, str(exc)[:120])
        return out


# 모델이 준 한 덩어리를 후보 하나로. **이 자리가 하나뿐이라야 한다.**
# 예전에는 같은 열두 줄이 두 곳에 복제돼 있었다(일반 초안 · 겨냥 초안). 자르는
# 한도도 양쪽에 똑같이 박혀 있어서, 한도를 고치면 한쪽만 고쳐지고 다른 쪽은
# 조용히 예전 값으로 남는다. 그런 어긋남은 터지지 않고 그냥 다르게 동작한다.
_LIMITS = {"rationale": 500, "line": 300, "steps": 12, "notes": 6}


def _candidate_from(item: dict, trace, drafter_id: str, signal: str) -> SkillCandidate:
    line, notes = _LIMITS["line"], _LIMITS["notes"]
    return SkillCandidate(
        name=_sanitize_name(str(item.get("name", ""))),
        kind="guide",
        scope=trace.scope,
        action="create",
        rationale=str(item.get("rationale", ""))[:_LIMITS["rationale"]],
        when_to_use=str(item.get("when_to_use", ""))[:line],
        procedure=[str(s)[:line] for s in item.get("procedure", [])][:_LIMITS["steps"]],
        pitfalls=[str(s)[:line] for s in item.get("pitfalls", [])][:notes],
        verification=[str(s)[:line] for s in item.get("verification", [])][:notes],
        provenance=Provenance(
            origin="llm_drafter",
            source_run_ids=[trace.run_id],
            curator_id=drafter_id,
            signal=signal,
        ),
    )


class EnsembleDrafter:
    """Self-consistency for weak models: sample the drafter k times, pool the
    candidates, and collapse near-duplicates. Selection among survivors is NOT
    done here - the curator (patch-over-create) and the gate (replay bench) do
    it deterministically, which is exactly what makes a weak drafter viable:
    breadth from sampling, quality from selection pressure."""

    VARIANTS = (
        "",
        "propose a different angle than the most obvious one",
        "focus on prevention and verification steps rather than the happy path",
        "focus on what the user corrected or what almost went wrong",
        "focus on ordering constraints between the tools used",
    )

    def __init__(self, drafter: LLMDrafter, samples: int = 3,
                 max_candidates: int = 4) -> None:
        self.drafter = drafter
        self.samples = samples
        self.max_candidates = max_candidates

    def draft(self, trace: RunTrace, hits: list[SignalHit]) -> list[SkillCandidate]:
        pool: list[SkillCandidate] = []
        for index in range(self.samples):
            variant = self.VARIANTS[index % len(self.VARIANTS)]
            pool.extend(self.drafter.draft(trace, hits, variant=variant))
        deduped: list[SkillCandidate] = []
        seen_names: set[str] = set()
        for candidate in pool:
            if candidate.name in seen_names:
                continue
            if any(_near_duplicate(candidate, kept) for kept in deduped):
                continue
            seen_names.add(candidate.name)
            deduped.append(candidate)
        return deduped[: self.max_candidates]


def _near_duplicate(a: "SkillCandidate", b: "SkillCandidate") -> bool:
    ta = set(re.findall(r"[a-z0-9가-힣]{2,}", (a.when_to_use + " " + a.rationale).lower()))
    tb = set(re.findall(r"[a-z0-9가-힣]{2,}", (b.when_to_use + " " + b.rationale).lower()))
    if not ta or not tb:
        return False
    return len(ta & tb) / min(len(ta), len(tb)) >= 0.7


def failover_completer(completers: list[Completer]) -> Completer:
    """Try each completer in order; remember the one that works.

    Observed failure this exists for: the box serving the primary model went
    down and curation stopped silently - the loop kept "running" while every
    draft returned nothing. With a second endpoint listed, learning continues.

    A completer that raises is treated as unhealthy and the next one is tried.
    The last known-good index is tried first next time, so the healthy path
    costs no extra calls.
    """
    live = [c for c in completers if c is not None]
    if not live:
        raise ValueError("failover_completer needs at least one completer")
    state = {"index": 0}

    def complete(prompt: str) -> str:
        order = list(range(state["index"], len(live))) + \
            list(range(0, state["index"]))
        last: Exception | None = None
        for i in order:
            try:
                result = live[i](prompt)
            except Exception as exc:
                last = exc
                logger.warning("[drafter] endpoint %d unhealthy: %s: %s",
                               i, type(exc).__name__, str(exc)[:120])
                continue
            if i != state["index"]:
                logger.warning("[drafter] failed over to endpoint %d", i)
                state["index"] = i
            return result
        raise last if last else RuntimeError("no endpoint answered")

    return complete


def anthropic_completer(model: str, api_key: str,
                        max_tokens: int = 2048,
                        timeout: float = 120.0) -> Completer:
    """Stdlib completer for the Anthropic Messages API. Raw HTTP is deliberate:
    this package ships with dependencies=[] (engine ethos), so the official SDK
    is not available here - hosts with the SDK installed should pass their own
    Completer instead."""
    url = "https://api.anthropic.com/v1/messages"

    def complete(prompt: str) -> str:
        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
        if body.get("stop_reason") == "refusal":
            return "[]"
        return "".join(block.get("text", "") for block in body.get("content", [])
                       if block.get("type") == "text")

    return complete


# 다시 물어 볼 만한 실패. 서버가 "지금은 안 되지만 곧 될 수도"라고 말한 것들이다.
# 400(잘못된 요청)·401(인증)·403·404 는 뺐다 - 다시 물어도 같은 답이고, 재시도가
# "뭔가 하고 있다"는 거짓 인상만 준다.
_RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


def _worth_retrying(exc: Exception) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in _RETRYABLE_STATUS
    # URLError·타임아웃·끊긴 연결. 서버가 답을 준 적이 없으니 일시적일 수 있다.
    return isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError))


def _with_retry(call, body_extra: dict, *, retries: int, backoff: float,
                sleep, remaining=None) -> tuple[str, str]:
    """일시적인 실패면 잠깐 쉬었다 다시 묻는다.

    예전에는 재시도가 없어서, `watch` 가 스무 세션을 도는 중에 502 가 한 번 나면
    거기서 끝났다. 사람이 자리를 비운 사이 도는 물건이라 아침에 아무것도 없다.

    **일시적인 것만** 다시 묻는다(`_worth_retrying`). 그리고 몇 번째에 무엇 때문에
    다시 묻는지 남긴다 - 조용히 재시도하면 느린 이유를 아무도 모른다.
    """
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return call(body_extra)
        except Exception as exc:            # noqa: BLE001 - 아래에서 갈라 본다
            if not _worth_retrying(exc) or attempt == retries:
                raise
            last = exc
            wait = backoff * (2 ** attempt)
            # 남은 시간이 없으면 다시 묻지 않는다. 쉬었다 또 묻는 사이에 상한이
            # 지나가면, 예산은 있는데 아무 일도 못 한 채로 끝난다.
            if remaining is not None and remaining() <= wait:
                raise
            logger.warning("[llm] %s - %.1f초 뒤 다시 (%d/%d)",
                           type(exc).__name__, wait, attempt + 1, retries)
            sleep(wait)
    raise last if last else RuntimeError("재시도 경로가 잘못됐습니다")


def openai_chat_completer(base_url: str, model: str,
                          temperature: float = 0.2,
                          timeout: float = 120.0,
                          api_key: str = "",
                          extra: dict | None = None,
                          retries: int = 3,
                          backoff: float = 1.0,
                          sleep=time.sleep,
                          remaining=None) -> Completer:
    """Stdlib completer for any OpenAI-compatible /chat/completions endpoint.

    `extra` merges provider-specific body params (e.g. DashScope Qwen3 needs
    {"enable_thinking": False} on non-streaming calls)."""
    url = base_url.rstrip("/") + "/chat/completions"

    # 마지막 호출이 길이 상한에 걸렸는가. 부르는 쪽이 0 건의 이유를 정확히
    # 말하려면 이게 필요하다.

    def complete(prompt: str) -> str:
        payload = {
            "model": model,
            "temperature": temperature,
            "messages": [{"role": "user", "content": prompt}],
            **(extra or {}),
        }
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                **({"Authorization": f"Bearer {api_key}"} if api_key else {}),
            },
            method="POST",
        )
        def call(body_extra: dict) -> tuple[str, str]:
            sent = dict(payload, **body_extra)
            request.data = json.dumps(sent, ensure_ascii=False).encode("utf-8")
            # **요청 하나가 예산보다 오래 살면 안 된다.** 남은 시간이 30초인데
            # 120초짜리 요청을 걸면 상한이 90초 늦게 걸린다. 한 번이면 몰라도
            # 재시도·자리넓힘까지 곱해지면 25분이 된다(실측).
            per = timeout if remaining is None else min(timeout, remaining())
            if per <= 0:
                raise BudgetExceeded("시간 상한에 닿아 더 묻지 않습니다")
            complete.last_requests += 1
            with urllib.request.urlopen(request, timeout=per) as response:
                body = json.loads(response.read().decode("utf-8"))
            # 엔드포인트가 준 usage 를 남긴다. 어림(문자수/4)보다 정확하고, 안 주는
            # 엔드포인트도 있으므로 없으면 없는 대로 둔다(계량기가 어림으로 넘어간다).
            complete.last_usage = body.get("usage")
            choice = body["choices"][0]
            return (choice["message"].get("content") or "",
                    str(choice.get("finish_reason") or ""))

        complete.last_truncated = False
        # **서버에 실제로 몇 번 나갔는가.** 논리적 호출 하나가 재시도·자리넓힘·
        # 사고끔을 타면 최대 12번 나간다. 계량기가 1 로 세면 `--max-calls` 는
        # 상한 구실을 못 하고, 화면의 "LLM 호출 N회" 도 사실이 아니다. 이 레포가
        # "효율적"이라고 말하려면 그 숫자가 참이어야 한다.
        # 실측: 도구 단조에서 타임아웃 4번이 나갔는데 계량기는 "1회" 였다.
        complete.last_requests = 0
        first = {"max_tokens": complete.wide_room} if complete.wide_room else {}
        text, finish = _with_retry(call, first, retries=retries,
                                   backoff=backoff, sleep=sleep,
                                   remaining=remaining)
        # 사고형 모델(Qwen3 계열)은 사고에 토큰을 다 쓰고 잘리면 content 가
        # **빈 채로** 온다. 그러면 위층은 "모델이 쓸 만한 걸 못 찾았다"고 읽는데,
        # 사실 모델은 답한 적이 없다 - 없는 결론을 지어내는 셈이다.
        # 실측: 실세션 학습에서 hits=6 인데 초안 0건이었고, 원인은 200토큰을
        # 전부 <think> 에 쓰고 잘린 것이었다. 끄고 다시 물으면 바로 답한다.
        if not text.strip():
            # 잘렸다는 것을 **부르는 쪽에 남긴다.** 안 남기면 위에서 파싱이
            # 실패했을 때 "모델이 빈 배열"이라고 말하게 되는데, 모델은 빈
            # 배열을 낸 적이 없다. 잘린 것과 안 낸 것은 사람이 할 일이 다르다.
            complete.last_truncated = finish == "length"
            # **먼저 자리를 넓혀 준다.** 예전에는 곧장 사고를 껐는데, 사고를 켠
            # 이유가 품질이었다(실측: 사고끔 초안 2·사실 6 -> 사고켬 초안 3·사실
            # 10). 잘릴 때마다 그 품질을 포기하면 길고 복잡한 세션일수록 나쁜
            # 답을 받는다 - 배울 게 제일 많은 쪽이다.
            #
            # 실측: 사실 증류에서 사고에 3,787 토큰을 썼다. 상한 4,000 의 95% 라
            # 조금만 흔들려도 넘친다. 넘친 뒤 사고를 끄고 물으면 `[]` 가 온다.
            # 모자란 것은 생각이 아니라 자리였다.
            room = complete.wide_room or int((extra or {}).get("max_tokens")
                                             or 4000) * 2
            # **넓혀야 했다는 것을 기억한다.** 안 그러면 어려운 과제마다 매번
            # 좁은 자리로 먼저 묻고, 사고에 다 쓰고 빈 응답을 받고, 그제서야
            # 넓힌다 - 호출이 늘 2배다. 실측: 한글 조사 도구를 단조할 때 첫
            # 호출이 120초를 쓰고 빈 채로 왔고, 넓힌 두 번째가 답을 냈다.
            complete.wide_room = room
            logger.warning("[llm] 빈 응답(finish=%s). 자리를 %d 로 넓혀 다시 묻습니다.",
                           finish or "?", room)
            text, finish = _with_retry(call, {"max_tokens": room},
                                       retries=retries, backoff=backoff,
                                       sleep=sleep, remaining=remaining)
        if not text.strip():
            # 넓혀도 비면 그때 사고를 끈다. 답이 나쁜 것이 답이 없는 것보다는 낫다.
            logger.warning("[llm] 아직 빔(finish=%s). 사고를 끄고 다시 묻습니다.",
                           finish or "?")
            text, finish = _with_retry(
                call, {"chat_template_kwargs": {"enable_thinking": False}},
                retries=retries, backoff=backoff, sleep=sleep,
                remaining=remaining)
        if not text.strip():
            # 두 번 물어도 비면 **비었다고 말한다**. 빈 문자열을 정상 응답인 척
            # 넘기면 그 위의 모든 판정이 조용히 무의미해진다.
            raise RuntimeError(f"모델이 빈 응답을 냈습니다 (finish={finish or '?'})")
        return text

    # 이 엔드포인트에서 자리를 넓혀야 했던 적이 있는가. 있으면 다음부터 그 자리로
    # 시작한다 - 같은 과제를 좁은 자리로 또 물어 볼 이유가 없다.
    complete.wide_room = 0
    complete.last_requests = 0
    return complete


# ────────────────────────────────────────────── 얼마나 썼는가, 그리고 그만

def _secs(value: float) -> str:
    """짧은 시간은 소수점까지. "0초"라고 쓰면 무제한으로 읽힌다."""
    return f"{value:.1f}초" if value < 10 else f"{value:.0f}초"


class BudgetExceeded(RuntimeError):
    """상한에 닿았다. **조용히 계속하지 않는다** - 예산은 세다가 넘기면 의미가 없다."""


@dataclass
class Budget:
    """LLM 사용량을 세고, 상한에 닿으면 멈춘다.

    왜 필요한가: 우리는 라우팅·툴 검증·회귀검사·기억 회상에서 LLM 을 0회 부른다.
    그건 자랑거리지만, **부르는 자리에서 얼마나 쓰는지 모르면** "효율적"이라는 말이
    측정이 아니라 인상이 된다. 경쟁 노드에는 이미 비용 상한이 있다.

    **가격을 코드에 박지 않는다.** 요율은 바뀌고, 박아 두면 언젠가 조용히 틀린 금액을
    보고한다. 그래서 토큰은 **항상** 세고 금액은 `usd_per_1k` 를 준 경우에만 낸다.
    금액을 모르면 모른다고 말하는 편이 틀린 숫자를 대는 것보다 낫다.

    토큰도 엔드포인트가 `usage` 를 줄 때만 정확하다. 안 주면 문자수/4 로 어림하고
    그렇게 어림했다고 표시한다.
    """

    max_calls: int = 0            # 0 = 무제한
    max_tokens: int = 0
    max_usd: float = 0.0
    # 벽시계 상한(초). 시간은 다른 상한과 성격이 다르다 - 토큰은 안 부르면 안
    # 늘지만 시간은 그냥 흐른다. 느린 엔드포인트에 붙으면 토큰 상한에 닿기
    # 전에 몇 시간이 지난다. 예산의 목적이 "자리를 비운 사이 폭주하지 않게"인데
    # 시간이 빠져 있으면 그 목적을 절반만 이룬다.
    max_seconds: float = 0.0
    usd_per_1k: float = 0.0       # 주면 금액을 낸다. 안 주면 토큰만 센다.

    calls: int = 0
    started: float = field(default_factory=time.monotonic)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    estimated: bool = False       # 한 번이라도 어림했으면 True

    @property
    def tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def remaining_seconds(self) -> float:
        """벽시계 상한까지 남은 초. 상한이 없으면 무한.

        **예산은 시작할지 말지만 정하는 것이 아니라 진행 중인 일도 묶어야 한다.**
        실측: 논리적 호출 하나가 3단계(기본 -> 자리 넓힘 -> 사고 끔) × 재시도 4회
        × 120초 = 최악 24분이었고, 그동안 `--max-seconds 1200` 은 한 번도 검사되지
        않았다(호출 **전에**만 보니까). 25분 벽에 걸려 판정 한 줄 없이 잘렸다.
        """
        if not self.max_seconds:
            return float("inf")
        return max(0.0, self.max_seconds - self.elapsed)

    @property
    def elapsed(self) -> float:
        """`monotonic` 이다. 시스템 시계가 바뀌어도(NTP·서머타임) 안 흔들린다."""
        return time.monotonic() - self.started

    @property
    def usd(self) -> float | None:
        """요율이 없으면 **None** - 0 이 아니다. 0 은 "안 썼다"로 읽힌다."""
        if self.usd_per_1k <= 0:
            return None
        return self.tokens / 1000.0 * self.usd_per_1k

    def spending_at_most(self, share: float) -> "Budget":
        """이 예산의 **일부만** 쓰는 이름표. 쓴 것은 원래 예산에 그대로 쌓인다.

        왜 필요한가(실측): 큰 세션에서 초안 만들기가 시간 상한 420초를 통째로
        먹었다. 그 뒤에 오는 것이 정작 값을 내는 두 가지다 - 재현벤치 측정과
        사실 증류. 결과는 `사실 증류 0건`, `기억 +0`, 그리고 게이트가 한 번도
        재 보지 못한 채 후보 5건을 내보낸 것이었다.

        초안은 재료일 뿐이고 **재 보지 않은 초안은 값이 0** 이다. 여섯 개를 쓰고
        하나도 못 재느니 두 개를 쓰고 재는 편이 낫다. 그래서 앞단에 몫을 정해
        준다 - 남기는 것이 아니라 **앞을 자르는 것**이다.
        """
        share = min(max(share, 0.0), 1.0)
        cut = Budget(max_calls=int(self.max_calls * share) if self.max_calls else 0,
                     max_tokens=int(self.max_tokens * share) if self.max_tokens else 0,
                     max_usd=self.max_usd * share if self.max_usd else 0.0,
                     max_seconds=self.max_seconds * share if self.max_seconds else 0.0,
                     usd_per_1k=self.usd_per_1k)
        cut.started = self.started
        cut.calls = self.calls
        cut.prompt_tokens = self.prompt_tokens
        cut.completion_tokens = self.completion_tokens
        cut._parent = self          # 쓴 것은 원래 예산에도 쌓아야 한다
        return cut

    def check(self) -> None:
        """다음 호출을 해도 되는가. 넘었으면 던진다."""
        if self.max_calls and self.calls >= self.max_calls:
            raise BudgetExceeded(f"호출 상한 {self.max_calls}회에 닿았습니다")
        if self.max_tokens and self.tokens >= self.max_tokens:
            raise BudgetExceeded(f"토큰 상한 {self.max_tokens:,}에 닿았습니다 "
                                 f"(현재 {self.tokens:,})")
        spent = self.usd
        if self.max_usd and spent is not None and spent >= self.max_usd:
            raise BudgetExceeded(f"금액 상한 ${self.max_usd:.2f} 에 닿았습니다 "
                                 f"(현재 ${spent:.4f})")
        if self.max_seconds and self.elapsed >= self.max_seconds:
            # `.0f` 로 찍으면 1초 미만이 "0초"가 된다. 상한 0 은 이 코드에서
            # **무제한**이라는 뜻이라 정반대로 읽힌다.
            raise BudgetExceeded(
                f"시간 상한 {_secs(self.max_seconds)}에 닿았습니다 "
                f"(현재 {_secs(self.elapsed)})")

    _parent: "Budget | None" = None

    def record(self, prompt: str, answer: str, usage: dict | None = None,
               requests: int = 1) -> None:
        if self._parent is not None:
            self._parent.record(prompt, answer, usage, requests)
        # 논리적 호출 하나가 서버에는 여러 번 나갈 수 있다(재시도·자리넓힘).
        # 나간 만큼 센다 - 안 그러면 상한이 상한 구실을 못 한다.
        self.calls += max(1, requests)
        if usage and usage.get("prompt_tokens") is not None:
            self.prompt_tokens += int(usage.get("prompt_tokens") or 0)
            self.completion_tokens += int(usage.get("completion_tokens") or 0)
        else:
            # 엔드포인트가 usage 를 안 준다. 어림하되 어림했다고 남긴다.
            self.estimated = True
            self.prompt_tokens += max(1, len(prompt) // 4)
            self.completion_tokens += max(1, len(answer or "") // 4)

    def summary(self) -> str:
        spent = self.usd
        money = f" · ${spent:.4f}" if spent is not None else " · 금액 미상(요율 없음)"
        mark = " (어림)" if self.estimated else ""
        limits = []
        if self.max_calls:
            limits.append(f"호출 {self.calls}/{self.max_calls}")
        if self.max_tokens:
            limits.append(f"토큰 {self.tokens:,}/{self.max_tokens:,}")
        if self.max_usd:
            limits.append(f"금액 ${self.max_usd:.2f} 상한")
        tail = " · 상한 " + ", ".join(limits) if limits else ""
        return (f"LLM 호출 {self.calls}회 · 토큰 {self.tokens:,}{mark}{money}{tail}")


# 완성기가 호출마다 남기는 진단값. 감싸는 쪽이 그대로 옮겨 준다.
DIAGNOSTICS = ("last_usage", "last_truncated", "last_requests")


def metered(complete: Completer, budget: Budget) -> Completer:
    """완성기를 감싸 사용량을 세고 상한을 집행한다.

    감싸는 자리를 하나로 두는 이유: 부르는 곳마다 세면 언젠가 한 곳을 빠뜨리고,
    빠뜨린 그 자리가 정확히 비용이 새는 자리가 된다.
    """
    def wrapped(prompt: str) -> str:
        budget.check()                       # 넘었으면 **부르기 전에** 멈춘다
        try:
            answer = complete(prompt)
        except Exception:
            # **실패도 센다.** 예전에는 성공한 호출만 셌다(실측: 실패 5회 뒤
            # calls=0). 재시도를 붙인 뒤로는 논리적 1회가 서버에 최대 4번
            # 나가는데 계량기는 0 이었다. 그러면 `--max-calls` 를 줘도 그보다
            # 훨씬 많이 나가고, 화면의 사용량은 실제보다 적다. 상한의 목적이
            # "자리를 비운 사이 폭주하지 않게"인데 세다 만 상한은 상한이 아니다.
            budget.record(prompt, "", None,
                          getattr(complete, "last_requests", 1) or 1)
            raise
        budget.record(prompt, answer, getattr(complete, "last_usage", None),
                      getattr(complete, "last_requests", 1) or 1)
        # 진단값을 **바깥으로 옮긴다.** 완성기가 남기는 것은 안쪽 함수에
        # 붙는데, 감싼 뒤에 부르는 쪽은 바깥을 들고 있어 안 보인다. 실측:
        # `last_truncated` 가 `learn` 에서 늘 False 로 읽혀, 답이 잘려서
        # 0 건이 된 경우에도 "모델이 빈 배열"이라는 틀린 이유가 나갔다.
        # 이름 하나씩 적으면 새 진단을 붙일 때 빠뜨리므로 목록으로 둔다.
        for mark in DIAGNOSTICS:
            setattr(wrapped, mark, getattr(complete, mark, None))
        return answer
    return wrapped


# ────────────────────────────────────────────── 세션에서 사실만 걸러 내기

_DISTILL_PROMPT = """이 작업 기록에서 **다음에도 참인 사실**만 뽑아라.

사실이 아닌 것(뽑지 마라): 이번에만 있었던 일, 대화 원문, 감정, 진행 상황,
"X 를 고쳤다" 같은 사건 기록, 임시 경로, 실패한 시도.
사실인 것(뽑아라): 이 환경이 어떻게 되어 있는가, 어떤 방식이 통하고 어떤 방식이
안 통하는가, 도구·서비스의 성질, 다음에 같은 상황에서 알아야 할 것.

한 줄에 하나, 그 자체로 읽히게 써라. 문맥 없이는 뜻이 안 통하는 문장은 버려라.
확실하지 않으면 넣지 마라 - **틀린 사실은 없느니만 못하다.**

작업 기록:
{trace}

기록된 교정:
{corrections}

JSON 배열만 출력하라. 각 항목은 {{"text": "사실 한 줄"}}.
뽑을 것이 없으면 [] 를 출력하라."""


def _fact_text(item) -> str:
    """사실 하나를 문자열로. **그릇을 가리지 않는다.**

    프롬프트는 `{"text": ...}` 를 요구하지만 사고를 끄면 모델은 `["사실", ...]`
    을 낸다. 실측: 그렇게 온 쓸 만한 사실 9건이 통째로 버려졌다. 모델이 틀린 게
    아니라 같은 내용을 다른 그릇에 담아 온 것뿐인데 우리가 못 받았다.

    특히 아픈 이유: 사고가 잘리면 완성기가 자동으로 사고를 끄고 다시 묻는다.
    즉 **길고 복잡한 세션일수록** 문자열 배열이 오고, 그런 세션일수록 배울 것이
    0 이 됐다. 배울 게 제일 많은 쪽이 제일 안 배워졌다.
    """
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        # `text` 가 정석이고, 나머지는 모델이 흔히 쓰는 이름들이다.
        for key in ("text", "fact", "content", "statement"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def distill_facts(complete: Completer, trace: RunTrace,
                  limit: int = 6) -> list[str]:
    """세션에서 **다음에도 참인 사실**만 걸러 낸다.

    왜 필요한가: Claude Code 같은 원천은 정제 기억을 주지 않는다(대화 원문은 사실이
    아니므로 원천이 비워 두는 것이 맞다). 그래서 로컬 세션으로 도는 동안 기억 적재는
    **늘 0건**이었고, 기억을 재고 신뢰도를 매기고 시간 유효창을 두는 장치가 통째로
    놀았다. 원문을 복사하지 않으면서 그 공백을 메우는 방법이 증류다.

    [주의] 여기서 나온 것은 **후보일 뿐이다.** 규약(never_learn)을 통과해야 실리고,
    실린 뒤에도 신뢰도는 측정으로만 오른다 - 모델이 그럴듯하게 썼다는 것은 근거가
    아니다.
    """
    corrections = "\n".join(
        f"- {event.detail}" for event in trace.events
        if event.type == "user_correction" and event.detail)[:1500] or "- 없음"
    prompt = _DISTILL_PROMPT.format(trace=_render_trace(trace, cap=30),
                                    corrections=corrections)
    # 실패를 **부르는 쪽에 돌려준다.** 로거로만 남기면 CLI 가 로깅을 안 켜서
    # 사용자는 "기억 +0" 만 보고 이유를 모른다. 실측: 엔드포인트가 느릴 때
    # 6회 중 6회가 TimeoutError 였는데 화면에는 아무 말도 안 나왔다.
    # "모든 0에는 이유가 있다"가 여기서만 안 지켜지고 있었다.
    distill_facts.last_error = ""
    raw = ""
    try:
        raw = complete(prompt)
        items = _extract_json_array(raw)
    except Exception as exc:
        distill_facts.last_error = f"{type(exc).__name__}: {str(exc)[:120]}"
        logger.warning("[distill] 실패: %s", distill_facts.last_error)
        return []
    facts, too_long, too_short = [], 0, 0
    for item in items[:limit]:
        text = _fact_text(item)
        # 너무 짧은 것은 사실이 아니라 낱말이고, 너무 긴 것은 사실이 아니라 이야기다.
        if 12 <= len(text) <= 300:
            facts.append(text)
        elif len(text) > 300:
            too_long += 1
        else:
            too_short += 1
    if not facts:
        # **왜** 0 건인지 정확히. 틀린 이유는 없는 이유보다 나쁘다 - 사람이
        # 엉뚱한 데를 본다. 실측으로 두 번 틀렸다: 모양 때문에 버려 놓고 "길이
        # 조건"이라 했고, 그 다음엔 첫 시도가 잘렸다고 "답이 잘렸습니다"라고 했다.
        # 그때 재질의는 **성공했고** 문제는 그 답이 배열이 아니었던 것이다.
        #
        # 그래서 **실제로 받은 것**부터 말한다. 잘림은 곁들이는 배경이지 원인이
        # 아니다 - 잘렸어도 재질의가 통했으면 0 건의 이유가 못 된다.
        cut = " (첫 시도는 사고에 토큰을 다 써 잘렸습니다)" \
            if getattr(complete, "last_truncated", False) else ""
        if not items:
            peek = " ".join((raw or "").split())[:120]
            distill_facts.last_error = (
                f"배열을 못 읽었습니다 · 받은 것: {peek!r}{cut}" if peek
                else f"모델이 아무것도 안 냈습니다{cut}")
        else:
            distill_facts.last_error = (
                f"{len(items)}건 전부 버림 (너무 김 {too_long} · 너무 짧거나 빔 "
                f"{too_short})")
    logger.info("[distill] 모델 %d -> 채택 %d", len(items), len(facts))
    return facts


# 마지막 증류가 왜 0건이었는지. 부르는 쪽이 화면에 옮긴다.
distill_facts.last_error = ""

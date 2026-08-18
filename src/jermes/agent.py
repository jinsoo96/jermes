"""Jermes - 하나의 에이전트. 흩어진 계층을 한 사이클로 묶는다.

지금까지 모듈은 다 있었지만 "에이전트"는 없었다. 호스트가 신호·초안·게이트·원장·
회상을 매번 손으로 이어붙였다. 여기가 그 조립을 한 자리로 모은다.

**남들과 다른 로직 네 가지** - 말이 아니라 이 파일의 코드가 지키는 것:

1. **기억과 스킬에 같은 잣대.** 둘 다 `BenchCase` + 점수 함수로 잰다. 스킬을 자동
   생성하되 효능은 안 재는 쪽도 있고, 기억을 대리 신호(조회·편집)로 등급 매기는 쪽도
   있다. 여기서는 **둘 다 "빼고 재생해서 나빠지면 값어치가 있다"** 로 잰다.
2. **딱지 없이는 컨텍스트에 못 들어간다.** 회상 결과는 검증됨/미검증이 표시된 채로
   나간다. 라벨 없는 주입은 모델이 추측을 사실로 믿게 만든다.
3. **자동 삭제 없음.** 스킬도 기억도 내려갈 때 `disputed`/`staged` 로 남고 이력이 붙는다.
   되돌릴 수 없는 자동 조치는 하지 않는다.
4. **모든 0 에는 이유가 있다.** 사이클 보고는 숫자로 말한다 - 신호 0인지, 초안 0인지,
   케이스가 모자라 못 잰 건지. (라이브에서 "성공인데 아무것도 안 배움"을 세 번 겪고 얻은 규칙.)

순수·동기 함수다. 일정과 영속은 호스트가 정한다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Sequence
from xml.sax.saxutils import escape

from .constitution import Constitution
from .gate import BenchCase, ForgeGate
from .ledger import SkillLedger
from .loop import ApprovalPolicy, ForgeEpisode, SkillForge
from .memory import (
    Contradiction,
    MemoryItem,
    MemoryPolicy,
    MemoryScoreFn,
    Resolution,
    apply_measurement,
    decay_unmeasured,
    detect_contradictions,
    measure,
    recall as recall_memory,
    resolve,
)
from .discovery import Capability, render_all
from .model import RunTrace, SkillCandidate, Unmeasurable


def _attr(value: str) -> str:
    """속성값을 **항상 큰따옴표**로 감싸고 내부 따옴표는 실체참조로 바꾼다.

    stdlib `quoteattr` 를 쓰면 안 된다 - 값에 큰따옴표가 있으면 작은따옴표로 감싸므로
    `name='a" status="검증됨'` 같은 결과가 나오고, 그 안의 `status="검증됨"` 이 **문자
    그대로 남는다**. XML 파서에는 안전하지만 이 문자열을 읽는 건 파서가 아니라 LLM 이다.
    (검수에서 실제로 이 형태가 나왔다.)
    """
    return '"' + escape(str(value), {'"': "&quot;", "'": "&apos;"}) + '"'


def _as_capability(record) -> Capability:
    """원장 기록 -> `Capability`. **표현이 하나여야 고르는 자리도 하나가 된다.**
    (예전에는 RecalledSkill·SkillListing·Capability 셋이 같은 것을 달리 담았다.)"""
    skill = record.skill
    evidence = ""
    if skill.kind == "tool":
        try:
            evidence = json.loads(skill.body).get("verification", "")
        except ValueError:
            evidence = ""
    return Capability(
        name=record.name,
        kind="tool" if skill.kind == "tool" else "skill",
        description=skill.description,
        source=f"ledger:{record.status}",
        invoke={"command": f"jermes run {record.name}"} if skill.kind == "tool" else {},
        verified=bool(skill.verified),
        evidence=evidence,
        examples=list((skill.meta or {}).get("examples") or []),
        body="" if skill.kind == "tool" else skill.body,
    )


@dataclass
class ContextPack:
    """다음 실행에 넣을 것들 - 검증 여부가 붙은 채로.

    블록을 **직접 만들지 않는다.** 프롬프트로 나가는 모양은 `Capability.render` 한
    자리에서만 정해진다(이스케이프 규율이 걸린 곳이라, 여러 벌이면 한 곳에서만
    지켜진다). 여기는 무엇을 넣을지만 고른다.
    """

    skills: list[Capability] = field(default_factory=list)
    memory: list[MemoryItem] = field(default_factory=list)
    level: str = Capability.FULL
    # 넣을 수 있는 **문자 수** 상한. `limit` 은 개수만 세는데, 개수가 적어도
    # 긴 것 하나가 컨텍스트를 삼킬 수 있다. 실측: FULL 스킬 5개가 46,289자,
    # 긴 기억 5건이 10,259자였다.
    #
    # 넘치면 **자르되 버리지 않는다.** 통째로 빼면 무엇이 있었는지도 모르게
    # 되지만, 잘린 것은 잘렸다고 적으면 사람이 안다.
    max_chars: int = 12000

    def render(self) -> str:
        """**라벨을 지우지 않는다** - 미검증을 검증된 것처럼 보이게 만드는 순간
        이 시스템의 의미가 사라진다."""
        memory_blocks = [
            f"<memory id={_attr(item.item_id)} "
            f"trust=\"{item.trust:.2f}\" "
            f"status={_attr(_memory_status(item))}>"
            f"{escape(item.text.strip())}</memory>"
            for item in self.memory]
        return _within_budget(render_all(self.skills, self.level,
                                         extra=memory_blocks), self.max_chars)


def _memory_status(item) -> str:
    """이 사실이 **어디까지 확인됐는지**. `측정됨` 과 `못가름` 은 다르다 - 후자는
    재기는 했는데 넣으나 빼나 같았다는 뜻이라, 0.50 이라는 숫자에 근거가 없다.
    실측: 측정 기록 78건 중 67건이 gain 정확히 0.000 이었다. 그것들을 전부
    `측정됨` 이라고 적으면 모델은 재서 얻은 값으로 읽는다.
    """
    if getattr(item, "told_us_nothing", False):
        return "측정했으나 못 가름"
    return "측정됨" if item.measured else "미측정"


def _within_budget(text: str, limit: int) -> str:
    """문자 예산을 지킨다. **자르되 잘렸다고 밝힌다.**

    `limit` 은 개수만 세는데, 개수가 적어도 긴 것 하나가 컨텍스트를 삼킬 수 있다.
    실측: FULL 스킬 5개가 46,289자였다.

    **완결된 블록 단위로** 자른다. 줄 단위로 자르면 여는 태그만 남고(실측: 여는 4
    닫는 3) 뒤에 오는 것이 그 열린 블록 안으로 빨려 들어가, 미검증 내용이 검증된
    블록의 일부처럼 읽힌다. 이 파일이 이스케이프에 공들이는 것과 같은 이유다.
    """
    if limit <= 0 or len(text) <= limit:
        return text

    kept, buffer, depth, used = [], [], 0, 0
    for line in text.splitlines():
        buffer.append(line)
        stripped = line.strip()
        if stripped.startswith("</"):
            depth -= 1
        elif stripped.startswith("<") and not stripped.endswith("/>"):
            depth += 1 if not stripped.startswith("</") else 0
        if depth > 0:
            continue                      # 아직 안 닫힌 블록이다
        block = "\n".join(buffer)
        buffer = []
        if used + len(block) + 1 > limit:
            break                         # 이 블록은 통째로 못 넣는다
        kept.append(block)
        used += len(block) + 1

    dropped = len(text) - used
    kept.append(f'<truncated chars="{dropped}" reason="컨텍스트 예산"/>')
    return "\n".join(kept)


@dataclass
class CycleReport:
    run_id: str
    signals: int = 0
    drafted: int = 0
    promoted: list[str] = field(default_factory=list)
    staged: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    memory_added: list[str] = field(default_factory=list)
    memory_measured: int = 0
    memory_up: int = 0
    memory_down: int = 0
    contradictions: int = 0
    resolved: int = 0
    disputed: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [f"run={self.run_id}",
                 f"신호 {self.signals} · 초안 {self.drafted}",
                 f"스킬 검증 {len(self.promoted)} / 대기 {len(self.staged)} / 거절 {len(self.rejected)}",
                 f"기억 +{len(self.memory_added)} · 측정 {self.memory_measured}"
                 f"(↑{self.memory_up} ↓{self.memory_down})",
                 f"모순 {self.contradictions} → 판정 {self.resolved} · 보류 {len(self.disputed)}"]
        if self.notes:
            parts.append("· " + " · ".join(self.notes))
        return " | ".join(parts)


class JermesAgent:
    """스킬 원장과 기억을 하나의 규율 아래 두는 에이전트.

    `memory` 는 호스트가 넘겨준 리스트를 **그대로 들고** 변경한다(영속은 호스트 몫).
    """

    def __init__(self, ledger: SkillLedger, gate: ForgeGate,
                 memory: list[MemoryItem] | None = None,
                 memory_policy: MemoryPolicy | None = None,
                 approval: ApprovalPolicy | None = None,
                 forge: SkillForge | None = None,
                 constitution: Constitution | None = None) -> None:
        self.ledger = ledger
        self.gate = gate
        self.memory: list[MemoryItem] = memory if memory is not None else []
        self.memory_policy = memory_policy or MemoryPolicy()
        self.constitution = constitution or Constitution()
        # 규약에 막힌 기억. 조용히 버리면 왜 안 실렸는지 아무도 모른다.
        self.blocked_memories: list[tuple[str, str]] = []
        # 규약은 게이트가 집행한다 - 에이전트가 "지키겠다"고 약속하는 구조가 아니다.
        if getattr(gate, "constitution", None) is None:
            gate.constitution = self.constitution
        self.forge = forge or SkillForge(ledger, gate, approval=approval)

    # ------------------------------------------------------------ 기억

    def remember(self, trace: RunTrace) -> list[MemoryItem]:
        """런에서 기억 후보를 뽑는다 - 교훈과 정제 기억만.

        도구 출력 원문은 담지 않는다. 그건 사실이 아니라 그때의 상황이고, 기억으로
        굳으면 다음 실행을 과거에 묶는다.
        """
        added: list[MemoryItem] = []
        seen = {item.text.strip() for item in self.memory}
        texts = [t for t in list(trace.lessons) + [trace.refined_memory] if t and t.strip()]
        for index, text in enumerate(texts):
            text = text.strip()
            if text in seen:
                continue          # 같은 사실을 두 번 적지 않는다(멱등)
            # 스킬 후보는 규약을 거치는데 기억은 안 거치고 있었다. 원천이 정제 기억을
            # 주는 순간(호스트 스파인, 또는 세션 증류) 비밀값이 그대로 기억에 박힌다.
            # 배우면 안 되는 것은 스킬이든 기억이든 안 된다.
            blocked = self.constitution.check_text(text)
            if blocked:
                self.blocked_memories.append((text[:120], blocked))
                continue
            # 프로젝트가 있으면 **그 프로젝트 것**이다. `trace.scope` 는 플랫폼
            # 열거형이라 늘 "user"(전역)라서, 그대로 쓰면 이 저장소에서만 참인
            # 사실이 모든 프로젝트에 딸려 간다.
            item = MemoryItem(item_id=f"{trace.run_id}#m{index}", text=text,
                              scope=trace.scope_key or trace.scope,
                              source_run_ids=[trace.run_id])
            self.memory.append(item)
            added.append(item)
            seen.add(text)
        return added

    def measure_memory(self, score: MemoryScoreFn, cases: Sequence[BenchCase],
                       items: Sequence[MemoryItem] | None = None,
                       limit: int | None = None) -> tuple[int, int, int]:
        """(잰 개수, 오른 개수, 내린 개수). 못 재면 0 을 돌려주고 조용히 넘어가지 않는다.

        `limit` 이 필요한 이유: 한 번 재는 데 케이스 수 × 2 번의 채점이 든다.
        기억이 200개면 한 사이클에 수천 번이고, 채점이 LLM 이면 그대로 멈춘 것처럼
        보인다. 그래서 **미측정 항목을 먼저** 재고 나머지는 다음 사이클로 넘긴다.
        """
        self.memory_measure_stopped = ""
        pool = list(items if items is not None else self.memory)
        if limit is not None:
            # 아직 안 재본 것 우선 - 새 기억이 영영 순번을 못 받는 걸 막는다.
            pool.sort(key=lambda i: (i.measured, i.item_id))
            pool = pool[:max(0, limit)]
        from .gate import relevant_cases

        measured = up = down = 0
        for item in pool:
            if item.status == "retired":
                continue
            # **그 사실이 관계된 실패로만 잰다.** 무관한 것을 평균에 섞으면
            # 아무리 맞는 사실도 이득이 0 이 되고, 0 은 neutral 이라 신뢰가
            # 안 움직인다. 실측: 여섯 건을 재고 ↑0 ↓0 이었다 - "메모리 기반
            # 자가개선"이라고 해놓고 자가개선의 결과가 0 이었다.
            #
            # 스킬 게이트에서 고친 것과 같은 뿌리다. 한쪽만 고치면 같은 결함이
            # 반만 없어진다.
            # `symmetric=True` - 기억은 주제가 **문장**이라 한쪽 방향만 보면
            # 틀린 사실이 통째로 걸러진다(고칠 낱말을 안 품고 있으니까).
            mine = relevant_cases(cases, item.text, symmetric=True)
            try:
                result = measure(item, score, mine, self.memory_policy)
            except Unmeasurable as why:
                # **못 잰 것은 잰 것이 아니다.** 예산이 끊기면 넣으나 빼나 0 점이라
                # 이득이 정확히 `+0.000` 이 되고, 그건 `neutral` 이라 신뢰가 안
                # 움직인다 - 못 쟀다는 사실이 "재봤는데 차이 없음" 으로 둔갑한다.
                #
                # 멈추되 **왜 멈췄는지 남긴다**. 남은 항목도 같은 사정이라 재봤자
                # 같은 거짓이고, 조용히 멈추면 화면의 "측정 0" 이 "잴 것이 없었다"
                # 처럼 읽힌다.
                self.memory_measure_stopped = str(why)
                break
            if result is None:
                continue
            before = item.trust
            apply_measurement(item, result, self.memory_policy)
            measured += 1
            up += item.trust > before
            down += item.trust < before
        return measured, up, down

    def reconcile(self, score: MemoryScoreFn | None = None,
                  cases: Sequence[BenchCase] = ()) -> tuple[list[Contradiction],
                                                            list[Resolution]]:
        """모순을 찾고, 잴 수 있으면 증거로 판정한다.

        점수 함수가 없으면 판정하지 않고 **드러내기만** 한다. 있으면 한 걸음 더 간다 -
        재현벤치가 이긴 쪽을 정한다.
        """
        found = detect_contradictions(self.memory)
        if not found or score is None:
            return found, []
        index = {item.item_id: item for item in self.memory}
        resolutions = []
        for contradiction in found:
            left, right = index.get(contradiction.left), index.get(contradiction.right)
            if left is None or right is None:
                continue
            resolutions.append(
                resolve(contradiction, left, right, score, cases, self.memory_policy))
        return found, resolutions

    # ------------------------------------------------------------ 회상

    def recall(self, skill_limit: int = 5, memory_limit: int = 5,
               include_unverified: bool = False, task: str = "",
               at: str = "") -> ContextPack:
        """다음 실행에 넣을 묶음. 기본은 **검증된 스킬만**.

        미검증 포함은 호출측이 명시적으로 켜야 하고, 켜도 라벨은 남는다.

        `task` 를 주면 그 과제에 맞는 것으로 **골라서** 준다. 안 주면 예전처럼
        이름순 상위 N 개다 - 원장이 작을 때는 그게 맞고, 커지면 그게 문제가 된다
        (실측: 카탈로그 50개에서 골라 주면 토큰 88% 절감, 정확도 동일).

        `at` 은 기억을 **그 시점 기준**으로 본다. 대체된 옛 사실을 최신인 양 넣으면
        에이전트가 지난 규칙대로 움직인다.
        """
        records = [r for r in self.ledger.list() if r.status == "active"]
        if not include_unverified:
            records = [r for r in records if r.skill.verified]

        if task:
            from .router import Router

            pool = [_as_capability(r) for r in records]
            ranked = Router(pool).route(task, limit=skill_limit).names()
            order = {name: index for index, name in enumerate(ranked)}
            records = sorted((r for r in records if r.name in order),
                             key=lambda r: order[r.name])
        else:
            records.sort(key=lambda r: (-int(r.skill.verified), r.name))

        skills = [_as_capability(r) for r in records[:skill_limit]]
        return ContextPack(skills=skills,
                           memory=recall_memory(self.memory, limit=memory_limit, at=at,
                                              task=task))

    # ------------------------------------------------------------ 한 사이클

    def cycle(self, trace: RunTrace,
              bench_cases: Sequence[BenchCase] = (),
              drafted: list[SkillCandidate] | None = None,
              memory_score: MemoryScoreFn | None = None,
              prior_signatures: dict[str, int] | None = None,
              decay: bool = True,
              memory_measure_limit: int | None = 20) -> CycleReport:
        """관찰 → 기억 → 학습 → 화해 → 보고. 이 순서가 곧 에이전트의 정의다."""
        report = CycleReport(run_id=trace.run_id)

        added = self.remember(trace)
        report.memory_added = [item.item_id for item in added]

        episode: ForgeEpisode = self.forge.process_trace(
            trace, bench_cases=bench_cases, prior_signatures=prior_signatures,
            drafted=drafted)
        report.signals = len(episode.signals)
        report.drafted = len(episode.drafted)
        # 거절은 두 곳에서 나온다 - 큐레이터(중복·안전)와 게이트(규약 위반).
        # 한쪽만 보면 규약으로 막힌 후보가 보고에서 사라진다.
        report.rejected = [r.candidate_name for r in episode.rejected]
        for skill, result in episode.results:
            # **승인 거버넌스를 여기서 집행한다.** 규약에 `approval_required_scopes`
            # 가 있고 `needs_human_approval()` 도 있었는데 **부르는 곳이 없었다** -
            # 정의만 있고 집행이 없으면 그건 규약이 아니라 장식이다.
            #
            # 게이트를 통과한 것과 남들이 쓰는 자리에 올려도 되는 것은 다르다.
            # 통과했어도 여럿이 쓰는 스코프면 `staged` 로 두고 사람을 기다린다.
            if (result.verdict == "promoted"
                    and self.constitution.needs_human_approval(skill.scope)):
                report.staged.append(skill.name)
                report.notes.append(
                    f"승인 대기 {skill.name}: 스코프 {skill.scope} 는 사람이 "
                    f"봐야 올라갑니다 (`jermes approve {skill.name} --by <이름>`)")
                self.ledger.set_status(skill.name, "staged",
                                       note="승인 대기(스코프 규약)")
                continue
            if result.verdict == "promoted":
                report.promoted.append(skill.name)
            elif result.verdict == "rejected":
                report.rejected.append(skill.name)
                report.notes.append(f"거절 {skill.name}: {'; '.join(result.reasons)[:120]}")
            else:
                report.staged.append(skill.name)

        # 화해를 먼저 한다. `resolve` 가 충돌 쌍을 이미 재기 때문에, 뒤에 일괄 측정을
        # 돌리면 같은 항목이 한 사이클에 두 번 측정돼 trust 가 이중으로 움직인다.
        found, resolutions = self.reconcile(memory_score, bench_cases)
        report.contradictions = len(found)
        report.resolved = sum(1 for r in resolutions if r.decided)
        adjudicated = {side for c in found for side in (c.left, c.right)}
        if found and memory_score is None:
            report.notes.append("모순 판정 안 함 - 점수 함수가 없어 드러내기만 함")

        if memory_score is not None:
            rest = [i for i in self.memory if i.item_id not in adjudicated]
            measured, up, down = self.measure_memory(
                memory_score, bench_cases, items=rest, limit=memory_measure_limit)
            report.memory_measured, report.memory_up, report.memory_down = measured, up, down
            if self.memory_measure_stopped:
                # **못 잰 것과 재봤는데 차이 없는 것은 다르다.** 예산이 끊기면
                # 넣으나 빼나 0 점이라 이득이 정확히 `+0.000` 이 되고, 그건
                # `neutral` 이라 신뢰가 안 움직인다. 조용히 넘기면 화면의 숫자가
                # "재봤다"고 말하는데 실은 잰 적이 없다.
                report.notes.append(
                    f"기억 측정 중단({measured}/{len(rest)}) - "
                    f"{self.memory_measure_stopped}")
            elif measured == 0 and rest:
                # 오늘 세 번 당한 실패 방식: 0인데 이유를 안 말하면 멈춘 것과 구분이 안 된다.
                report.notes.append(
                    f"기억 측정 0 - 케이스 {len(bench_cases)}개 < 최소 "
                    f"{self.memory_policy.min_cases}")
            if memory_measure_limit is not None and len(rest) > memory_measure_limit:
                report.notes.append(
                    f"기억 측정 {memory_measure_limit}/{len(rest)} - 나머지는 다음 사이클")
        elif self.memory:
            report.notes.append("기억 측정 안 함 - 점수 함수 미지정")

        report.disputed = [item.item_id for item in self.memory
                           if item.status == "disputed"]

        if decay:
            decay_unmeasured(self.memory, self.memory_policy)
        return report

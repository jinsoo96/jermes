"""SF4 - the forge verification gate.

Same promote algebra as SelfForge: promote = dev_up AND held_ok AND sec_ok
AND NOT overopt. Bench cases are split deterministically by id hash into
dev/holdout (the Synapse/forge discipline: holdout never trains, only judges).

With no bench cases available the gate is honest: verdict "staged"
(unverified 2nd-track), never a fake pass.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from typing import Callable, Protocol, Sequence

from .curator import safety_check
from .model import GateResult, SkillCandidate, SkillDef, Unmeasurable


@dataclass
class BenchCase:
    case_id: str
    payload: dict = field(default_factory=dict)


def sign_test(pairs: Sequence[tuple[float, float]]) -> tuple[int, int, float]:
    """(좋아진 수, 나빠진 수, p값). **평균이 못 하는 말을 한다.**

    평균 차이는 "얼마나" 는 말해도 "우연인가" 는 못 말한다. 실측으로 그 한계를
    직접 봤다 - 같은 스킬이 케이스 12건에서 `+0.056`, 28건에서 `-0.016` 이었다.
    숫자가 적혀 있으니 잰 것처럼 보이지만 부호조차 안 정해진 상태였다.

    짝지은 자료(같은 케이스를 넣고/빼고 잰 것)에서 물어야 할 것은 **몇 건이
    좋아졌고 몇 건이 나빠졌는가**다. 그 둘만 세면(비긴 것은 버린다 - 그게
    부호검정의 요점이다) 나머지는 동전 던지기와 같은 계산이 된다:
    n번 중 k번 이상 앞면이 나올 확률.

    이 검정을 고른 이유:
      - 분포를 가정하지 않는다. 우리 점수는 `통과한 검사 / 전체 검사`라
        어떤 때는 0/1 이고 어떤 때는 분수다 - 정규성을 가정하는 t검정은
        여기서 근거가 없다.
      - 이진 자료에서는 McNemar 검정과 같은 것이 된다(경쟁 엔진이 쓰는 그
        검정이다). 우리는 분수 점수까지 같은 식으로 다룬다.
      - 표준 라이브러리만으로 **정확히** 계산된다(근사 아님). 케이스가 적을 때
        정규근사는 못 믿는데, 우리는 케이스가 적은 쪽이 정상이다.

    p값은 단측이다 - "좋아졌는가" 만 묻는다. 나빠진 것은 게이트의 다른 축
    (`max_holdout_drop`)이 이미 따로 잡는다.
    """
    better = sum(1 for before, after in pairs if after > before)
    worse = sum(1 for before, after in pairs if after < before)
    trials = better + worse
    if trials == 0:
        # 하나도 안 갈렸다. 우연일 확률을 물을 것도 없다.
        return 0, 0, 1.0
    # P(X >= better) where X ~ Binomial(trials, 0.5)
    tail = sum(math.comb(trials, k) for k in range(better, trials + 1))
    return better, worse, tail / (2 ** trials)


def case_hash(case_id: str) -> int:
    """케이스 id -> 안정적인 정수. 프로세스마다 달라지는 `hash()` 는 쓸 수 없다 -
    어제의 `verified` 가 오늘 다른 뜻이 되면 안 된다."""
    return int.from_bytes(hashlib.blake2b(case_id.encode("utf-8"),
                                          digest_size=8).digest(), "big")


def split_holdout(cases: Sequence, ratio: float = 0.25) -> tuple[list, list]:
    """(dev, holdout). **이 시스템에서 홀드아웃을 가르는 유일한 자리다.**

    예전에는 케이스마다 따로 동전을 던졌다(`digest[0]/255 < ratio`). 실측하면
    케이스 8개일 때 **10% 확률로 holdout 이 0개**가 됐고, 그러면
    `require_holdout_gain` 때문에 게이트는 무엇을 넣어도 승격할 수 없다(조용히
    staged 로 떨어진다). 121/400 은 holdout 이 1개뿐이라 잡음 하나가 판정을 정했다.

    그래서 해시로 **정렬해서 비율만큼 잘라낸다**. 결정적이면서(같은 케이스는 늘 같은
    쪽) 비율도 지킨다. 케이스가 2개 이상이면 양쪽에 최소 1개는 보장한다.

    툴 검증과 스킬 검증이 서로 다르게 가르면 "감춘 것으로 확인했다"가 조용히 거짓이
    된다. 그래서 두 경로 모두 여기를 부른다.
    """
    cases = list(cases)
    if ratio <= 0 or len(cases) < 2:
        return cases, []
    ordered = sorted(cases, key=lambda c: case_hash(c.case_id))
    held_n = max(1, min(len(cases) - 1, round(len(cases) * ratio)))
    held_ids = {c.case_id for c in ordered[:held_n]}
    return ([c for c in cases if c.case_id not in held_ids],
            [c for c in cases if c.case_id in held_ids])


# 홀드아웃이 무엇을 말했는가. 셋뿐이고, **판정은 이 셋에서만 나온다.**
HOLDOUT_CONFIRMED = "confirmed"     # 감춘 것에서도 좋아졌다
HOLDOUT_UNPROVEN = "unproven"       # 좋아지지도 나빠지지도 않았다
HOLDOUT_REGRESSED = "regressed"     # 감춘 것에서 나빠졌다 - 외운 것이다


def decide(measured: bool, dev_ok: bool, holdout: str) -> str:
    """`promoted` | `staged` | `rejected`. **판정이 나오는 유일한 자리다.**

    스킬은 확률적 개선으로 재고 툴은 전건 통과로 재지만, 세 낱말이 뜻하는 바는 같아야
    한다. 두 곳에서 각자 계산하면 언젠가 어긋나고, 어긋난 쪽이 조용히 이긴다
    (홀드아웃 가르기에서 이미 한 번 그랬다).

    지켜야 할 불변식 둘:
      ① **`staged` 는 "못 쟀다"이지 "실패했다"가 아니다.** 케이스가 모자라거나 감춘
         것으로 확인이 안 됐을 때만 여기 온다. 사람이 보고 승인할 수 있는 자리다.
      ② **`promoted` 는 감춘 것의 근거가 있어야 한다.** dev 만 좋아진 것은 그 케이스에
         맞춘 증거이지 재사용 가능한 능력의 증거가 아니다.
    """
    if not measured:
        return "staged"
    if not dev_ok:
        return "rejected"
    if holdout == HOLDOUT_REGRESSED:
        return "rejected"
    if holdout == HOLDOUT_CONFIRMED:
        return "promoted"
    return "staged"


class BenchRunner(Protocol):
    """Scores one bench case, optionally with the candidate skill installed.
    Hosts back this with PipelineRunner + ReproBundle replays."""

    def score(self, case: BenchCase, skill: SkillDef | None) -> float: ...


ScoreFn = Callable[[BenchCase, SkillDef | None], float]


@dataclass
class GateConfig:
    holdout_ratio: float = 0.25
    min_gain: float = 0.0        # dev must strictly beat baseline by more than this
    max_holdout_drop: float = 0.02
    overopt_gap: float = 0.25    # dev-holdout gain divergence alarm
    min_cases: int = 4
    # **승격**에 필요한 홀드아웃 최소 개수. `min_cases` 를 올리는 대신 이걸
    # 둔다 - 케이스가 적은 사람도 재보기는 해야 하고, 다만 그 결과로 `검증됨`
    # 을 붙이지는 않는다.
    #
    # 실측: 케이스 4건이면 홀드아웃이 1건이라, 운 좋게 그 하나만 맞은 스킬이
    # promoted 를 받았다. 같은 스킬이 8건에서는 rejected 다. 이 파일의 다른
    # 주석이 "홀드아웃 1개라 잡음 하나가 판정을 정했다"를 문제로 적어 뒀는데,
    # 최소 설정이 정확히 그 상황이었다.
    min_holdout_to_promote: int = 2
    # 후보 **하나를 재는 데** 쓸 케이스 상한. 자르는 목적이 LLM 채점 비용을
    # 막는 것이고 그 비용은 후보마다 나므로, 자르는 자리도 후보마다여야 한다.
    # 예전에는 모으자마자 잘라서, 관계도 필터가 볼 때는 이미 남은 게 없었다.
    #
    # 기본은 **0(무제한)** 이다. 게이트는 준 것을 재는 물건이고, 얼마나 쓸지는
    # 예산을 쥔 쪽(CLI)이 정한다. 여기에 기본 상한을 박았더니 일부러 만든
    # 케이스 집합을 넘긴 호출자의 dev/holdout 구성이 조용히 바뀌었다.
    max_cases: int = 0
    require_holdout_gain: bool = True
    """A dev-only gain is not evidence of a reusable skill - it is evidence of
    fitting the cases the skill was written from. Verified promotion therefore
    requires the gain to reproduce on held-out cases. When dev improves but
    holdout stays flat the verdict is `staged` (unproven, human may approve),
    not `promoted` and not `rejected`."""


def case_text(case: BenchCase) -> str:
    """이 케이스가 **무엇에 관한 것인지**. 관계도를 재는 재료다."""
    payload = case.payload if isinstance(case.payload, dict) else {}
    # `about` 은 그 케이스가 **무엇을 요구하는지**다. 오류 문구만큼이나 그 케이스가
    # 무엇에 관한 것인지를 말한다 - 인코딩 실패 케이스는 PYTHONIOENCODING 을 요구한다.
    return " ".join(str(payload.get(k, "")) for k in
                    ("tool", "error_detail", "about", "task", "query"))


def described(case: BenchCase) -> str:
    """이 케이스가 **무엇에 관한 것인지 사람 말로** 적힌 부분.

    `case_text` 와 다르다. 저쪽은 짝을 맞출 재료를 다 모으고(요구조건 포함),
    이쪽은 "우리가 이 케이스를 아는가"를 묻는다. 요구가 `200`·`404` 뿐이면
    아는 것이 아니다.
    """
    payload = case.payload if isinstance(case.payload, dict) else {}
    return " ".join(str(payload.get(k, "")) for k in
                    ("tool", "error_detail", "task", "query"))


def asks_for_something(case: BenchCase) -> bool:
    """이 케이스가 **무엇을 해내야 하는지** 적고 있는가.

    금지 조건만 있는 케이스(`Exit code 1` 이 다시 나오면 안 된다)는 스킬이 무엇을
    내놓든 만점이다 - 답변에 오류 문구를 그대로 옮겨 적는 일은 없으니까. 그런
    케이스는 스킬을 가릴 수 없으면서 평균에서는 한 자리를 차지한다.
    """
    payload = case.payload if isinstance(case.payload, dict) else {}
    return bool(str(payload.get("asks", "")).strip())


def relevant_cases(cases: Sequence[BenchCase], topic: str,
                   floor: float = 0.05, symmetric: bool = False) -> list[BenchCase]:
    """그 스킬이 **관계된** 케이스만.

    한 세션의 실패는 서로 무관한 잡탕이다 - Bash 종료코드, TodoWrite JSON 파싱,
    Edit 문자열 불일치, git 128, tsc 오류. git 줄바꿈 스킬이 TodoWrite 파싱
    실패를 도울 리가 없다. 그런데 전부를 평균으로 재고 있었다.

    실측: 초안 넷이 각각 9건 중 0~1건에만 관계됐다. 완벽한 스킬이어도 평균을
    1/9 밖에 못 움직인다. 그래서 "도움이 안 된다"는 판정이 나왔는데, 그 스킬은
    도울 기회를 받은 적이 없다. 틀린 이유는 없는 이유보다 나쁘다.

    관계도는 라우터가 계산한다 - 결정적이고 LLM 을 안 쓴다. 판정에 모델을 쓰지
    않는다는 규율이 여기에도 걸린다.
    """
    from .router import relevance

    topic = (topic or "").strip()
    if not topic:
        return list(cases)
    # 케이스가 **무엇에 관한 것인지 아예 안 적혀 있으면** 거르지 않는다. 모르는
    # 것을 근거로 빼면, 호스트가 준 케이스나 payload 에 글이 없는 케이스가 통째로
    # 사라진다. 모르면 판단하지 않는다는 규율이 여기에도 걸린다.
    #
    # **설명 쪽만 본다.** `about`(요구조건)은 짝을 맞출 때는 쓸모 있지만 "이 케이스가
    # 무엇에 관한 것인지 우리가 아는가"의 답은 아니다. 요구가 `200`·`404` 뿐인
    # 케이스는 설명이 없는 것이지 설명이 있는 것이 아니다 - 그걸 설명으로 치면
    # 아무 주제와도 안 겹쳐서 전부 걸러진다(실측: 합성 케이스 16건이 통째로 사라져
    # 멀쩡한 스킬이 staged 로 떨어졌다).
    if not any(described(c).strip() for c in cases):
        return list(cases)
    # **주제가 앞이다.** `relevance(task, text)` 는 `task` 의 낱말 수로 나눈다.
    # 케이스를 앞에 두면 긴 트레이스백이 통째로 분모가 되어, 오류 문구가 긴
    # 케이스일수록 점수가 눌린다. 실측: `PYTHONIOENCODING` 을 요구하는 케이스가
    # 5건인데 관계된 것으로는 1건만 잡혔다 - 정작 그 낱말이 들어 있는데도.
    #
    # 물어야 할 것은 "이 케이스가 그 주제에 관한 것인가" 이고, 그건 주제의
    # 낱말이 케이스에 얼마나 나오는가다. 분모는 주제여야 한다.
    # **주제가 문장일 때는 반대 방향도 본다**(`symmetric`).
    #
    # 관계도는 `topic` 의 낱말 수로 나눈다. 스킬은 주제가 이름+설명이라 짧아서
    # 잘 맞는데, **기억은 주제가 문장 하나다**. 스물몇 낱말짜리 사실이 짧은 오류
    # 한 줄과 겹치는 것은 한둘뿐이라 늘 문턱 아래로 떨어진다.
    #
    # 그리고 그 편향은 한쪽으로만 작동한다 - 실측:
    #     "cp949 오류가 나면 PYTHONUTF8=1 을 붙인다"     0.077  통과
    #     "cp949 오류가 나면 그냥 무시한다"              0.034  걸러짐
    # 맞는 사실은 고칠 낱말을 품고 있어서 통과하고, **틀린 사실은 그 낱말이
    # 없어서 관련 없다고 걸러진다.** 그러면 자가검증이 상만 주고 벌은 못 준다.
    #
    # 케이스를 분모로 두면 둘 다 통과하고(0.222 · 0.111), 무관한 사실은 그대로
    # 0.000 이다. 어느 쪽이 도움이 되는지는 그 다음에 **재서** 가린다.
    def score(case) -> float:
        forward = relevance(topic, case_text(case))
        if not symmetric:
            return forward
        return max(forward, relevance(case_text(case), topic))

    return [c for c in cases if score(c) > floor]


class ForgeGate:
    def __init__(self, runner: BenchRunner | ScoreFn,
                 config: GateConfig | None = None,
                 constitution=None) -> None:
        self._score: ScoreFn = runner.score if hasattr(runner, "score") else runner  # type: ignore[union-attr]
        self.config = config or GateConfig()
        # 규약(constitution.py). Hermes 는 "배우지 말 것"을 프롬프트 문장으로 두지만
        # 문장은 모델이 지키면 지켜지고 안 지키면 안 지켜진다 - 여기서 집행한다.
        self.constitution = constitution
        # 케이스 묶음 -> 베이스라인 점수. 게이트 하나가 사는 동안만 유효하다
        # (한 번 학습 = 게이트 하나). 프로세스를 넘겨 캐시하지 않는 이유는
        # 채점기가 바뀌면 베이스라인도 바뀌기 때문이다.
        # **케이스 하나마다** 기억한다. 부분집합 단위로 잡으면 여러 후보가 같은
        # 케이스를 공유해도 매번 다시 잰다 - 관계된 케이스만 고르기 시작하면
        # 부분집합이 후보마다 달라지므로 그 낭비가 커진다.
        self._baselines: dict[str, float] = {}

    def _baseline(self, cases: Sequence[BenchCase]) -> float:
        """아무것도 안 넣었을 때의 평균. 케이스마다 **한 번만** 잰다."""
        for case in cases:
            if case.case_id not in self._baselines:
                self._baselines[case.case_id] = self._score(case, None)
        return sum(self._baselines[c.case_id] for c in cases) / len(cases)

    def verify(self, candidate: SkillCandidate, skill: SkillDef,
               cases: Sequence[BenchCase]) -> GateResult:
        reason = safety_check(candidate)
        if reason:
            return GateResult(verdict="rejected", reasons=[f"sec: {reason}"])

        if self.constitution is not None:
            violation = self.constitution.check_candidate(candidate)
            if violation:
                # 벤치를 돌려보기 전에 막는다 - 배우면 안 되는 것은 성능이 좋아도 안 된다.
                return GateResult(verdict="rejected", reasons=[violation])

        if len(cases) < self.config.min_cases:
            return GateResult(
                verdict="staged",
                reasons=[
                    f"unverified: {len(cases)} bench case(s) < min {self.config.min_cases}"
                ],
            )

        # **그 스킬이 관계된 케이스로만 잰다.** 무관한 실패를 평균에 섞으면 아무리
        # 좋은 스킬도 이득이 희석되고, 그 결과가 "도움이 안 된다"로 보고된다.
        # 도울 기회를 안 준 것과 도와 봤는데 안 된 것은 다르다.
        topic = " ".join(x for x in (skill.name, skill.description,
                                     candidate.when_to_use) if x)
        pool = len(cases)
        cases = relevant_cases(cases, topic)
        if self.config.max_cases and len(cases) > self.config.max_cases:
            # **무엇을 해내야 하는지 적힌 케이스를 먼저 채운다.** 금지 조건만 있는
            # 케이스는 스킬이 무엇을 내놓아도 만점이라, 자리를 차지하면서 평균만
            # 눅인다(실측: 관계된 12건 중 5건이 그랬고, 진짜 이득이 12로 나뉘었다).
            # 그렇다고 버리지는 않는다 - 스킬이 실패를 되풀이하게 만드는지는 그
            # 케이스만 잡아낸다. 순서만 바꾸고, 남는 자리에 채운다.
            asks = [c for c in cases if asks_for_something(c)]
            guards = [c for c in cases if not asks_for_something(c)]
            room = self.config.max_cases
            # 고르게 솎는다. 앞에서부터 자르면 한 세션 것만 남는다.
            if len(asks) > room:
                step = len(asks) / room
                asks = [asks[int(i * step)] for i in range(room)]
            cases = asks + guards[:room - len(asks)]
        if len(cases) < self.config.min_cases:
            return GateResult(
                verdict="staged",
                reasons=[f"관계된 실패가 {len(cases)}건뿐입니다(전체 {pool}건 중, "
                         f"최소 {self.config.min_cases}건 필요). 못 쟀다는 뜻이지 "
                         f"도움이 안 된다는 뜻이 아닙니다 - 같은 실패가 더 쌓이면 "
                         f"그때 판정합니다"],
            )

        dev, holdout = split_holdout(cases, self.config.holdout_ratio)
        if not dev or not holdout:
            return GateResult(verdict="staged",
                              reasons=["unverified: degenerate dev/holdout split"])

        # 케이스마다 (넣기 전, 넣은 뒤) 를 **짝으로** 들고 있는다. 평균만 내면
        # "몇 건이 갈렸는가" 를 영영 못 묻는다 - 그게 우연 여부를 판단하는
        # 유일한 재료다(`sign_test` 주석 참조).
        paired: dict[str, list[tuple[float, float]]] = {"dev": [], "holdout": []}

        def mean(cs: Sequence[BenchCase], skill_def: SkillDef | None,
                 bucket: str = "") -> float:
            total = 0.0
            for case in cs:
                got = self._score(case, skill_def)
                total += got
                if bucket:
                    paired[bucket].append((self._baselines[case.case_id], got))
            return total / len(cs)

        # 베이스라인은 **후보와 무관하다.** 아무것도 안 넣었을 때의 점수라
        # 케이스가 같으면 답도 같다. 그런데 후보마다 처음부터 다시 쟀다.
        # 실측: 스킬 4건 · 케이스 12건에서 LLM 96회 중 36회가 바이트 단위로
        # 똑같은 프롬프트였다(38%). 같은 질문을 서른여섯 번 더 한 셈이다.
        try:
            baseline_dev = self._baseline(dev)
            baseline_holdout = self._baseline(holdout)
            dev_score = mean(dev, skill, "dev")
            holdout_score = mean(holdout, skill, "holdout")
        except Unmeasurable as why:
            # **못 잰 것을 "도움이 안 된다"로 내지 않는다.** 예산이 떨어지면 재생이
            # 전부 빈 답이 되고, 그러면 넣으나 빼나 같은 점수라 `+0.000` 이 나온다.
            # 실측: 큰 세션 하나에서 LLM 호출 5회로 후보 5건을 전부 그렇게 거절했다.
            # 한 번도 재 보지 않고 확신에 찬 판정을 내는 것이 이 게이트가 막으려는
            # 바로 그것이다.
            return GateResult(verdict="staged",
                              reasons=[f"못 쟀습니다: {why}. 도움이 안 된다는 뜻이 "
                                       f"아닙니다 - 상한을 올리거나 다음 사이클에 "
                                       f"다시 잽니다"])

        dev_gain = dev_score - baseline_dev
        holdout_gain = holdout_score - baseline_holdout

        dev_up = dev_gain > self.config.min_gain
        held_ok = holdout_gain >= -self.config.max_holdout_drop
        overopt = (dev_gain - holdout_gain) > self.config.overopt_gap

        reasons = [
            f"dev {baseline_dev:.3f}->{dev_score:.3f} ({dev_gain:+.3f})",
            f"holdout {baseline_holdout:.3f}->{holdout_score:.3f} ({holdout_gain:+.3f})",
        ]
        # **한 케이스어치보다 작은 차이는 잴 수 없는 차이다.** dev 가 9건이면 한
        # 케이스가 0.111 을 움직인다. 그보다 작은 `+0.056` 을 이득이라고 적으면
        # 숫자가 있으니 잰 것처럼 보이는데, 실은 어느 케이스가 뽑혔느냐만 말한다.
        #
        # 실측(같은 스킬·같은 풀, 케이스 수만 바꿈):
        #     wrong-working-directory   12건 +0.056 -> 28건 -0.016
        #     safe-unique-string-repl   12건 +0.056 -> 28건 -0.016
        #     sanitize-git-staging      12건 +0.093 -> 28건 +0.000
        #
        # 판정은 안 바꾼다 - 홀드아웃이 이미 이런 것을 걸러 냈고, 문턱을 올리면
        # 진짜로 작은 개선까지 같이 죽는다. 다만 **얼마나 거친 자로 쟀는지**는
        # 말해야 한다. 사람이 케이스를 더 모을지 정할 수 있어야 한다.
        # **평균 옆에 "우연인가" 를 같이 적는다.** 평균만 보면 `+0.056` 이
        # 이득으로 보이는데, 아홉 건 중 한 건만 갈린 것이면 그건 동전 던지기다
        # (p=0.5). 실측으로 그 숫자가 케이스를 늘리자 `-0.016` 으로 뒤집혔다.
        better, worse, p_value = sign_test(paired["dev"])
        reasons.append(f"dev 갈림 {better}↑/{worse}↓ (p={p_value:.3f})")
        step = 1.0 / len(dev)
        if abs(dev_gain) < step:
            reasons.append(f"자가 거칩니다 - dev {len(dev)}건이라 한 건이 "
                           f"{step:.3f} 를 움직입니다(그보다 작은 차이입니다). "
                           f"--max-bench 를 올리면 또렷해집니다")
        # **감춘 것에서도 "더 많이" 좋아져야 한다 - 평균만으로는 모자란다.**
        #
        # 평균은 크기에 휘둘린다. 한 건이 크게 좋아지고 다른 한 건이 조금
        # 나빠지면 평균은 양수가 되는데, 갈린 방향으로 보면 1↑/1↓ 즉 동전
        # 던지기다. 실측으로 그 상황을 만들어 보니 `holdout +0.100` 으로
        # **promoted** 가 나왔다(부호검정 p=0.750, 동전보다 못하다). 그걸
        # `검증됨` 이라 부르고 남의 에이전트에 MCP 로 내주는 것이 이 물건이
        # 막으려던 바로 그것이다.
        #
        # 문턱은 **가장 약한 형태**로만 건다: "좋아진 건수 > 나빠진 건수".
        # p<0.05 같은 문턱을 걸지 않은 이유는 오늘 이미 한 번 배웠기 때문이다 -
        # 케이스가 적을 때 엄격한 문턱은 진짜로 작은 개선까지 같이 죽인다
        # (그날 유일한 자동 승격이 dev +0.037 이었다). 다수결은 그보다 훨씬
        # 약한 요구이고, 진짜 개선이라면 당연히 넘는다.
        h_better, h_worse, h_p = sign_test(paired["holdout"])
        reasons.append(f"holdout 갈림 {h_better}↑/{h_worse}↓ (p={h_p:.3f})")

        if not held_ok or overopt:
            holdout_state = HOLDOUT_REGRESSED
        elif self.config.require_holdout_gain and holdout_gain <= 0:
            holdout_state = HOLDOUT_UNPROVEN
        elif (h_better + h_worse) > 0 and h_worse >= h_better:
            # **갈린 것이 있을 때만** 다수결을 묻는다. 전부 비긴 경우(0↑/0↓)는
            # 다른 상황이고 `require_holdout_gain` 이 이미 다룬다 - 그것까지
            # 여기서 막으면 그 옵션을 끈 사람에게 옵션이 없어진다(시험이 잡았다).
            holdout_state = HOLDOUT_UNPROVEN
            reasons.append("감춘 것에서 좋아진 건수가 나빠진 건수를 못 넘었습니다"
                           " - 평균이 양수인 것은 한쪽이 크게 움직였기 때문입니다")
        else:
            holdout_state = HOLDOUT_CONFIRMED
        # 홀드아웃이 얇으면 **못 쟀다**고 본다. 재긴 쟀지만 근거가 하나뿐인
        # 것을 `검증됨` 이라고 부르지 않는다 - 그게 이 물건의 전부다.
        thin_holdout = len(holdout) < self.config.min_holdout_to_promote
        if thin_holdout and holdout_state == HOLDOUT_CONFIRMED:
            holdout_state = HOLDOUT_UNPROVEN
        verdict = decide(measured=True, dev_ok=dev_up, holdout=holdout_state)

        if verdict == "staged":
            reasons.append(
                f"홀드아웃이 {len(holdout)}건뿐이라 승격 보류 "
                f"(최소 {self.config.min_holdout_to_promote}건, "
                f"케이스 {len(cases)}건을 {self.config.min_cases * 2}건 이상으로 "
                "늘리면 판정합니다)" if thin_holdout else
                "dev gain did not reproduce on held-out cases - "
                "unproven, not auto-verified")
        elif verdict == "rejected":
            reasons.append("no dev gain - the skill does not help" if not dev_up
                           else "holdout regressed - memorization, not knowledge"
                           if not held_ok
                           else "over-optimization gap - dev gain does not generalize")
        return GateResult(
            verdict=verdict,
            reasons=reasons,
            dev_score=dev_score,
            holdout_score=holdout_score,
            baseline_dev=baseline_dev,
            baseline_holdout=baseline_holdout,
        )

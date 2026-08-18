"""여러 단계를 밟아 하나의 질문을 끝낸다.

지적(재현함): `ask` 는 한 번에 한 능력만 썼다. 고르고, 입력을 뽑고, 부르고, 끝.
결과를 **다시 보지 않으니** 두 도구가 필요한 일은 사용자가 직접 두 번 물어야 했고,
첫 도구가 엉뚱한 것을 돌려줘도 그대로 답이 됐다. "쿼리 하나로 다 된다"는 말이
"도구 하나로 끝나는 일에 한해서"라는 뜻이었다.

여기가 그 자리다. 한 단계가 끝나면 **그 결과를 보고** 다음을 정한다. 끝났으면
끝났다고 말하고 멈춘다.

지키는 선 - 이게 없으면 그냥 폭주하는 루프다:

- **있는 것 중에서만 고른다.** 모델이 목록에 없는 이름을 대면 거기서 멈춘다.
  없는 도구를 부르는 시늉을 하느니 못 한다고 말하는 편이 낫다.
- **읽기 전용이 아니면 물어본다.** 한 번에 한 단계씩 보여 주고 승인을 받는다.
  사람이 미리 전부를 볼 수 없는 것이 여러 단계의 본질이라, 승인도 그때그때여야
  한다. 미리 다 받아 두는 승인은 승인이 아니다.
- **반드시 끝난다.** 단계 상한과 예산을 둘 다 본다. 같은 능력을 같은 입력으로 두
  번 부르면 헛돌고 있는 것이라 거기서 멈춘다.
- **실패도 다음 단계에 보인다.** 무엇을 넣었다 왜 깨졌는지가 그대로 들어가야 다른
  길을 찾는다. 이 물건이 학습 재료로 제일 값지게 치는 것도 같은 쌍이다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable

__all__ = ["Episode", "Offer", "Step", "next_step", "run_episode"]


@dataclass
class Offer:
    """다음 단계에 쓸 수 있는 능력 하나. 모델에게 보여 줄 만큼만 담는다."""

    name: str
    description: str
    risk: str = "safe"
    read_only: bool = True


@dataclass
class Step:
    name: str
    payload: dict = field(default_factory=dict)
    ok: bool = False
    output: object = None
    error: str = ""
    seconds: float = 0.0
    why: str = ""

    def line(self) -> str:
        head = f"{self.name}({json.dumps(self.payload, ensure_ascii=False)[:120]})"
        if self.ok:
            return f"{head} -> {json.dumps(self.output, ensure_ascii=False)[:200]}"
        return f"{head} -> 실패: {self.error[:200]}"


@dataclass
class Episode:
    query: str
    steps: list[Step] = field(default_factory=list)
    stopped: str = ""          # 왜 멈췄는지. 빈 값이면 아직 안 끝났다는 뜻이다

    @property
    def ok(self) -> bool:
        return bool(self.steps) and self.steps[-1].ok

    def transcript(self) -> str:
        """지금까지 무엇을 했는지. 다음 판단의 재료이자 사람에게 보여 줄 기록."""
        if not self.steps:
            return "(아직 한 것 없음)"
        return "\n".join(f"{i}. {s.line()}" for i, s in enumerate(self.steps, 1))


_DECIDE_PROMPT = """사용자의 요청을 끝내려면 다음에 무엇을 해야 하는지 정하세요.

요청: {query}

지금까지 한 것:
{done}

쓸 수 있는 것(이 목록에 **있는 이름만** 쓸 수 있습니다):
{offers}

규칙:
- 요청이 이미 충족됐으면 {{"done": true, "answer": "사용자에게 할 답"}}
- 더 해야 하면 {{"next": "<위 목록의 이름>", "payload": {{...}}, "why": "한 줄"}}
- 목록에 없는 것이 필요하면 {{"done": true, "answer": "무엇이 없어서 못 하는지"}}
- 값을 지어내지 마세요. 모르면 없다고 답하세요.

JSON 만 출력하세요."""


def next_step(complete: Callable[[str], str], query: str, episode: Episode,
              offers: list[Offer]) -> dict:
    """다음에 무엇을 할지 묻는다. 반환은 `{done,answer}` 또는 `{next,payload,why}`.

    답이 목록 밖을 가리키면 **여기서 걸러 낸다.** 없는 도구를 부르는 시늉을 하느니
    못 한다고 말하는 편이 낫다.
    """
    listing = "\n".join(
        f"- {o.name}: {o.description[:120]}"
        + ("" if o.read_only else f"  [{o.risk} · 읽기전용 아님]")
        for o in offers) or "(없음)"
    raw = complete(_DECIDE_PROMPT.format(
        query=query, done=episode.transcript(), offers=listing))

    plan = _as_json(raw)
    if plan is None:
        return {"done": True, "answer": "다음 단계를 정하지 못했습니다(응답이 JSON 이 아님)."}
    if plan.get("done"):
        return {"done": True, "answer": str(plan.get("answer") or "")}

    name = str(plan.get("next") or "")
    if name not in {o.name for o in offers}:
        # 목록 밖이다. 있는 척하지 않는다.
        return {"done": True,
                "answer": f"'{name}' 이 필요한데 쓸 수 있는 것 중에 없습니다."}
    payload = plan.get("payload")
    return {"next": name,
            "payload": payload if isinstance(payload, dict) else {},
            "why": str(plan.get("why") or "")}


def _as_json(raw: str) -> dict | None:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.split("```")[1] if "```" in text[3:] else text[3:]
        text = text.split("\n", 1)[-1] if text[:4].lower().startswith("json") else text
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def run_episode(query: str, *, decide, execute, offers_for,
                approve=None, max_steps: int = 4,
                on_step=None, check_budget=None) -> Episode:
    """끝날 때까지 - 다만 **반드시 끝난다.**

    `decide(episode, offers)`  다음에 무엇을 할지
    `execute(name, payload)`   실제로 한다 -> `Step`
    `offers_for(episode)`      지금 쓸 수 있는 것(단계마다 다시 묻는다 - 앞 단계가
                               새 능력을 만들었을 수 있다)
    `approve(name, payload, offer)`  읽기 전용이 아닐 때 사람에게. None 이면 거절로
                               본다. 승인을 안 받는 길을 기본값으로 두지 않는다.
    """
    episode = Episode(query=query)
    seen: set[tuple[str, str]] = set()

    for _ in range(max_steps):
        if check_budget is not None:
            try:
                check_budget()
            except Exception as exc:          # BudgetExceeded 등
                episode.stopped = f"예산: {exc}"
                return episode

        offers = offers_for(episode)
        plan = decide(episode, offers)
        if plan.get("done"):
            episode.stopped = plan.get("answer") or "끝났습니다."
            return episode

        name = plan["next"]
        payload = plan.get("payload") or {}
        offer = next((o for o in offers if o.name == name), None)

        # 같은 것을 같은 입력으로 또 부르면 헛돌고 있는 것이다. 상한까지 기다리면
        # 그만큼 시간과 토큰을 버린다.
        signature = (name, json.dumps(payload, ensure_ascii=False, sort_keys=True))
        if signature in seen:
            episode.stopped = f"같은 것을 반복하고 있습니다: {name}"
            return episode
        seen.add(signature)

        if offer is not None and not offer.read_only:
            # 미리 다 받아 두는 승인은 승인이 아니다. 그때그때 묻는다.
            granted = approve(name, payload, offer) if approve else False
            if not granted:
                episode.stopped = f"승인을 받지 못해 멈췄습니다: {name}"
                return episode

        step = execute(name, payload)
        step.why = plan.get("why", "")
        episode.steps.append(step)
        if on_step:
            on_step(step)

    episode.stopped = f"단계 상한 {max_steps}회에 닿았습니다."
    return episode

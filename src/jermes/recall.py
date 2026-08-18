"""SF5 recall side - progressive disclosure over the ledger.

Mirrors the engine skill_registry contract (name -> short description, body on
demand) so absorption is a thin `LedgerSkillSource` added to
tools/skill_registry.py. Two-step PD: list (metadata only) -> view (body).

Discipline: viewing/loading is NEVER recorded as a positive signal. Hosts call
`record_run_outcome` once per finished run with the set of skills that were
loaded and whether the run succeeded.
"""

from __future__ import annotations

from dataclasses import dataclass

from .ledger import SkillLedger
from .model import verified_mark


@dataclass
class SkillListing:
    name: str
    kind: str
    description: str
    version: str
    verified: bool
    rank: float


class LedgerSkillSource:
    def __init__(self, ledger: SkillLedger, scope: str | None = None,
                 include_staged: bool = False) -> None:
        self.ledger = ledger
        self.scope = scope
        self.include_staged = include_staged

    def list(self) -> list[SkillListing]:
        listings: list[SkillListing] = []
        statuses = ("active", "staged") if self.include_staged else ("active",)
        for status in statuses:
            for record in self.ledger.list(scope=self.scope, status=status):
                listings.append(
                    SkillListing(
                        name=record.name,
                        kind=record.skill.kind,
                        description=record.description,
                        version=record.skill.version,
                        verified=record.skill.verified,
                        rank=record.rank_score(),
                    )
                )
        return sorted(listings, key=lambda item: (-item.rank, item.name))

    def view(self, name: str) -> str:
        """Raw body only - this feeds the prompt-injection path (skill_registry
        get_skill_body), so no annotation header may precede the frontmatter."""
        record = self.ledger.get(name)
        if record is None:
            raise KeyError(f"unknown skill {name!r}")
        return record.skill.body

    def view_annotated(self, name: str) -> str:
        """Human-facing variant with the verification label (console use)."""
        record = self.ledger.get(name)
        if record is None:
            raise KeyError(f"unknown skill {name!r}")
        # 딱지 어휘는 `model.verified_mark` 한 곳이 정한다.
        header = (f"[{verified_mark(record.skill.verified)}] {record.name} "
                  f"v{record.skill.version} ({record.skill.kind})\n")
        return header + record.skill.body

    def render_index(self, limit: int = 20) -> str:
        """Compact index for prompt injection (s03) - metadata only."""
        lines = []
        for item in self.list()[:limit]:
            lines.append(f"[{verified_mark(item.verified)}] {item.name} "
                         f"v{item.version} - {item.description}")
        return "\n".join(lines)

    def record_run_outcome(self, loaded_skills: list[str], success: bool,
                           task: str = "") -> None:
        """실행 결과를 원장에 되먹인다.

        `task` 를 같이 넘기면 성공한 과제 문장이 그 스킬의 이력으로 쌓인다. 다음에
        비슷한 과제가 오면 그게 찾는 근거가 된다 - E9 실측에서 이 이력이 라우팅을
        갈랐다(실사용 질문 top-5 43.8% -> 97.9%). 안 넘기면 예전처럼 횟수만 센다.
        """
        self.ledger.record_outcome(loaded_skills, success, task)

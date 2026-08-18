"""SF5 - the skill ledger: versioned, provenance-carrying skill records.

Protocol + two reference backends (in-memory, JSONL append-only). The 플랫폼
host maps this onto the State Spine (`spine_type=skill_def`) - same shape:
every mutation is an append with version + provenance, state is a fold.
"""

from __future__ import annotations

import json
import os
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Protocol

from .model import (
    SkillDef,
    UsageStats,
    bump_patch,
)


@dataclass
class SkillRecord:
    skill: SkillDef
    usage: UsageStats = field(default_factory=UsageStats)
    history: list[str] = field(default_factory=list)  # "<version>: <note>"

    @property
    def name(self) -> str:
        return self.skill.name

    @property
    def status(self) -> str:
        return self.skill.status

    @property
    def description(self) -> str:
        return self.skill.description

    def rank_score(self) -> float:
        """Recall ordering: verified beats unverified, then Wilson lower bound.
        Loading is never counted - only run outcomes feed `usage`."""
        base = 0.5 if self.skill.verified else 0.0
        return base + self.usage.wilson_lower()


# 스킬 하나가 들고 다닐 과제 이력 상한.
MAX_EXAMPLES = 60


class SkillLedger(Protocol):
    def get(self, name: str) -> SkillRecord | None: ...
    def list(self, scope: str | None = None,
             status: str | None = None) -> list[SkillRecord]: ...
    def commit(self, skill: SkillDef, note: str = "") -> SkillRecord: ...
    def set_status(self, name: str, status: str, note: str = "") -> SkillRecord: ...
    def record_outcome(self, names: Iterable[str], success: bool,
                       task: str = "") -> None: ...


class InMemorySkillLedger:
    def __init__(self) -> None:
        self._records: dict[str, SkillRecord] = {}

    def get(self, name: str) -> SkillRecord | None:
        return self._records.get(name)

    def list(self, scope: str | None = None,
             status: str | None = None) -> list[SkillRecord]:
        records = self._records.values()
        if scope is not None:
            records = [r for r in records if r.skill.scope == scope]
        if status is not None:
            records = [r for r in records if r.status == status]
        return sorted(records, key=lambda r: (-r.rank_score(), r.name))

    def commit(self, skill: SkillDef, note: str = "") -> SkillRecord:
        existing = self._records.get(skill.name)
        if existing is not None:
            prior = existing.skill
            skill.version = bump_patch(prior.version)
            skill.supersedes = f"{prior.name}@{prior.version}"
            existing.skill = skill
            existing.history.append(f"{skill.version}: {note or 'update'}")
            return existing
        record = SkillRecord(skill=skill, history=[f"{skill.version}: {note or 'create'}"])
        self._records[skill.name] = record
        return record

    def set_status(self, name: str, status: str, note: str = "") -> SkillRecord:
        record = self._records[name]
        record.skill.status = status
        record.history.append(f"{record.skill.version}: status={status} {note}".rstrip())
        return record

    def record_outcome(self, names: Iterable[str], success: bool,
                       task: str = "") -> None:
        for name in names:
            record = self._records.get(name)
            if record is None:
                continue
            if success:
                record.usage.successes += 1
            else:
                record.usage.failures += 1
            # 성공한 과제 문장을 남긴다 - 다음에 비슷한 과제가 오면 이 스킬을 찾는
            # 근거가 된다. E9 실측에서 이 이력이 라우팅을 갈랐다(top-5 43.8% -> 97.9%).
            # 실패한 과제는 안 남긴다. "이걸 못 했다"를 "이걸 한다"로 광고하는 꼴이다.
            if success and task.strip():
                examples = record.skill.meta.setdefault("examples", [])
                trimmed = task.strip()[:300]
                if trimmed not in examples:
                    examples.append(trimmed)
                    # 오래된 것부터 버린다 - 무한히 쌓이면 프롬프트도 색인도 부담이다.
                    del examples[:-MAX_EXAMPLES]

    def sweep_deprecate(self, min_uses: int = 5, max_wilson: float = 0.2) -> list[str]:
        """Low-signal prune (4-scope routine #5): enough real outcomes, and the
        Wilson lower bound still under the floor -> deprecate, never delete."""
        deprecated = []
        for record in self._records.values():
            if record.status != "active":
                continue
            if record.usage.total >= min_uses and record.usage.wilson_lower() <= max_wilson:
                self.set_status(record.name, "deprecated", "low-signal sweep")
                deprecated.append(record.name)
        return deprecated


# 잠금을 기다리는 한계. 넘으면 잠금 없이 쓰되, 그건 잃을 수 있다는 뜻이다.
_LOCK_SECONDS = 30.0


@contextmanager
def _exclusive(handle: int):
    """파일 하나를 프로세스 사이에서 독점한다. 표준 라이브러리만 쓴다.

    잠글 수 없는 파일계(일부 네트워크 드라이브)에서도 **기록은 되어야 한다** -
    잠금은 줄이 섞이지 않게 하려는 것이지, 기록을 막으려는 것이 아니다.
    """
    locked = False
    deadline = time.monotonic() + _LOCK_SECONDS
    while True:
        try:
            if os.name == "posix":
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_EX)
            else:
                import msvcrt

                os.lseek(handle, 0, os.SEEK_SET)
                msvcrt.locking(handle, msvcrt.LK_LOCK, 1)
            locked = True
            break
        except OSError:
            # **못 잡았다고 조용히 넘어가지 않는다.** 넘어가면 잠금 없이 쓰게 되고,
            # 그게 정확히 잃어버리는 자리다(실측: 동시 6건에서 한 건이 사라졌다).
            # 윈도우 `LK_LOCK` 은 10초 뒤 포기하므로 우리가 더 기다린다.
            if time.monotonic() >= deadline:
                # 여기까지 왔으면 쓰기는 한다 - 안 쓰는 것보다 낫다. 그러나
                # **말은 하고 쓴다.** 조용히 잃는 것이 오늘 내내 고친 그 패턴이고,
                # 잠금 없이 쓴 순간은 잃을 수 있는 순간이다.
                sys.stderr.write(
                    f"[jermes] 파일 잠금을 {_LOCK_SECONDS:.0f}초 동안 못 잡아 "
                    "그냥 씁니다 - 같은 원장을 동시에 쓰는 프로세스가 많으면 "
                    "이 줄이 사라질 수 있습니다." + chr(10))
                break
            time.sleep(0.05)
    try:
        yield
    finally:
        if not locked:
            return
        try:
            if os.name == "posix":
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_UN)
            else:
                import msvcrt

                os.lseek(handle, 0, os.SEEK_SET)
                msvcrt.locking(handle, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass


class JsonlSkillLedger(InMemorySkillLedger):
    """Append-only JSONL journal + in-memory fold. Crash-safe enough for the
    research phase; the production backend is the State Spine."""

    def __init__(self, path: str | Path) -> None:
        super().__init__()
        self.path = Path(path)
        if self.path.exists():
            self._replay()

    def _append(self, kind: str, payload: dict) -> None:
        """한 줄을 **잃지 않고** 덧붙인다.

        원장은 여러 프로세스가 같이 쓴다 - `watch` 가 도는 중에 손으로 `learn` 을
        돌리는 것이 문서에 적힌 사용법이다. 실측(같은 원장에 동시 8건): 여덟
        프로세스가 모두 "들여왔습니다" 라고 보고했는데 원장에는 다섯~일곱 줄만
        남았다. **성공이라 말하고 잃는 것**이 가장 나쁜 실패다.

        `O_APPEND` 로는 모자랐다(같은 시험에서 7·7·8). 윈도우 CRT 는 덧붙이기를
        `seek(끝) + write` 로 흉내 내서 두 프로세스가 같은 자리를 잡는다. 그래서
        **파일 잠금**을 건다 - POSIX 는 `fcntl.flock`, 윈도우는 `msvcrt.locking`.
        둘 다 표준 라이브러리다(이 패키지는 의존성이 없다).
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = (json.dumps({"kind": kind, **payload}, ensure_ascii=False)
                + chr(10)).encode("utf-8")
        handle = os.open(self.path, os.O_WRONLY | os.O_CREAT, 0o644)
        try:
            with _exclusive(handle):
                os.lseek(handle, 0, os.SEEK_END)
                written = 0
                while written < len(line):
                    written += os.write(handle, line[written:])
        finally:
            os.close(handle)

    #: 재생하면서 못 읽고 넘어간 줄 수. 조용히 넘기지 않으려고 남긴다.
    skipped_lines: int = 0

    def _replay(self) -> None:
        """**깨진 줄 하나에 원장 전체를 잃지 않는다.**

        예전에는 `json.loads` 가 그대로 터져서 `jermes list` 조차 안 됐다. 원장은
        이 물건의 1차 저장소라, 못 읽으면 사용자는 자기가 뭘 배웠는지 볼 방법이
        없다. 기억 쪽은 이미 깨진 줄을 버리고 있었는데(`load_memory`) 정작 더
        중요한 쪽이 안 그랬다.

        찢어진 줄은 가상의 사고가 아니다 - 이 파일은 여러 프로세스가 같이 쓰고,
        잠금을 걸기 전에는 실제로 줄이 사라졌다.

        **버린 것은 센다.** 조용히 넘기면 "그 스킬을 안 배웠다"와 "그 줄을 못
        읽었다"가 같아 보이고, 그 둘은 사람이 할 일이 다르다.
        """
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                    kind = event.pop("kind")
                except (ValueError, KeyError):
                    self.skipped_lines += 1
                    continue
                if kind == "commit":
                    data = event["skill"]
                    provenance = data.pop("provenance", None)
                    skill = SkillDef(**{**data, "provenance": None})
                    if provenance:
                        from .model import Provenance
                        skill.provenance = Provenance(**provenance)
                    super().commit(skill, note=event.get("note", ""))
                    # replay keeps journal authority over derived version fields
                    record = super().get(skill.name)
                    if record is not None:
                        record.skill.version = data.get("version", record.skill.version)
                elif kind == "status":
                    super().set_status(event["name"], event["status"], event.get("note", ""))
                elif kind == "outcome":
                    # 과제 문장까지 되살린다. 안 되살리면 재기동할 때마다 라우팅
                    # 근거가 사라져서 어제 잘 찾던 스킬을 오늘 못 찾는다.
                    super().record_outcome([event["name"]], event["success"],
                                           event.get("task", ""))

    def versions(self, name: str) -> list[tuple[str, dict]]:
        """그 스킬의 **지나간 판본들**, 오래된 순. 원장이 append-only 라 다 남아 있다.

        폴드된 원장은 마지막 판본만 들고 있다. 그런데 되돌리려면 이전 것이 있어야
        하고, 그건 메모리가 아니라 저널에 있다 - 읽으면 된다.
        """
        found: list[tuple[str, dict]] = []
        if not self.path.exists():
            return found
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("kind") != "commit":
                    continue
                data = event.get("skill") or {}
                if data.get("name") == name:
                    found.append((str(data.get("version", "")), data))
        return found

    def rollback(self, name: str, version: str = "") -> SkillRecord | None:
        """지난 판본을 **다시 커밋한다**. 지우지 않는다.

        되돌리기를 이력 삭제로 구현하면 "그때 무엇이 있었나" 를 영영 못 묻는다.
        이 원장의 규율은 그 반대다 - 되돌린 것도 하나의 사건으로 남긴다. 그래서
        되돌린 뒤에도 되돌리기 전으로 다시 갈 수 있다.

        `version` 이 없으면 **바로 이전 판본**으로 간다.
        """
        past = self.versions(name)
        if len(past) < 2:
            return None
        if version:
            picked = next((data for ver, data in past if ver == version), None)
        else:
            picked = past[-2][1]
        if picked is None:
            return None
        data = dict(picked)
        was = data.pop("version", "")
        provenance = data.pop("provenance", None)
        skill = SkillDef(**{**data, "provenance": None})
        if provenance:
            from .model import Provenance
            skill.provenance = Provenance(**provenance)
        return self.commit(skill, note=f"rollback -> {was}")

    def commit(self, skill: SkillDef, note: str = "") -> SkillRecord:
        record = super().commit(skill, note)
        self._append("commit", {"skill": record.skill.to_dict(), "note": note})
        return record

    def set_status(self, name: str, status: str, note: str = "") -> SkillRecord:
        record = super().set_status(name, status, note)
        self._append("status", {"name": name, "status": status, "note": note})
        return record

    def record_outcome(self, names: Iterable[str], success: bool,
                       task: str = "") -> None:
        names = list(names)
        super().record_outcome(names, success, task)
        for name in names:
            if super().get(name) is not None:
                self._append("outcome", {"name": name, "success": success,
                                         "task": task})

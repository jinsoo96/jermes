"""SF8 - 규약(constitution). Hermes 의 SOUL.md 에 대응하되, 텍스트가 아니라 **집행**이다.

Hermes 는 페르소나를 `SOUL.md` 로 이어가고 "학습하지 말 것" 규칙을 스킬 프롬프트 안에
문장으로 둔다. 문장으로 둔 규칙은 모델이 지키면 지켜지고 안 지키면 안 지켜진다 -
그리고 페르소나 파일은 에이전트가 스스로 고치므로 **조용히 표류**한다(그 표류를 알
방법이 없다는 게 진짜 문제다).

여기서는 세 가지를 다르게 한다.

1. **금지는 게이트가 집행한다.** `never_learn` 은 프롬프트가 아니라 `check_candidate()`
   로 걸러진다. 모델의 선의에 기대지 않는다.
2. **규약은 에이전트가 못 고친다.** `propose()` 는 **차이만** 돌려준다. 적용은 사람이
   `adopt()` 를 부르는 것으로만 일어나고 이력이 남는다.
3. **표류가 보인다.** `diff()` 가 무엇이 언제 바뀌었는지 줄 단위로 말한다.

파일 형식은 agentskills 프론트매터와 같은 모양이라(`---` YAML + 본문) 다른 도구가
읽어도 깨지지 않는다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Sequence

from .model import SkillCandidate

DEFAULT_IDENTITY = "Jermes"
DEFAULT_ROLE = "끝난 실행에서 재사용 가능한 절차를 배우고, 검증된 것만 남기는 큐레이터"


# ── 규약보다 아래에 있는 바닥 ────────────────────────────────────────────────
#
# `never_learn` 은 사람이 고치는 목록이라 낱말 위주다. 그것만으로는 **모양이
# 비밀인 것**을 못 잡는다. 실측: 기본 규약에서 아래가 전부 통과했다.
#
#     Bearer eyJhbGciOiJIUzI1NiJ9.abc.def
#     AKIAIOSFODNN7EXAMPLE
#     관리자 암호는 hunter2      ("암호"는 목록에 없다)
#     the admin credential is hunter2
#     주민등록번호 900101-1234567
#
# 게다가 같은 판단을 `curator._SECRET_PATTERNS` 가 따로 하고 있었고, **기억은
# 약한 쪽(낱말)만 거쳤다.** 스킬은 두 겹으로 막히는데 기억은 한 겹이었다는 뜻이다.
#
# 그래서 모양 기반 검사를 여기 한 자리로 모으고, 이것은 **규약 파일을 고쳐서
# 끌 수 없게** 한다. 무엇을 배울지는 사람이 정하지만, 자격증명을 배우지 않는 것은
# 정하고 말고 할 문제가 아니다.
SECRET_SHAPES = [
    # 라틴 낱말은 **값이 대입된 모양**일 때만. 낱말만 나오는 것은 자격증명이 아니라
    # 그 낱말을 논하는 문장이다("secret 이라는 낱말 자체를"). 그건 사람이 고치는
    # never_learn 이 판단할 몫이고, 바닥이 강제할 일이 아니다.
    (re.compile(r"(?i)\b(api[_-]?key|secret|token|passwd|password|credentials?)\b"
                r"\s*[:=]\s*\S{4,}"), "자격증명 대입"),
    # 한글은 `\b` 를 못 쓴다. `암호는` 은 뒤가 조사(단어문자)라 경계가 없다.
    # 조사를 허용하고 뒤에 값이 오는지를 본다.
    (re.compile(r"(암호|비밀번호|자격\s*증명)\s*(?:는|은|이|가)?\s*[:=]?\s*"
                r"[A-Za-z0-9!@#$%^&*_+\-]{4,}"), "자격증명 대입(한국어)"),
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}"), "OpenAI 형식 키"),
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}"),
     "GitHub 토큰"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS 액세스 키"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._-]{20,}"), "Bearer 토큰"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\."), "JWT"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "개인키"),
    (re.compile(r"\bpypi-[A-Za-z0-9_-]{20,}"), "PyPI 토큰"),
    # 주민등록번호: 6자리 - 성별자리(1~4) + 6자리.
    (re.compile(r"\b\d{6}-[1-4]\d{6}\b"), "주민등록번호"),
    # 카드번호: 4자리 묶음 넷. 버전(1.2.3)이나 포트범위(8000-8003)와 안 겹친다.
    (re.compile(r"\b\d{4}[ -]\d{4}[ -]\d{4}[ -]\d{4}\b"), "카드번호로 보이는 숫자"),
]


def secret_shape(text: str) -> str:
    """자격증명·식별번호처럼 **모양만으로 아는** 것. 없으면 빈 문자열.

    여기서만 정한다. 두 곳에서 정하면 한쪽만 고쳐지고, 안 고친 쪽으로 샌다.
    """
    for pattern, label in SECRET_SHAPES:
        if pattern.search(text or ""):
            return label
    return ""


@dataclass
class Constitution:
    """에이전트가 스스로 바꿀 수 없는 부분."""

    identity: str = DEFAULT_IDENTITY
    role: str = DEFAULT_ROLE
    principles: list[str] = field(default_factory=lambda: [
        "검증되지 않은 것을 검증된 것처럼 제시하지 않는다.",
        "증거 없이 지우지 않는다 - 내려갈 때도 이력과 함께 남긴다.",
        "0건일 때는 왜 0건인지 말한다.",
    ])
    never_learn: list[str] = field(default_factory=lambda: [
        # 배우면 안 되는 것 = 오래가지 않거나, 배우는 순간 위험해지는 것.
        # `token` 을 맨낱말로 막지 않는다. 토크나이저·JSON 토큰·`old_string`
        # 토큰처럼 자격증명과 무관한 자리에 흔하게 나오는 말이라, 막으면
        # 멀쩡한 스킬이 조용히 죽는다(실측: `read-before-edit` 이 본문에
        # "token" 한 번 나왔다고 규약 위반으로 거절됐다). 바로 아래 바닥
        # (`SECRET_SHAPES`)이 이미 **값이 대입된 모양**을 잡고, 그건 규약
        # 파일로 끌 수도 없다 - 낱말 목록이 같은 일을 두 번 할 이유가 없다.
        r"비밀번호|password|api[_-]?key|secret|자격증명|credential"
        r"|(?:access|refresh|bearer|auth|session)[_ -]?token",
        r"특정 날짜에만 맞는|only on \d{4}-\d{2}-\d{2}",
        r"검증을 건너뛰|skip (?:the )?(?:verification|gate|bench)",
        r"사람 승인 없이|without (?:human )?approval",
    ])
    # **남에게 미치는 스코프만** 승인을 요구한다. 예전 기본값은 `["project","org"]`
    # 였는데, 배운 스킬은 대개 `user`(내 것) 이거나 `project:<열쇠>` 라서 `project`
    # 를 걸면 혼자 쓰는 사람도 모든 학습에 승인을 눌러야 한다. 그건 규율이 아니라
    # 마찰이다. 여럿이 쓰는 자리(`platform`·`org`)가 사람이 봐야 하는 자리다.
    approval_required_scopes: list[str] = field(
        default_factory=lambda: ["platform", "org"])
    version: str = "1.0.0"
    history: list[str] = field(default_factory=list)

    # ------------------------------------------------------------ 집행

    def check_text(self, text: str) -> str | None:
        """이 글이 배워도 되는가. 위반이면 이유, 아니면 None.

        **never_learn 집행은 여기서만 한다.** 스킬 후보든 기억이든 같은 규칙을
        받아야 한다 - 두 곳에서 따로 집행하면 언젠가 한쪽만 고쳐지고, 그 한쪽으로
        비밀값이 샌다.

        `never_learn` 앞에 **바닥**이 먼저 온다(`secret_shape`). 규약은 사람이
        고치는 것이지만 자격증명을 안 배우는 것은 정하고 말고 할 문제가 아니라서,
        규약 파일을 고쳐도 이 검사는 안 꺼진다.
        """
        shape = secret_shape(text)
        if shape:
            return f"자격증명 차단: {shape} 이 들어 있습니다(규약보다 아래 규칙)"
        for pattern in self.never_learn:
            try:
                match = re.search(pattern, text, re.IGNORECASE)
            except re.error:
                continue      # 잘못된 규칙 하나가 집행 전체를 멈추게 두지 않는다
            if match:
                return f"규약 위반(never_learn): {pattern!r} 이 {match.group(0)!r} 에 걸림"
        return None

    def check_candidate(self, candidate: SkillCandidate) -> str | None:
        """규약 위반이면 이유, 아니면 None. `safety_check` 와 같은 계약이라
        게이트에 그대로 꽂힌다."""
        return self.check_text(" ".join([
            candidate.name or "", candidate.rationale or "", candidate.when_to_use or "",
            " ".join(candidate.procedure or []), " ".join(candidate.pitfalls or []),
            " ".join(candidate.verification or []), str(candidate.payload or {}),
        ]))

    def needs_human_approval(self, scope: str) -> bool:
        """그 스코프에 올리려면 사람이 승인해야 하는가.

        **앞자리로 견준다.** 실제 스코프는 `project:d--` 처럼 열쇠가 붙어 오는데
        규약에는 `project` 라고 적힌다. 정확히 같은지만 보면 규약에 뭘 적어도 절대
        안 걸린다 - 실측으로 그 상태였다(게다가 이 메서드를 부르는 곳이 아예 없어서
        아무도 몰랐다).

        스코프가 넷인 것은 **남에게 미치는 범위**가 다르기 때문이다. 자기 것
        (`session`·`workflow`·`user`)은 스스로 책임지면 되고, 여럿이 쓰는 자리
        (`platform`·`org`)는 올리기 전에 사람이 봐야 한다.
        """
        scope = (scope or "").strip()
        return any(scope == required or scope.startswith(required + ":")
                   for required in self.approval_required_scopes)

    # ------------------------------------------------------------ 변경 통제

    def propose(self, **changes) -> list[str]:
        """제안만 한다 - **적용하지 않는다**. 에이전트가 자기 규약을 바꾸는 경로는 없다."""
        lines: list[str] = []
        for key, value in changes.items():
            if not hasattr(self, key) or key in ("history", "version"):
                lines.append(f"거부: {key} 는 제안 대상이 아니다")
                continue
            current = getattr(self, key)
            if current == value:
                continue
            lines.append(f"{key}: {current!r} -> {value!r}")
        return lines

    def adopt(self, changes: dict, approved_by: str) -> list[str]:
        """사람이 승인했을 때만 적용된다. 승인자 없이 부르면 거부."""
        if not approved_by.strip():
            raise ValueError("규약 변경에는 승인자가 필요하다")
        applied = self.propose(**changes)
        applied = [line for line in applied if not line.startswith("거부:")]
        for key, value in changes.items():
            if hasattr(self, key) and key not in ("history", "version"):
                setattr(self, key, value)
        if applied:
            major, minor, patch = (self.version.split(".") + ["0", "0"])[:3]
            self.version = f"{major}.{int(minor) + 1}.0"
            self.history.append(f"{self.version} by {approved_by}: " + "; ".join(applied))
        return applied

    # ------------------------------------------------------------ 표류 감시

    def diff(self, other: "Constitution") -> list[str]:
        """무엇이 달라졌는지 줄 단위로. 표류를 눈에 보이게 하는 것이 목적이다."""
        lines: list[str] = []
        for field_name in ("identity", "role", "version"):
            mine, theirs = getattr(self, field_name), getattr(other, field_name)
            if mine != theirs:
                lines.append(f"{field_name}: {mine!r} -> {theirs!r}")
        for field_name in ("principles", "never_learn", "approval_required_scopes"):
            mine, theirs = set(getattr(self, field_name)), set(getattr(other, field_name))
            for gone in sorted(mine - theirs):
                lines.append(f"{field_name} 삭제: {gone}")
            for added in sorted(theirs - mine):
                lines.append(f"{field_name} 추가: {added}")
        return lines

    # ------------------------------------------------------------ 직렬화

    def to_markdown(self) -> str:
        """agentskills 프론트매터와 같은 모양 - 다른 도구가 읽어도 안 깨진다."""
        def block(name: str, values: Sequence[str]) -> str:
            return f"{name}:\n" + "".join(f'  - "{v}"\n' for v in values)

        return (
            "---\n"
            f'name: {self.identity.lower()}-constitution\n'
            f'description: "{self.role}"\n'
            f'metadata:\n  version: "{self.version}"\n'
            "---\n\n"
            f"# {self.identity}\n\n{self.role}\n\n"
            "## Principles\n" + "".join(f"- {p}\n" for p in self.principles) +
            "\n## Never learn\n" + "".join(f"- `{p}`\n" for p in self.never_learn) +
            "\n## Approval required\n" +
            "".join(f"- {s}\n" for s in self.approval_required_scopes) +
            ("\n## History\n" + "".join(f"- {h}\n" for h in self.history)
             if self.history else "")
        )

    @classmethod
    def from_markdown(cls, text: str) -> "Constitution":
        """to_markdown 의 역. 못 읽는 줄은 조용히 버리지 않고 기본값으로 남긴다."""
        def section(title: str) -> list[str]:
            match = re.search(rf"^## {re.escape(title)}\n(.*?)(?=^## |\Z)",
                              text, re.MULTILINE | re.DOTALL)
            if not match:
                return []
            return [re.sub(r"^[-*]\s*", "", line).strip().strip("`")
                    for line in match.group(1).splitlines() if line.strip().startswith(("-", "*"))]

        identity = re.search(r"^# (.+)$", text, re.MULTILINE)
        version = re.search(r'version:\s*"?([0-9.]+)"?', text)
        role = re.search(r'description:\s*"([^"]*)"', text)
        constitution = cls(
            identity=(identity.group(1).strip() if identity else DEFAULT_IDENTITY),
            role=(role.group(1) if role else DEFAULT_ROLE),
            version=(version.group(1) if version else "1.0.0"),
        )
        for name, values in (("principles", section("Principles")),
                             ("never_learn", section("Never learn")),
                             ("approval_required_scopes", section("Approval required"))):
            if values:
                setattr(constitution, name, values)
        constitution.history = section("History")
        return constitution

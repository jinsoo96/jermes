"""agentskills.io 상호운용 - 스펙을 어기면 다른 에이전트가 조용히 무시한다.

Agent Skills 는 앤트로픽이 공개 표준으로 풀어 수십 개 제품(Claude Code·Cursor·
Copilot·Gemini CLI·Codex·OpenHands·Goose·Letta…)이 채택했다. 표준의 `validate` 는
프론트매터 문법과 이름 규칙만 본다 - **효과 검증은 표준에 없다**. 그래서 우리는
① 스펙을 정확히 지켜 내보내고(안 지키면 통째로 무시됨) ② 검증 증거는 스펙이 허용한
`metadata` 로 실어 보내고 ③ 남의 스킬을 들여올 때 그쪽이 붙인 verified 는 믿지 않는다.

스펙: https://agentskills.io/specification
"""

import pytest

from jermes.model import Provenance, SkillDef
from jermes.portable import (
    MAX_DESCRIPTION,
    META_PREFIX,
    candidate_from_skill_md,
    parse_skill_md,
    skill_package,
    spec_name,
    to_skill_md,
    validate_name,
    validate_skill_md,
)


def guide(**kw):
    base = dict(name="paginate-with-cursor", kind="guide", scope="user",
                description="Use when listing more than one page from an API",
                body="---\nname: x\ndescription: y\n---\n\n# Body\n\n- step one\n")
    base.update(kw)
    return SkillDef(**base)


# ---------------------------------------------------------------- 이름 규칙

@pytest.mark.parametrize("bad", ["PDF-Processing", "-pdf", "pdf-", "pdf--processing",
                                 "", "has space", "under_score"])
def test_spec_invalid_names_are_rejected(bad):
    assert validate_name(bad), f"{bad!r} 는 스펙 위반인데 통과했다"


@pytest.mark.parametrize("ok", ["pdf-processing", "data-analysis", "code-review", "a1"])
def test_spec_valid_names_pass(ok):
    assert validate_name(ok) == []


def test_truncation_never_leaves_a_trailing_hyphen():
    """관측된 버그 유형: 64자로 자르면서 하이픈으로 끝나 스펙을 어긴다.
    자른 다음에 다시 깎아야 한다 - 순서가 중요하다."""
    raw = ("a" * 63) + "-tail"
    name = spec_name(raw)
    assert len(name) <= 64 and not name.endswith("-")
    assert validate_name(name) == []


def test_spec_name_collapses_and_never_returns_empty():
    assert spec_name("Repo Fetch_Ref Pin!!") == "repo-fetch-ref-pin"
    # 남는 글자가 없으면 원문에서 만든다. 요점은 특정 상수가 아니라
    # **비지 않는 것**과 서로 다른 원문이 서로 다른 이름을 받는 것이다.
    assert spec_name("!!!").startswith("skill-")
    assert spec_name("!!!") != spec_name("???")


# ---------------------------------------------------------------- 내보내기

def test_export_is_spec_valid_and_carries_verification_in_metadata():
    skill = guide(verified=True, status="active",
                  provenance=Provenance(origin="llm_drafter",
                                        source_run_ids=["r1", "r2"], signal="recovery"))
    text = to_skill_md(skill, evidence={"holdout-gain": 0.34, "bench-cases": 8})
    assert validate_skill_md(text) == []
    front, body = parse_skill_md(text)
    assert front["name"] == "paginate-with-cursor"
    meta = front["metadata"]
    # 표준에 없는 단 하나의 정보 = 효과가 실측됐는지. 이게 우리 차별점이다.
    assert meta[f"{META_PREFIX}verified"] == "true"
    assert meta[f"{META_PREFIX}holdout-gain"] == "0.34"
    assert meta[f"{META_PREFIX}source-runs"] == "r1,r2"
    assert "# Body" in body


def test_metadata_values_are_always_quoted_strings():
    """스펙: metadata 는 '문자열 -> 문자열' 맵. 날값이면 true/0.34 가 불리언·숫자로
    파싱돼 타입 계약이 깨진다 - 라이브 내보내기에서 실제로 그랬다."""
    text = to_skill_md(guide(verified=True), evidence={"holdout-gain": 0.34, "n": 0})
    lines = [l.strip() for l in text.splitlines() if l.startswith("  " + META_PREFIX)]
    assert lines, text
    for line in lines:
        assert line.split(": ", 1)[1].startswith('"'), f"따옴표 없음: {line}"
    meta = parse_skill_md(text)[0]["metadata"]
    assert meta[f"{META_PREFIX}verified"] == "true"      # 문자열이어야 한다
    assert meta[f"{META_PREFIX}n"] == "0"


def test_no_spec_undefined_fields_at_top_level():
    """kind/scope/version 을 최상위에 두면 호환이 깨진다 - metadata 아래여야 한다."""
    front, _ = parse_skill_md(to_skill_md(guide()))
    assert set(front) <= {"name", "description", "license", "compatibility",
                          "metadata", "allowed-tools"}
    assert f"{META_PREFIX}kind" in front["metadata"]


def test_description_with_a_colon_does_not_break_the_frontmatter():
    """관측된 결함: 콜론이 든 description 을 날값으로 적으면 YAML 이 깨져 스킬이
    통째로 무시된다. 조용히 사라지는 부류라 더 위험하다."""
    skill = guide(description="Use when: the API returns 404 # not found")
    text = to_skill_md(skill)
    assert validate_skill_md(text) == []
    front, _ = parse_skill_md(text)
    assert front["description"] == "Use when: the API returns 404 # not found"


def test_description_is_not_truncated_to_60_chars():
    """description 이 곧 발견 품질이다 - 에이전트는 name+description 만 보고
    활성화를 결정한다. 60자로 자르면 애초에 안 뜬다(예전 동작)."""
    long = ("Extracts text and tables from PDF files, fills forms, merges PDFs. "
            "Use when the user mentions PDFs, forms, or document extraction.")
    front, _ = parse_skill_md(to_skill_md(guide(description=long)))
    assert front["description"] == long


def test_export_refuses_to_emit_spec_violations():
    with pytest.raises(ValueError):
        to_skill_md(guide(name="Bad_Name"))
    with pytest.raises(ValueError):
        to_skill_md(guide(description="   "))
    with pytest.raises(ValueError):
        to_skill_md(guide(description="x" * (MAX_DESCRIPTION + 1)))


def test_inner_frontmatter_is_not_emitted_twice():
    """우리 body 에는 내부 프론트매터가 붙어 있다. 안 떼고 내보내면 파일에 두 번
    들어가고 두 번째는 본문으로 읽혀 프롬프트를 오염시킨다."""
    text = to_skill_md(guide())
    assert text.count("---") == 2


def test_non_guide_payload_is_fenced_and_also_shipped_as_an_asset():
    skill = guide(kind="config", body='{"stage_params": {"judge": 0.8}}')
    text = to_skill_md(skill)
    assert "```json" in text and validate_skill_md(text) == []
    files = skill_package(skill)
    assert f"{skill.name}/SKILL.md" in files
    assert files[f"{skill.name}/assets/payload.json"].startswith("{")


def test_package_directory_matches_the_name_rule():
    files = skill_package(guide())
    assert list(files) == ["paginate-with-cursor/SKILL.md"]


# ---------------------------------------------------------------- 들여오기

FOREIGN = """---
name: competitor-skill
description: Does something useful. Use when the task looks like X.
license: Apache-2.0
metadata:
  author: someone-else
  xgen-jermes-verified: "true"
---

# Competitor Skill

1. First do this
2. Then do that
- and a bullet
"""


def test_import_extracts_steps_and_keeps_the_body():
    candidate = candidate_from_skill_md(FOREIGN, scope="user")
    assert candidate.name == "competitor-skill"
    assert candidate.procedure[:2] == ["First do this", "Then do that"]
    assert "Competitor Skill" in candidate.payload["imported_body"]
    assert candidate.payload["license"] == "Apache-2.0"


def test_imported_verified_claim_is_never_trusted():
    """남이 스스로 붙인 verified 를 그대로 믿으면 우리 원장의 의미가 사라진다.
    주장은 보존해서 감사 가능하게 두고, 판정은 우리 벤치가 다시 한다."""
    candidate = candidate_from_skill_md(FOREIGN)
    assert candidate.payload["claimed_verified"] == "true"   # 주장은 기록
    assert candidate.provenance.origin == "skill_md_import"  # 출처는 수입으로 표시
    # 후보 단계에는 verified 개념이 없다 - 게이트만 부여한다
    assert not hasattr(candidate, "verified")


def test_import_rejects_spec_violating_files():
    with pytest.raises(ValueError):
        candidate_from_skill_md("no frontmatter here")
    with pytest.raises(ValueError):
        candidate_from_skill_md("---\nname: Bad_Name\ndescription: x\n---\nbody")


def test_roundtrip_survives_export_then_import():
    text = to_skill_md(guide(verified=True), evidence={"holdout-gain": 0.5})
    candidate = candidate_from_skill_md(text)
    assert candidate.name == "paginate-with-cursor"
    assert candidate.payload["imported_metadata"][f"{META_PREFIX}holdout-gain"] == "0.5"


def test_payload_containing_a_code_fence_does_not_leak_out():
    """검수에서 확인: payload 안에 ``` 가 있으면 3중 백틱이 거기서 닫혀 뒷부분이
    본문으로 새어 나온다 - 받는 에이전트가 그걸 지시문으로 읽는다."""
    skill = guide(kind="config", body='{"note": "``` 여기서 닫힘", "x": 1}')
    text = to_skill_md(skill)
    assert validate_skill_md(text) == []
    fences = [line for line in text.splitlines() if line.startswith("`")]
    assert len(fences) == 2                       # 열고 닫는 것 딱 한 쌍
    assert fences[0].rstrip("json") == fences[1]  # 같은 길이로 닫힌다
    assert len(fences[1]) > 3                     # 내용보다 길어졌다


def test_import_always_lands_as_guide_and_records_the_original_kind():
    """모르는 출처의 config/tool 을 그대로 그 kind 로 받으면 실행 가능한 산출물을
    남의 파일이 정하게 된다. 항상 guide 로 받고 원래 kind 는 감사용으로 남긴다."""
    text = to_skill_md(guide(kind="config", body='{"a": 1}'))
    candidate = candidate_from_skill_md(text)
    assert candidate.kind == "guide"
    assert candidate.payload["imported_metadata"][f"{META_PREFIX}kind"] == "config"


def test_a_non_latin_name_does_not_collide_with_every_other_one():
    """실측: 서로 다른 스킬 3개(파일 정리하기·데이터 백업·로그 분석)를 넣었더니
    원장에 1개만 남고 둘이 경고 없이 사라졌다. 라틴 글자가 없는 이름이 전부 상수
    하나(unnamed-skill)로 접혔기 때문이다."""
    from jermes.portable import spec_name

    names = {spec_name(n) for n in ("파일 정리하기", "데이터 백업", "로그 분석",
                                    "ファイル整理", "删除文件")}
    assert len(names) == 5, f"이름이 겹쳤다: {names}"
    assert all(n.startswith("skill-") for n in names)


def test_the_derived_name_is_the_same_tomorrow():
    """파이썬 hash() 는 프로세스마다 달라서 어제 만든 스킬이 오늘 다른 이름이 된다.
    gate.case_hash 가 blake2b 를 쓰는 것과 같은 이유다."""
    from jermes.portable import spec_name

    assert spec_name("파일 정리하기") == "skill-76d8692456"


def test_an_empty_name_is_still_named():
    from jermes.portable import spec_name

    assert spec_name("") == "unnamed-skill"
    assert spec_name("   ") == "unnamed-skill"


def test_a_latin_name_is_untouched():
    from jermes.portable import spec_name

    assert spec_name("normalize line endings") == "normalize-line-endings"

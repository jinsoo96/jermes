"""Dashboard collection - the numbers people read must be right.

The counting bug this locks down: `staged` was used both as a verification
label and as a ledger status, so a staged skill was counted twice (4 skills
showed as 1 verified + 6 staged). Verification labels and ledger statuses are
different axes and must never share a key.
"""

import json
from unittest.mock import patch

from jermes import dashboard


def skill(name, kind="guide", status="staged", verified=False, ok=0, fail=0):
    return {"name": name, "kind": kind, "scope": "user", "version": "0.1.0",
            "status": status, "verified": verified, "description": f"{name} 설명",
            "rank": 0.5, "usage": {"successes": ok, "failures": fail},
            "history": []}


def fake_get(mapping, errors=()):
    def _get(path):
        for key, value in mapping.items():
            if key in path:
                return value, ""
        if path in errors:
            return None, "HTTP 500"
        return None, "HTTP 404"
    return _get


def test_verified_and_unverified_partition_the_total():
    data = {"skills": [skill("a", verified=True, status="active"),
                       skill("b"), skill("c")]}
    with patch.object(dashboard, "SCOPES", ["user:x"]), \
         patch.object(dashboard, "_get", fake_get({"/skills": data, "/schedule": {}})):
        out = dashboard.collect()
    t = out["totals"]
    assert t["skills"] == 3
    assert t["verified"] + t["unverified"] == t["skills"]  # 이중집계 회귀 방지
    assert t["verified"] == 1 and t["unverified"] == 2


def test_status_axis_counted_separately_from_verification():
    data = {"skills": [skill("a", status="active", verified=True),
                       skill("b", status="staged"),
                       skill("c", status="rejected")]}
    with patch.object(dashboard, "SCOPES", ["user:x"]), \
         patch.object(dashboard, "_get", fake_get({"/skills": data, "/schedule": {}})):
        t = dashboard.collect()["totals"]
    assert (t["active"], t["staged"], t["rejected"]) == (1, 1, 1)
    assert t["skills"] == 3


def test_usage_totals_accumulate():
    data = {"skills": [skill("a", ok=3, fail=1), skill("b", ok=2, fail=0)]}
    with patch.object(dashboard, "SCOPES", ["user:x"]), \
         patch.object(dashboard, "_get", fake_get({"/skills": data, "/schedule": {}})):
        t = dashboard.collect()["totals"]
    assert t["successes"] == 5 and t["failures"] == 1


def test_kinds_are_tallied():
    data = {"skills": [skill("a", kind="guide"), skill("b", kind="config"),
                       skill("c", kind="guide")]}
    with patch.object(dashboard, "SCOPES", ["user:x"]), \
         patch.object(dashboard, "_get", fake_get({"/skills": data, "/schedule": {}})):
        kinds = dashboard.collect()["kinds"]
    assert kinds == {"guide": 2, "config": 1}


def test_empty_scopes_are_omitted_not_crashing():
    with patch.object(dashboard, "SCOPES", ["user:x", "user:y"]), \
         patch.object(dashboard, "_get",
                      fake_get({"/skills": {"skills": []}, "/schedule": {}})):
        out = dashboard.collect()
    assert out["scopes"] == [] and out["totals"]["skills"] == 0


def test_backend_down_surfaces_errors_but_still_returns_state():
    """XGEN 이 죽어도 대시보드는 살아서 원인을 보여줘야 한다."""
    def boom(path):
        return None, "URLError"
    with patch.object(dashboard, "SCOPES", ["user:x"]), \
         patch.object(dashboard, "_get", boom):
        out = dashboard.collect()
    assert out["errors"] and out["totals"]["skills"] == 0
    assert out["schedule"] is None
    assert "generated_at" in out


def test_state_is_json_serializable():
    data = {"skills": [skill("a", verified=True)]}
    with patch.object(dashboard, "SCOPES", ["user:x"]), \
         patch.object(dashboard, "_get",
                      fake_get({"/skills": data, "/schedule": {"running": True}})):
        out = dashboard.collect()
    json.dumps(out, ensure_ascii=False)  # 직렬화 실패하면 /api/state 가 500


def test_malformed_skill_list_does_not_crash():
    with patch.object(dashboard, "SCOPES", ["user:x"]), \
         patch.object(dashboard, "_get",
                      fake_get({"/skills": {"skills": "not-a-list"},
                                "/schedule": {}})):
        out = dashboard.collect()
    assert out["totals"]["skills"] == 0


def test_page_has_no_unescaped_template_bug():
    """대시보드 HTML 은 문법이 깨지면 조용히 빈 화면이 된다 - 최소 계약만 확인."""
    page = dashboard._PAGE
    assert "id=\"stats\"" in page and "id=\"sched\"" in page and "id=\"body\"" in page
    assert "/api/state" in page
    assert page.count("<script>") == page.count("</script>")


def test_keeper_restarts_a_stopped_scheduler():
    """관측된 실패: XGEN 컨테이너 리로드마다 스케줄러가 초기화돼 학습이
    조용히 멈췄다. keeper 는 별도 프로세스라 그걸 감지해 되살린다."""
    posted = {}

    def fake_get(path):
        return {"running": False}, ""

    def fake_post(path, body):
        posted["path"] = path
        posted["body"] = body
        return {"running": True}, ""

    with patch.object(dashboard, "KEEPER_BODY",
                      {**dashboard.KEEPER_BODY, "base_url": "http://x/v1"}), \
         patch.object(dashboard, "_get", fake_get), \
         patch.object(dashboard, "_post", fake_post):
        out = dashboard.keeper_tick()
    assert "재기동" in out
    assert posted["path"] == "/schedule"


def test_keeper_leaves_a_running_scheduler_alone():
    calls = []
    with patch.object(dashboard, "KEEPER_BODY",
                      {**dashboard.KEEPER_BODY, "base_url": "http://x/v1"}), \
         patch.object(dashboard, "_get", lambda p: ({"running": True}, "")), \
         patch.object(dashboard, "_post", lambda p, b: calls.append(b) or ({}, "")):
        out = dashboard.keeper_tick()
    assert "정상" in out and calls == []


def test_keeper_is_inert_without_config():
    """설정 없이 켜두면 아무 것도 하지 않는다 - 잘못된 기본값으로 남의 스코프를
    학습시키는 것보다 안 하는 게 낫다."""
    with patch.object(dashboard, "KEEPER_BODY",
                      {**dashboard.KEEPER_BODY, "base_url": ""}):
        assert "미설정" in dashboard.keeper_tick()


def test_keeper_reports_backend_outage_without_raising():
    with patch.object(dashboard, "KEEPER_BODY",
                      {**dashboard.KEEPER_BODY, "base_url": "http://x/v1"}), \
         patch.object(dashboard, "_get", lambda p: (None, "URLError")):
        out = dashboard.keeper_tick()
    assert "실패" in out or "내려" in out


def test_native_schedule_status_is_surfaced():
    """XGEN 네이티브 스케줄(DB 기반, 컨테이너 리로드에도 생존)이 주 경로다.
    여기가 조용히 멈추면 학습이 서므로 대시보드에 반드시 보여야 한다."""
    # _native_sessions 가 이미 화면용으로 접어서 돌려주는 형태
    rows = [{"name": "Jermes 자동학습", "status": "active", "total": 3, "ok": 3,
             "failed": 0, "last_status": "success",
             "last_at": "2026-07-29T23:20:48"}]
    with patch.object(dashboard, "SCOPES", ["user:x"]), \
         patch.object(dashboard, "_get",
                      fake_get({"/skills": {"skills": []}, "/schedule": {}})), \
         patch.object(dashboard, "_native_sessions", lambda: (rows, "")):
        out = dashboard.collect()
    assert out["native_schedule"][0]["status"] == "active"
    assert out["native_schedule"][0]["ok"] == 3


def test_second_instance_refuses_the_port_instead_of_shadowing():
    """관측된 실패: 윈도우 SO_REUSEADDR 로 두 번째 인스턴스가 같은 포트에 붙어,
    재기동이 성공한 듯 보이는데 낡은 프로세스가 계속 응답했다."""
    assert dashboard._Server.allow_reuse_address is False


def test_native_schedule_read_carries_the_permission_header(monkeypatch):
    # 플랫폼 경로를 재는 시험은 **붙었다고 밝힌다.** 예전에는 모듈 기본값이
    # localhost:8002 라 밝히지 않아도 돌았고, 그래서 시험 8건이 진짜로 남의
    # 포트로 나가고 있었다.
    monkeypatch.setattr(dashboard, "PLATFORM_BASE", "http://platform.test")
    """라이브에서 관측된 실패: identity 헤더 3종만 보내면 이 라우트는
    `main.agentflow-schedule:read` 로 403 이라 자동학습 상태가 통째로 안 보였다."""
    seen = {}

    class _Resp:
        def read(self):
            return json.dumps({"sessions": [{"name": "s", "status": "active",
                                             "total_executions": 2,
                                             "successful_executions": 2}]}).encode()
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def fake_urlopen(request, timeout=None):
        seen["url"] = request.full_url
        seen["superuser"] = request.get_header("X-user-superuser")
        return _Resp()

    with patch.object(dashboard.urllib.request, "urlopen", fake_urlopen):
        rows, err = dashboard._native_sessions()
    assert err == "" and rows[0]["ok"] == 2
    assert seen["superuser"] == "true"
    assert seen["url"].endswith("/api/agentflow/schedule/sessions")


def test_native_schedule_error_keeps_the_http_code(monkeypatch):
    """'HTTPError' 만 보이면 권한(403)인지 경로(404)인지 구분이 안 된다."""
    monkeypatch.setattr(dashboard, "PLATFORM_BASE", "http://platform.test")
    import urllib.error

    def boom(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 403, "no", {}, None)

    with patch.object(dashboard.urllib.request, "urlopen", boom):
        rows, err = dashboard._native_sessions()
    assert rows is None and err == "HTTP 403"


def test_native_schedule_outage_becomes_a_visible_error():
    with patch.object(dashboard, "SCOPES", ["user:x"]), \
         patch.object(dashboard, "_get",
                      fake_get({"/skills": {"skills": []}, "/schedule": {}})), \
         patch.object(dashboard, "_native_sessions", lambda: (None, "URLError")):
        out = dashboard.collect()
    assert out["native_schedule"] is None
    assert any("native-schedule" in e for e in out["errors"])


def test_a_schedule_that_fires_but_never_advances_is_flagged_as_stalled():
    """관측된 사고(가장 지독한 부류): XGEN 이 실행 #31 을 끝냈는데 세션 카운터 갱신이
    실패해(테이블 재생성으로 컬럼이 사라짐) total 이 30 에 멈췄다. 다음 틱은 다시 #31 을
    계산하고 '이미 처리됨'으로 **영구히 건너뛴다**. 스케줄러는 계속 발화하므로
    status=active, 실패 0 - 대시보드가 완벽히 정상으로 보였다."""
    import datetime
    past = (datetime.datetime.now() - datetime.timedelta(hours=5)).isoformat()
    fresh = (datetime.datetime.now() + datetime.timedelta(minutes=10)).isoformat()

    stalled = dashboard._is_stalled({"status": "active", "next_at": past})
    assert stalled and "멈춘 것으로 보임" in stalled

    assert dashboard._is_stalled({"status": "active", "next_at": fresh}) == ""
    # 멈춘 것은 status 로는 구분이 안 된다 - 그래서 next_at 을 본다
    assert dashboard._is_stalled({"status": "paused", "next_at": past}) == ""
    assert "없음" in dashboard._is_stalled({"status": "active", "next_at": None})


def test_stall_is_raised_as_an_error_not_just_a_card():
    """카드에만 있으면 '정상'과 한눈에 구분이 안 된다 - errors 로 올라와야 한다."""
    import datetime
    past = (datetime.datetime.now() - datetime.timedelta(hours=5)).isoformat()
    rows = [{"name": "Jermes 자동학습", "status": "active", "total": 30, "ok": 30,
             "failed": 0, "last_status": "success", "last_at": past,
             "next_at": past, "stalled": "예정 지남 - 멈춘 것으로 보임"}]
    with patch.object(dashboard, "SCOPES", ["user:x"]), \
         patch.object(dashboard, "_get",
                      fake_get({"/skills": {"skills": []}, "/schedule": {}})), \
         patch.object(dashboard, "_native_sessions", lambda: (rows, "")):
        out = dashboard.collect()
    assert any("스케줄 정지" in e for e in out["errors"])


def test_a_late_but_within_grace_schedule_is_not_cried_wolf_about():
    """조금 늦은 것과 멈춘 것은 다르다 - 매번 빨간불이면 아무도 안 본다."""
    import datetime
    slightly_late = (datetime.datetime.now() - datetime.timedelta(minutes=3)).isoformat()
    assert dashboard._is_stalled({"status": "active", "next_at": slightly_late}) == ""


def test_dashboard_shows_local_work_when_xgen_is_absent():
    """XGEN HTTP 에만 매달리면 XGEN 이 없는 환경에서 화면이 통째로 빈다 -
    Jermes 는 단독으로도 도는데 그게 안 보이면 '안 도는 것'과 구분되지 않는다."""
    local = [{"name": "local-skill", "kind": "guide", "scope": "user",
              "version": "0.1.0", "status": "staged", "verified": False,
              "description": "설명", "rank": 0.5,
              "usage": {"successes": 0, "failures": 0}, "history": []}]
    rows = [{"name": "abc12345", "tool_calls": 120, "errors": 3, "recoveries": 2,
             "corrections": 1, "signals": ["recovery"], "learnable": True}]

    def dead(path):
        return None, "URLError"

    with patch.object(dashboard, "SCOPES", ["user:x"]), \
         patch.object(dashboard, "_get", dead), \
         patch.object(dashboard, "_native_sessions", lambda: (None, "URLError")), \
         patch.object(dashboard, "_local_ledger", lambda: (local, "")), \
         patch.object(dashboard, "_local_sessions", lambda: (rows, "")):
        out = dashboard.collect()

    assert out["local_skills"] == local
    assert out["sessions"] == rows
    assert out["totals"]["skills"] == 1        # 로컬 스킬도 합계에 든다
    assert any(s["scope"].startswith("로컬") for s in out["scopes"])
    assert out["errors"]                        # XGEN 장애는 그대로 보고된다


def test_local_source_failure_is_reported_not_swallowed():
    with patch.object(dashboard, "SCOPES", []), \
         patch.object(dashboard, "_get", lambda p: ({}, "")), \
         patch.object(dashboard, "_native_sessions", lambda: ([], "")), \
         patch.object(dashboard, "_local_ledger", lambda: ([], "PermissionError")), \
         patch.object(dashboard, "_local_sessions", lambda: ([], "OSError")):
        out = dashboard.collect()
    joined = " ".join(out["errors"])
    assert "local-ledger" in joined and "local-sessions" in joined


def test_sessions_are_cached_so_the_refresh_does_not_reparse_every_time():
    """15초마다 수천 줄 jsonl 을 다시 파싱하면 대시보드가 관측 대상보다 무거워진다."""
    calls = []

    def counting(path, max_lines=0):
        calls.append(path)
        class S:
            tool_calls = errors = recoveries = corrections = 0
            signals: list = []
            worth_learning = False
        return S()

    dashboard._SESSION_CACHE.update({"at": 0.0, "rows": []})
    with patch.object(dashboard, "SESSION_TTL", 999), \
         patch("jermes.sources.iter_session_files", lambda *a, **k: [__import__("pathlib").Path("x.jsonl")]), \
         patch("jermes.sources.summarize_session", counting):
        dashboard._local_sessions()
        dashboard._local_sessions()
    assert len(calls) == 1        # 두 번째는 캐시
    dashboard._SESSION_CACHE.update({"at": 0.0, "rows": []})


def test_page_has_the_sessions_container():
    page = dashboard._PAGE
    assert 'id="sessions"' in page and "getElementById('sessions')" in page


def test_no_platform_means_no_outbound_call_at_all(monkeypatch):
    """실측: 기본값이 localhost:8002(다른 제품의 포트)라, 붙인 적 없는 사람이
    대시보드를 열 때마다 그 포트를 세 번 두드렸다. 아무것도 없으면 매 새로고침이
    타임아웃을 먹고 설정한 적 없는 오류 세 줄이 뜬다. 무언가 떠 있으면 더 나쁘다 -
    남의 서비스로 우리 identity 헤더와 X-User-Superuser: true 가 나간다."""
    monkeypatch.setattr(dashboard, "PLATFORM_BASE", "")

    def forbidden(*a, **k):
        raise AssertionError("안 붙인 플랫폼을 불렀다")

    monkeypatch.setattr(dashboard.urllib.request, "urlopen", forbidden)
    assert dashboard._native_sessions() == (None, "")
    assert dashboard._get("/schedule") == (None, "")

    state = dashboard.collect()
    assert not [e for e in state["errors"] if "schedule" in e],         "안 붙인 것이 오류로 보이면 안 된다"


# --- 관측성 - 무엇을 할 수 있는지(권한) · 기억 자가검증 루프가 뭘 하는지 ------

def test_tool_permissions_show_what_it_can_actually_do(tmp_path, monkeypatch):
    """검증 여부만으로는 "옳게 동작한다" 는 알아도 "무엇에 손댈 수 있는가" 는
    안 보인다. `--policy files` 로 단조한 도구는 파일 쓰기가 보여야 한다."""
    monkeypatch.setenv("JERMES_HOME", str(tmp_path))
    from jermes.model import SkillDef
    from jermes.tools import ToolPolicy

    manifest = {"script": "def run(p):\n    return 1\n",
               "policy": ToolPolicy(allow_write=True).to_dict()}
    skill = SkillDef(name="writer", kind="tool", scope="user",
                     description="d", body=json.dumps(manifest))
    from jermes.cli import open_ledger
    open_ledger().commit(skill)

    rows, err = dashboard._local_ledger()
    assert err == ""
    row = next(r for r in rows if r["name"] == "writer")
    assert row["permissions"] == ["allow_write"]


def test_permissions_is_none_not_empty_for_non_tool_skills(tmp_path, monkeypatch):
    """"권한이 없다" 와 "권한이라는 개념이 없다" 는 다른 사실이다. 가이드
    스킬을 `[]` 로 적으면 둘이 화면에서 구분이 안 된다."""
    monkeypatch.setenv("JERMES_HOME", str(tmp_path))
    from jermes.model import SkillDef
    from jermes.cli import open_ledger

    open_ledger().commit(SkillDef(name="how-to", kind="guide", scope="user",
                                  description="d", body="## Procedure\n- 하나\n"))
    rows, _ = dashboard._local_ledger()
    row = next(r for r in rows if r["name"] == "how-to")
    assert row["permissions"] is None


def test_memory_summary_separates_unmeasured_from_measured_but_uninformative(
        tmp_path, monkeypatch):
    """기억 78건 중 67건이 gain 0.000 이었던 그 실측이 여기서도 구분돼야 한다 -
    "안 재봤다" 와 "재봤는데 못 갈랐다" 를 한 숫자로 뭉치면 다시 안 보인다."""
    monkeypatch.setenv("JERMES_HOME", str(tmp_path))
    from jermes.cli import save_memory
    from jermes.memory import MemoryItem

    untouched = MemoryItem(item_id="a", text="아직 안 재본 사실")
    uninformative = MemoryItem(item_id="b", text="재봤는데 못 갈랐다")
    uninformative.evidence["measurements"] = [{"cases": 5, "gain": 0.0, "verdict": "neutral"}]
    moved = MemoryItem(item_id="c", text="도움 되는 사실", trust=0.65)
    moved.evidence["measurements"] = [{"cases": 5, "gain": 0.3, "verdict": "helpful"}]
    save_memory([untouched, uninformative, moved])

    summary = dashboard._memory_summary()
    assert summary["total"] == 3
    assert summary["unmeasured"] == 1
    assert summary["measured_but_uninformative"] == 1
    assert summary["moved"] == 1


def test_memory_summary_is_present_in_collect(tmp_path, monkeypatch):
    """`collect()` 한 번으로 스킬과 기억이 같이 나와야 XGEN 쪽 화면이 두 번
    안 물어도 된다."""
    monkeypatch.setenv("JERMES_HOME", str(tmp_path))
    with patch.object(dashboard, "SCOPES", []), \
         patch.object(dashboard, "_get", fake_get({"/schedule": {}})):
        state = dashboard.collect()
    assert "memory" in state and "error" not in state["memory"]
    assert state["memory"]["total"] == 0

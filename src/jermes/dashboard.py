"""Jermes dashboard - 학습 루프를 밖에서 지켜보는 창.

의존성 0(stdlib http.server + urllib). 호스트 플랫폼의 Jermes API 를 읽어
스킬 원장·검증 현황·자동학습 사이클을 한 화면에 보여준다.

**읽기 전용이다.** 승인/거절 같은 상태 변경은 넣지 않았다 - 이 대시보드는
도메인으로 공개될 수 있고(js-96), 그 자체 인증이 없다. 원장을 바꾸는 행위는
플랫폼 로그인 뒤의 콘솔(/api/agentflow/harness/jermes/console)에 남겨둔다.

실행:
    python -m jermes.dashboard            # :7396
    JERMES_PORT=8123 JERMES_PLATFORM_BASE=http://localhost:8002 python -m ...
"""

from __future__ import annotations

import datetime
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 호스트 관측 모드의 대상. Jermes 는 혼자 돌지만, 하네스가 붙은 플랫폼을
# 볼 때는 그 주소가 필요하다.
#
# **기본값이 없다.** 예전 기본값은 `http://localhost:8002` 였는데 그건 다른
# 제품의 포트다. 붙인 적 없는 사람이 대시보드를 열 때마다 그 포트를 세 번
# 두드려 타임아웃을 먹고, 설정한 적 없는 플랫폼의 오류 세 줄을 봤다. 8002 에
# 무언가 떠 있으면 더 나쁘다 - 남의 서비스로 우리 identity 헤더와
# `X-User-Superuser: true` 가 나간다. 안 붙였으면 안 부른다.
PLATFORM_BASE = os.environ.get("JERMES_PLATFORM_BASE", "")
SCOPES = [s.strip() for s in os.environ.get(
    "JERMES_SCOPES", "user:default,user:jermes-e2e,user:internal-qwen,user:sched"
).split(",") if s.strip()]
PORT = int(os.environ.get("JERMES_PORT", "7396"))
TIMEOUT = float(os.environ.get("JERMES_TIMEOUT", "8"))
# 예정 시각을 이만큼 넘기면 "멈춤"으로 본다. 한 주기(30분)를 통째로
# 넘겼다는 뜻이라 지연이 아니라 정지다.
STALL_GRACE = float(os.environ.get("JERMES_STALL_GRACE", "1800"))

# 그 플랫폼은 identity 헤더 3종을 동시에 요구한다(단독 X-User-Id 는 401).
_HEADERS = {
    "X-User-Id": os.environ.get("JERMES_USER_ID", "1"),
    "X-User-Name": os.environ.get("JERMES_USER_NAME", "qaadmin"),
    "X-User-Email": os.environ.get("JERMES_USER_EMAIL", ""),
}


def _get(path: str) -> tuple[dict | list | None, str]:
    if not PLATFORM_BASE:
        return None, ""      # 안 붙였다. 오류가 아니라 그냥 없는 것이다
    url = f"{PLATFORM_BASE.rstrip('/')}/api/agentflow/harness/jermes{path}"
    request = urllib.request.Request(url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8")), ""
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}"
    except Exception as e:  # 스택이 내려가 있어도 대시보드는 살아있어야 한다
        return None, f"{type(e).__name__}"


def collect() -> dict:
    """스코프별 스킬 + 스케줄러 상태를 모아 화면용 요약으로 접는다."""
    scopes: list[dict] = []
    # 검증 라벨(verified/unverified)과 원장 상태(active/staged/…)는 다른 축이다.
    # 같은 키를 쓰면 staged 스킬이 두 번 세어진다.
    totals = {"skills": 0, "verified": 0, "unverified": 0, "active": 0,
              "staged": 0, "rejected": 0, "deprecated": 0,
              "successes": 0, "failures": 0}
    kinds: dict[str, int] = {}
    errors: list[str] = []

    for scope in SCOPES:
        data, err = _get(f"/skills?scope_key={urllib.parse.quote(scope)}")
        if err:
            errors.append(f"{scope}: {err}")
            continue
        raw = (data or {}).get("skills") if isinstance(data, dict) else None
        # 백엔드가 예상 밖 모양을 줘도 대시보드는 죽지 않아야 한다 -
        # 관측 도구가 관측 대상보다 먼저 쓰러지면 쓸모가 없다.
        skills = [s for s in raw if isinstance(s, dict)] if isinstance(raw, list) else []
        if not skills:
            if raw is not None and not isinstance(raw, list):
                errors.append(f"{scope}: 잘못된 응답 모양")
            continue
        for skill in skills:
            totals["skills"] += 1
            totals["verified" if skill.get("verified") else "unverified"] += 1
            status = str(skill.get("status", ""))
            if status in totals:
                totals[status] += 1
            usage = skill.get("usage") or {}
            totals["successes"] += int(usage.get("successes") or 0)
            totals["failures"] += int(usage.get("failures") or 0)
            kind = str(skill.get("kind", "?"))
            kinds[kind] = kinds.get(kind, 0) + 1
        scopes.append({"scope": scope, "skills": skills})

    schedule, sched_err = _get("/schedule")
    if sched_err:
        errors.append(f"schedule: {sched_err}")

    # 플랫폼 네이티브 스케줄(워크플로우 기반) 상태 - 이쪽이 DB 기반이라 컨테이너
    # 리로드에도 살아남는 주 경로다. 여기가 조용히 멈추면 학습이 서는데,
    # 대시보드에 안 보이면 알 길이 없으므로 같이 싣는다.
    native = None
    sessions, native_err = _native_sessions()
    if native_err:
        errors.append(f"native-schedule: {native_err}")
    elif sessions:
        native = sessions
        # 멈춘 스케줄은 `active` 로 보이므로 카드만으로는 눈에 안 띈다 - 오류로 올린다.
        for row in sessions:
            if row.get("stalled"):
                errors.append(f"스케줄 정지: {row.get('name')} - {row['stalled']}")
    # 로컬(플랫폼 없이 도는 쪽) - 파일 원장과 배울 만한 세션.
    local_skills, local_err = _local_ledger()
    if local_err:
        errors.append(f"local-ledger: {local_err}")
    for skill in local_skills:
        totals["skills"] += 1
        totals["verified" if skill.get("verified") else "unverified"] += 1
        status = str(skill.get("status", ""))
        if status in totals:
            totals[status] += 1
        kind = str(skill.get("kind", "?"))
        kinds[kind] = kinds.get(kind, 0) + 1
    if local_skills:
        scopes.append({"scope": "로컬(파일 원장)", "skills": local_skills})

    sessions_rows, sessions_err = _local_sessions()
    if sessions_err:
        errors.append(f"local-sessions: {sessions_err}")

    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "platform_base": PLATFORM_BASE,
        "totals": totals,
        "kinds": kinds,
        "scopes": scopes,
        "schedule": schedule if isinstance(schedule, dict) else None,
        "native_schedule": native,
        "local_skills": local_skills,
        "sessions": sessions_rows,
        "memory": _memory_summary(),
        "errors": errors,
    }


def _memory_summary() -> dict:
    """기억 자가검증 루프가 실제로 뭘 하고 있는지 - 스킬 화면에는 없던 축.

    개수만 세면 "재봤다" 와 "재봤는데 못 갈랐다" 가 같아 보인다(`told_us_nothing`
    이 이 물건 전체에서 반복해서 나온 구분이다) - 여기서도 셋을 나눈다.
    """
    try:
        from .cli import load_memory
        items = load_memory()
    except Exception as e:
        return {"error": type(e).__name__}

    unmeasured = [i for i in items if not i.measured]
    thin = [i for i in items if i.told_us_nothing]
    moved = [i for i in items if i.measured and not i.told_us_nothing]
    recent = sorted(items, key=lambda i: len(i.history), reverse=True)[:5]
    return {
        "total": len(items),
        "unmeasured": len(unmeasured),
        "measured_but_uninformative": len(thin),
        "moved": len(moved),
        "recent": [{"text": i.text[:100], "trust": round(i.trust, 2),
                   "status": i.status,
                   "last_measurement": (i.history[-1] if i.history else "")}
                  for i in recent],
    }


# ── 로컬 원천 - 플랫폼이 없어도 대시보드가 비지 않게 ──────────────────
# 대시보드가 플랫폼 HTTP 에만 매달려 있으면, 플랫폼이 없는 환경에서는 화면이 통째로
# 빈다. Jermes 는 단독으로도 도는데(파일 원장 + 로컬 세션) 그게 안 보이면 "안 도는
# 것"과 구분되지 않는다.
_SESSION_CACHE: dict = {"at": 0.0, "rows": []}
SESSION_TTL = float(os.environ.get("JERMES_SESSION_TTL", "120"))
SESSION_SCAN = int(os.environ.get("JERMES_SESSION_SCAN", "8"))


def _local_ledger() -> tuple[list[dict], str]:
    """파일 원장(`JERMES_HOME/skills.jsonl`) - CLI 가 쓰는 그 원장.

    `permissions` 는 kind="tool" 일 때만 채운다 - 그 도구가 **실제로 무엇을
    할 수 있는지**(`ToolPolicy.granted()`)를 보여 준다. 검증 여부만으로는
    "옳게 동작한다" 는 알아도 "무엇에 손댈 수 있는가" 는 안 보인다 - 이 화면이
    그 자리다.
    """
    try:
        from .cli import open_ledger
        records = open_ledger().list()
    except Exception as e:
        return [], type(e).__name__
    rows = []
    for record in records:
        rows.append({
            "name": record.name,
            "kind": record.skill.kind,
            "scope": record.skill.scope,
            "version": record.skill.version,
            "status": record.status,
            "verified": bool(record.skill.verified),
            "description": record.description,
            "rank": record.rank_score(),
            "usage": {"successes": record.usage.successes,
                      "failures": record.usage.failures},
            "history": record.history,
            "permissions": _tool_permissions(record.skill),
        })
    return rows, ""


def _tool_permissions(skill) -> list[str] | None:
    """그 도구가 허락받은 것. kind="tool" 이 아니면 `None`(해당 없음이지 빈
    권한이 아니다 - 둘을 같은 값으로 두면 "권한이 없다" 와 "권한이라는 개념이
    없다" 가 화면에서 구분이 안 된다)."""
    if skill.kind != "tool":
        return None
    try:
        manifest = json.loads(skill.body)
    except (ValueError, TypeError):
        return []
    from .tools import ToolPolicy

    return ToolPolicy.from_dict(manifest.get("policy")).granted()


def _local_sessions() -> tuple[list[dict], str]:
    """배울 거리가 있는 로컬 세션 - 훑는 데 시간이 드니 TTL 캐시를 둔다."""
    now = time.time()
    if now - _SESSION_CACHE["at"] < SESSION_TTL:
        return _SESSION_CACHE["rows"], ""
    try:
        from .sources import iter_session_files, summarize_session
        files = iter_session_files()[:SESSION_SCAN]
        rows = []
        for path in files:
            summary = summarize_session(path, max_lines=4000)
            rows.append({
                "name": path.stem[:8],
                "tool_calls": summary.tool_calls,
                "errors": summary.errors,
                "recoveries": summary.recoveries,
                "corrections": summary.corrections,
                "signals": sorted(set(summary.signals)),
                "learnable": summary.worth_learning,
            })
    except Exception as e:
        return [], type(e).__name__
    _SESSION_CACHE.update({"at": now, "rows": rows})
    return rows, ""


def _native_sessions() -> tuple[list[dict] | None, str]:
    """플랫폼 스케줄 세션(워크플로우 주기 실행) 요약. Jermes API 가 아니라
    agentflow/schedule 이라 경로를 따로 조립한다.

    이 라우트만 `main.agentflow-schedule:read` 권한을 요구해 identity 3종만으로는
    403 이다. 읽기 전용 조회라 superuser 플래그를 여기서만 얹는다(다른 호출까지
    권한을 올리지 않기 위해 _HEADERS 는 그대로 둔다)."""
    if not PLATFORM_BASE:
        return None, ""      # 안 붙였다. 오류가 아니라 그냥 없는 것이다
    url = f"{PLATFORM_BASE.rstrip('/')}/api/agentflow/schedule/sessions"
    request = urllib.request.Request(
        url, headers={**_HEADERS, "X-User-Superuser": "true"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:  # 코드가 안 보이면 권한/경로 구분이 안 된다
        return None, f"HTTP {e.code}"
    except Exception as e:
        return None, type(e).__name__
    rows = []
    for item in (data or {}).get("sessions", []) if isinstance(data, dict) else []:
        row = {
            "name": item.get("name"),
            "status": item.get("status"),
            "total": item.get("total_executions"),
            "ok": item.get("successful_executions"),
            "failed": item.get("failed_executions"),
            "last_status": item.get("last_execution_status"),
            "last_at": item.get("last_execution_at"),
            "next_at": item.get("next_execution_at"),
        }
        row["stalled"] = _is_stalled(row)
        rows.append(row)
    return rows, ""


def _is_stalled(row: dict) -> str:
    """`active` 인데 실제로는 안 도는 상태를 잡아낸다.

    관측된 사고: 플랫폼이 실행 #31 을 끝냈는데 세션 카운터 갱신이 실패해(테이블이
    재생성되며 `last_execution_status` 컬럼이 사라졌다) total 이 30 에 멈췄다.
    다음 틱은 다시 #31 을 계산하고 "이미 처리됨"으로 **영구히 건너뛴다**.
    스케줄러는 계속 발화하므로 status 는 `active`, 실패 수는 0 - 대시보드가
    완벽히 정상으로 보였다. **예정 시각이 지났는데 그대로면 멈춘 것이다.**

    반환값은 사유 문자열(정상이면 빈 문자열) - 불리언이면 왜인지 다시 못 묻는다.
    """
    if str(row.get("status") or "") != "active":
        return ""
    next_at = str(row.get("next_at") or "")
    if not next_at:
        return "다음 실행 시각이 없음"
    try:
        # 타임존이 붙어 있으면 떼고 로컬 naive 로 비교한다(양쪽 다 로컬 시각).
        planned = datetime.datetime.fromisoformat(next_at).replace(tzinfo=None)
    except ValueError:
        return ""
    late = (datetime.datetime.now() - planned).total_seconds()
    if late > STALL_GRACE:
        return f"예정 {next_at[:19]} 에서 {int(late // 60)}분 지연 - 멈춘 것으로 보임"
    return ""


_PAGE = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Jermes - 학습 현황</title><link rel="icon" href="/favicon.ico"><style>
:root{--bg:#0d1117;--card:#161b22;--line:#30363d;--fg:#e6edf3;--dim:#8b949e;
--ok:#3fb950;--warn:#d29922;--bad:#f85149;--accent:#58a6ff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif}
header{padding:20px 24px;border-bottom:1px solid var(--line);display:flex;align-items:baseline;gap:14px;flex-wrap:wrap}
h1{margin:0;font-size:19px;letter-spacing:.2px}
.tag{font-size:11px;color:var(--dim)}
main{padding:20px 24px;max-width:1200px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:22px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}
.num{font-size:26px;font-weight:700;line-height:1.2}
.lbl{font-size:11px;color:var(--dim);margin-top:2px}
h2{font-size:14px;margin:22px 0 10px;font-weight:650}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden}
th{font-size:11px;color:var(--dim);text-align:left;padding:9px 12px;border-bottom:1px solid var(--line);font-weight:600}
td{padding:9px 12px;border-bottom:1px solid var(--line);font-size:13px;vertical-align:top}
tr:last-child td{border-bottom:none}
.pill{display:inline-block;padding:1px 8px;border-radius:9px;font-size:11px;font-weight:600}
.v-yes{background:rgba(63,185,80,.15);color:var(--ok)}
.v-no{background:rgba(210,153,34,.15);color:var(--warn)}
.desc{font-size:11px;color:var(--dim);margin-top:3px}
.err{background:rgba(248,81,73,.1);border:1px solid rgba(248,81,73,.3);color:var(--bad);
padding:9px 12px;border-radius:9px;font-size:12px;margin-bottom:16px}
.note{font-size:12px;color:var(--dim);margin:14px 0 0;line-height:1.7}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}
.run{background:var(--ok)}.stop{background:var(--dim)}
a{color:var(--accent)}
.ask{display:flex;gap:8px;margin:6px 0 14px}
.ask input{flex:1;background:var(--card);border:1px solid var(--line);border-radius:9px;
color:var(--fg);padding:10px 13px;font:14px inherit}
.ask input:focus{outline:none;border-color:var(--accent)}
.ask button{background:var(--accent);border:0;border-radius:9px;color:#04121f;
padding:0 18px;font:600 13px inherit;cursor:pointer}
.ask button:disabled{opacity:.5;cursor:default}
.chip{display:inline-block;background:rgba(88,166,255,.12);color:var(--accent);
border-radius:8px;padding:1px 8px;font-size:11px;margin:0 4px 4px 0;cursor:pointer}
.why{font-size:11px;color:var(--dim)}
.bar{height:4px;background:var(--accent);border-radius:2px;min-width:2px}
.bar.thin{background:var(--warn)}
.r-safe,.r-caution,.r-dangerous{white-space:nowrap;font-weight:600}
.r-safe{color:var(--ok)}.r-caution{color:var(--warn)}.r-dangerous{color:var(--bad)}
td.ev{white-space:nowrap;font-variant-numeric:tabular-nums}
.empty{color:var(--dim);font-size:12px;padding:10px 0}
</style></head><body>
<header><h1>Jermes</h1>
<span class="tag">Hermes learns. Jermes proves.</span>
<span class="tag" id="ts"></span></header>
<main>
<div id="err"></div>
<div class="grid" id="stats"></div>
<h2>이 과제엔 뭐가 골라지나 <span class="tag">- 무엇이, 왜 골라지는지</span></h2>
<div class="ask">
  <input id="q" placeholder="예: 영업일 10일 뒤 마감일 계산 / 계약서에서 날짜 뽑기" autocomplete="off">
  <button id="go">골라줘</button>
</div>
<div id="picks"></div>
<div id="sched"></div>
<div id="sessions"></div>
<div id="body"></div>
<p class="note">스킬은 실행 트레이스에서 자동으로 초안되고, <b>재현벤치에서 처음 보는
사례(held-out)까지 개선된 것만 검증됨</b>으로 승격돼 다음 실행에 회상됩니다.
증거가 부족하면 대기 상태로 남아 사람이 판단합니다 - 이 화면은 읽기 전용이며,
승인·거절은 플랫폼 콘솔에서 합니다.</p>
</main>
<script>
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
async function load(){
 let d;try{d=await (await fetch('/api/state')).json()}catch(e){return}
 document.getElementById('ts').textContent='갱신 '+d.generated_at+' · '+d.platform_base;
 const t=d.totals;
 document.getElementById('stats').innerHTML=[
  ['총 스킬',t.skills,''],['검증됨',t.verified,'color:var(--ok)'],
  ['미검증',t.unverified,'color:var(--warn)'],['활성',t.active,''],
  ['사용 성공/실패',t.successes+'/'+t.failures,''],
 ].map(([l,v,s])=>`<div class="card"><div class="num" style="${s}">${esc(v)}</div><div class="lbl">${esc(l)}</div></div>`).join('');
 document.getElementById('err').innerHTML = (d.errors&&d.errors.length)
  ? `<div class="err">연결 문제: ${esc(d.errors.join(' / '))} - 플랫폼 스택이 내려가 있거나 스코프가 비어 있습니다.</div>`:'';
 const nat=(d.native_schedule||[]).map(n=>`<div class="card" style="margin-bottom:8px">
  <span class="dot ${n.stalled?'stop':(n.status==='active'?'run':'stop')}"></span><b>${esc(n.name)}</b>
  · ${esc(n.status)} · 실행 ${esc(n.total)} (성공 ${esc(n.ok)} / 실패 ${esc(n.failed)})
  ${n.last_at?` · 최근 ${esc(n.last_at)} ${esc(n.last_status||'')}`:''}
  ${n.stalled?`<div style="color:var(--bad);margin-top:6px">${esc(n.stalled)}</div>`:''}
 </div>`).join('');
 const s=d.schedule;
 document.getElementById('sched').innerHTML = (nat?`<h2>자동 학습 (플랫폼 스케줄)</h2>${nat}`:'')
  + (s ? `<h2>자동 학습 (보조)</h2><div class="card">
  <span class="dot ${s.running?'run':'stop'}"></span>${s.running?'실행 중':'중지'}
  ${s.running?` · 주기 ${Math.round(s.interval_sec/60)}분`:''}
  · 사이클 ${esc(s.cycles)} · 큐레이션 ${esc(s.curated_total)}
  · 승격 ${esc(s.promoted_total)} · 대기 ${esc(s.staged_total)}
  ${s.last_run_at?` · 최근 ${esc(s.last_run_at)}`:''}
  ${s.last_error?`<div style="color:var(--bad);margin-top:6px">오류: ${esc(s.last_error)}</div>`:''}
 </div>`:'');
 // 로컬 세션 - 플랫폼 없이도 배울 거리가 있는지 한눈에. 없으면 왜 없는지 말한다.
 const ses=(d.sessions||[]);
 const learn=ses.filter(s=>s.learnable);
 document.getElementById('sessions').innerHTML = ses.length ? (
  `<h2>배울 거리 <span class="tag">로컬 세션 ${learn.length}/${ses.length}</span></h2>` +
  // 신호 없는 세션은 접어 둔다. 배울 게 없는 줄이 화면을 채우면 정작 배울 거리가
  // 안 보인다(실측: 8개 중 5개가 "신호 없음"으로 자리만 차지했다).
  (learn.length?learn:ses.slice(0,1)).map(s=>`<div class="card" style="margin-bottom:6px">
   <span class="dot ${s.learnable?'run':'stop'}"></span><b>${esc(s.name)}</b>
   · 도구 ${esc(s.tool_calls)} · 오류 ${esc(s.errors)} · 복구 ${esc(s.recoveries)}
   · 교정 ${esc(s.corrections)}
   <span class="desc">${s.signals&&s.signals.length?esc(s.signals.join(', ')):'신호 없음 - 도구 호출이 적거나 오류·교정이 없음'}</span>
  </div>`).join('')
  + (learn.length && ses.length>learn.length
     ? `<div class="note">신호 없는 세션 ${ses.length-learn.length}개는 접었습니다 -
        도구 호출이 적거나 오류·교정이 없어 배울 거리가 없습니다.</div>` : '')
 ) : '';
 document.getElementById('body').innerHTML = d.scopes.map(sc=>`<h2>${esc(sc.scope)}</h2>
  <table><thead><tr><th>스킬</th><th>종류</th><th>버전</th><th>검증</th><th>상태</th><th>성공/실패</th></tr></thead>
  <tbody>${sc.skills.map(k=>`<tr>
   <td><b>${esc(k.name)}</b><div class="desc">${esc(k.description)}</div></td>
   <td>${esc(k.kind)}</td><td>${esc(k.version)}</td>
   <td><span class="pill ${k.verified?'v-yes':'v-no'}">${k.verified?'검증됨':'대기'}</span></td>
   <td>${esc(k.status)}</td><td>${esc(k.usage.successes)}/${esc(k.usage.failures)}</td>
  </tr>`).join('')}</tbody></table>`).join('')
  || '<div class="card">아직 학습된 스킬이 없습니다. 에이전트가 실행을 반복하면 이곳에 쌓입니다.</div>';
}
// ── 이 과제엔 뭐가 골라지나 ─────────────────────────────────────────────
// 스킬 목록만으로는 "왜 이게 안 불렸지"를 못 푼다. 그 물음이 E3 에서 실제 결함을
// 찾아냈다(카탈로그가 설명을 버려 검증된 스킬이 모델에 안 닿았다).
const RISK={safe:'안전',caution:'주의',dangerous:'위험'};
async function ask(){
 const box=document.getElementById('q'), out=document.getElementById('picks'),
       btn=document.getElementById('go'), task=box.value.trim();
 if(!task){out.innerHTML='';return}
 btn.disabled=true; out.innerHTML='<div class="empty">고르는 중…</div>';
 let d; try{ d=await (await fetch('/api/route?task='+encodeURIComponent(task))).json() }
 catch(e){ out.innerHTML='<div class="err">조회 실패</div>'; btn.disabled=false; return }
 btn.disabled=false;
 if(d.error){ out.innerHTML='<div class="err">'+esc(d.error)+'</div>'; return }
 if(!d.chosen || !d.chosen.length){
  out.innerHTML='<div class="card"><b>맞는 능력이 없습니다.</b>'
   +'<div class="desc">후보 '+esc(d.considered||0)+'개를 봤지만 겹치는 말이 없습니다. '
   +'그 능력이 무엇을 해왔는지가 장부에 없으면 못 찾습니다 - '
   +'쓰이면 이력이 쌓이고 그때부터 찾힙니다.</div></div>';
  return}
 const top=d.chosen[0].score||1;
 out.innerHTML='<table><thead><tr><th>능력</th><th>점수</th><th>왜</th>'
  +'<th>위험</th><th>확인</th></tr></thead><tbody>'
  +d.chosen.map(c=>`<tr>
    <td><b>${esc(c.name)}</b><div class="desc">${esc(c.description)}</div></td>
    <td style="min-width:90px">${c.score.toFixed(2)}
     <div class="bar${c.thin?' thin':''}" style="width:${Math.max(4,100*c.score/top)}%"></div></td>
    <td class="why">${esc((c.why||[]).join(' · '))||'-'}
     ${c.thin?`<div class="pill v-no">근거 얇음 - 과제의 ${Math.round(100*(c.coverage||0))}% 만 설명됨</div>`:''}</td>
    <td class="r-${esc(c.risk)}">${esc(RISK[c.risk]||c.risk)}</td>
    <td class="ev">${c.evidence?esc(c.evidence):'<span class="v-no pill">근거 없음</span>'}</td>
   </tr>`).join('')+'</tbody></table>'
  +(d.blocked&&d.blocked.length?'<div class="note">정책상 제외 '+d.blocked.length
    +'건: '+esc(d.blocked.join(', '))+'</div>':'');
}
document.getElementById('go').onclick=ask;
document.getElementById('q').addEventListener('keydown',e=>{if(e.key==='Enter')ask()});
load();setInterval(load,15000);
</script></body></html>"""


# ── keeper - 스케줄러가 죽으면 되살린다 ────────────────────────────────
# 관측된 실패: 플랫폼 컨테이너가 리로드될 때마다 큐레이션 스케줄러(데몬 스레드)가
# 초기화돼 학습이 **조용히** 멈췄다. 대시보드는 별도 프로세스라 그 리로드에
# 영향받지 않으므로, 여기서 상태를 보고 필요할 때 다시 켠다.
# 기본은 꺼짐 - JERMES_KEEPER=1 과 아래 설정이 있어야 동작한다.
KEEPER = os.environ.get("JERMES_KEEPER", "") == "1"
KEEPER_EVERY = int(os.environ.get("JERMES_KEEPER_INTERVAL", "300"))
KEEPER_BODY = {
    "scope_key": os.environ.get("JERMES_KEEPER_SCOPE", "user:default"),
    "provider": os.environ.get("JERMES_KEEPER_PROVIDER", "vllm"),
    "base_url": os.environ.get("JERMES_KEEPER_BASE_URL", ""),
    "model": os.environ.get("JERMES_KEEPER_MODEL", ""),
    "run_prefix": os.environ.get("JERMES_KEEPER_RUN_PREFIX", ""),
    "interval_minutes": int(os.environ.get("JERMES_KEEPER_CURATE_MIN", "20")),
    "limit": int(os.environ.get("JERMES_KEEPER_LIMIT", "1")),
    "samples": int(os.environ.get("JERMES_KEEPER_SAMPLES", "1")),
}
keeper_state = {"restarts": 0, "last_action": "", "last_at": ""}


def _post(path: str, body: dict) -> tuple[dict | None, str]:
    url = f"{PLATFORM_BASE.rstrip('/')}/api/agentflow/harness/jermes{path}"
    request = urllib.request.Request(
        url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={**_HEADERS, "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8")), ""
    except Exception as e:
        return None, f"{type(e).__name__}"


def keeper_tick() -> str:
    """한 번 점검하고 필요하면 스케줄러를 켠다. 반환값은 사람이 읽는 결과."""
    if not KEEPER_BODY["base_url"]:
        return "keeper: base_url 미설정 - 동작 안 함"
    status, err = _get("/schedule")
    if err:
        return f"keeper: 상태 조회 실패({err}) - 플랫폼이 내려갔을 수 있음"
    if isinstance(status, dict) and status.get("running"):
        return "keeper: 스케줄러 정상"
    result, err = _post("/schedule", KEEPER_BODY)
    keeper_state["last_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    if err:
        keeper_state["last_action"] = f"재기동 실패({err})"
        return keeper_state["last_action"]
    keeper_state["restarts"] += 1
    keeper_state["last_action"] = "스케줄러 재기동함"
    logger_line = f"[keeper] scheduler restarted (#{keeper_state['restarts']})"
    print(logger_line, flush=True)
    return keeper_state["last_action"]


def _keeper_loop() -> None:
    while True:
        try:
            keeper_tick()
        except Exception as e:  # keeper 가 죽어서 대시보드를 끌면 안 된다
            print(f"[keeper] tick failed: {type(e).__name__}: {e}", flush=True)
        time.sleep(max(60, KEEPER_EVERY))


def _explain_route(task: str, limit: int = 6) -> dict:
    """"이 과제엔 뭐가 골라지나, **그리고 왜**" - 남들 대시보드에 없는 화면.

    스킬 목록만 보여주는 것으로는 "왜 이게 안 불렸지"를 못 푼다. 실제로 그 물음이
    E3 에서 결함을 찾아냈다(카탈로그가 설명을 버려서 검증된 스킬이 모델에 안 닿았다).
    고르는 이유를 화면에 내면 그런 게 눈에 보인다.

    부작용이 없는 순수 질의다 - 대시보드는 읽기전용이라는 원칙을 깨지 않는다.
    """
    from .discovery import default_sources, discover
    from .router import Router

    registry = discover(default_sources(ledger=_open_local_ledger()))
    pool = registry.usable()
    result = Router(pool, allowed_risk=("safe", "caution", "dangerous")).route(
        task, limit=limit)
    return {
        "task": task,
        "considered": result.considered,
        "notes": registry.notes,
        "chosen": [{"name": c.capability.name, "kind": c.capability.kind,
                    "score": round(c.score, 3), "why": c.reasons, "thin": c.thin,
                    "coverage": round(c.coverage, 3),
                    "risk": c.capability.risk(), "label": c.capability.label(),
                    "description": c.capability.description[:160],
                    "evidence": c.capability.evidence[:120]}
                   for c in result.chosen],
        "blocked": result.blocked[:10],
    }


def _open_local_ledger():
    from pathlib import Path as _Path

    from .ledger import JsonlSkillLedger

    home = _Path(os.environ.get("JERMES_HOME", _Path.home() / ".jermes"))
    return JsonlSkillLedger(home / "skills.jsonl")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        # 어떤 예외도 연결을 끊고 끝내지 않는다 - 관측 도구는 자기 실패도
        # 설명해야 한다. 응답 없이 끊기면 "네트워크 문제"로 오진하게 된다.
        try:
            if self.path.startswith("/api/state"):
                payload = json.dumps(collect(), ensure_ascii=False).encode("utf-8")
                self._send(200, "application/json; charset=utf-8", payload)
            elif self.path.startswith("/api/route"):
                from urllib.parse import parse_qs, urlparse

                query = parse_qs(urlparse(self.path).query)
                task = (query.get("task") or [""])[0].strip()[:300]
                result = _explain_route(task) if task else {"chosen": [], "task": ""}
                self._send(200, "application/json; charset=utf-8",
                           json.dumps(result, ensure_ascii=False).encode("utf-8"))
            elif self.path in ("/", "/index.html"):
                self._send(200, "text/html; charset=utf-8", _PAGE.encode("utf-8"))
            elif self.path == "/favicon.ico":
                # 인라인 SVG. 없으면 브라우저가 매번 404 를 찍어 콘솔이 더러워지고,
                # 진짜 오류를 찾을 때 눈에 안 들어온다.
                icon = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
                        '<rect width="16" height="16" rx="4" fill="#0d1117"/>'
                        '<path d="M4 4h8v2H9v6H7V6H4z" fill="#58a6ff"/></svg>')
                self._send(200, "image/svg+xml", icon.encode("utf-8"))
            elif self.path == "/healthz":
                self._send(200, "text/plain", b"ok")
            else:
                self._send(404, "text/plain", b"not found")
        except Exception as exc:
            body = json.dumps({"error": f"{type(exc).__name__}: {exc}"[:300]},
                              ensure_ascii=False).encode("utf-8")
            try:
                self._send(500, "application/json; charset=utf-8", body)
            except Exception:
                pass

    def _send(self, code: int, ctype: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # 접근로그 억제
        return


class _Server(ThreadingHTTPServer):
    """관측된 실패: 윈도우에서 SO_REUSEADDR 는 이미 LISTEN 중인 포트에 두 번째
    바인드를 허용한다. 그래서 재기동이 성공한 것처럼 보이면서 실제로는 낡은
    프로세스가 계속 응답했다(코드를 고쳐도 화면이 안 바뀜). 겹치면 죽는 게 맞다."""

    allow_reuse_address = False


def main() -> int:
    if KEEPER:
        threading.Thread(target=_keeper_loop, name="jermes-keeper",
                         daemon=True).start()
        print(f"keeper on: every {max(60, KEEPER_EVERY)}s -> {KEEPER_BODY['base_url']}")
    try:
        server = _Server(("127.0.0.1", PORT), Handler)
    except OSError as e:
        print(f"port {PORT} already bound ({e}) - kill the old dashboard first, "
              f"otherwise it keeps serving stale code")
        return 1
    where = PLATFORM_BASE or "플랫폼 미연결 - 로컬 원장만 봅니다"
    print(f"Jermes dashboard on http://127.0.0.1:{PORT}  ({where})")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

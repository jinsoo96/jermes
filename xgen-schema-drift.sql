-- XGEN 로컬 dev 스택의 스키마 드리프트 보정 (2026-07-29)
--
-- Jermes 와 무관한 XGEN 자체 문제다. 코드는 컬럼/제약을 쓰는데 DB 에는 없어서
-- 로컬에서 **하네스가 아예 실행되지 않았다**(`[loopback] durable begin failed
-- type=InvalidColumnReference` → `execution record could not be started`).
-- 새 하네스 런이 안 생기니 Jermes 도 배울 재료가 없었다.
--
-- 전부 가산적·멱등이라 여러 번 돌려도 안전하다. 적용 후 같은 워크플로우가
-- 완주했고(`[harness] done — output 318 chars · 1 iterations`) DB 오류는 0건.
--
--   docker exec xgen-harness-postgresql-1 sh -c \
--     'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1' < xgen-schema-drift.sql
--
-- ⚠️ 적용 범위: **로컬 dev 스택만**. dev/stg/prod 도 같은 드리프트가 있을 수 있고,
-- 더 근본적으로는 XGEN 의 `insert_record` 가 INSERT 실패를 삼키고 성공 로그 + HTTP 200
-- 을 반환하는 문제를 고쳐야 한다 — 그래서 이것들이 지금까지 안 보였다.

-- 1) 하네스를 죽이던 원인. 코드는 ON CONFLICT(interaction_id) 로 세션을 선점하는데
--    유니크 제약이 없어 매번 실패했다. 정본 DDL 은 interaction_id ... NOT NULL UNIQUE.
CREATE UNIQUE INDEX IF NOT EXISTS uq_harness_session_owner_interaction
    ON harness_session_owner (interaction_id);

-- 2) 2026-06 커밋(f5d2cb0e "mint execution_id per run")이 코드에만 반영되고
--    모델/DDL 에는 안 들어갔다. 둘 다 nullable 이 코드의 전제.
ALTER TABLE node_performance ADD COLUMN IF NOT EXISTS interaction_id VARCHAR(500);
ALTER TABLE node_performance ADD COLUMN IF NOT EXISTS execution_id   VARCHAR(200);

-- 3) 트레이스 집계 컬럼. trace_collector 가 항상 넣는다.
ALTER TABLE agent_traces ADD COLUMN IF NOT EXISTS total_errors   INTEGER DEFAULT 0;
ALTER TABLE agent_traces ADD COLUMN IF NOT EXISTS total_warnings INTEGER DEFAULT 0;

-- 4) span 분류 기준(trace_collector.py: level = error | warning | info, 기본 info).
ALTER TABLE agent_trace_spans ADD COLUMN IF NOT EXISTS level VARCHAR(20) DEFAULT 'info';

-- 5) 스케줄 세션 생성이 HTTP 200 을 반환하면서 DB 0행이던 원인(먼저 발견분).
--    ⚠️ 이 컬럼은 **다시 사라진다**. 모델/DDL 정의에 없어서 테이블이 재생성될 때마다
--    빠지고, 그러면 실행 후 세션 카운터 UPDATE 가 실패한다(그리고 그 실패는 삼켜진다).
ALTER TABLE schedule_sessions ADD COLUMN IF NOT EXISTS last_execution_status VARCHAR(50);

-- 6) 위 컬럼이 사라졌던 동안 카운터가 멈춰 생긴 **영구 스킵** 복구.
--    증상: 스케줄러는 계속 발화하는데 로그에 "이미 처리된 실행입니다. 중복 실행 방지"
--    만 남고 아무 것도 안 돈다. 원인 = 실행 #N 은 끝났는데 세션의 total_executions 가
--    N-1 에 멈춰, 다음 틱이 다시 #N 을 계산하고 "이미 성공"이라 건너뛴다(무한).
--    status=active · 실패 0 이라 겉보기엔 완벽히 정상이다.
--    아래는 실행 로그를 정본으로 삼아 카운터를 되맞춘다. 멱등(이미 맞으면 0행).
UPDATE schedule_sessions s SET
    total_executions      = l.total,
    successful_executions = l.ok,
    failed_executions     = l.failed,
    last_execution_status = l.last_status,
    last_execution_at     = l.last_at
FROM (SELECT session_id,
             count(*)                                        AS total,
             count(*) FILTER (WHERE status = 'success')       AS ok,
             count(*) FILTER (WHERE status = 'failed')        AS failed,
             (array_agg(status ORDER BY execution_number DESC))[1] AS last_status,
             max(completed_at)                                AS last_at
      FROM schedule_execution_logs GROUP BY session_id) l
WHERE s.session_id = l.session_id AND s.total_executions <> l.total;

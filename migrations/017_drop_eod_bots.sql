-- 017_drop_eod_bots.sql
--
-- 모의 트레이딩 기능(EOD 봇 시뮬레이션) 폐기.
-- 016_eod_bots.sql 에서 만든 모든 테이블/뷰/인덱스를 제거한다.
--
-- 순서:
--   1) 뷰 먼저 (테이블 의존)
--   2) 테이블 (자식 → 부모. FK CASCADE 가 있어 부모만 drop 해도 되지만
--      명시적으로 자식부터 drop 하여 의도를 분명히 함)
--
-- 016 마이그레이션을 이미 적용한 DB 에서 본 파일이 멱등하게 동작하도록
-- 모든 객체에 IF EXISTS 사용.

-- 1) Views
DROP VIEW IF EXISTS v_eod_bot_equity_curve;
DROP VIEW IF EXISTS v_eod_bot_summary;

-- 2) 자식 테이블 → 부모 테이블 순서로 drop
DROP TABLE IF EXISTS eod_bot_runs          CASCADE;
DROP TABLE IF EXISTS eod_bot_daily_pnl     CASCADE;
DROP TABLE IF EXISTS eod_bot_positions     CASCADE;
DROP TABLE IF EXISTS eod_bot_orders        CASCADE;
DROP TABLE IF EXISTS eod_bot_spec_history  CASCADE;
DROP TABLE IF EXISTS eod_bots              CASCADE;

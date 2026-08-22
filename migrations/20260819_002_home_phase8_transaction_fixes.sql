-- ============================================================
-- Phase 8: 种植与烹饪事务修复
-- 迁移名: home_phase8_transaction_fixes
-- 性质: CREATE OR REPLACE FUNCTION，签名不变，无 DELETE/DROP/TRUNCATE
-- ============================================================

-- 1. rpc_home_eat_dish: 锁定顺序改为 state→dish（与 feed_member 一致），增加 HOME_STATE_NOT_FOUND
-- 2. rpc_home_water_plant: 初始 SELECT 增加 FOR UPDATE，防止并发收获后仍浇水
-- 3. rpc_home_harvest_plant: stage 比较增加 COALESCE 防 NULL 旁路

-- 注: 实际 SQL 通过 Supabase apply_migration 执行，此文件为版本记录。
-- 函数签名均不变。

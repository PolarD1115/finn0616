-- ============================================================
-- 迁移：Phase 7 Advisor 修复
-- 目标：
--   1. 删除重复索引
--   2. REVOKE anon/authenticated 对 SECURITY DEFINER 函数的 EXECUTE 权限
-- ============================================================

-- ============================================================
-- 1. 删除重复索引
-- ============================================================
DROP INDEX IF EXISTS idx_chat_messages_assistant;
DROP INDEX IF EXISTS idx_memory_summaries_created;

-- ============================================================
-- 2. REVOKE EXECUTE：从 anon/authenticated 移除 SECURITY DEFINER 函数权限
-- ============================================================

-- Helper 函数（wallet 系统）
REVOKE EXECUTE ON FUNCTION _bj_week_start(timestamp with time zone) FROM anon, authenticated;
REVOKE EXECUTE ON FUNCTION _is_birthday_week(timestamp with time zone) FROM anon, authenticated;

-- Agent 系统函数
REVOKE EXECUTE ON FUNCTION agent_claim_jobs(text, text, integer) FROM anon, authenticated;
REVOKE EXECUTE ON FUNCTION agent_enqueue_job(text, text, timestamp with time zone, integer, jsonb, text, integer) FROM anon, authenticated;
REVOKE EXECUTE ON FUNCTION agent_heartbeat(text, text, integer) FROM anon, authenticated;
REVOKE EXECUTE ON FUNCTION agent_log(text, text, text, jsonb) FROM anon, authenticated;
REVOKE EXECUTE ON FUNCTION agent_mark_job(uuid, text, jsonb, text) FROM anon, authenticated;
REVOKE EXECUTE ON FUNCTION agent_reclaim_stale_jobs(text, integer) FROM anon, authenticated;
REVOKE EXECUTE ON FUNCTION agent_try_become_leader(text, text, integer) FROM anon, authenticated;

-- House 系统 RPC
REVOKE EXECUTE ON FUNCTION rpc_house_do(text, text, text, text, text, text[]) FROM anon, authenticated;
REVOKE EXECUTE ON FUNCTION rpc_house_look(text) FROM anon, authenticated;
REVOKE EXECUTE ON FUNCTION rpc_house_put(text, text, text, text) FROM anon, authenticated;
REVOKE EXECUTE ON FUNCTION rpc_house_take(uuid) FROM anon, authenticated;
REVOKE EXECUTE ON FUNCTION rpc_house_update_desc(text, text) FROM anon, authenticated;

-- Wallet 系统 RPC
REVOKE EXECUTE ON FUNCTION rpc_wallet_check(text, numeric, boolean) FROM anon, authenticated;
REVOKE EXECUTE ON FUNCTION rpc_wallet_earn(text, numeric, text, text, jsonb, numeric, numeric, boolean) FROM anon, authenticated;
REVOKE EXECUTE ON FUNCTION rpc_wallet_exchange(text, text, text, jsonb, numeric, numeric, boolean) FROM anon, authenticated;
REVOKE EXECUTE ON FUNCTION rpc_wallet_log(text, integer, integer) FROM anon, authenticated;
REVOKE EXECUTE ON FUNCTION rpc_wallet_overtime_withdraw(text, numeric, text, jsonb, numeric) FROM anon, authenticated;
REVOKE EXECUTE ON FUNCTION rpc_wallet_spend(text, numeric, text, jsonb) FROM anon, authenticated;

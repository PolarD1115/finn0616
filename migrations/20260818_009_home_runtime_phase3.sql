-- ============================================================
-- 20260818_009_home_runtime_phase3.sql
-- Phase 3: RLS 收紧 + 成员初始化 + 状态结算 + 生活动作 RPC
-- 无 DELETE / DROP / TRUNCATE，不影响旧表
-- ============================================================

-- Part 1: ALTER POLICY 收紧访问（不删除 Policy）
ALTER POLICY home_events_select_all ON public.home_events
    TO authenticated USING (visibility NOT IN ('private','system'));
ALTER POLICY home_rooms_select_all ON public.home_rooms TO authenticated USING (true);
ALTER POLICY home_members_select_all ON public.home_members TO authenticated USING (true);
ALTER POLICY home_member_states_select_all ON public.home_member_states TO authenticated USING (true);
ALTER POLICY home_objects_select_all ON public.home_objects TO authenticated USING (true);
ALTER POLICY home_action_runs_select_all ON public.home_action_runs TO authenticated USING (true);
ALTER POLICY home_jobs_select_all ON public.home_jobs TO authenticated USING (true);

-- Part 2-7: RPC 函数定义（与 apply_migration 内容相同，存档用）
-- _home_settle_internal / rpc_home_initialize_members / rpc_home_settle_member
-- rpc_home_enter_room / rpc_home_rest / rpc_home_spend_time
-- （完整定义见 apply_migration home_runtime_phase3_rls_and_rpcs）

-- Part 8: REVOKE（含 PUBLIC）
REVOKE EXECUTE ON FUNCTION public._home_settle_internal(uuid) FROM anon, authenticated, PUBLIC;
REVOKE EXECUTE ON FUNCTION public.rpc_home_initialize_members() FROM anon, authenticated, PUBLIC;
REVOKE EXECUTE ON FUNCTION public.rpc_home_settle_member(text) FROM anon, authenticated, PUBLIC;
REVOKE EXECUTE ON FUNCTION public.rpc_home_enter_room(text, text, text) FROM anon, authenticated, PUBLIC;
REVOKE EXECUTE ON FUNCTION public.rpc_home_rest(text, text, integer, text) FROM anon, authenticated, PUBLIC;
REVOKE EXECUTE ON FUNCTION public.rpc_home_spend_time(text, text, text, text, integer) FROM anon, authenticated, PUBLIC;

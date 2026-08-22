-- ============================================================
-- Migration: 20260812_008_house_objects_select_rls
-- 给 house_objects 补 SELECT 策略（RLS 已开但无 policy，anon 无法直查）
-- 约束：非破坏性、向后兼容、无 DELETE/DROP/TRUNCATE
-- ============================================================

-- house_objects 表在 003 迁移中已 ENABLE ROW LEVEL SECURITY，
-- 但未定义任何 policy，导致 anon key 直查返回空（RLS 拒绝）。
-- 其余 6 张表（wallet/wallet_log/house_rooms/house_diary/pets/pet_inventory）
-- 都已有 SELECT USING(true) 策略。

DROP POLICY IF EXISTS house_objects_select_all ON public.house_objects;
CREATE POLICY house_objects_select_all ON public.house_objects
    FOR SELECT USING (true);

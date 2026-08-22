-- ============================================================
-- 20260819_003_home_anon_select_policies.sql
-- 修复：garden_observe / pantry_observe 返回全空
-- ============================================================
-- 根因：
--   所有 home_* 表的 RLS SELECT policy 只授权给 {authenticated} 角色，
--   没有一条给 anon。网关 Python（home/repository.py）用 SUPABASE_KEY
--   初始化 client 直查表，实际注入的是 anon key，被 RLS 静默过滤成 0 行。
--   RLS 对不匹配的 SELECT 不报错只返回空，fetch_* 的 try/except 只兜
--   异常不兜空数据，导致 garden_observe/pantry_observe 返回 ok=true 但
--   available_seeds/available_recipes 全空，无报错无日志。
--
-- 修复：
--   给 anon 角色补 SELECT policy，复用各表原有 authenticated policy 的
--   qual 过滤，使 anon 与 authenticated 可见范围一致。
--   home_private_diaries 原 qual=false（owner 专属），不给 anon 加。
--
-- 实际通过 apply_migration home_runtime_anon_select_policies 执行。
-- 本文件为存档，可重复执行（policy 名唯一，重复执行前需先 DROP）。
-- ============================================================

-- 公共基础数据（qual = true）
CREATE POLICY home_seed_catalog_select_anon
  ON public.home_seed_catalog FOR SELECT TO anon USING (true);

CREATE POLICY home_recipe_catalog_select_anon
  ON public.home_recipe_catalog FOR SELECT TO anon USING (true);

CREATE POLICY home_plants_select_anon
  ON public.home_plants FOR SELECT TO anon USING (true);

CREATE POLICY home_inventory_select_anon
  ON public.home_inventory FOR SELECT TO anon USING (true);

CREATE POLICY home_dishes_select_anon
  ON public.home_dishes FOR SELECT TO anon USING (true);

CREATE POLICY home_rooms_select_anon
  ON public.home_rooms FOR SELECT TO anon USING (true);

CREATE POLICY home_members_select_anon
  ON public.home_members FOR SELECT TO anon USING (true);

CREATE POLICY home_member_states_select_anon
  ON public.home_member_states FOR SELECT TO anon USING (true);

CREATE POLICY home_objects_select_anon
  ON public.home_objects FOR SELECT TO anon USING (true);

CREATE POLICY home_action_runs_select_anon
  ON public.home_action_runs FOR SELECT TO anon USING (true);

CREATE POLICY home_jobs_select_anon
  ON public.home_jobs FOR SELECT TO anon USING (true);

-- 带可见性过滤的表（复用原 authenticated policy 的 qual）
CREATE POLICY home_events_select_anon
  ON public.home_events FOR SELECT TO anon
  USING (visibility <> ALL (ARRAY['private'::text, 'system'::text]));

CREATE POLICY home_notes_select_anon
  ON public.home_notes FOR SELECT TO anon
  USING ((status <> 'archived'::text) AND (visibility <> 'private'::text));

CREATE POLICY home_letters_select_anon
  ON public.home_letters FOR SELECT TO anon
  USING (status <> 'archived'::text);

-- home_private_diaries：原 qual=false（owner 专属），不给 anon 加，保持不可读

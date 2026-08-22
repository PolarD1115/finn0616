-- ============================================================
-- 20260820_001: observe 前批量结算植物
-- ============================================================
-- 问题：garden_observe / build_home_context 只做 SELECT，不触发
--   _home_plant_settle，导致 AI 看到的 stage/health/water_level
--   是上次种植/浇水/收获时的 stale 快照。
--   典型症状：植物按时间早已成熟，但 observe 仍显示 growing，
--   AI 判断"没熟"不去收获，成熟的植物烂在地里。
--
-- 修复：新增 rpc_home_settle_plants()，批量结算所有 active 植物
--   后返回完整列表。Python 侧 fetch_plants_settled() 调用此 RPC，
--   service_role 不可用时降级为不结算的 fetch_plants()。
--
-- 部署方式：已通过 Supabase apply_migration 部署到生产库，
--   本文件为仓库归档。
-- ============================================================

CREATE OR REPLACE FUNCTION public.rpc_home_settle_plants()
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO 'public', 'pg_temp'
AS $function$
DECLARE
    v_plant_ids uuid[];
    v_pid       uuid;
    v_count     int := 0;
    v_plants    jsonb;
BEGIN
    -- 收集所有 active 植物的 ID
    SELECT array_agg(id) INTO v_plant_ids
    FROM home_plants WHERE status = 'active';

    -- 逐株结算（_home_plant_settle 内部有 FOR UPDATE + 幂等短路）
    IF v_plant_ids IS NOT NULL THEN
        FOREACH v_pid IN ARRAY v_plant_ids LOOP
            PERFORM public._home_plant_settle(v_pid);
            v_count := v_count + 1;
        END LOOP;
    END IF;

    -- 返回所有植物（结算后，含 harvested），按创建时间倒序
    SELECT COALESCE(jsonb_agg(to_jsonb(t)), '[]'::jsonb) INTO v_plants
    FROM (
        SELECT * FROM home_plants ORDER BY created_at DESC
    ) t;

    RETURN jsonb_build_object('ok', true, 'plants', v_plants, 'settled_count', v_count);
END;
$function$;

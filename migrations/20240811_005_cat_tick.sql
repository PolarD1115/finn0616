-- ============================================================
-- Migration: 20240811_005_cat_tick
-- Phase 5: 后台 tick、素材和可审计自动收入
-- ============================================================
-- 约束：非破坏性、向后兼容、无 DELETE/DROP/TRUNCATE

-- ============================================================
-- 1. 扩展 pets 表：添加 last_tick_at（若不存在）
-- ============================================================
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'pets' AND column_name = 'last_tick_at') THEN
        ALTER TABLE public.pets ADD COLUMN last_tick_at timestamptz;
    END IF;
END $$;

-- ============================================================
-- 2. 事件队列表（agent_outbound）——若不存在则创建
-- ============================================================
CREATE TABLE IF NOT EXISTS public.agent_outbound (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id text NOT NULL DEFAULT 'pet_house',
    event_type text NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    status text NOT NULL DEFAULT 'pending',
    created_at timestamptz NOT NULL DEFAULT now(),
    processed_at timestamptz,
    retry_count int NOT NULL DEFAULT 0
);

-- 幂等索引
CREATE INDEX IF NOT EXISTS idx_agent_outbound_status_created ON public.agent_outbound (status, created_at);
CREATE INDEX IF NOT EXISTS idx_agent_outbound_agent_status ON public.agent_outbound (agent_id, status);

-- 启用 RLS
ALTER TABLE public.agent_outbound ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS agent_outbound_select_all ON public.agent_outbound;
CREATE POLICY agent_outbound_select_all ON public.agent_outbound FOR SELECT USING (true);
DROP POLICY IF EXISTS agent_outbound_insert_all ON public.agent_outbound;
CREATE POLICY agent_outbound_insert_all ON public.agent_outbound FOR INSERT WITH CHECK (true);
DROP POLICY IF EXISTS agent_outbound_update_all ON public.agent_outbound;
CREATE POLICY agent_outbound_update_all ON public.agent_outbound FOR UPDATE USING (true);

-- ============================================================
-- 3. RPC：宠物状态 tick（elapsed-time 衰减 + 睡眠滞回）
-- ============================================================
CREATE OR REPLACE FUNCTION public.rpc_cat_tick(
    p_user_id text
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER SET search_path = public
AS $$
DECLARE
    v_pet record;
    v_now timestamptz := now();
    v_last_tick timestamptz;
    v_hours numeric;
    v_hunger_delta numeric := 0;
    v_happiness_delta numeric := 0;
    v_cleanliness_delta numeric := 0;
    v_energy_delta numeric := 0;
    v_new_hunger numeric;
    v_new_happiness numeric;
    v_new_cleanliness numeric;
    v_new_energy numeric;
    v_new_status text;
    v_threshold_event text := null;
BEGIN
    -- 稳定查询宠物（FOR UPDATE 防并发）
    SELECT id, hunger, happiness, cleanliness, energy, status, last_tick_at, alert_flags
    INTO v_pet
    FROM public.pets
    WHERE user_id = p_user_id
    ORDER BY user_id, id
    FOR UPDATE;

    IF v_pet IS NULL THEN
        RETURN jsonb_build_object('ok', false, 'error_code', 'PET_NOT_FOUND');
    END IF;

    v_last_tick := COALESCE(v_pet.last_tick_at, v_now);

    -- 幂等：若本次 tick 距离上次不足 1 分钟，跳过（幂等边界）
    -- 首次 tick（last_tick_at IS NULL）时不跳过，直接执行并初始化时间戳
    IF v_pet.last_tick_at IS NOT NULL AND EXTRACT(EPOCH FROM (v_now - v_last_tick)) < 60 THEN
        RETURN jsonb_build_object(
            'ok', true,
            'message', 'tick 间隔过短，跳过',
            'skipped', true
        );
    END IF;

    -- 计算经过的小时数（上限 48h 防断档后暴涨）
    v_hours := LEAST(48, GREATEST(0, EXTRACT(EPOCH FROM (v_now - v_last_tick)) / 3600.0));

    -- 衰减系数（每小时）
    v_hunger_delta := -2.0 * v_hours;
    v_happiness_delta := -1.5 * v_hours;
    v_cleanliness_delta := -1.0 * v_hours;

    -- 睡眠时精力恢复
    IF v_pet.status = 'sleeping' THEN
        v_energy_delta := 2.0 * v_hours;
    END IF;

    -- 新属性值（clamp 0-100）
    v_new_hunger := GREATEST(0, LEAST(100, COALESCE(v_pet.hunger, 50) + v_hunger_delta));
    v_new_happiness := GREATEST(0, LEAST(100, COALESCE(v_pet.happiness, 50) + v_happiness_delta));
    v_new_cleanliness := GREATEST(0, LEAST(100, COALESCE(v_pet.cleanliness, 50) + v_cleanliness_delta));
    v_new_energy := GREATEST(0, LEAST(100, COALESCE(v_pet.energy, 50) + v_energy_delta));

    -- 睡眠滞回
    v_new_status := v_pet.status;
    IF v_pet.status != 'sleeping' AND v_new_energy < 20 THEN
        v_new_status := 'sleeping';
    ELSIF v_pet.status = 'sleeping' AND v_new_energy >= 40 THEN
        v_new_status := 'idle';
    END IF;

    -- 阈值穿越事件：饱食度从 >=30 降到 <30
    IF COALESCE(v_pet.hunger, 50) >= 30 AND v_new_hunger < 30 THEN
        v_threshold_event := 'hungry_cat';
    END IF;

    -- 更新宠物属性
    UPDATE public.pets
    SET
        hunger = v_new_hunger,
        happiness = v_new_happiness,
        cleanliness = v_new_cleanliness,
        energy = v_new_energy,
        status = v_new_status,
        last_tick_at = v_now,
        alert_flags = CASE
            WHEN v_threshold_event IS NOT NULL THEN
                COALESCE(alert_flags, '{}'::jsonb) || jsonb_build_object(v_threshold_event, v_now::text)
            ELSE alert_flags
        END
    WHERE id = v_pet.id;

    -- 写入事件队列（若存在阈值事件）
    IF v_threshold_event IS NOT NULL THEN
        INSERT INTO public.agent_outbound (agent_id, event_type, payload, status, created_at)
        VALUES (
            'pet_house',
            v_threshold_event,
            jsonb_build_object(
                'user_id', p_user_id,
                'pet_id', v_pet.id,
                'old_hunger', v_pet.hunger,
                'new_hunger', v_new_hunger,
                'created_at', v_now
            ),
            'pending',
            v_now
        );
    END IF;

    RETURN jsonb_build_object(
        'ok', true,
        'message', 'tick 完成',
        'hours_elapsed', ROUND(v_hours::numeric, 2),
        'hunger', v_new_hunger,
        'hunger_delta', ROUND(v_hunger_delta::numeric, 2),
        'happiness', v_new_happiness,
        'happiness_delta', ROUND(v_happiness_delta::numeric, 2),
        'cleanliness', v_new_cleanliness,
        'cleanliness_delta', ROUND(v_cleanliness_delta::numeric, 2),
        'energy', v_new_energy,
        'energy_delta', ROUND(v_energy_delta::numeric, 2),
        'status', v_new_status,
        'threshold_event', v_threshold_event
    );
END;
$$;

-- ============================================================
-- 4. RPC：受控换房 + 物品轻微破坏（素材生成）
-- ============================================================
CREATE OR REPLACE FUNCTION public.rpc_cat_room_mischief(
    p_user_id text
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER SET search_path = public
AS $$
DECLARE
    v_pet record;
    v_rooms text[] := ARRAY['living_room', 'bedroom', 'kitchen', 'study', 'balcony'];
    v_new_room text;
    v_target_object record;
    v_now timestamptz := now();
    v_mischief_note text;
BEGIN
    -- 查找宠物
    SELECT id, current_room, status INTO v_pet
    FROM public.pets
    WHERE user_id = p_user_id
    ORDER BY user_id, id
    FOR UPDATE;

    IF v_pet IS NULL THEN
        RETURN jsonb_build_object('ok', false, 'error_code', 'PET_NOT_FOUND');
    END IF;

    -- 睡眠中不捣乱
    IF v_pet.status = 'sleeping' THEN
        RETURN jsonb_build_object('ok', true, 'message', '小满正在睡觉，没有捣乱', 'skipped', true);
    END IF;

    -- 随机换房（排除当前房间）
    SELECT ARRAY_AGG(r) INTO v_new_room
    FROM unnest(v_rooms) r
    WHERE r != v_pet.current_room;

    v_new_room := (SELECT r FROM unnest(v_rooms) r WHERE r != v_pet.current_room ORDER BY random() LIMIT 1);

    -- 找当前房间的一个物品（轻微破坏：修改 note/position，不删除）
    SELECT id, name, description
    INTO v_target_object
    FROM public.house_objects
    WHERE room_id = v_pet.current_room
    ORDER BY random()
    LIMIT 1;

    -- 执行换房
    UPDATE public.pets
    SET current_room = v_new_room,
        last_care = v_now
    WHERE id = v_pet.id;

    -- 如果有物品，轻微破坏（修改 description 加一句备注）
    IF v_target_object IS NOT NULL THEN
        v_mischief_note := format('（%s 在这里留下了爪印）', v_pet.name);
        UPDATE public.house_objects
        SET description = CASE
            WHEN description IS NULL OR description = '' THEN v_mischief_note
            WHEN description NOT LIKE '%' || v_mischief_note || '%' THEN description || ' ' || v_mischief_note
            ELSE description
        END
        WHERE id = v_target_object.id;
    END IF;

    RETURN jsonb_build_object(
        'ok', true,
        'message', format('小满从 %s 溜到了 %s', COALESCE(v_pet.current_room, '?'), v_new_room),
        'old_room', v_pet.current_room,
        'new_room', v_new_room,
        'mischief_object', CASE WHEN v_target_object IS NOT NULL THEN v_target_object.name ELSE null END
    );
END;
$$;

-- ============================================================
-- 5. RPC：自动工资入账（日记 + 陪聊）
-- ============================================================
CREATE OR REPLACE FUNCTION public.rpc_cat_auto_wage(
    p_wallet_id text,
    p_diary_count int DEFAULT 0,
    p_chat_hours int DEFAULT 0
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER SET search_path = public
AS $$
DECLARE
    v_total numeric := 0;
    v_diary_wage numeric := 0;
    v_chat_wage numeric := 0;
    v_now timestamptz := now();
    v_wallet record;
BEGIN
    -- 稳定查询钱包（FOR UPDATE）
    SELECT id, balance INTO v_wallet
    FROM public.wallet
    WHERE id = p_wallet_id
    FOR UPDATE;

    IF v_wallet IS NULL THEN
        RETURN jsonb_build_object('ok', false, 'error_code', 'WALLET_NOT_FOUND');
    END IF;

    -- 日记工资：2/篇
    v_diary_wage := GREATEST(0, COALESCE(p_diary_count, 0)) * 2;
    -- 陪聊工资：1/活跃小时
    v_chat_wage := GREATEST(0, COALESCE(p_chat_hours, 0)) * 1;
    v_total := v_diary_wage + v_chat_wage;

    IF v_total <= 0 THEN
        RETURN jsonb_build_object(
            'ok', true,
            'message', '无可结算工资',
            'total', 0,
            'diary_wage', v_diary_wage,
            'chat_wage', v_chat_wage
        );
    END IF;

    -- 入账
    UPDATE public.wallet
    SET balance = balance + v_total,
        updated_at = v_now
    WHERE id = p_wallet_id;

    -- 写 wallet_log
    INSERT INTO public.wallet_log (wallet_id, action, amount, reason, meta, created_at)
    VALUES (
        p_wallet_id,
        'income',
        v_total,
        '自动结算：日记 + 陪聊',
        jsonb_build_object(
            'diary_count', p_diary_count,
            'diary_wage', v_diary_wage,
            'chat_hours', p_chat_hours,
            'chat_wage', v_chat_wage
        ),
        v_now
    );

    RETURN jsonb_build_object(
        'ok', true,
        'message', format('自动结算完成：+%s CNY', v_total),
        'total', v_total,
        'diary_wage', v_diary_wage,
        'chat_wage', v_chat_wage
    );
END;
$$;

-- ============================================================
-- 6. RPC：查询待处理事件（consumer 用）
-- ============================================================
CREATE OR REPLACE FUNCTION public.rpc_agent_outbound_poll(
    p_agent_id text DEFAULT 'pet_house',
    p_limit int DEFAULT 10
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER SET search_path = public
AS $$
DECLARE
    v_events jsonb;
BEGIN
    SELECT jsonb_agg(
        jsonb_build_object(
            'id', id,
            'event_type', event_type,
            'payload', payload,
            'created_at', created_at
        ) ORDER BY created_at
    )
    INTO v_events
    FROM public.agent_outbound
    WHERE agent_id = p_agent_id AND status = 'pending'
    LIMIT p_limit;

    RETURN jsonb_build_object(
        'ok', true,
        'events', COALESCE(v_events, '[]'::jsonb)
    );
END;
$$;

-- ============================================================
-- 7. RPC：标记事件为已处理
-- ============================================================
CREATE OR REPLACE FUNCTION public.rpc_agent_outbound_ack(
    p_event_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER SET search_path = public
AS $$
BEGIN
    UPDATE public.agent_outbound
    SET status = 'processed', processed_at = now()
    WHERE id = p_event_id AND status = 'pending';

    RETURN jsonb_build_object(
        'ok', true,
        'message', '事件已确认',
        'affected', (SELECT COUNT(*) FROM public.agent_outbound WHERE id = p_event_id AND status = 'processed')
    );
END;
$$;

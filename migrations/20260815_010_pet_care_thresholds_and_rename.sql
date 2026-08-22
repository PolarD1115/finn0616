-- ============================================================
-- Migration: 20260815_010_pet_care_thresholds_and_rename
-- 1. rpc_cat_feed：喂食提示文案「饥饿度」→「饱食度」
-- 2. rpc_cat_tick：新增 dirty_cat / tired_cat 阈值穿越事件，
--    与 hungry_cat 同模式写入 pet_agent_outbound 队列；
--    threshold_event 返回值按优先级取单个（向后兼容）。
-- 约束：非破坏性、向后兼容、无 DELETE/DROP/TRUNCATE
-- 依赖：20260812_007（pet_tick_log + pet_agent_outbound 写入）
-- ============================================================

-- ============================================================
-- 1. 重写 rpc_cat_feed：仅改 message 文案
-- ============================================================
CREATE OR REPLACE FUNCTION public.rpc_cat_feed(
    p_user_id text,
    p_item_id text
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER SET search_path = public
AS $$
DECLARE
    v_pet_id uuid;
    v_item_type text;
    v_item_exists boolean;
    v_effect_value numeric;
    v_inv_qty int;
    v_now timestamptz := now();
BEGIN
    -- 校验物品是 food 类型
    SELECT type, effect_value INTO v_item_type, v_effect_value
    FROM public.cat_shop_whitelist
    WHERE id = p_item_id;

    IF v_item_type IS NULL THEN
        RETURN jsonb_build_object('ok', false, 'error_code', 'ITEM_NOT_IN_WHITELIST');
    END IF;

    IF v_item_type != 'food' THEN
        RETURN jsonb_build_object('ok', false, 'error_code', 'NOT_FOOD_ITEM');
    END IF;

    -- 查找宠物（稳定顺序）
    SELECT id INTO v_pet_id
    FROM public.pets
    WHERE user_id = p_user_id
    ORDER BY user_id, id
    LIMIT 1;

    IF v_pet_id IS NULL THEN
        RETURN jsonb_build_object('ok', false, 'error_code', 'PET_NOT_FOUND');
    END IF;

    -- 扣减库存（消耗品）
    UPDATE public.pet_inventory
    SET quantity = quantity - 1
    WHERE user_id = p_user_id AND item_id = p_item_id AND quantity > 0
    RETURNING quantity + 1 INTO v_inv_qty;

    IF v_inv_qty IS NULL THEN
        RETURN jsonb_build_object('ok', false, 'error_code', 'INSUFFICIENT_INVENTORY');
    END IF;

    -- 更新宠物属性（hunger clamp 0-100，增加经验）
    UPDATE public.pets
    SET
        hunger = LEAST(100, GREATEST(0, COALESCE(hunger, 0) + v_effect_value)),
        last_fed = v_now,
        exp = COALESCE(exp, 0) + 1,
        last_care = v_now
    WHERE id = v_pet_id;

    RETURN jsonb_build_object(
        'ok', true,
        'message', format('喂食 %s 成功，饱食度 +%s', p_item_id, v_effect_value),
        'hunger_delta', v_effect_value,
        'exp_gained', 1
    );
END;
$$;

-- ============================================================
-- 2. 重写 rpc_cat_tick：新增 dirty_cat / tired_cat 阈值事件
--    基于 20260812_007 版本，新增两种事件 + 多事件队列写入
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
    v_threshold_event text := null;       -- 返回值（优先级最高者，向后兼容）
    v_events text[] := ARRAY[]::text[];   -- 本轮所有触发的事件
BEGIN
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

    IF v_pet.last_tick_at IS NOT NULL AND EXTRACT(EPOCH FROM (v_now - v_last_tick)) < 60 THEN
        INSERT INTO public.pet_tick_log (user_id, pet_id, ticked_at, skipped, skipped_reason)
        VALUES (p_user_id, v_pet.id, v_now, true, 'tick 间隔过短，跳过');
        RETURN jsonb_build_object(
            'ok', true,
            'message', 'tick 间隔过短，跳过',
            'skipped', true
        );
    END IF;

    v_hours := LEAST(48, GREATEST(0, EXTRACT(EPOCH FROM (v_now - v_last_tick)) / 3600.0));
    v_hunger_delta := -2.0 * v_hours;
    v_happiness_delta := -1.5 * v_hours;
    v_cleanliness_delta := -1.0 * v_hours;
    IF v_pet.status = 'sleeping' THEN
        v_energy_delta := 2.0 * v_hours;
    END IF;

    v_new_hunger := GREATEST(0, LEAST(100, COALESCE(v_pet.hunger, 50) + v_hunger_delta));
    v_new_happiness := GREATEST(0, LEAST(100, COALESCE(v_pet.happiness, 50) + v_happiness_delta));
    v_new_cleanliness := GREATEST(0, LEAST(100, COALESCE(v_pet.cleanliness, 50) + v_cleanliness_delta));
    v_new_energy := GREATEST(0, LEAST(100, COALESCE(v_pet.energy, 50) + v_energy_delta));

    v_new_status := v_pet.status;
    IF v_pet.status != 'sleeping' AND v_new_energy < 20 THEN
        v_new_status := 'sleeping';
    ELSIF v_pet.status = 'sleeping' AND v_new_energy >= 40 THEN
        v_new_status := 'idle';
    END IF;

    -- ── 阈值穿越事件检测（三种）──
    -- 1) 饱食度从 >=30 降到 <30
    IF COALESCE(v_pet.hunger, 50) >= 30 AND v_new_hunger < 30 THEN
        v_events := array_append(v_events, 'hungry_cat');
    END IF;

    -- 2) 清洁度从 >=30 降到 <30
    IF COALESCE(v_pet.cleanliness, 50) >= 30 AND v_new_cleanliness < 30 THEN
        v_events := array_append(v_events, 'dirty_cat');
    END IF;

    -- 3) 精力从 >=20 降到 <20（且非 sleeping，避免与自动入睡重复告警）
    IF COALESCE(v_pet.energy, 50) >= 20 AND v_new_energy < 20 AND v_new_status != 'sleeping' THEN
        v_events := array_append(v_events, 'tired_cat');
    END IF;

    -- 返回值取优先级最高者（hungry > dirty > tired），向后兼容单字段
    IF 'hungry_cat' = ANY(v_events) THEN
        v_threshold_event := 'hungry_cat';
    ELSIF 'dirty_cat' = ANY(v_events) THEN
        v_threshold_event := 'dirty_cat';
    ELSIF 'tired_cat' = ANY(v_events) THEN
        v_threshold_event := 'tired_cat';
    END IF;

    UPDATE public.pets
    SET hunger = v_new_hunger,
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

    -- 写入事件队列（所有触发的事件，每条一行）
    IF array_length(v_events, 1) IS NOT NULL THEN
        FOR i IN 1..array_length(v_events, 1) LOOP
            INSERT INTO public.pet_agent_outbound (agent_id, event_type, payload, status, created_at)
            VALUES (
                'pet_house',
                v_events[i],
                jsonb_build_object(
                    'user_id', p_user_id,
                    'pet_id', v_pet.id,
                    'old_hunger', COALESCE(v_pet.hunger, 50),
                    'new_hunger', v_new_hunger,
                    'old_cleanliness', COALESCE(v_pet.cleanliness, 50),
                    'new_cleanliness', v_new_cleanliness,
                    'old_energy', COALESCE(v_pet.energy, 50),
                    'new_energy', v_new_energy,
                    'created_at', v_now
                ),
                'pending',
                v_now
            );
        END LOOP;
    END IF;

    INSERT INTO public.pet_tick_log (
        user_id, pet_id, ticked_at, hours_elapsed,
        hunger_before, hunger_after, hunger_delta,
        happiness_before, happiness_after, happiness_delta,
        cleanliness_before, cleanliness_after, cleanliness_delta,
        energy_before, energy_after, energy_delta,
        status_before, status_after, threshold_event, skipped
    ) VALUES (
        p_user_id, v_pet.id, v_now, ROUND(v_hours::numeric, 2),
        COALESCE(v_pet.hunger, 50), v_new_hunger, ROUND(v_hunger_delta::numeric, 2),
        COALESCE(v_pet.happiness, 50), v_new_happiness, ROUND(v_happiness_delta::numeric, 2),
        COALESCE(v_pet.cleanliness, 50), v_new_cleanliness, ROUND(v_cleanliness_delta::numeric, 2),
        COALESCE(v_pet.energy, 50), v_new_energy, ROUND(v_energy_delta::numeric, 2),
        v_pet.status, v_new_status, v_threshold_event, false
    );

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
        'threshold_event', v_threshold_event,
        'threshold_events', to_jsonb(v_events)
    );
END;
$$;

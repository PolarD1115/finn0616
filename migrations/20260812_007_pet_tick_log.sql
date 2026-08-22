-- ============================================================
-- Migration: 20260812_007_pet_tick_log
-- 宠物 tick 日志表 + 改 rpc_cat_tick 写日志
-- 约束：非破坏性、向后兼容、无 DELETE/DROP/TRUNCATE
-- ============================================================

-- 1. 日志表
CREATE TABLE IF NOT EXISTS public.pet_tick_log (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id text NOT NULL,
    pet_id uuid,
    ticked_at timestamptz NOT NULL DEFAULT now(),
    hours_elapsed numeric,
    hunger_before numeric, hunger_after numeric, hunger_delta numeric,
    happiness_before numeric, happiness_after numeric, happiness_delta numeric,
    cleanliness_before numeric, cleanliness_after numeric, cleanliness_delta numeric,
    energy_before numeric, energy_after numeric, energy_delta numeric,
    status_before text, status_after text,
    threshold_event text,
    skipped boolean NOT NULL DEFAULT false,
    skipped_reason text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_pet_tick_log_user_ticked
    ON public.pet_tick_log (user_id, ticked_at DESC);

ALTER TABLE public.pet_tick_log ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS pet_tick_log_select_all ON public.pet_tick_log;
CREATE POLICY pet_tick_log_select_all ON public.pet_tick_log
    FOR SELECT USING (true);
DROP POLICY IF EXISTS pet_tick_log_insert_all ON public.pet_tick_log;
CREATE POLICY pet_tick_log_insert_all ON public.pet_tick_log
    FOR INSERT WITH CHECK (true);

-- 2. 重写 rpc_cat_tick：在跳过/成功两个分支各写一条日志（其余逻辑不变）
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

    IF COALESCE(v_pet.hunger, 50) >= 30 AND v_new_hunger < 30 THEN
        v_threshold_event := 'hungry_cat';
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

    -- 写入事件队列（若存在阈值事件）
    IF v_threshold_event IS NOT NULL THEN
        INSERT INTO public.pet_agent_outbound (agent_id, event_type, payload, status, created_at)
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
        'threshold_event', v_threshold_event
    );
END;
$$;

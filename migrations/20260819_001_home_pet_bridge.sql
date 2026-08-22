-- ============================================================
-- Phase 7: 宠物状态权威源与 Home Runtime 融合
-- 迁移名: home_pet_bridge
-- 性质: CREATE OR REPLACE FUNCTION，签名不变，无 DELETE/DROP/TRUNCATE
-- ============================================================

-- ============================================================
-- 1. rpc_home_feed_member
-- 修改: 宠物目标分支写入 pets.hunger（而非 home_member_states.hunger）
-- 事务顺序: 幂等 → 成员校验 → 宠物映射校验+FOR UPDATE pets →
--          FOR UPDATE home_member_states → FOR UPDATE home_dishes →
--          业务写入(扣菜品/更新pets/更新关系/写事件/完成action_run)
-- 签名不变: (p_action_key text, p_actor_key text, p_target_key text, p_dish_id uuid)
-- ============================================================

CREATE OR REPLACE FUNCTION public.rpc_home_feed_member(
    p_action_key text, p_actor_key text, p_target_key text, p_dish_id uuid
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'public', 'pg_temp'
AS $function$
DECLARE
    v_run_id         uuid;
    v_actor_id       uuid;
    v_target_id      uuid;
    v_target_type    text;
    v_profile        jsonb;
    v_dish           record;
    v_state          record;
    v_pet_row        record;
    v_pet_uuid       uuid;
    v_legacy_source  text;
    v_legacy_id_text text;
    v_event_id       uuid;
    v_changes        jsonb := '[]'::jsonb;
    v_today_intimacy numeric;
    v_intimacy_gain  numeric;
BEGIN
    -- 1. action_key 幂等
    BEGIN
        INSERT INTO home_action_runs (action_key, action_type, status, requested_at, started_at)
        VALUES (p_action_key, 'feed_member', 'running', now(), now())
        RETURNING id INTO v_run_id;
    EXCEPTION WHEN unique_violation THEN
        RETURN jsonb_build_object('ok', false, 'error_code', 'ACTION_EXISTS');
    END;

    -- 2. 解析 actor 和 target
    SELECT id INTO v_actor_id
      FROM home_members WHERE stable_key = p_actor_key AND is_active LIMIT 1;
    SELECT id, member_type, profile INTO v_target_id, v_target_type, v_profile
      FROM home_members WHERE stable_key = p_target_key AND is_active LIMIT 1;

    IF v_actor_id IS NULL THEN
        UPDATE home_action_runs SET status='failed', finished_at=now(), error_code='ACTOR_NOT_FOUND' WHERE id=v_run_id;
        RETURN jsonb_build_object('ok', false, 'error_code', 'ACTOR_NOT_FOUND');
    END IF;
    IF v_target_id IS NULL THEN
        UPDATE home_action_runs SET status='failed', finished_at=now(), error_code='TARGET_NOT_FOUND' WHERE id=v_run_id;
        RETURN jsonb_build_object('ok', false, 'error_code', 'TARGET_NOT_FOUND');
    END IF;
    IF v_actor_id = v_target_id THEN
        UPDATE home_action_runs SET status='failed', finished_at=now(), error_code='SELF_TARGET' WHERE id=v_run_id;
        RETURN jsonb_build_object('ok', false, 'error_code', 'SELF_TARGET');
    END IF;

    -- 3-6. 宠物目标：校验映射 + FOR UPDATE 锁定 pets
    IF v_target_type = 'pet' THEN
        -- 4. 校验 legacy_source
        v_legacy_source := v_profile->>'legacy_source';
        IF v_legacy_source IS NULL OR v_legacy_source != 'pets' THEN
            UPDATE home_action_runs SET status='failed', finished_at=now(), error_code='PET_MAPPING_NOT_FOUND' WHERE id=v_run_id;
            RETURN jsonb_build_object('ok', false, 'error_code', 'PET_MAPPING_NOT_FOUND');
        END IF;

        -- 5. 校验 legacy_id 非空且可安全转换为 UUID
        v_legacy_id_text := v_profile->>'legacy_id';
        IF v_legacy_id_text IS NULL OR v_legacy_id_text = '' THEN
            UPDATE home_action_runs SET status='failed', finished_at=now(), error_code='PET_MAPPING_NOT_FOUND' WHERE id=v_run_id;
            RETURN jsonb_build_object('ok', false, 'error_code', 'PET_MAPPING_NOT_FOUND');
        END IF;
        BEGIN
            v_pet_uuid := v_legacy_id_text::uuid;
        EXCEPTION WHEN invalid_text_representation THEN
            UPDATE home_action_runs SET status='failed', finished_at=now(), error_code='PET_MAPPING_NOT_FOUND' WHERE id=v_run_id;
            RETURN jsonb_build_object('ok', false, 'error_code', 'PET_MAPPING_NOT_FOUND');
        END;

        -- 6. FOR UPDATE 锁定 pets，确认存在且允许喂食（is_alive 复用现有字段）
        SELECT * INTO v_pet_row FROM pets WHERE id = v_pet_uuid FOR UPDATE;
        IF NOT FOUND THEN
            UPDATE home_action_runs SET status='failed', finished_at=now(), error_code='PET_NOT_FOUND' WHERE id=v_run_id;
            RETURN jsonb_build_object('ok', false, 'error_code', 'PET_NOT_FOUND');
        END IF;
        IF NOT v_pet_row.is_alive THEN
            UPDATE home_action_runs SET status='failed', finished_at=now(), error_code='PET_NOT_FEEDABLE' WHERE id=v_run_id;
            RETURN jsonb_build_object('ok', false, 'error_code', 'PET_NOT_FEEDABLE');
        END IF;
    END IF;

    -- 7. FOR UPDATE 锁定 home_member_states，确认存在
    SELECT * INTO v_state FROM home_member_states WHERE member_id = v_target_id FOR UPDATE;
    IF NOT FOUND THEN
        UPDATE home_action_runs SET status='failed', finished_at=now(), error_code='HOME_STATE_NOT_FOUND' WHERE id=v_run_id;
        RETURN jsonb_build_object('ok', false, 'error_code', 'HOME_STATE_NOT_FOUND');
    END IF;

    -- 8. FOR UPDATE 锁定 home_dishes，确认存在且 servings > 0
    SELECT * INTO v_dish FROM home_dishes WHERE id = p_dish_id FOR UPDATE;
    IF NOT FOUND OR v_dish.servings <= 0 THEN
        UPDATE home_action_runs SET status='failed', finished_at=now(), error_code='DISH_NOT_AVAILABLE' WHERE id=v_run_id;
        RETURN jsonb_build_object('ok', false, 'error_code', 'DISH_NOT_AVAILABLE');
    END IF;

    -- ====== 所有校验和 FOR UPDATE 锁定完成，以下为业务写入 ======

    -- 9a. 扣除菜品份数
    UPDATE home_dishes SET servings = servings - 1, updated_at = now() WHERE id = p_dish_id;

    -- 9b. 更新状态
    IF v_target_type = 'pet' THEN
        -- 宠物目标：更新 pets 权威生理状态（仅 hunger，依据 cat_feed 规则）
        UPDATE pets SET
            hunger = LEAST(100, GREATEST(0, COALESCE(hunger, 0) + v_dish.hunger_restore)),
            last_fed = now(),
            last_care = now()
        WHERE id = v_pet_uuid;

        -- 计算 intimacy 增量（每日上限 3.0，单次上限 0.5）
        SELECT COALESCE(SUM((details->>'intimacy_delta')::numeric), 0) INTO v_today_intimacy
        FROM home_events WHERE event_type = 'fed_member' AND actor_member_id = v_actor_id
          AND occurred_at >= date_trunc('day', now());
        v_intimacy_gain := GREATEST(0, LEAST(0.5, 3.0 - v_today_intimacy));

        -- 仅更新 Home 关系状态（intimacy），不更新 hunger/mood/energy
        UPDATE home_member_states
           SET intimacy = LEAST(100, intimacy + v_intimacy_gain),
               updated_at = now()
         WHERE member_id = v_target_id;

        v_changes := jsonb_build_array(
            jsonb_build_object('field','hunger','source','pets','delta',v_dish.hunger_restore),
            jsonb_build_object('field','intimacy','source','home_member_states','delta',v_intimacy_gain)
        );
    ELSE
        -- AI 目标：保持现有逻辑
        SELECT COALESCE(SUM((details->>'intimacy_delta')::numeric), 0) INTO v_today_intimacy
        FROM home_events WHERE event_type = 'fed_member' AND actor_member_id = v_actor_id
          AND occurred_at >= date_trunc('day', now());
        v_intimacy_gain := GREATEST(0, LEAST(0.5, 3.0 - v_today_intimacy));

        UPDATE home_member_states
           SET hunger = LEAST(100, hunger + v_dish.hunger_restore),
               mood = LEAST(100, mood + v_dish.mood_restore),
               energy = LEAST(100, energy + v_dish.energy_restore),
               intimacy = LEAST(100, intimacy + v_intimacy_gain),
               updated_at = now()
         WHERE member_id = v_target_id;

        v_changes := jsonb_build_array(
            jsonb_build_object('field','hunger','delta',v_dish.hunger_restore),
            jsonb_build_object('field','mood','delta',v_dish.mood_restore),
            jsonb_build_object('field','energy','delta',v_dish.energy_restore),
            jsonb_build_object('field','intimacy','delta',v_intimacy_gain)
        );
    END IF;

    -- 关联 actor
    UPDATE home_action_runs SET actor_member_id = v_actor_id WHERE id = v_run_id;

    -- 9c. 写入 home_events
    INSERT INTO home_events (event_key, event_type, actor_member_id, target_member_id, source, visibility, summary, details, occurred_at)
    VALUES (p_action_key, 'fed_member', v_actor_id, v_target_id, 'home_runtime', 'home',
            '给' || p_target_key || '喂了' || v_dish.name,
            jsonb_build_object('dish_id', p_dish_id, 'dish_name', v_dish.name,
                               'target', p_target_key, 'target_type', v_target_type,
                               'changes', v_changes, 'intimacy_delta', v_intimacy_gain),
            now())
    RETURNING id INTO v_event_id;

    -- 9d. 完成 home_action_runs
    UPDATE home_action_runs SET status='succeeded', finished_at=now(),
        result = jsonb_build_object('dish_id', p_dish_id, 'target', p_target_key,
                                    'target_type', v_target_type, 'changes', v_changes, 'event_id', v_event_id)
    WHERE id = v_run_id;

    RETURN jsonb_build_object('ok', true, 'action_key', p_action_key, 'status', 'succeeded',
        'dish_name', v_dish.name, 'target', p_target_key, 'target_type', v_target_type,
        'changes', v_changes, 'event_id', v_event_id,
        'narrative_facts', jsonb_build_array('给' || p_target_key || '喂了' || v_dish.name));
END;
$function$;


-- ============================================================
-- 2. rpc_home_enter_room
-- 修改: 在成员解析后增加 member_type='pet' 拦截
-- 保持现有 action_key 幂等和失败状态契约
-- 签名不变: (p_action_key text, p_member_key text, p_room_key text)
-- ============================================================

CREATE OR REPLACE FUNCTION public.rpc_home_enter_room(
    p_action_key text, p_member_key text, p_room_key text
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'public', 'pg_temp'
AS $function$
DECLARE
    v_run_id        uuid;
    v_member_id     uuid;
    v_member_type   text;
    v_room_id       uuid;
    v_room_row      record;
    v_old_room_id   uuid;
    v_settle        jsonb;
    v_event_id      uuid;
BEGIN
    -- 1. action_key 幂等
    BEGIN
        INSERT INTO home_action_runs (action_key, action_type, actor_member_id, status, requested_at, started_at)
        VALUES (p_action_key, 'enter_room', NULL, 'running', now(), now())
        RETURNING id INTO v_run_id;
    EXCEPTION WHEN unique_violation THEN
        SELECT id, status INTO v_run_id, v_room_id FROM home_action_runs WHERE action_key = p_action_key LIMIT 1;
        RETURN jsonb_build_object('ok', false, 'error_code',
            CASE WHEN v_room_id::text = 'succeeded' THEN 'ACTION_ALREADY_DONE'
                 WHEN v_room_id::text = 'running' THEN 'ACTION_IN_PROGRESS'
                 ELSE 'ACTION_FAILED' END,
            'action_key', p_action_key);
    END;

    -- 2. 解析成员
    SELECT id INTO v_member_id FROM home_members WHERE stable_key = p_member_key AND is_active LIMIT 1;
    IF v_member_id IS NULL THEN
        UPDATE home_action_runs SET status='failed', finished_at=now(), error_code='MEMBER_NOT_FOUND', error_message='成员不存在或未激活' WHERE id=v_run_id;
        RETURN jsonb_build_object('ok', false, 'error_code', 'MEMBER_NOT_FOUND');
    END IF;

    -- 3. 宠物 actor 拦截（在结算和房间操作之前）
    SELECT member_type INTO v_member_type FROM home_members WHERE id = v_member_id LIMIT 1;
    IF v_member_type = 'pet' THEN
        UPDATE home_action_runs SET status='failed', finished_at=now(),
            error_code='PET_CANNOT_ACT', error_message='宠物移动由旧宠物系统控制'
        WHERE id=v_run_id;
        RETURN jsonb_build_object('ok', false, 'error_code', 'PET_CANNOT_ACT', 'action_key', p_action_key);
    END IF;

    -- 4. 解析房间
    SELECT * INTO v_room_row FROM home_rooms WHERE stable_key = p_room_key LIMIT 1;
    IF v_room_row IS NULL THEN
        UPDATE home_action_runs SET status='failed', finished_at=now(), error_code='ROOM_NOT_FOUND' WHERE id=v_run_id;
        RETURN jsonb_build_object('ok', false, 'error_code', 'ROOM_NOT_FOUND');
    END IF;
    IF NOT v_room_row.is_enabled THEN
        UPDATE home_action_runs SET status='failed', finished_at=now(), error_code='ROOM_DISABLED' WHERE id=v_run_id;
        RETURN jsonb_build_object('ok', false, 'error_code', 'ROOM_DISABLED');
    END IF;
    IF v_room_row.is_hidden THEN
        UPDATE home_action_runs SET status='failed', finished_at=now(), error_code='ROOM_LOCKED' WHERE id=v_run_id;
        RETURN jsonb_build_object('ok', false, 'error_code', 'ROOM_LOCKED');
    END IF;

    v_room_id := v_room_row.id;

    -- 5. 结算（AI 成员正常结算，宠物已被拦截）
    v_settle := public._home_settle_internal(v_member_id);

    -- 6. 更新 current_room_id
    SELECT current_room_id INTO v_old_room_id FROM home_member_states WHERE member_id = v_member_id LIMIT 1;
    UPDATE home_member_states SET current_room_id = v_room_id, updated_at = now() WHERE member_id = v_member_id;

    -- 7. 关联 action_run actor
    UPDATE home_action_runs SET actor_member_id = v_member_id WHERE id = v_run_id;

    -- 8. 写事件
    INSERT INTO home_events (event_key, event_type, actor_member_id, room_id, source, visibility, summary, details, occurred_at)
    VALUES (p_action_key, 'entered_room', v_member_id, v_room_id, 'home_runtime', 'home',
            '进入了' || v_room_row.name,
            jsonb_build_object('room_key', p_room_key, 'room_name', v_room_row.name, 'settle', v_settle),
            now())
    RETURNING id INTO v_event_id;

    -- 9. 完成 action_run
    UPDATE home_action_runs SET status='succeeded', finished_at=now(), result =
        jsonb_build_object('room_key', p_room_key, 'room_name', v_room_row.name, 'event_id', v_event_id, 'settle', v_settle)
    WHERE id = v_run_id;

    RETURN jsonb_build_object(
        'ok', true, 'action_key', p_action_key, 'action_run_id', v_run_id, 'status', 'succeeded',
        'event_id', v_event_id,
        'actor', jsonb_build_object('stable_key', p_member_key),
        'room', jsonb_build_object('stable_key', p_room_key, 'name', v_room_row.name),
        'settle', v_settle,
        'narrative_facts', jsonb_build_array('进入了' || v_room_row.name)
    );
END;
$function$;

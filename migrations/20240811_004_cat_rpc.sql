-- ============================================================
-- Migration: 20240811_004_cat_rpc
-- Phase 4: 小满及猫商店 (Home Cat + Shop)
-- ============================================================
-- 约束：非破坏性、向后兼容、无 DELETE/DROP/TRUNCATE

-- 1. 猫商店白名单视图（10个白名单物品）
-- 白名单：fish, cat_milk, tuna_can, wet_food, apple, ball, catnip, feather, brush, soap
CREATE OR REPLACE VIEW public.cat_shop_whitelist AS
SELECT
    id,
    name,
    type,
    emoji,
    description,
    effect_type,
    effect_value,
    shop_price
FROM public.pet_items
WHERE id IN (
    'fish', 'cat_milk', 'tuna_can', 'wet_food', 'apple',
    'ball', 'catnip', 'feather',
    'brush', 'soap'
);

-- 2. RPC 函数：查询宠物状态（含属性、冷却、库存摘要）
CREATE OR REPLACE FUNCTION public.rpc_cat_status(
    p_user_id text
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER SET search_path = public
AS $$
DECLARE
    v_pet jsonb;
    v_inventory jsonb;
    v_now timestamptz := now();
    v_pet_id uuid;
    v_last_petted timestamptz;
    v_cooldown_seconds int := 0;
BEGIN
    -- 稳定查询宠物（按 user_id 排序避免死锁）
    SELECT id, last_petted_at INTO v_pet_id, v_last_petted
    FROM public.pets
    WHERE user_id = p_user_id
    ORDER BY user_id, id
    LIMIT 1;

    IF v_pet_id IS NULL THEN
        RETURN jsonb_build_object('ok', false, 'error_code', 'PET_NOT_FOUND');
    END IF;

    -- 计算 pet 冷却（10分钟 = 600秒）
    IF v_last_petted IS NOT NULL THEN
        v_cooldown_seconds := GREATEST(0, 600 - EXTRACT(EPOCH FROM (v_now - v_last_petted))::int);
    END IF;

    -- 宠物完整属性
    SELECT jsonb_build_object(
        'id', id,
        'name', name,
        'species_id', species_id,
        'hunger', LEAST(100, GREATEST(0, COALESCE(hunger, 0)::numeric)),
        'happiness', LEAST(100, GREATEST(0, COALESCE(happiness, 0)::numeric)),
        'health', LEAST(100, GREATEST(0, COALESCE(health, 0)::numeric)),
        'energy', LEAST(100, GREATEST(0, COALESCE(energy, 0)::numeric)),
        'cleanliness', LEAST(100, GREATEST(0, COALESCE(cleanliness, 0)::numeric)),
        'exp', COALESCE(exp, 0),
        'level', COALESCE(level, 1),
        'status', COALESCE(status, 'idle'),
        'mood', COALESCE(mood, 'happy'),
        'current_room', current_room,
        'last_petted_at', last_petted_at,
        'last_fed', last_fed,
        'last_played', last_played,
        'last_cleaned', last_cleaned,
        'cooldown_seconds', v_cooldown_seconds
    )
    INTO v_pet
    FROM public.pets
    WHERE id = v_pet_id;

    -- 库存摘要（仅白名单物品）
    SELECT jsonb_agg(
        jsonb_build_object(
            'item_id', i.id,
            'name', i.name,
            'type', i.type,
            'quantity', COALESCE(inv.quantity, 0)
        ) ORDER BY i.type, i.id
    )
    INTO v_inventory
    FROM public.cat_shop_whitelist i
    LEFT JOIN public.pet_inventory inv
        ON inv.item_id = i.id AND inv.user_id = p_user_id;

    RETURN jsonb_build_object(
        'ok', true,
        'pet', v_pet,
        'inventory', COALESCE(v_inventory, '[]'::jsonb)
    );
END;
$$;

-- 3. RPC 函数：喂食（food 类型，扣消耗品库存）
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

-- 4. RPC 函数：玩耍（toy 耐用品，不扣数量）
CREATE OR REPLACE FUNCTION public.rpc_cat_play(
    p_user_id text,
    p_item_id text DEFAULT NULL
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER SET search_path = public
AS $$
DECLARE
    v_pet_id uuid;
    v_item_type text;
    v_effect_value numeric;
    v_current_energy numeric;
    v_current_status text;
    v_now timestamptz := now();
    v_bonus numeric := 0;
BEGIN
    -- 查找宠物（稳定顺序）
    SELECT id, COALESCE(energy, 0), COALESCE(status, 'idle')
    INTO v_pet_id, v_current_energy, v_current_status
    FROM public.pets
    WHERE user_id = p_user_id
    ORDER BY user_id, id
    LIMIT 1;

    IF v_pet_id IS NULL THEN
        RETURN jsonb_build_object('ok', false, 'error_code', 'PET_NOT_FOUND');
    END IF;

    -- sleeping 拒绝
    IF v_current_status = 'sleeping' THEN
        RETURN jsonb_build_object('ok', false, 'error_code', 'PET_IS_SLEEPING');
    END IF;

    -- 精力过低拒绝（< 10）
    IF v_current_energy < 10 THEN
        RETURN jsonb_build_object('ok', false, 'error_code', 'LOW_ENERGY');
    END IF;

    -- 如果有指定玩具
    IF p_item_id IS NOT NULL THEN
        SELECT type, effect_value INTO v_item_type, v_effect_value
        FROM public.cat_shop_whitelist
        WHERE id = p_item_id;

        IF v_item_type IS NULL THEN
            RETURN jsonb_build_object('ok', false, 'error_code', 'ITEM_NOT_IN_WHITELIST');
        END IF;

        IF v_item_type != 'toy' THEN
            RETURN jsonb_build_object('ok', false, 'error_code', 'NOT_TOY_ITEM');
        END IF;

        -- 检查库存（但耐用品不扣数量）
        IF NOT EXISTS (
            SELECT 1 FROM public.pet_inventory
            WHERE user_id = p_user_id AND item_id = p_item_id AND quantity > 0
        ) THEN
            RETURN jsonb_build_object('ok', false, 'error_code', 'TOY_NOT_OWNED');
        END IF;

        v_bonus := COALESCE(v_effect_value, 0);
    ELSE
        -- 空手效果较低
        v_bonus := 5;
    END IF;

    -- 更新宠物属性（happiness + bonus，energy - 5）
    UPDATE public.pets
    SET
        happiness = LEAST(100, GREATEST(0, COALESCE(happiness, 0) + v_bonus)),
        energy = GREATEST(0, COALESCE(energy, 0) - 5),
        last_played = v_now,
        last_care = v_now
    WHERE id = v_pet_id;

    RETURN jsonb_build_object(
        'ok', true,
        'message', format('玩耍成功，快乐 +%s，精力 -5', v_bonus),
        'happiness_delta', v_bonus,
        'energy_delta', -5
    );
END;
$$;

-- 5. RPC 函数：清洁（clean 消耗品，扣库存）
CREATE OR REPLACE FUNCTION public.rpc_cat_clean(
    p_user_id text,
    p_item_id text DEFAULT NULL
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER SET search_path = public
AS $$
DECLARE
    v_pet_id uuid;
    v_item_type text;
    v_effect_value numeric;
    v_inv_qty int;
    v_now timestamptz := now();
    v_bonus numeric := 10;
BEGIN
    -- 查找宠物（稳定顺序）
    SELECT id INTO v_pet_id
    FROM public.pets
    WHERE user_id = p_user_id
    ORDER BY user_id, id
    LIMIT 1;

    IF v_pet_id IS NULL THEN
        RETURN jsonb_build_object('ok', false, 'error_code', 'PET_NOT_FOUND');
    END IF;

    -- 如果有指定清洁道具
    IF p_item_id IS NOT NULL THEN
        SELECT type, effect_value INTO v_item_type, v_effect_value
        FROM public.cat_shop_whitelist
        WHERE id = p_item_id;

        IF v_item_type IS NULL THEN
            RETURN jsonb_build_object('ok', false, 'error_code', 'ITEM_NOT_IN_WHITELIST');
        END IF;

        IF v_item_type != 'clean' THEN
            RETURN jsonb_build_object('ok', false, 'error_code', 'NOT_CLEAN_ITEM');
        END IF;

        -- 扣减库存（消耗品）
        UPDATE public.pet_inventory
        SET quantity = quantity - 1
        WHERE user_id = p_user_id AND item_id = p_item_id AND quantity > 0
        RETURNING quantity + 1 INTO v_inv_qty;

        IF v_inv_qty IS NULL THEN
            RETURN jsonb_build_object('ok', false, 'error_code', 'INSUFFICIENT_INVENTORY');
        END IF;

        v_bonus := COALESCE(v_effect_value, 10);
    END IF;

    -- 更新宠物属性
    UPDATE public.pets
    SET
        cleanliness = LEAST(100, GREATEST(0, COALESCE(cleanliness, 0) + v_bonus)),
        last_cleaned = v_now,
        last_care = v_now
    WHERE id = v_pet_id;

    RETURN jsonb_build_object(
        'ok', true,
        'message', format('清洁成功，清洁度 +%s', v_bonus),
        'cleanliness_delta', v_bonus
    );
END;
$$;

-- 6. RPC 函数：抚摸（快乐+5，10分钟冷却）
CREATE OR REPLACE FUNCTION public.rpc_cat_pet(
    p_user_id text
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER SET search_path = public
AS $$
DECLARE
    v_pet_id uuid;
    v_last_petted timestamptz;
    v_cooldown_seconds int := 0;
    v_now timestamptz := now();
BEGIN
    -- 查找宠物（稳定顺序）
    SELECT id, last_petted_at
    INTO v_pet_id, v_last_petted
    FROM public.pets
    WHERE user_id = p_user_id
    ORDER BY user_id, id
    LIMIT 1;

    IF v_pet_id IS NULL THEN
        RETURN jsonb_build_object('ok', false, 'error_code', 'PET_NOT_FOUND');
    END IF;

    -- 计算冷却
    IF v_last_petted IS NOT NULL THEN
        v_cooldown_seconds := GREATEST(0, 600 - EXTRACT(EPOCH FROM (v_now - v_last_petted))::int);
    END IF;

    -- 冷却中：零副作用，返回剩余秒数
    IF v_cooldown_seconds > 0 THEN
        RETURN jsonb_build_object(
            'ok', true,
            'message', format('还在冷却中，剩余 %s 秒', v_cooldown_seconds),
            'on_cooldown', true,
            'cooldown_seconds', v_cooldown_seconds,
            'happiness_delta', 0
        );
    END IF;

    -- 未冷却：增加快乐
    UPDATE public.pets
    SET
        happiness = LEAST(100, GREATEST(0, COALESCE(happiness, 0) + 5)),
        last_petted_at = v_now,
        last_care = v_now
    WHERE id = v_pet_id;

    RETURN jsonb_build_object(
        'ok', true,
        'message', '抚摸成功，快乐 +5',
        'on_cooldown', false,
        'cooldown_seconds', 0,
        'happiness_delta', 5
    );
END;
$$;

-- 7. RPC 函数：恢复精力（明确、受限的恢复路径）
CREATE OR REPLACE FUNCTION public.rpc_cat_restore_energy(
    p_user_id text
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER SET search_path = public
AS $$
DECLARE
    v_pet_id uuid;
    v_current_energy numeric;
    v_restore_amount numeric := 30;
    v_now timestamptz := now();
BEGIN
    -- 查找宠物（稳定顺序）
    SELECT id, COALESCE(energy, 0)
    INTO v_pet_id, v_current_energy
    FROM public.pets
    WHERE user_id = p_user_id
    ORDER BY user_id, id
    LIMIT 1;

    IF v_pet_id IS NULL THEN
        RETURN jsonb_build_object('ok', false, 'error_code', 'PET_NOT_FOUND');
    END IF;

    -- 如果精力已经很高，减少恢复量
    IF v_current_energy >= 80 THEN
        v_restore_amount := 10;
    ELSIF v_current_energy >= 50 THEN
        v_restore_amount := 20;
    END IF;

    UPDATE public.pets
    SET
        energy = LEAST(100, GREATEST(0, COALESCE(energy, 0) + v_restore_amount)),
        last_care = v_now
    WHERE id = v_pet_id;

    RETURN jsonb_build_object(
        'ok', true,
        'message', format('精力恢复 +%s', v_restore_amount),
        'energy_delta', v_restore_amount
    );
END;
$$;

-- 8. RPC 函数：商店列表（10个白名单物品）
CREATE OR REPLACE FUNCTION public.rpc_cat_shop_list(
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER SET search_path = public
AS $$
DECLARE
    v_items jsonb;
BEGIN
    SELECT jsonb_agg(
        jsonb_build_object(
            'item_id', id,
            'name', name,
            'type', type,
            'emoji', emoji,
            'description', description,
            'effect_type', effect_type,
            'effect_value', effect_value,
            'shop_price', shop_price
        ) ORDER BY type, id
    )
    INTO v_items
    FROM public.cat_shop_whitelist;

    RETURN jsonb_build_object(
        'ok', true,
        'items', COALESCE(v_items, '[]'::jsonb)
    );
END;
$$;

-- 9. RPC 函数：商店购买（钱包扣款 + wallet_log + inventory upsert，同一事务）
CREATE OR REPLACE FUNCTION public.rpc_cat_shop_buy(
    p_user_id text,
    p_item_id text,
    p_qty int DEFAULT 1
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER SET search_path = public
AS $$
DECLARE
    v_item record;
    v_total_price int;
    v_wallet_id text;
    v_wallet_balance numeric;
    v_inv_id uuid;
    v_now timestamptz := now();
BEGIN
    -- 校验数量
    IF p_qty IS NULL OR p_qty <= 0 OR p_qty > 99 THEN
        RETURN jsonb_build_object('ok', false, 'error_code', 'INVALID_QTY');
    END IF;

    -- 查找白名单物品
    SELECT id, name, type, shop_price INTO v_item
    FROM public.cat_shop_whitelist
    WHERE id = p_item_id;

    IF v_item IS NULL THEN
        RETURN jsonb_build_object('ok', false, 'error_code', 'ITEM_NOT_IN_WHITELIST');
    END IF;

    v_total_price := v_item.shop_price * p_qty;

    -- 查找宠物对应的 wallet_id（稳定顺序 FOR UPDATE）
    SELECT w.id, w.balance
    INTO v_wallet_id, v_wallet_balance
    FROM public.wallet w
    WHERE w.id = 'finn_wallet'
    FOR UPDATE;

    IF v_wallet_id IS NULL THEN
        RETURN jsonb_build_object('ok', false, 'error_code', 'WALLET_NOT_FOUND');
    END IF;

    -- 余额校验
    IF v_wallet_balance < v_total_price THEN
        RETURN jsonb_build_object('ok', false, 'error_code', 'INSUFFICIENT_BALANCE');
    END IF;

    -- 扣款
    UPDATE public.wallet
    SET balance = balance - v_total_price, updated_at = v_now
    WHERE id = v_wallet_id;

    -- 写 wallet_log
    INSERT INTO public.wallet_log (wallet_id, action, amount, reason, meta, created_at)
    VALUES (
        v_wallet_id,
        'expense',
        v_total_price,
        format('购买 %s x%s', v_item.name, p_qty),
        jsonb_build_object('item_id', p_item_id, 'qty', p_qty, 'user_id', p_user_id),
        v_now
    );

    -- 库存 upsert（消耗品和耐用品都累加数量）
    -- 先尝试 UPDATE
    UPDATE public.pet_inventory
    SET quantity = quantity + p_qty, obtained_at = v_now
    WHERE user_id = p_user_id AND item_id = p_item_id;

    -- 如果没更新到，INSERT
    IF NOT FOUND THEN
        INSERT INTO public.pet_inventory (user_id, item_id, quantity, obtained_at)
        VALUES (p_user_id, p_item_id, p_qty, v_now);
    END IF;

    RETURN jsonb_build_object(
        'ok', true,
        'message', format('购买 %s x%s 成功，花费 %s CNY', v_item.name, p_qty, v_total_price),
        'item_id', p_item_id,
        'qty', p_qty,
        'total_price', v_total_price
    );
END;
$$;

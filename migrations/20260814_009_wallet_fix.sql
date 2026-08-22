-- ============================================================
-- Migration: 20260814_009_wallet_fix
-- 修复 wallet_earn 函数重载 + balance_after 漏写 + 清理 auto_wage
-- ============================================================
-- 约束：非破坏性、向后兼容、幂等
-- 背景：
--   1. rpc_wallet_earn 存在两个重载版本（8参数旧版 + 9参数含bypass_cap新版），
--      PostgREST 无法正确路由重载函数，导致 wallet_earn 调用全部失败。
--   2. rpc_cat_shop_buy 写 wallet_log 时漏写 balance_after 列（默认值 0），
--      导致所有购买流水的 balance_after 都是 0。
--   3. rpc_cat_auto_wage 已无 Python 调用方（home_system.py 中的 cat_auto_wage()
--      和 heartbeat.py 中的调用点已于早期清理），SQL 函数残留在数据库中。
-- ============================================================

-- ============================================================
-- 1. 删除旧版 rpc_wallet_earn（8参数版，消除函数重载）
--    保留 9 参数版（含 p_bypass_cap），让 PostgREST 能正确路由
-- ============================================================
DROP FUNCTION IF EXISTS public.rpc_wallet_earn(
    text, numeric, text, text, jsonb, numeric, numeric, boolean
);

-- ============================================================
-- 2. 修复 rpc_cat_shop_buy：补写 balance_after
--    原问题：INSERT wallet_log 时漏写 balance_after 列，默认值 0
-- ============================================================
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
    v_new_balance numeric;
    v_inv_id uuid;
    v_now timestamptz := now();
BEGIN
    IF p_qty IS NULL OR p_qty <= 0 OR p_qty > 99 THEN
        RETURN jsonb_build_object('ok', false, 'error_code', 'INVALID_QTY');
    END IF;

    SELECT id, name, type, shop_price INTO v_item
    FROM public.cat_shop_whitelist
    WHERE id = p_item_id;

    IF v_item IS NULL THEN
        RETURN jsonb_build_object('ok', false, 'error_code', 'ITEM_NOT_IN_WHITELIST');
    END IF;

    v_total_price := v_item.shop_price * p_qty;

    SELECT w.id, w.balance
    INTO v_wallet_id, v_wallet_balance
    FROM public.wallet w
    WHERE w.id = 'finn_wallet'
    FOR UPDATE;

    IF v_wallet_id IS NULL THEN
        RETURN jsonb_build_object('ok', false, 'error_code', 'WALLET_NOT_FOUND');
    END IF;

    IF v_wallet_balance < v_total_price THEN
        RETURN jsonb_build_object('ok', false, 'error_code', 'INSUFFICIENT_BALANCE');
    END IF;

    -- 扣款并获取扣款后的新余额
    UPDATE public.wallet
    SET balance = balance - v_total_price, updated_at = v_now
    WHERE id = v_wallet_id
    RETURNING balance INTO v_new_balance;

    -- 写 wallet_log（补上 balance_after 列）
    INSERT INTO public.wallet_log (wallet_id, action, amount, balance_after, note, meta, created_at)
    VALUES (
        v_wallet_id,
        'expense',
        v_total_price,
        v_new_balance,
        format('购买 %s x%s', v_item.name, p_qty),
        jsonb_build_object('item_id', p_item_id, 'qty', p_qty, 'user_id', p_user_id),
        v_now
    );

    -- 库存 upsert
    UPDATE public.pet_inventory
    SET quantity = quantity + p_qty, obtained_at = v_now
    WHERE user_id = p_user_id AND item_id = p_item_id;

    IF NOT FOUND THEN
        INSERT INTO public.pet_inventory (user_id, item_id, quantity, obtained_at)
        VALUES (p_user_id, p_item_id, p_qty, v_now);
    END IF;

    RETURN jsonb_build_object(
        'ok', true,
        'message', format('购买 %s x%s 成功，花费 %s CNY', v_item.name, p_qty, v_total_price),
        'item_id', p_item_id,
        'qty', p_qty,
        'total_price', v_total_price,
        'balance_after', v_new_balance
    );
END;
$$;

-- ============================================================
-- 3. 删除 rpc_cat_auto_wage（自动结算工资，已无调用方）
--    Python 层的 cat_auto_wage() 函数和 heartbeat.py 调用点
--    已于早期清理，此 SQL 函数仅残留在数据库中
-- ============================================================
DROP FUNCTION IF EXISTS public.rpc_cat_auto_wage(text, int, int);

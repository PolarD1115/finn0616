-- ============================================================
-- Migration: 20240811_002_wallet_rpc
-- Phase 2: 小钱包 RPC（原子操作）
-- ============================================================
-- 约束：
--   - 无 DELETE / DROP / TRUNCATE
--   - 幂等：重复执行安全
--   - 向后兼容：新增列带默认值，不影响现有数据
-- ============================================================

-- ============================================================
-- 1. 扩展 wallet 表：追加周统计与加班银行字段
-- ============================================================
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'wallet' AND column_name = 'week_earned'
    ) THEN
        ALTER TABLE public.wallet ADD COLUMN week_earned numeric DEFAULT 0 CHECK (week_earned >= 0);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'wallet' AND column_name = 'week_start'
    ) THEN
        ALTER TABLE public.wallet ADD COLUMN week_start timestamptz;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'wallet' AND column_name = 'overtime_bank'
    ) THEN
        ALTER TABLE public.wallet ADD COLUMN overtime_bank numeric DEFAULT 0 CHECK (overtime_bank >= 0);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'wallet' AND column_name = 'total_earned'
    ) THEN
        ALTER TABLE public.wallet ADD COLUMN total_earned numeric DEFAULT 0 CHECK (total_earned >= 0);
    END IF;
END $$;

-- ============================================================
-- 2. 初始化现有 finn_wallet 的统计字段（如为空）
-- ============================================================
UPDATE public.wallet
SET week_earned = COALESCE(week_earned, 0),
    overtime_bank = COALESCE(overtime_bank, 0),
    total_earned = COALESCE(total_earned, 0)
WHERE week_earned IS NULL OR overtime_bank IS NULL OR total_earned IS NULL;

-- ============================================================
-- 3. 辅助函数：北京时间周一 00:00 作为 timestamptz
-- ============================================================
CREATE OR REPLACE FUNCTION public._bj_week_start(t timestamptz)
RETURNS timestamptz
LANGUAGE plpgsql
IMMUTABLE
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    bj timestamp;
    monday timestamp;
BEGIN
    -- t 是 UTC 存储的 timestamptz；先转成上海本地时间，截断到周一，再转回 timestamptz
    bj := t AT TIME ZONE 'Asia/Shanghai';
    monday := date_trunc('week', bj);
    RETURN monday AT TIME ZONE 'Asia/Shanghai';
END;
$$;

-- ============================================================
-- 4. 辅助函数：判断给定时间是否落在生日周（4月5日或11月15日所在周）
-- ============================================================
CREATE OR REPLACE FUNCTION public._is_birthday_week(t timestamptz)
RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    bj date;
    yr int;
    apr5 date;
    nov15 date;
    wk_start date;
    wk_end date;
BEGIN
    bj := (t AT TIME ZONE 'Asia/Shanghai')::date;
    yr := extract(year from bj)::int;
    apr5 := make_date(yr, 4, 5);
    nov15 := make_date(yr, 11, 15);
    -- 周一 00:00 (含) 到下周日 23:59:59 (含)
    wk_start := (date_trunc('week', bj AT TIME ZONE 'Asia/Shanghai') AT TIME ZONE 'Asia/Shanghai')::date;
    wk_end := wk_start + 6;
    RETURN (apr5 >= wk_start AND apr5 <= wk_end)
        OR (nov15 >= wk_start AND nov15 <= wk_end);
END;
$$;

-- ============================================================
-- 5. RPC: wallet_earn（原子入账）
--    参数校验 → source_key 幂等 → FOR UPDATE 锁定 → 周重置 → 上限/加班银行计算 → 双写
-- ============================================================
CREATE OR REPLACE FUNCTION public.rpc_wallet_earn(
    p_wallet_id text,
    p_amount numeric,
    p_source_key text,
    p_reason text,
    p_meta jsonb DEFAULT '{}'::jsonb,
    p_week_cap numeric DEFAULT 80,
    p_overtime_rate numeric DEFAULT 0.5,
    p_birthday_enabled boolean DEFAULT true
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_wallet record;
    v_now timestamptz;
    v_week_start timestamptz;
    v_is_birthday boolean;
    v_cap numeric;
    v_to_balance numeric := 0;
    v_to_overtime numeric := 0;
    v_new_balance numeric;
    v_new_week_earned numeric;
    v_new_ot numeric;
    v_new_total numeric;
BEGIN
    -- ---- 参数校验 ----
    IF p_wallet_id IS NULL OR trim(p_wallet_id) = '' THEN
        RETURN jsonb_build_object('ok', false, 'error_code', 'INVALID_WALLET', 'message', '钱包ID无效');
    END IF;
    IF p_amount IS NULL OR p_amount <= 0 THEN
        RETURN jsonb_build_object('ok', false, 'error_code', 'INVALID_AMOUNT', 'message', '金额必须为正数');
    END IF;
    IF p_reason IS NULL OR trim(p_reason) = '' THEN
        RETURN jsonb_build_object('ok', false, 'error_code', 'EMPTY_REASON', 'message', '原因不能为空');
    END IF;
    -- 超大数校验（超过 1e9 视为非法）
    IF p_amount > 1e9 THEN
        RETURN jsonb_build_object('ok', false, 'error_code', 'OVERSIZED_AMOUNT', 'message', '金额过大');
    END IF;

    -- ---- source_key 幂等：如已存在则拒绝重复入账 ----
    IF p_source_key IS NOT NULL AND trim(p_source_key) != '' THEN
        PERFORM 1 FROM public.wallet_log WHERE source_key = p_source_key;
        IF FOUND THEN
            RETURN jsonb_build_object('ok', false, 'error_code', 'DUPLICATE_SOURCE', 'message', 'source_key 已存在');
        END IF;
    END IF;

    -- ---- 锁定钱包行 ----
    SELECT * INTO v_wallet FROM public.wallet WHERE id = p_wallet_id FOR UPDATE;
    IF NOT FOUND THEN
        RETURN jsonb_build_object('ok', false, 'error_code', 'WALLET_NOT_FOUND', 'message', '钱包不存在');
    END IF;

    v_now := now();
    v_week_start := public._bj_week_start(v_now);

    -- ---- 周重置 ----
    IF v_wallet.week_start IS NULL OR v_wallet.week_start < v_week_start THEN
        v_wallet.week_earned := 0;
    END IF;

    -- ---- 生日周取消上限 ----
    v_is_birthday := public._is_birthday_week(v_now);
    IF v_is_birthday AND p_birthday_enabled THEN
        v_cap := 'Infinity'::numeric;
    ELSE
        v_cap := p_week_cap;
    END IF;

    -- ---- 分配：余额 vs 加班银行 ----
    IF v_wallet.week_earned + p_amount <= v_cap THEN
        v_to_balance := p_amount;
        v_to_overtime := 0;
    ELSE
        v_to_balance := greatest(0, v_cap - v_wallet.week_earned);
        v_to_overtime := floor((p_amount - v_to_balance) * p_overtime_rate);
    END IF;

    -- 确保向下取整后非负
    v_to_overtime := greatest(0, v_to_overtime);

    v_new_balance := v_wallet.balance + v_to_balance;
    v_new_week_earned := v_wallet.week_earned + v_to_balance;
    v_new_ot := v_wallet.overtime_bank + v_to_overtime;
    v_new_total := v_wallet.total_earned + v_to_balance;

    -- ---- 原子更新 wallet ----
    UPDATE public.wallet SET
        balance = v_new_balance,
        week_earned = v_new_week_earned,
        week_start = v_week_start,
        overtime_bank = v_new_ot,
        total_earned = v_new_total,
        updated_at = v_now
    WHERE id = p_wallet_id;

    -- ---- 原子写入 wallet_log ----
    INSERT INTO public.wallet_log (wallet_id, action, amount, balance_after, note, source_key, meta, created_at)
    VALUES (p_wallet_id, 'income', p_amount, v_new_balance, p_reason, p_source_key, p_meta, v_now);

    RETURN jsonb_build_object(
        'ok', true,
        'message', '入账成功',
        'data', jsonb_build_object(
            'amount', p_amount,
            'to_balance', v_to_balance,
            'to_overtime', v_to_overtime,
            'balance_after', v_new_balance,
            'week_earned_after', v_new_week_earned,
            'overtime_bank_after', v_new_ot,
            'is_birthday_week', v_is_birthday
        )
    );
END;
$$;

-- ============================================================
-- 6. RPC: wallet_spend（原子支出）
-- ============================================================
CREATE OR REPLACE FUNCTION public.rpc_wallet_spend(
    p_wallet_id text,
    p_amount numeric,
    p_reason text,
    p_meta jsonb DEFAULT '{}'::jsonb
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_wallet record;
    v_now timestamptz;
    v_new_balance numeric;
BEGIN
    -- ---- 参数校验 ----
    IF p_wallet_id IS NULL OR trim(p_wallet_id) = '' THEN
        RETURN jsonb_build_object('ok', false, 'error_code', 'INVALID_WALLET', 'message', '钱包ID无效');
    END IF;
    IF p_amount IS NULL OR p_amount <= 0 THEN
        RETURN jsonb_build_object('ok', false, 'error_code', 'INVALID_AMOUNT', 'message', '金额必须为正数');
    END IF;
    IF p_amount > 1e9 THEN
        RETURN jsonb_build_object('ok', false, 'error_code', 'OVERSIZED_AMOUNT', 'message', '金额过大');
    END IF;
    IF p_reason IS NULL OR trim(p_reason) = '' THEN
        RETURN jsonb_build_object('ok', false, 'error_code', 'EMPTY_REASON', 'message', '原因不能为空');
    END IF;

    -- ---- 锁定 ----
    SELECT * INTO v_wallet FROM public.wallet WHERE id = p_wallet_id FOR UPDATE;
    IF NOT FOUND THEN
        RETURN jsonb_build_object('ok', false, 'error_code', 'WALLET_NOT_FOUND', 'message', '钱包不存在');
    END IF;

    IF v_wallet.balance < p_amount THEN
        RETURN jsonb_build_object('ok', false, 'error_code', 'INSUFFICIENT_BALANCE', 'message', '余额不足');
    END IF;

    v_now := now();
    v_new_balance := v_wallet.balance - p_amount;

    UPDATE public.wallet SET
        balance = v_new_balance,
        updated_at = v_now
    WHERE id = p_wallet_id;

    INSERT INTO public.wallet_log (wallet_id, action, amount, balance_after, note, source_key, meta, created_at)
    VALUES (p_wallet_id, 'expense', p_amount, v_new_balance, p_reason, NULL, p_meta, v_now);

    RETURN jsonb_build_object(
        'ok', true,
        'message', '支出成功',
        'data', jsonb_build_object(
            'amount', p_amount,
            'balance_after', v_new_balance
        )
    );
END;
$$;

-- ============================================================
-- 7. RPC: wallet_exchange（原子兑换：tea/gift）
--    本质也是 earn，但 source_key 固定前缀 + 校验 target
-- ============================================================
CREATE OR REPLACE FUNCTION public.rpc_wallet_exchange(
    p_wallet_id text,
    p_target text,
    p_reason text,
    p_meta jsonb DEFAULT '{}'::jsonb,
    p_week_cap numeric DEFAULT 80,
    p_overtime_rate numeric DEFAULT 0.5,
    p_birthday_enabled boolean DEFAULT true
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_amount numeric;
    v_source_key text;
    v_result jsonb;
BEGIN
    IF p_wallet_id IS NULL OR trim(p_wallet_id) = '' THEN
        RETURN jsonb_build_object('ok', false, 'error_code', 'INVALID_WALLET', 'message', '钱包ID无效');
    END IF;
    IF p_target IS NULL THEN
        RETURN jsonb_build_object('ok', false, 'error_code', 'INVALID_TARGET', 'message', '兑换目标不能为空');
    END IF;

    -- 校验 target
    v_amount := CASE lower(trim(p_target))
        WHEN 'tea'  THEN 50
        WHEN 'gift' THEN 100
        ELSE NULL
    END;

    IF v_amount IS NULL THEN
        RETURN jsonb_build_object('ok', false, 'error_code', 'INVALID_TARGET', 'message', '非法兑换目标');
    END IF;

    IF p_reason IS NULL OR trim(p_reason) = '' THEN
        RETURN jsonb_build_object('ok', false, 'error_code', 'EMPTY_REASON', 'message', '原因不能为空');
    END IF;

    -- 生成幂等 source_key
    v_source_key := 'exchange:' || lower(trim(p_target)) || ':' || p_wallet_id || ':' || to_char(now(), 'YYYYMMDD');

    -- 复用 earn RPC
    v_result := public.rpc_wallet_earn(
        p_wallet_id, v_amount, v_source_key, p_reason, p_meta,
        p_week_cap, p_overtime_rate, p_birthday_enabled
    );

    -- 追加 target 信息到返回
    IF (v_result->>'ok')::boolean THEN
        v_result := jsonb_set(v_result, '{data,target}', to_jsonb(trim(p_target)));
        v_result := jsonb_set(v_result, '{data,exchange_amount}', to_jsonb(v_amount));
    END IF;

    RETURN v_result;
END;
$$;

-- ============================================================
-- 8. RPC: wallet_overtime_withdraw（从加班银行取出）
--    取出不增加 total_earned/week_earned
-- ============================================================
CREATE OR REPLACE FUNCTION public.rpc_wallet_overtime_withdraw(
    p_wallet_id text,
    p_amount numeric,
    p_reason text,
    p_meta jsonb DEFAULT '{}'::jsonb,
    p_single_max numeric DEFAULT 20
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_wallet record;
    v_now timestamptz;
    v_new_balance numeric;
    v_new_ot numeric;
BEGIN
    -- ---- 参数校验 ----
    IF p_wallet_id IS NULL OR trim(p_wallet_id) = '' THEN
        RETURN jsonb_build_object('ok', false, 'error_code', 'INVALID_WALLET', 'message', '钱包ID无效');
    END IF;
    IF p_amount IS NULL OR p_amount <= 0 THEN
        RETURN jsonb_build_object('ok', false, 'error_code', 'INVALID_AMOUNT', 'message', '金额必须为正数');
    END IF;
    IF p_amount > 1e9 THEN
        RETURN jsonb_build_object('ok', false, 'error_code', 'OVERSIZED_AMOUNT', 'message', '金额过大');
    END IF;
    IF p_reason IS NULL OR trim(p_reason) = '' THEN
        RETURN jsonb_build_object('ok', false, 'error_code', 'EMPTY_REASON', 'message', '原因不能为空');
    END IF;
    IF p_amount > p_single_max THEN
        RETURN jsonb_build_object('ok', false, 'error_code', 'OVERTIME_WITHDRAW_LIMIT', 'message',
            format('单次最多取出 %s', p_single_max));
    END IF;

    -- ---- 锁定 ----
    SELECT * INTO v_wallet FROM public.wallet WHERE id = p_wallet_id FOR UPDATE;
    IF NOT FOUND THEN
        RETURN jsonb_build_object('ok', false, 'error_code', 'WALLET_NOT_FOUND', 'message', '钱包不存在');
    END IF;

    IF COALESCE(v_wallet.overtime_bank, 0) < p_amount THEN
        RETURN jsonb_build_object('ok', false, 'error_code', 'INSUFFICIENT_OVERTIME', 'message', '加班银行余额不足');
    END IF;

    v_now := now();
    v_new_balance := v_wallet.balance + p_amount;
    v_new_ot := v_wallet.overtime_bank - p_amount;

    UPDATE public.wallet SET
        balance = v_new_balance,
        overtime_bank = v_new_ot,
        updated_at = v_now
    WHERE id = p_wallet_id;

    INSERT INTO public.wallet_log (wallet_id, action, amount, balance_after, note, source_key, meta, created_at)
    VALUES (p_wallet_id, 'adjust', p_amount, v_new_balance, p_reason, NULL, p_meta, v_now);

    RETURN jsonb_build_object(
        'ok', true,
        'message', '取出成功',
        'data', jsonb_build_object(
            'amount', p_amount,
            'balance_after', v_new_balance,
            'overtime_bank_after', v_new_ot
        )
    );
END;
$$;

-- ============================================================
-- 9. RPC: wallet_check（查询当前状态 + 本周统计）
-- ============================================================
CREATE OR REPLACE FUNCTION public.rpc_wallet_check(
    p_wallet_id text,
    p_week_cap numeric DEFAULT 80,
    p_birthday_enabled boolean DEFAULT true
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_wallet record;
    v_week_start timestamptz;
    v_is_birthday boolean;
    v_cap numeric;
    v_week_earned numeric;
BEGIN
    IF p_wallet_id IS NULL OR trim(p_wallet_id) = '' THEN
        RETURN jsonb_build_object('ok', false, 'error_code', 'INVALID_WALLET', 'message', '钱包ID无效');
    END IF;

    SELECT * INTO v_wallet FROM public.wallet WHERE id = p_wallet_id;
    IF NOT FOUND THEN
        RETURN jsonb_build_object('ok', false, 'error_code', 'WALLET_NOT_FOUND', 'message', '钱包不存在');
    END IF;

    v_week_start := public._bj_week_start(now());

    -- 若周已切换，展示时视为已重置
    IF v_wallet.week_start IS NULL OR v_wallet.week_start < v_week_start THEN
        v_week_earned := 0;
    ELSE
        v_week_earned := COALESCE(v_wallet.week_earned, 0);
    END IF;

    v_is_birthday := public._is_birthday_week(now());
    IF v_is_birthday AND p_birthday_enabled THEN
        v_cap := 'Infinity'::numeric;
    ELSE
        v_cap := p_week_cap;
    END IF;

    RETURN jsonb_build_object(
        'ok', true,
        'message', '查询成功',
        'data', jsonb_build_object(
            'id', v_wallet.id,
            'owner_id', v_wallet.owner_id,
            'balance', v_wallet.balance,
            'currency', v_wallet.currency,
            'status', v_wallet.status,
            'week_earned', v_week_earned,
            'week_start', v_week_start,
            'overtime_bank', COALESCE(v_wallet.overtime_bank, 0),
            'total_earned', COALESCE(v_wallet.total_earned, 0),
            'week_cap', v_cap,
            'is_birthday_week', v_is_birthday,
            'week_remaining', greatest(0, v_cap - v_week_earned)
        )
    );
END;
$$;

-- ============================================================
-- 10. RPC: wallet_log（分页查询流水）
-- ============================================================
CREATE OR REPLACE FUNCTION public.rpc_wallet_log(
    p_wallet_id text,
    p_limit int DEFAULT 20,
    p_offset int DEFAULT 0
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_count int;
    v_logs jsonb;
BEGIN
    IF p_wallet_id IS NULL OR trim(p_wallet_id) = '' THEN
        RETURN jsonb_build_object('ok', false, 'error_code', 'INVALID_WALLET', 'message', '钱包ID无效');
    END IF;
    IF p_limit < 1 OR p_limit > 100 THEN
        RETURN jsonb_build_object('ok', false, 'error_code', 'INVALID_LIMIT', 'message', 'limit 必须在 1~100 之间');
    END IF;

    SELECT COUNT(*) INTO v_count FROM public.wallet_log WHERE wallet_id = p_wallet_id;

    SELECT COALESCE(jsonb_agg(
        jsonb_build_object(
            'id', id,
            'action', action,
            'amount', amount,
            'balance_after', balance_after,
            'note', note,
            'source_key', source_key,
            'meta', meta,
            'created_at', created_at
        )
        ORDER BY created_at DESC
    ), '[]'::jsonb)
    INTO v_logs
    FROM (
        SELECT id, action, amount, balance_after, note, source_key, meta, created_at
        FROM public.wallet_log
        WHERE wallet_id = p_wallet_id
        ORDER BY created_at DESC
        LIMIT p_limit OFFSET p_offset
    ) sub;

    RETURN jsonb_build_object(
        'ok', true,
        'message', '查询成功',
        'data', jsonb_build_object(
            'total', v_count,
            'limit', p_limit,
            'offset', p_offset,
            'logs', v_logs
        )
    );
END;
$$;

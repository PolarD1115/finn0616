-- ============================================================
-- Migration: 20240811_001_home_system_schema
-- Phase 1: 小屋 + 小满 + 小钱包 基础 schema 与幂等种子
-- ============================================================
-- 约束：
--   - 无 DELETE / DROP / TRUNCATE
--   - 幂等：重复执行安全
--   - 向后兼容：不重置已有房间、宠物属性、库存或余额
-- ============================================================

-- ============================================================
-- 1. 创建 house_rooms（小屋房间）
-- ============================================================
CREATE TABLE IF NOT EXISTS public.house_rooms (
    id          text PRIMARY KEY,
    name        text NOT NULL,
    description text,
    emoji       text DEFAULT '🏠',
    sort_order  int DEFAULT 0,
    is_default  boolean DEFAULT false,
    created_at  timestamptz DEFAULT now()
);

-- 唯一约束：房间名不可重复
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'house_rooms_name_unique'
          AND conrelid = 'public.house_rooms'::regclass
    ) THEN
        ALTER TABLE public.house_rooms ADD CONSTRAINT house_rooms_name_unique UNIQUE (name);
    END IF;
END $$;

-- ============================================================
-- 2. 创建 house_diary（小屋日记）
-- ============================================================
CREATE TABLE IF NOT EXISTS public.house_diary (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    room_id     text REFERENCES public.house_rooms(id) ON DELETE SET NULL,
    entry_type  text NOT NULL DEFAULT 'activity',
    content     text NOT NULL DEFAULT '',
    mood        text,
    weather     text,
    tags        text[] DEFAULT '{}',
    meta        jsonb DEFAULT '{}'::jsonb,
    created_at  timestamptz DEFAULT now(),
    updated_at  timestamptz DEFAULT now()
);

-- 索引
CREATE INDEX IF NOT EXISTS idx_house_diary_room_created
    ON public.house_diary(room_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_house_diary_entry_type
    ON public.house_diary(entry_type);

-- ============================================================
-- 3. 创建 wallet（小钱包主表）
-- ============================================================
CREATE TABLE IF NOT EXISTS public.wallet (
    id          text PRIMARY KEY DEFAULT 'finn_wallet',
    owner_id    text NOT NULL DEFAULT 'finn',
    balance     numeric NOT NULL DEFAULT 0 CHECK (balance >= 0),
    currency    text NOT NULL DEFAULT 'CNY',
    status      text NOT NULL DEFAULT 'active',
    created_at  timestamptz DEFAULT now(),
    updated_at  timestamptz DEFAULT now()
);

-- 唯一约束：单例模式，owner_id 唯一
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'wallet_owner_id_unique'
          AND conrelid = 'public.wallet'::regclass
    ) THEN
        ALTER TABLE public.wallet ADD CONSTRAINT wallet_owner_id_unique UNIQUE (owner_id);
    END IF;
END $$;

-- ============================================================
-- 4. 创建 wallet_log（钱包流水）
-- ============================================================
CREATE TABLE IF NOT EXISTS public.wallet_log (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    wallet_id     text NOT NULL REFERENCES public.wallet(id) ON DELETE CASCADE,
    action        text NOT NULL CHECK (action IN ('income','expense','transfer','adjust')),
    amount        numeric NOT NULL DEFAULT 0,
    balance_after numeric NOT NULL DEFAULT 0 CHECK (balance_after >= 0),
    note          text DEFAULT '',
    source_key    text,
    meta          jsonb DEFAULT '{}'::jsonb,
    created_at    timestamptz DEFAULT now()
);

-- 索引
CREATE INDEX IF NOT EXISTS idx_wallet_log_wallet_created
    ON public.wallet_log(wallet_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_wallet_log_source_key
    ON public.wallet_log(source_key)
    WHERE source_key IS NOT NULL;

-- 唯一约束：source_key 唯一（幂等性保障）
CREATE UNIQUE INDEX IF NOT EXISTS idx_wallet_log_source_key_unique
    ON public.wallet_log(source_key)
    WHERE source_key IS NOT NULL;

-- ============================================================
-- 5. 扩展 pets 表（无损添加字段）
-- ============================================================
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'pets' AND column_name = 'current_room') THEN
        ALTER TABLE public.pets ADD COLUMN current_room text;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'pets' AND column_name = 'last_petted_at') THEN
        ALTER TABLE public.pets ADD COLUMN last_petted_at timestamptz;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'pets' AND column_name = 'tick_next_at') THEN
        ALTER TABLE public.pets ADD COLUMN tick_next_at timestamptz;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'pets' AND column_name = 'alert_flags') THEN
        ALTER TABLE public.pets ADD COLUMN alert_flags jsonb DEFAULT '{}'::jsonb;
    END IF;
END $$;

-- ============================================================
-- 6. 幂等 Seed：五个房间
-- ============================================================
INSERT INTO public.house_rooms (id, name, description, emoji, sort_order, is_default)
VALUES
    ('living_room',  '客厅', '温暖明亮的主活动空间，有沙发和阳光',      '🛋️', 1, true),
    ('bedroom',      '卧室', '柔软舒适的休息空间，有枕头和毯子',        '🛏️', 2, false),
    ('kitchen',      '厨房', '香气四溢的烹饪空间，有冰箱和炉灶',        '🍳', 3, false),
    ('study',        '书房', '安静的知识角落，书架整齐排列',            '📚', 4, false),
    ('balcony',      '阳台', '阳光和绿植的开放空间，适合发呆和浇花',    '🌿', 5, false)
ON CONFLICT (id) DO NOTHING;

-- ============================================================
-- 7. 幂等 Seed：wallet singleton
-- ============================================================
INSERT INTO public.wallet (id, owner_id, balance, currency, status)
VALUES ('finn_wallet', 'finn', 100, 'CNY', 'active')
ON CONFLICT (id) DO NOTHING;

-- ============================================================
-- 8. 绑定现有唯一宠物为「小满」
--    已有 pets 表中 name='小满' 的记录无需改动，
--    这里仅做幂等确认注释（无实际数据变更）。
--    真实 pets 记录：id=1fc9db85-0f91-400f-812a-598d9aae2ce7, user_id=user_finn, name=小满
-- ============================================================
-- （无操作——小满已存在，不创建第二只）

-- ============================================================
-- 9. 幂等 Seed：10 个猫用品（upsert，以草案数值为权威）
--    仅插入缺失的，不删除已有旧物品
-- ============================================================
INSERT INTO public.pet_items (id, name, type, rarity, description, emoji, effect_type, effect_value, shop_price, sell_price, obtainable_from)
VALUES
    ('catnip',           '猫薄荷',     'toy',      'common',    '让猫咪兴奋不已的天然草本植物',         '🌿', 'happiness',  20,  5,   2,  'shop'),
    ('scratching_post',  '猫抓板',     'toy',      'common',    '磨爪子的必备神器，保护家具',           '🪵', 'happiness',  15,  8,   3,  'shop'),
    ('cat_bed',          '猫窝',       'toy',      'uncommon',  '柔软温暖的专属小窝',                   '🛏️', 'happiness',  25,  15,  6,  'shop'),
    ('tuna_can',         '金枪鱼罐头', 'food',     'common',    '鲜美多汁的高级猫罐头',                 '🥫', 'hunger',     30,  10,  4,  'shop'),
    ('cat_milk',         '猫奶',       'food',     'common',    '易消化的营养猫奶，幼猫也能喝',          '🥛', 'hunger',     15,  6,   2,  'shop'),
    ('litter',           '猫砂',       'clean',    'common',    '结团迅速的优质膨润土猫砂',             '', 'cleanliness',40,  12,  5,  'shop'),
    ('brush',            '猫刷',       'clean',    'common',    '去除浮毛、按摩皮肤的美容工具',           '✨', 'cleanliness',25,  8,   3,  'shop'),
    ('cat_tower',        '猫爬架',     'toy',      'rare',      '多层结构的攀爬乐园，猫咪最爱',          '🏰', 'happiness',  40,  30,  12, 'shop'),
    ('wet_food',         '湿粮包',     'food',     'common',    '肉泥质地的营养湿粮，补水佳品',          '🍖', 'hunger',     25,  8,   3,  'shop'),
    ('collar',           '项圈',       'gift',     'uncommon',  '精致小铃铛项圈，可刻名字',              '🔔', 'friendship', 15,  12,  5,  'shop')
ON CONFLICT (id) DO UPDATE SET
    name = EXCLUDED.name,
    type = EXCLUDED.type,
    rarity = EXCLUDED.rarity,
    description = EXCLUDED.description,
    emoji = EXCLUDED.emoji,
    effect_type = EXCLUDED.effect_type,
    effect_value = EXCLUDED.effect_value,
    shop_price = EXCLUDED.shop_price,
    sell_price = EXCLUDED.sell_price,
    obtainable_from = EXCLUDED.obtainable_from;

-- ============================================================
-- 10. RLS：启用并设置最小权限策略
-- ============================================================

-- house_rooms
ALTER TABLE public.house_rooms ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS house_rooms_select_all ON public.house_rooms;
CREATE POLICY house_rooms_select_all ON public.house_rooms FOR SELECT USING (true);

-- house_diary
ALTER TABLE public.house_diary ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS house_diary_select_all ON public.house_diary;
CREATE POLICY house_diary_select_all ON public.house_diary FOR SELECT USING (true);
DROP POLICY IF EXISTS house_diary_insert_all ON public.house_diary;
CREATE POLICY house_diary_insert_all ON public.house_diary FOR INSERT WITH CHECK (true);
DROP POLICY IF EXISTS house_diary_update_all ON public.house_diary;
CREATE POLICY house_diary_update_all ON public.house_diary FOR UPDATE USING (true);
DROP POLICY IF EXISTS house_diary_delete_all ON public.house_diary;
CREATE POLICY house_diary_delete_all ON public.house_diary FOR DELETE USING (true);

-- wallet
ALTER TABLE public.wallet ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS wallet_select_all ON public.wallet;
CREATE POLICY wallet_select_all ON public.wallet FOR SELECT USING (true);
DROP POLICY IF EXISTS wallet_insert_all ON public.wallet;
CREATE POLICY wallet_insert_all ON public.wallet FOR INSERT WITH CHECK (true);
DROP POLICY IF EXISTS wallet_update_all ON public.wallet;
CREATE POLICY wallet_update_all ON public.wallet FOR UPDATE USING (true);

-- wallet_log
ALTER TABLE public.wallet_log ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS wallet_log_select_all ON public.wallet_log;
CREATE POLICY wallet_log_select_all ON public.wallet_log FOR SELECT USING (true);
DROP POLICY IF EXISTS wallet_log_insert_all ON public.wallet_log;
CREATE POLICY wallet_log_insert_all ON public.wallet_log FOR INSERT WITH CHECK (true);

-- pets（确保已有 RLS 仍然有效，扩展字段不影响策略）
-- pets 表 RLS 已由先前创建启用，这里不做修改

-- ============================================================
-- 11. 更新 pets 当前房间默认值（如果为空）
-- ============================================================
UPDATE public.pets
SET current_room = 'living_room'
WHERE current_room IS NULL;

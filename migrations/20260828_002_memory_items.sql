-- ============================================================
-- 第 4 阶段：长期记忆产物表 memory_items
-- 职责：经事实抽取后的「应该记住什么」（提炼层），区别于
--       memory_events（原始事件账本「发生过什么」）与 memories（旧流水/总结混装表）。
-- 边界：纯 additive 变更；不触碰任何旧表与 memory_events；不写入任何业务数据。
-- 远端已通过 Supabase apply_migration 执行（migration 名 create_memory_items_table）。
-- 注：embedding 暂不创建——未来检索方案与维度未定（现有 3 表均为 vector(1024)，
--     但不得因此假定新表维度），后续以 additive migration 补列。
-- ============================================================

CREATE TABLE IF NOT EXISTS public.memory_items (
    id                uuid        NOT NULL DEFAULT gen_random_uuid(),
    user_id           text        NOT NULL,
    memory_type       text        NOT NULL,
    content           text        NOT NULL,
    content_hash      text        NOT NULL,
    status            text        NOT NULL DEFAULT 'active',
    importance        integer     NOT NULL DEFAULT 3,
    confidence        double precision NOT NULL DEFAULT 0.5,
    source            text        NOT NULL DEFAULT 'unknown',
    source_event_ids  uuid[]      NOT NULL DEFAULT '{}'::uuid[],
    source_batch_id   uuid            NULL,
    subject_key       text            NULL,
    valid_at          timestamptz     NULL,
    invalid_at        timestamptz     NULL,
    expires_at        timestamptz     NULL,
    superseded_by     uuid            NULL,
    last_confirmed_at timestamptz     NULL,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    metadata          jsonb       NOT NULL DEFAULT '{}'::jsonb,
    created_by        text        NOT NULL DEFAULT 'memory_extractor',
    CONSTRAINT memory_items_pkey PRIMARY KEY (id),
    -- 自引用替代链：新事实落地时旧行置 status='superseded' + invalid_at + superseded_by=新行 id
    CONSTRAINT memory_items_superseded_by_fkey FOREIGN KEY (superseded_by)
        REFERENCES public.memory_items(id),
    -- memory_type：core/current/long_term 为固化层级语义（可选使用，层级主要由
    -- status+importance 表达）；fact/shared_experience 为纯语义种类（提取器推荐）
    CONSTRAINT memory_items_memory_type_check CHECK (memory_type IN
        ('core', 'current', 'long_term', 'moment', 'memo', 'fact', 'shared_experience')),
    -- status：expired 值保留但约定「过期判定以 expires_at 派生为准」（召回查询必须
    -- 带 expires_at 过滤），该状态仅作未来显式收束使用，避免双真相
    CONSTRAINT memory_items_status_check CHECK (status IN
        ('active', 'superseded', 'expired', 'rejected', 'pending_review')),
    CONSTRAINT memory_items_importance_check CHECK (importance BETWEEN 1 AND 10),
    CONSTRAINT memory_items_confidence_check CHECK (confidence >= 0 AND confidence <= 1),
    -- 替代一致性：被替代的事实必须记录失效时间
    CONSTRAINT memory_items_superseded_invalid_check
        CHECK (status <> 'superseded' OR invalid_at IS NOT NULL),
    -- 时间窗口一致性（双方可空，NULL 不受阻；锚点是 valid_at 而非 now()，不阻碍历史回填）
    CONSTRAINT memory_items_valid_window_check
        CHECK ((invalid_at IS NULL OR valid_at IS NULL OR invalid_at >= valid_at)
           AND (valid_at IS NULL OR expires_at IS NULL OR expires_at >= valid_at))
);

COMMENT ON TABLE public.memory_items IS
    '长期记忆产物表（第4阶段）：经事实抽取后的「应该记住什么」；source_event_ids 追溯 memory_events.id（数组列无 FK，引用完整性由应用层保证）；过期判定以 expires_at 派生为准';

COMMENT ON COLUMN public.memory_items.content_hash IS
    '对规范化后事实文本的 SHA-256（规范化由写入方在应用层完成，不落中间态）；仅普通索引用于去重候选，无唯一约束——moment 类记忆与同一事实多次确认可合法并存';

COMMENT ON COLUMN public.memory_items.superseded_by IS
    '替代链指针：本条事实被哪条新事实替代（非破坏性收束，旧事实永不物理删除）';

COMMENT ON COLUMN public.memory_items.source_event_ids IS
    '来源 memory_events.id 数组（uuid[]，对齐 eventide_dream_cards.after_effect_tags 的数组先例；NOT NULL DEFAULT 空数组以消除 NULL/空双态）';

-- 索引（仅当前设计与未来提取器确定需要的 3 条；无 GIN/HNSW/全局唯一）
-- 1. 读取当前有效记忆（按重要度/更新时间排序）
CREATE INDEX IF NOT EXISTS memory_items_user_status_type_idx
    ON public.memory_items (user_id, status, memory_type, importance DESC, updated_at DESC);
-- 2. 主题归并/替代链：提取器按 subject_key 查找当前版本
CREATE INDEX IF NOT EXISTS memory_items_user_subject_valid_idx
    ON public.memory_items (user_id, subject_key, valid_at DESC);
-- 3. 去重候选查找（普通索引，非唯一）
CREATE INDEX IF NOT EXISTS memory_items_user_hash_idx
    ON public.memory_items (user_id, content_hash);

-- RLS：启用且不创建任何策略（deny-by-default，与 memory_events 同款）。
-- 原因：user_id 是应用层隔离字段，不等于 auth.uid()；项目无安全用户认证映射；
-- 本阶段零读取零写入代码，不影响任何现有链路；后续接入前必须配合 service_role
-- 或明确的用户身份方案设计策略，不得复制旧表 public 全放行模式。
ALTER TABLE public.memory_items ENABLE ROW LEVEL SECURITY;

-- ============================================================
-- 第 2 阶段：独立原始记忆事件表 memory_events
-- 职责：只记录「发生过什么」（原始事件账本），不做长期记忆判断。
-- 边界：纯 additive 变更，不触碰任何旧表，不写入任何业务数据。
-- 远端已通过 Supabase apply_migration 执行（version 20260828142354）。
-- ============================================================

-- 1. 表结构（IF NOT EXISTS 幂等；不使用 DROP）
CREATE TABLE IF NOT EXISTS public.memory_events (
    id                uuid        NOT NULL DEFAULT gen_random_uuid(),
    user_id           text        NOT NULL,
    session_id        text            NULL,
    channel           text        NOT NULL,
    role              text        NOT NULL,
    content           text        NOT NULL,
    content_hash      text        NOT NULL,
    occurred_at       timestamptz NOT NULL,
    created_at        timestamptz NOT NULL DEFAULT now(),
    source_event_id   text            NULL,
    batch_id          uuid            NULL,
    processing_status text        NOT NULL DEFAULT 'pending',
    processed_at      timestamptz     NULL,
    attempt_count     integer     NOT NULL DEFAULT 0,
    last_error        text            NULL,
    metadata          jsonb       NOT NULL DEFAULT '{}'::jsonb,
    created_by        text        NOT NULL,
    CONSTRAINT memory_events_pkey PRIMARY KEY (id),
    -- 枚举 CHECK 沿用项目既有风格（agent_jobs.status / agent_outbound.status 同款）
    CONSTRAINT memory_events_channel_check CHECK (channel IN
        ('web', 'tg', 'qq', 'email', 'background', 'home', 'mcp', 'unknown')),
    CONSTRAINT memory_events_role_check CHECK (role IN
        ('user', 'assistant', 'tool', 'system', 'event')),
    CONSTRAINT memory_events_processing_status_check CHECK (processing_status IN
        ('pending', 'processed', 'failed', 'ignored')),
    CONSTRAINT memory_events_attempt_count_check CHECK (attempt_count >= 0)
);

COMMENT ON TABLE public.memory_events IS
    '原始记忆事件账本（第2阶段）：只记录发生过什么，不做长期记忆判断；后续事实提取/摘要/长期记忆以此为唯一数据源';

COMMENT ON COLUMN public.memory_events.content_hash IS
    '内容哈希，用于未来幂等去重；不做全局唯一约束（同一内容可在不同时间/渠道合法重复）';

COMMENT ON COLUMN public.memory_events.processing_status IS
    'pending/processed/failed/ignored：后续阶段事实提取的处理状态机；本阶段全部保持 pending 默认值';

-- 2. 索引（仅当前需求明确的三条；metadata 不建 GIN——暂无查询路径）
--    用户事件时间线读取
CREATE INDEX IF NOT EXISTS memory_events_user_occurred_idx
    ON public.memory_events (user_id, occurred_at DESC);
--    后续处理 pending 事件
CREATE INDEX IF NOT EXISTS memory_events_user_status_created_idx
    ON public.memory_events (user_id, processing_status, created_at);
--    幂等去重辅助（普通索引，非唯一）
CREATE INDEX IF NOT EXISTS memory_events_content_hash_idx
    ON public.memory_events (content_hash);

-- 3. RLS：启用，但【不创建任何业务读写策略】
--    deny-by-default：anon / authenticated 的读写全部拒绝；service_role 绕过 RLS 可访问。
--    原因：user_id 是应用层隔离字段，不等于 auth.uid()，当前项目没有可安全映射的
--    用户认证体系；旧表的 public 全放行策略是不安全的历史设计，本表不复制该模式。
--    本阶段表未接入任何写入链路，不会影响现有网关运行；后续阶段接入写入前，
--    必须配合 service_role 或明确的用户身份方案设计策略。
ALTER TABLE public.memory_events ENABLE ROW LEVEL SECURITY;

-- ============================================================
-- 20260831_001_activity_logs.sql
-- 阶段 C3：结构化行动日志 activity_logs
-- ============================================================
-- 目的：为顶层自主活动（自由活动 / Home 自主生活）提供"轮级"结构化行动记录：
--   活动开始前先建 running 记录（start-before-side-effect），
--   结束后按真实业务结果 finalize（succeeded/observed/partial/failed/skipped）。
--   与既有日志的分工（不互相替代）：
--     memories(Free_Activity/Home_Autonomy/Secret_Diary) = 叙事日志；
--     home_events = 单个家庭事件；home_action_runs = 单个 Home 工具动作幂等记录；
--     activity_logs = 一次完整自主活动的轮级留痕。
--
-- 边界（用户确认）：
--   只新增；不删除；不迁移旧数据；不修改 memories/home_events/home_action_runs/
--   house_diary 等既有日志与历史记录。
--   thought_summary 是模型明确生成的可展示摘要（用户可见），绝不保存
--   reasoning/thinking/隐藏思维链/系统 Prompt（应用层过滤兜底）。
--   tools_used 只存安全摘要 {name, ok, status, error_code}，不存参数、
--   UUID、action_key、原始返回 text/raw 与任何正文。
--
-- 权限：RLS 开启且不创建任何 policy（deny-by-default）；对 anon/authenticated
--   直接 REVOKE 全部权限——读写均走 service_role（绕过 RLS）。前端读取留到
--   C6 经 API_SECRET 保护的网关接口实现。
--
-- 实际通过 apply_migration activity_logs_c3 执行；本文件为存档，可重复审阅。
-- ============================================================

CREATE TABLE IF NOT EXISTS public.activity_logs (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    -- 一次顶层自主活动的稳定唯一键（代码生成，非模型值）；创建 running 与
    -- 最终完成使用同一 key，防止网络重试产生重复记录。
    activity_key    text NOT NULL UNIQUE,
    -- 稳定活动标识（当前简单规则：free:<slug> / home:autonomy / free:secret_diary），
    -- 完整 activity_id 统一留待后续调度合并阶段。
    activity_id     text,
    -- 展示用活动名（如 写秘密日记 / 查天气 / 家庭自主生活）。
    activity_name   text NOT NULL DEFAULT '',
    -- 来源：free_activity / home_autonomy（text + 应用校验，不用 enum）。
    source          text NOT NULL,
    -- running=已登记执行中；succeeded=有效完成；observed=仅观察无真实写动作；
    -- partial=有成功也有失败/跳过；failed=执行或关键规划失败；skipped=登记后未执行。
    status          text NOT NULL DEFAULT 'running'
                    CHECK (status IN ('running','succeeded','observed','partial','failed','skipped')),
    -- 可展示的"当时为什么想做"摘要（应用层限 500 字，过滤思维链载体）。
    thought_summary text NOT NULL DEFAULT '',
    -- 面向用户的执行结果摘要（应用层限 1000 字，敏感活动脱敏）。
    result_summary  text NOT NULL DEFAULT '',
    -- 安全工具摘要数组：[{"name","ok","status","error_code"}]（应用层归一化）。
    tools_used      jsonb NOT NULL DEFAULT '[]'::jsonb,
    started_at      timestamptz NOT NULL DEFAULT now(),
    finished_at     timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_activity_logs_started_at_desc
    ON public.activity_logs (started_at DESC);

ALTER TABLE public.activity_logs ENABLE ROW LEVEL SECURITY;

-- anon/authenticated 一律无权限（service_role 不受 RLS/权限影响）：
REVOKE ALL ON public.activity_logs FROM anon, authenticated;

-- ============================================================
-- 20260918_001_period_tracker.sql
-- 经期记录：数据层 public.period_records
-- ============================================================
-- 目的：为「经期记录」提供独立数据表，保存每次经期：
--   开始日（必填）、结束日（可空，空=进行中或只记了开始）、
--   流量、症状、备注；
--   由网关 REST API（/api/period/*）经 service_role 读写，
--   供控制台 / Mini App 展示与编辑，以及 MCP 工具供 AI 记录。
--
-- 边界（本迁移确认）：
--   只新增本表、索引、RLS 设置与注释；不删除任何表/记录/字段/约束；
--   不实现前端、MCP 工具或调度逻辑。
--
-- 权限（沿用 courses / ai_todos 敏感业务表风格）：
--   RLS 开启且不创建任何 anon/authenticated 读写策略（deny-by-default）；
--   对 anon/authenticated 直接 REVOKE 全部表权限——读写均走 service_role
--   （绕过 RLS）。前端只能调用网关 API，不直接访问 Supabase。
-- ============================================================

CREATE TABLE IF NOT EXISTS public.period_records (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),

    -- 经期开始日（必填，日历日，按北京时间记录）。
    start_date      date NOT NULL,

    -- 经期结束日（可空）。NULL 表示「仍在进行中」或「只记了开始日」。
    -- 若非空，必须 ≥ start_date。
    end_date        date,

    -- 流量：light / medium / heavy（应用层白名单校验；库内只存文本）。
    flow            text NOT NULL DEFAULT 'medium',

    -- 症状描述（如 痛经/腰酸/乏力）；可为空字符串。
    symptoms        text NOT NULL DEFAULT '',

    -- 自由备注；可为空字符串。
    notes           text NOT NULL DEFAULT '',

    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),

    CHECK (end_date IS NULL OR end_date >= start_date),
    CHECK (flow IN ('light', 'medium', 'heavy'))
);

COMMENT ON TABLE public.period_records IS
    '经期记录：每次经期的开始日/结束日/流量/症状/备注。读写仅经网关 service_role，前端不直连。';

COMMENT ON COLUMN public.period_records.start_date IS
    '经期开始日（必填，date）';

COMMENT ON COLUMN public.period_records.end_date IS
    '经期结束日（可空；NULL=进行中或只记开始；非空须 ≥ start_date）';

COMMENT ON COLUMN public.period_records.flow IS
    '流量：light/medium/heavy，默认 medium';

COMMENT ON COLUMN public.period_records.symptoms IS
    '症状描述（如 痛经/腰酸）；可为空字符串';

COMMENT ON COLUMN public.period_records.notes IS
    '自由备注；可为空字符串';

-- 索引：按开始日倒序，服务历史列表与周期计算（取最近 N 次）。
CREATE INDEX IF NOT EXISTS idx_period_records_start_date
    ON public.period_records (start_date DESC);

-- RLS：开启，但【不创建任何 anon/authenticated 读写策略】（deny-by-default），
-- 并直接撤销两类角色的全部表权限；读写均由网关服务端经 service_role 完成。
ALTER TABLE public.period_records ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON public.period_records FROM anon, authenticated;

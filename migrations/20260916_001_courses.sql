-- ============================================================
-- 20260916_001_courses.sql
-- 课程表：数据层 public.courses
-- ============================================================
-- 目的：为「课程表」提供独立数据表，保存一学期的课程条目：
--   课程名、教师、地点、星期、起止节次、展示颜色；
--   由网关 REST API（/api/courses）经 service_role 读写，
--   供控制台 / Mini App 展示与编辑。
--
-- 边界（本迁移确认）：
--   只新增本表、索引、RLS 设置与注释；不删除任何表/记录/字段/约束；
--   不实现前端、MCP 工具或调度逻辑。
--   本阶段未在 Supabase 执行本文件；后续由人工审阅后执行。
--
-- 权限（沿用 ai_todos / activity_logs / memory_events 敏感业务表风格）：
--   RLS 开启且不创建任何 anon/authenticated 读写策略（deny-by-default）；
--   对 anon/authenticated 直接 REVOKE 全部表权限——读写均走 service_role
--   （绕过 RLS）。前端只能调用网关 API，不直接访问 Supabase。
-- ============================================================

CREATE TABLE IF NOT EXISTS public.courses (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),

    -- 课程名称（展示用）。
    name            text NOT NULL,

    -- 任课教师；可为空字符串。
    teacher         text NOT NULL DEFAULT '',

    -- 上课地点；可为空字符串。
    location        text NOT NULL DEFAULT '',

    -- 星期：1=周一 … 7=周日。
    weekday         smallint NOT NULL
                    CHECK (weekday BETWEEN 1 AND 7),

    -- 起始节次（1–12）。
    start_slot      smallint NOT NULL
                    CHECK (start_slot BETWEEN 1 AND 12),

    -- 结束节次（1–12），必须 ≥ start_slot。
    end_slot        smallint NOT NULL
                    CHECK (end_slot BETWEEN 1 AND 12),

    -- 展示颜色标识（应用层白名单校验；库内只存文本）。
    color           text NOT NULL DEFAULT 'blue',

    created_at      timestamptz NOT NULL DEFAULT now(),
    -- 项目迁移中无统一的 updated_at 自动触发器（已核实），本阶段不额外建立
    -- 通用触发器；由应用层在更新时显式写入。
    updated_at      timestamptz NOT NULL DEFAULT now(),

    CHECK (end_slot >= start_slot)
);

COMMENT ON TABLE public.courses IS
    '课程表：一学期课程条目（名称/教师/地点/星期/起止节次/颜色）。读写仅经网关 service_role，前端不直连。';

COMMENT ON COLUMN public.courses.name IS
    '课程名称';

COMMENT ON COLUMN public.courses.teacher IS
    '任课教师；可为空字符串';

COMMENT ON COLUMN public.courses.location IS
    '上课地点；可为空字符串';

COMMENT ON COLUMN public.courses.weekday IS
    '星期：1=周一 … 7=周日';

COMMENT ON COLUMN public.courses.start_slot IS
    '起始节次（1–12）';

COMMENT ON COLUMN public.courses.end_slot IS
    '结束节次（1–12），须 ≥ start_slot';

COMMENT ON COLUMN public.courses.color IS
    '展示颜色标识；应用层白名单：blue/pink/purple/gray/orange/green';

-- 索引：按星期 + 起始节次排列，服务课表网格展示与冲突检测前置筛选。
CREATE INDEX IF NOT EXISTS idx_courses_weekday
    ON public.courses (weekday, start_slot);

-- RLS：开启，但【不创建任何 anon/authenticated 读写策略】（deny-by-default），
-- 并直接撤销两类角色的全部表权限；读写均由网关服务端经 service_role 完成。
ALTER TABLE public.courses ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON public.courses FROM anon, authenticated;

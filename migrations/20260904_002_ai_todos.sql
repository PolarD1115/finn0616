-- ============================================================
-- 20260904_002_ai_todos.sql
-- AI 待办系统：阶段 1 —— 基础数据层 ai_todos
-- ============================================================
-- 目的：为「AI 待办 / AI 想说的话」提供独立数据表：
--   1) AI 记录一件未来要提醒用户的事（吃药、上课、出门等）；
--   2) AI 记录某个时间想对用户说的一段话（月初、生日、纪念日等）；
--   3) 到时间后由网关后台推送到 Telegram。
--   消息可以预先保存固定内容，也可以到时间结合上下文动态生成。
--
-- 边界（阶段 1 确认）：
--   只新增本表、索引、RLS 设置与注释；不删除任何表/记录/字段/约束；
--   不迁移旧 reminders 数据；不修改 reminders 及其代码
--   （server.py manage_reminder / heartbeat.py async_reminder_worker 原样保留）。
--   本阶段未在 Supabase 执行本文件；后续由人工/工具审阅后执行。
--   本阶段不实现 MCP 工具、API、调度器、动态生成与 Telegram 发送。
--
-- 权限（沿用 activity_logs / memory_events 敏感业务表风格）：
--   RLS 开启且不创建任何 anon/authenticated 读写策略（deny-by-default）；
--   对 anon/authenticated 直接 REVOKE 全部表权限——读写均走 service_role
--   （绕过 RLS）。前端只能调用网关 API，不直接访问 Supabase。
-- ============================================================

CREATE TABLE IF NOT EXISTS public.ai_todos (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),

    -- 记录类型：
    --   reminder = 提醒事项（例如提醒吃药、上课、出门）；
    --   message  = AI 想在某个时间对用户主动说的一段话
    --              （例如某个月初、生日、纪念日）。
    kind            text NOT NULL CHECK (kind IN ('reminder', 'message')),

    -- 触达方式：
    --   static  = 到时间直接发送 content，不调用模型；
    --   dynamic = 到时间读取有限上下文，调用 background 模型生成最终消息；
    --             generation_prompt 保存表达意图和限制（给后台模型的要求），
    --             fallback_content 保存可选的备用内容（动态生成失败时发送）。
    delivery_mode   text NOT NULL DEFAULT 'static'
                    CHECK (delivery_mode IN ('static', 'dynamic')),

    -- 列表（控制台 / Mini App）中展示的简短标题。
    title           text NOT NULL DEFAULT '',

    -- static 模式：实际发送的内容；
    -- dynamic 模式：作为动态生成失败时的备用内容（与 fallback_content 等效兜底）。
    content         text NOT NULL DEFAULT '',

    -- dynamic 模式专用：给后台模型的表达要求（意图、口吻、限制）。
    generation_prompt text NOT NULL DEFAULT '',

    -- dynamic 模式生成失败时的备用发送内容；可为空（为空且生成失败时按
    -- 后续实现阶段的策略降级，本阶段不实现）。
    fallback_content text NOT NULL DEFAULT '',

    -- 触发时间，必须使用带时区时间（timestamptz），由应用层负责换算。
    scheduled_at    timestamptz NOT NULL,

    -- 只保存时区信息（IANA 名称），默认北京时间；当前不参与任何计算，
    -- 由应用层在换算/展示时使用。
    timezone        text NOT NULL DEFAULT 'Asia/Shanghai',

    -- 重复规则（jsonb）。第一版只允许应用层接受以下格式，数据库不做复杂
    -- CHECK 约束（合法性由应用层校验）：
    --   NULL                                     -> 不重复（一次性触发）；
    --   {"type": "daily"}                        -> 每天；
    --   {"type": "weekly", "weekdays": [1, 3, 5]} -> 每周指定星期（1=周一..7=周日）；
    --   {"type": "weekdays"}                     -> 工作日。
    -- 本阶段只保存字段，不实现规则计算。
    repeat_rule     jsonb,

    -- 调度状态：
    --   pending   = 等待触发；
    --   sending   = 正在发送，防止后台重复领取（幂等锁语义）；
    --   completed = 已处理，不再继续触发；
    --   cancelled = 已取消。
    -- 注意：completed 只表示「这条记录已经不再继续调度」，不代表系统判断
    -- 用户真的吃药、上课或完成了现实中的事情。对于重复待办，发送成功后
    -- 不能直接改为 completed，后续实现阶段会计算下一次 scheduled_at 并
    -- 继续保持 pending。
    status          text NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'completed', 'cancelled', 'sending')),

    -- 最近一次实际生成的动态消息（dynamic 模式留痕，便于追溯发送了什么）。
    last_generated_content text NOT NULL DEFAULT '',
    last_generated_at      timestamptz,

    -- 最近一次成功发送时间。
    last_sent_at    timestamptz,

    -- 最近一次失败的安全错误分类（如 TIMEOUT / GENERATION_FAILED / SEND_FAILED），
    -- 不保存密钥、Token、原始响应或任何敏感正文。
    last_error_code text NOT NULL DEFAULT '',
    last_attempt_at timestamptz,

    created_at      timestamptz NOT NULL DEFAULT now(),
    -- 项目迁移中无统一的 updated_at 自动触发器（已核实），本阶段不额外建立
    -- 通用触发器；由后续应用层在更新时显式写入。
    updated_at      timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE public.ai_todos IS
    'AI 待办 / AI 想说的话（阶段1基础数据层）：AI 记录未来要提醒用户的事或某个时间想说的话，由网关后台到时推送到 Telegram。与旧 reminders 表互不影响。';

COMMENT ON COLUMN public.ai_todos.kind IS
    'reminder=提醒事项（吃药/上课/出门等）；message=AI 想在某个时间主动对用户说的话（月初/生日/纪念日等）';

COMMENT ON COLUMN public.ai_todos.delivery_mode IS
    'static=到时直接发送 content，不调用模型；dynamic=到时结合有限上下文调用 background 模型生成，generation_prompt 为表达要求，生成失败时用 fallback_content 兜底';

COMMENT ON COLUMN public.ai_todos.status IS
    'pending=等待触发；sending=正在发送（防重复领取）；completed=已处理、不再继续调度（不代表用户现实中真的完成）；cancelled=已取消。重复待办发送成功后保持 pending，由应用层推进下一次 scheduled_at';

COMMENT ON COLUMN public.ai_todos.repeat_rule IS
    '重复规则，应用层只接受：NULL（不重复）/ {"type":"daily"} / {"type":"weekly","weekdays":[1,3,5]} / {"type":"weekdays"}；数据库不做复杂 CHECK，合法性由应用层校验';

-- 索引：调度器后续重点查询 status IN ('pending','sending') AND scheduled_at <= now()，
-- 部分索引覆盖该热路径；其余索引服务于列表展示与按类型筛选。
CREATE INDEX IF NOT EXISTS idx_ai_todos_pending_scheduled_at
    ON public.ai_todos (scheduled_at ASC)
    WHERE status IN ('pending', 'sending');

CREATE INDEX IF NOT EXISTS idx_ai_todos_status_scheduled_at
    ON public.ai_todos (status, scheduled_at ASC);

CREATE INDEX IF NOT EXISTS idx_ai_todos_kind
    ON public.ai_todos (kind);

-- RLS：开启，但【不创建任何 anon/authenticated 读写策略】（deny-by-default），
-- 并直接撤销两类角色的全部表权限；读写均由网关服务端经 service_role 完成。
ALTER TABLE public.ai_todos ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON public.ai_todos FROM anon, authenticated;

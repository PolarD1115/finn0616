-- ============================================================
-- 20260818_008_home_runtime_base.sql
-- Home Runtime 基础模型 (Phase 2)
-- 7 张新表 + 约束 + 索引 + RLS + 种子房间
-- 无 DELETE / DROP / TRUNCATE，不影响任何旧表
-- ============================================================

-- 1. home_rooms — 房间定义
CREATE TABLE IF NOT EXISTS public.home_rooms (
    id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    stable_key  text        NOT NULL UNIQUE,
    name        text        NOT NULL,
    description text,
    emoji       text        NOT NULL DEFAULT '🏠',
    room_type   text        NOT NULL DEFAULT 'common'
        CHECK (room_type IN ('common','private','outdoor','special')),
    sort_order  integer     NOT NULL DEFAULT 0,
    is_enabled  boolean     NOT NULL DEFAULT true,
    is_hidden   boolean     NOT NULL DEFAULT false,
    unlock_condition jsonb  NOT NULL DEFAULT '{}'::jsonb,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- 2. home_members — 家庭成员
CREATE TABLE IF NOT EXISTS public.home_members (
    id              uuid    PRIMARY KEY DEFAULT gen_random_uuid(),
    stable_key      text    NOT NULL UNIQUE,
    name            text    NOT NULL,
    member_type     text    NOT NULL
        CHECK (member_type IN ('ai','pet','doll','custom')),
    is_active       boolean NOT NULL DEFAULT true,
    lifecycle_status text   NOT NULL DEFAULT 'alive'
        CHECK (lifecycle_status IN ('alive','sleeping','inactive','departed')),
    profile         jsonb   NOT NULL DEFAULT '{}'::jsonb,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- 3. home_member_states — 成员状态 (1:1 with home_members)
CREATE TABLE IF NOT EXISTS public.home_member_states (
    id              uuid    PRIMARY KEY DEFAULT gen_random_uuid(),
    member_id       uuid    NOT NULL UNIQUE REFERENCES public.home_members(id) ON DELETE CASCADE,
    hunger          numeric NOT NULL DEFAULT 50 CHECK (hunger >= 0 AND hunger <= 100),
    energy          numeric NOT NULL DEFAULT 80 CHECK (energy >= 0 AND energy <= 100),
    mood            numeric NOT NULL DEFAULT 60 CHECK (mood >= 0 AND mood <= 100),
    comfort         numeric NOT NULL DEFAULT 60 CHECK (comfort >= 0 AND comfort <= 100),
    connection      numeric NOT NULL DEFAULT 30 CHECK (connection >= 0 AND connection <= 100),
    intimacy        numeric NOT NULL DEFAULT 30 CHECK (intimacy >= 0 AND intimacy <= 100),
    health          numeric NOT NULL DEFAULT 90 CHECK (health >= 0 AND health <= 100),
    cleanliness     numeric NOT NULL DEFAULT 70 CHECK (cleanliness >= 0 AND cleanliness <= 100),
    current_room_id uuid    REFERENCES public.home_rooms(id) ON DELETE SET NULL,
    extra           jsonb   NOT NULL DEFAULT '{}'::jsonb,
    last_settled_at timestamptz NOT NULL DEFAULT now(),
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- 4. home_objects — 房间物品
CREATE TABLE IF NOT EXISTS public.home_objects (
    id          uuid    PRIMARY KEY DEFAULT gen_random_uuid(),
    room_id     uuid    NOT NULL REFERENCES public.home_rooms(id) ON DELETE CASCADE,
    object_type text    NOT NULL
        CHECK (object_type IN ('furniture','container','decoration','interactive','plant','appliance')),
    stable_key  text,
    name        text    NOT NULL,
    description text,
    visual      jsonb   NOT NULL DEFAULT '{}'::jsonb,
    is_hidden   boolean NOT NULL DEFAULT false,
    state       jsonb   NOT NULL DEFAULT '{}'::jsonb,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- 5. home_events — 统一生活事件 (追加型，不保存当前状态)
CREATE TABLE IF NOT EXISTS public.home_events (
    id                uuid    PRIMARY KEY DEFAULT gen_random_uuid(),
    event_key         text    UNIQUE,
    event_type        text    NOT NULL
        CHECK (event_type IN ('entered_room','rested','ate','cooked','planted','watered',
               'harvested','fed_member','played','created_art','wrote_letter','left_note',
               'wrote_diary','state_changed','system_tick')),
    actor_member_id   uuid    REFERENCES public.home_members(id) ON DELETE SET NULL,
    target_member_id  uuid    REFERENCES public.home_members(id) ON DELETE SET NULL,
    room_id           uuid    REFERENCES public.home_rooms(id) ON DELETE SET NULL,
    source            text    NOT NULL DEFAULT 'system',
    visibility        text    NOT NULL DEFAULT 'home'
        CHECK (visibility IN ('private','home','user_visible','system')),
    summary           text    NOT NULL,
    details           jsonb   NOT NULL DEFAULT '{}'::jsonb,
    occurred_at       timestamptz NOT NULL DEFAULT now(),
    created_at        timestamptz NOT NULL DEFAULT now()
);

-- 6. home_action_runs — 行动执行记录
CREATE TABLE IF NOT EXISTS public.home_action_runs (
    id              uuid    PRIMARY KEY DEFAULT gen_random_uuid(),
    action_key      text    NOT NULL UNIQUE,
    action_type     text    NOT NULL,
    actor_member_id uuid    REFERENCES public.home_members(id) ON DELETE SET NULL,
    status          text    NOT NULL DEFAULT 'requested'
        CHECK (status IN ('requested','running','succeeded','failed','skipped')),
    requested_at    timestamptz NOT NULL DEFAULT now(),
    started_at      timestamptz,
    finished_at     timestamptz,
    input           jsonb   NOT NULL DEFAULT '{}'::jsonb,
    result          jsonb,
    error_code      text,
    error_message   text,
    created_at      timestamptz NOT NULL DEFAULT now()
);

-- 7. home_jobs — Home Runtime 后台任务队列
CREATE TABLE IF NOT EXISTS public.home_jobs (
    id          uuid    PRIMARY KEY DEFAULT gen_random_uuid(),
    job_type    text    NOT NULL,
    dedupe_key  text    UNIQUE,
    status      text    NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','claimed','running','succeeded','failed','cancelled')),
    priority    integer NOT NULL DEFAULT 50,
    not_before  timestamptz NOT NULL DEFAULT now(),
    attempts    integer NOT NULL DEFAULT 0,
    max_attempts integer NOT NULL DEFAULT 3,
    payload     jsonb   NOT NULL DEFAULT '{}'::jsonb,
    last_error  text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- ============================================================
-- 索引
-- ============================================================
CREATE INDEX IF NOT EXISTS idx_home_objects_room ON public.home_objects(room_id);
CREATE INDEX IF NOT EXISTS idx_home_events_occurred ON public.home_events(occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_home_events_room_time ON public.home_events(room_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_home_events_actor_time ON public.home_events(actor_member_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_home_events_type_time ON public.home_events(event_type, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_home_action_runs_status ON public.home_action_runs(status);
CREATE INDEX IF NOT EXISTS idx_home_jobs_status_notbefore ON public.home_jobs(status, not_before);

-- ============================================================
-- RLS — anon/authenticated 只读，写操作需 service_role
-- ============================================================
ALTER TABLE public.home_rooms ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.home_members ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.home_member_states ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.home_objects ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.home_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.home_action_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.home_jobs ENABLE ROW LEVEL SECURITY;

CREATE POLICY home_rooms_select_all ON public.home_rooms FOR SELECT TO anon, authenticated USING (true);
CREATE POLICY home_members_select_all ON public.home_members FOR SELECT TO anon, authenticated USING (true);
CREATE POLICY home_member_states_select_all ON public.home_member_states FOR SELECT TO anon, authenticated USING (true);
CREATE POLICY home_objects_select_all ON public.home_objects FOR SELECT TO anon, authenticated USING (true);
CREATE POLICY home_events_select_all ON public.home_events FOR SELECT TO anon, authenticated USING (true);
CREATE POLICY home_action_runs_select_all ON public.home_action_runs FOR SELECT TO anon, authenticated USING (true);
CREATE POLICY home_jobs_select_all ON public.home_jobs FOR SELECT TO anon, authenticated USING (true);

-- ============================================================
-- 种子数据：9 个初始房间（幂等）
-- ============================================================
INSERT INTO public.home_rooms (stable_key, name, description, emoji, room_type, sort_order, is_enabled, is_hidden, unlock_condition) VALUES
('living_room', '客厅', '一家人聚在一起的地方，沙发、电视、暖色灯光。', '🛋️', 'common', 10, true, false, '{}'::jsonb),
('bedroom', '卧室', '安静私密的休息空间，有床和衣柜。', '🛏️', 'private', 20, true, false, '{}'::jsonb),
('kitchen', '厨房', '做饭和存放食材的地方，有灶台和冰箱。', '🍳', 'common', 30, true, false, '{}'::jsonb),
('study', '书房', '看书和学习的小天地，书架和书桌。', '📚', 'common', 40, true, false, '{}'::jsonb),
('studio', '工作室', '创作和手工的空间，画架和工具。', '🎨', 'common', 50, true, false, '{}'::jsonb),
('garden', '花园', '户外种植区，阳光和泥土的味道。', '🌱', 'outdoor', 60, true, false, '{}'::jsonb),
('seaside', '海边', '离家不远的海岸，能听到浪声。', '🌊', 'outdoor', 70, true, false, '{}'::jsonb),
('observatory', '观星台', '能仰望星空的高处，尚未解锁。', '🔭', 'special', 80, true, true, '{"type":"manual","description":"需要特殊条件解锁"}'::jsonb),
('basement', '地下室', '神秘的地下空间，尚未解锁。', '🚪', 'special', 90, true, true, '{"type":"manual","description":"需要特殊条件解锁"}'::jsonb)
ON CONFLICT (stable_key) DO NOTHING;

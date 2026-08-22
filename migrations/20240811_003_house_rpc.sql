-- ============================================================
-- Migration: 20240811_003_house_rpc
-- Phase 3: 有状态小屋 (Stateful Memory House)
-- ============================================================
-- 约束：非破坏性、向后兼容、无 DELETE/DROP/TRUNCATE

-- 1. 房间物品表（若不存在则创建）
CREATE TABLE IF NOT EXISTS public.house_objects (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    room_id    text NOT NULL,
    name       text NOT NULL,
    emoji      text DEFAULT '📦',
    description text,
    is_hidden  boolean DEFAULT false,
    created_at timestamptz DEFAULT now()
);

-- RLS（若尚未启用）
ALTER TABLE public.house_objects ENABLE ROW LEVEL SECURITY;

-- 2. 房间物品表索引
CREATE INDEX IF NOT EXISTS idx_house_objects_room
    ON public.house_objects USING btree (room_id);

CREATE INDEX IF NOT EXISTS idx_house_objects_visible
    ON public.house_objects USING btree (room_id, is_hidden)
    WHERE is_hidden = false;

-- 3. RPC 函数：查看房间（含物品 + 近期日记）
CREATE OR REPLACE FUNCTION public.rpc_house_look(
    p_room_id text
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER SET search_path = public
AS $$
DECLARE
    v_room jsonb;
    v_objects jsonb;
    v_diary jsonb;
BEGIN
    -- 房间信息
    SELECT jsonb_build_object(
        'id', id,
        'name', name,
        'description', description,
        'emoji', emoji,
        'sort_order', sort_order,
        'is_default', is_default
    )
    INTO v_room
    FROM house_rooms
    WHERE id = p_room_id;

    IF v_room IS NULL THEN
        RETURN jsonb_build_object('ok', false, 'error_code', 'ROOM_NOT_FOUND');
    END IF;

    -- 房间内可见物品
    SELECT jsonb_agg(
        jsonb_build_object(
            'id', id,
            'name', name,
            'emoji', emoji,
            'description', description
        ) ORDER BY created_at DESC
    )
    INTO v_objects
    FROM house_objects
    WHERE room_id = p_room_id AND is_hidden = false;

    -- 近期日记（最近 10 条）
    SELECT jsonb_agg(
        jsonb_build_object(
            'id', id,
            'entry_type', entry_type,
            'content', content,
            'mood', mood,
            'weather', weather,
            'tags', tags,
            'created_at', created_at
        ) ORDER BY created_at DESC
    )
    INTO v_diary
    FROM (
        SELECT id, entry_type, content, mood, weather, tags, created_at
        FROM house_diary
        WHERE room_id = p_room_id
        ORDER BY created_at DESC
        LIMIT 10
    ) sub;

    RETURN jsonb_build_object(
        'ok', true,
        'room', v_room,
        'objects', COALESCE(v_objects, '[]'::jsonb),
        'diary', COALESCE(v_diary, '[]'::jsonb)
    );
END;
$$;

-- 4. RPC 函数：在房间做某事（写入日记）
CREATE OR REPLACE FUNCTION public.rpc_house_do(
    p_room_id text,
    p_entry_type text,
    p_content text,
    p_mood text DEFAULT NULL,
    p_weather text DEFAULT NULL,
    p_tags text[] DEFAULT array[]::text[]
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER SET search_path = public
AS $$
DECLARE
    v_room_exists boolean;
    v_id bigint;
BEGIN
    SELECT EXISTS(SELECT 1 FROM house_rooms WHERE id = p_room_id)
    INTO v_room_exists;

    IF NOT v_room_exists THEN
        RETURN jsonb_build_object('ok', false, 'error_code', 'ROOM_NOT_FOUND');
    END IF;

    INSERT INTO house_diary (room_id, entry_type, content, mood, weather, tags)
    VALUES (p_room_id, p_entry_type, p_content, p_mood, p_weather, p_tags)
    RETURNING id INTO v_id;

    RETURN jsonb_build_object('ok', true, 'id', v_id);
END;
$$;

-- 5. RPC 函数：放置物品到房间
CREATE OR REPLACE FUNCTION public.rpc_house_put(
    p_room_id text,
    p_name text,
    p_emoji text DEFAULT '📦',
    p_description text DEFAULT NULL
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER SET search_path = public
AS $$
DECLARE
    v_room_exists boolean;
    v_id uuid;
BEGIN
    SELECT EXISTS(SELECT 1 FROM house_rooms WHERE id = p_room_id)
    INTO v_room_exists;

    IF NOT v_room_exists THEN
        RETURN jsonb_build_object('ok', false, 'error_code', 'ROOM_NOT_FOUND');
    END IF;

    INSERT INTO house_objects (room_id, name, emoji, description)
    VALUES (p_room_id, p_name, p_emoji, p_description)
    RETURNING id INTO v_id;

    RETURN jsonb_build_object('ok', true, 'id', v_id);
END;
$$;

-- 6. RPC 函数：从房间拿走物品
CREATE OR REPLACE FUNCTION public.rpc_house_take(
    p_object_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER SET search_path = public
AS $$
DECLARE
    v_exists boolean;
BEGIN
    SELECT EXISTS(SELECT 1 FROM house_objects WHERE id = p_object_id)
    INTO v_exists;

    IF NOT v_exists THEN
        RETURN jsonb_build_object('ok', false, 'error_code', 'OBJECT_NOT_FOUND');
    END IF;

    DELETE FROM house_objects WHERE id = p_object_id;

    RETURN jsonb_build_object('ok', true);
END;
$$;

-- 7. RPC 函数：更新房间描述
CREATE OR REPLACE FUNCTION public.rpc_house_update_desc(
    p_room_id text,
    p_description text
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER SET search_path = public
AS $$
DECLARE
    v_room_exists boolean;
BEGIN
    SELECT EXISTS(SELECT 1 FROM house_rooms WHERE id = p_room_id)
    INTO v_room_exists;

    IF NOT v_room_exists THEN
        RETURN jsonb_build_object('ok', false, 'error_code', 'ROOM_NOT_FOUND');
    END IF;

    UPDATE house_rooms SET description = p_description WHERE id = p_room_id;

    RETURN jsonb_build_object('ok', true);
END;
$$;

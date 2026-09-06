-- ============================================================
-- 第 30 阶段：memory_items 向量列、HNSW 与 active-only RPC 基础设施
-- 职责：为 memory_items 建立 additive 向量基础设施（3 个可空列 + HNSW 索引 +
--       service_role-only 的 active-only 余弦召回 RPC），供未来手动 backfill
--       与 lexical+vector 混合召回使用。本阶段不回填任何向量、不接正式上下文。
-- 前提：第 29 阶段生产诊断 EMBEDDING_DIMENSION_CONFIRMED=1024；
--       pgvector 0.8.0（vector / <=> / vector_dims / vector_cosine_ops
--       均位于 extensions schema，已在生产目录中核实）。
-- 官方依据：
--       - PostgreSQL CREATE FUNCTION 文档明确参数类型的 typmod 会被丢弃
--         （"parenthesized type modifiers ... are discarded by CREATE FUNCTION"），
--         因此 RPC 参数采用不带 typmod 的 extensions.vector，
--         维度约束在函数体内显式校验 vector_dims()=1024，非法参数 RAISE EXCEPTION；
--       - pgvector README：NULL 向量与零向量（cosine）不入 HNSW 索引；
--       - CREATE FUNCTION 默认授予 PUBLIC EXECUTE，REVOKE 必须同 migration 执行。
-- 边界：纯 additive DDL（ADD COLUMN / CREATE INDEX / CREATE FUNCTION /
--       REVOKE / GRANT / COMMENT）；无任何 DML；不修改旧表、旧向量列
--       （memories / memory_summaries / active_memories）、旧 RPC
--       （match_memories / match_active_memories）、RLS 与既有策略；
--       不删除任何数据或对象。
-- 远端通过 Supabase apply_migration 执行
--       （migration 名 add_memory_items_vector_infrastructure）。
-- ============================================================

-- 1) 三个可空向量字段：不设默认值、不自动生成、无触发器、不回填现有行
ALTER TABLE public.memory_items
    ADD COLUMN IF NOT EXISTS embedding       extensions.vector(1024),
    ADD COLUMN IF NOT EXISTS embedding_model text,
    ADD COLUMN IF NOT EXISTS embedded_at     timestamptz;

-- 2) 三列一致性约束：要么全空（现有数据/未回填），要么三列在同一 UPDATE 中
--    原子写入（防半写入）。现有行三列全 NULL，天然满足；未来 backfill 必须
--    单条 UPDATE 同时写三列。
ALTER TABLE public.memory_items
    ADD CONSTRAINT memory_items_embedding_triplet_check
    CHECK (
        (embedding IS NULL AND embedding_model IS NULL AND embedded_at IS NULL)
        OR
        (embedding IS NOT NULL AND embedding_model IS NOT NULL AND embedded_at IS NOT NULL)
    );

-- 3) HNSW 索引：cosine opclass + pgvector 默认参数（m=16, ef_construction=64，
--    未经校准不显式设置）。NULL 向量与零向量（cosine）不入索引，
--    与 RPC 的 embedding IS NOT NULL 过滤天然一致。
CREATE INDEX IF NOT EXISTS memory_items_embedding_idx
    ON public.memory_items
    USING hnsw (embedding extensions.vector_cosine_ops);

-- 4) active-only 向量召回 RPC（只读；仅 service_role 可执行）
--    - 过滤条件固定且不可由调用方关闭：user_id / status='active' /
--      expires_at 未过期 / embedding IS NOT NULL；
--    - 排序与相似度使用同一余弦距离表达式（<=>，extensions schema 全限定）；
--    - 返回内部 memory_item_id 供未来 lexical+vector 候选合并，
--      不返回 user_id / embedding / content_hash / source_event_ids /
--      source_batch_id / metadata / created_by / superseded_by；
--    - 非法参数（NULL 向量 / 维度≠1024 / 空白 user_id / match_count 出界）
--      一律 RAISE EXCEPTION，不静默返回空集；错误信息不含向量与 user_id 值。
CREATE OR REPLACE FUNCTION public.match_memory_items(
    query_embedding extensions.vector,
    p_user_id       text,
    match_count     integer DEFAULT 5
)
RETURNS TABLE (
    memory_item_id uuid,
    content        text,
    memory_type    text,
    importance     integer,
    confidence     double precision,
    subject_key    text,
    valid_at       timestamptz,
    expires_at     timestamptz,
    source         text,
    similarity     double precision
)
LANGUAGE plpgsql
STABLE
SECURITY INVOKER
SET search_path TO ''
AS $$
BEGIN
    IF query_embedding IS NULL THEN
        RAISE EXCEPTION 'match_memory_items: query_embedding is required';
    END IF;
    IF extensions.vector_dims(query_embedding) <> 1024 THEN
        RAISE EXCEPTION 'match_memory_items: query_embedding dimension mismatch, expected 1024';
    END IF;
    IF p_user_id IS NULL OR btrim(p_user_id) = '' THEN
        RAISE EXCEPTION 'match_memory_items: p_user_id is required';
    END IF;
    IF match_count IS NULL OR match_count < 1 OR match_count > 10 THEN
        RAISE EXCEPTION 'match_memory_items: match_count must be between 1 and 10';
    END IF;

    RETURN QUERY
    SELECT
        mi.id,
        mi.content,
        mi.memory_type,
        mi.importance,
        mi.confidence,
        mi.subject_key,
        mi.valid_at,
        mi.expires_at,
        mi.source,
        (1 - (mi.embedding OPERATOR(extensions.<=>) query_embedding))::double precision AS similarity
    FROM public.memory_items AS mi
    WHERE mi.user_id = p_user_id
      AND mi.status = 'active'
      AND (mi.expires_at IS NULL OR mi.expires_at > now())
      AND mi.embedding IS NOT NULL
    ORDER BY mi.embedding OPERATOR(extensions.<=>) query_embedding
    LIMIT match_count;
END;
$$;

-- 5) 收紧执行权限：CREATE FUNCTION 默认授予 PUBLIC EXECUTE（官方文档确认），
--    必须与函数创建在同一 migration 内撤销；REVOKE/GRANT 使用精确签名；
--    不修改旧 RPC 权限，不授予任何表权限，RLS 保持原状。
REVOKE EXECUTE ON FUNCTION public.match_memory_items(extensions.vector, text, integer) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.match_memory_items(extensions.vector, text, integer) FROM anon;
REVOKE EXECUTE ON FUNCTION public.match_memory_items(extensions.vector, text, integer) FROM authenticated;
GRANT  EXECUTE ON FUNCTION public.match_memory_items(extensions.vector, text, integer) TO service_role;

-- 6) 注释（不含任何业务值/模型名/密钥）
COMMENT ON FUNCTION public.match_memory_items(extensions.vector, text, integer) IS
    'memory_items active-only cosine recall (Phase 30). service_role-only (PUBLIC/anon/authenticated EXECUTE revoked). Hard filters: user_id, status=active, expires_at, embedding IS NOT NULL. Raises on null vector / vector_dims<>1024 / blank p_user_id / match_count outside 1..10. Returns internal memory_item_id for future lexical+vector candidate merge. STABLE + SECURITY INVOKER + empty search_path, all objects schema-qualified.';

COMMENT ON COLUMN public.memory_items.embedding IS
    'pgvector(1024)（第30阶段 additive）；仅允许未来手动 backfill 从经人工批准的 active 正文重新生成，禁止从旧 Pinecone/旧 Supabase 向量表导入；与 embedding_model/embedded_at 受 memory_items_embedding_triplet_check 约束三列同批原子写入';

COMMENT ON COLUMN public.memory_items.embedding_model IS
    '生成 embedding 的模型标识（backfill 时与 embedding/embedded_at 同批写入）；本阶段不写入';

COMMENT ON COLUMN public.memory_items.embedded_at IS
    'embedding 生成时间（timestamptz，backfill 时同批写入）；本阶段不写入';

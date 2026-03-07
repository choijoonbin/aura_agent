-- RAG 청킹 고도화: 동의어/형태소 확장 토큰 컬럼 (BM25 품질 향상)
-- 대상: dwp_aura.rag_chunk
-- chunking3.md 프롬프트 4 적용 시 실행

ALTER TABLE dwp_aura.rag_chunk
  ADD COLUMN IF NOT EXISTS search_tokens text;

CREATE INDEX IF NOT EXISTS ix_rag_chunk_search_tokens_gin
  ON dwp_aura.rag_chunk
  USING gin (to_tsvector('simple', coalesce(search_tokens, '')));

COMMENT ON COLUMN dwp_aura.rag_chunk.search_tokens IS '동의어·조사 제거 확장 토큰 (BM25 보조 매칭)';

ANALYZE dwp_aura.rag_chunk;

-- Azure/OpenAI text-embedding-3-large(3072) 전환용 마이그레이션
-- 대상: dwp_aura.rag_chunk

-- 3072 차원은 vector 타입(HNSW/IVFFLAT) 인덱스 제한(<=2000)과 충돌하므로 halfvec 사용.
ALTER TABLE dwp_aura.rag_chunk
  ADD COLUMN IF NOT EXISTS embedding_az halfvec(3072);

-- 기존 인덱스 제거(타입/ops 변경 대비)
DROP INDEX IF EXISTS ix_rag_chunk_embedding_az_hnsw;
DROP INDEX IF EXISTS ix_rag_chunk_embedding_az_ivfflat;

-- halfvec + hnsw (차원 4000 이하 지원)
-- 운영 중 락 영향을 줄이려면 CONCURRENTLY 사용을 검토하세요.
CREATE INDEX IF NOT EXISTS ix_rag_chunk_embedding_az_hnsw
  ON dwp_aura.rag_chunk
  USING hnsw (embedding_az halfvec_cosine_ops)
  WITH (m = 16, ef_construction = 64);

ANALYZE dwp_aura.rag_chunk;

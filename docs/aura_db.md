-- dwp_aura.agent_action_simulation definition

-- Drop table

-- DROP TABLE dwp_aura.agent_action_simulation;

CREATE TABLE dwp_aura.agent_action_simulation (
	simulation_id bigserial NOT NULL,
	tenant_id int8 NOT NULL,
	case_id int8 NOT NULL,
	action_type varchar(50) NOT NULL,
	payload_json jsonb NULL,
	before_json jsonb NULL,
	after_json jsonb NULL,
	validation_json jsonb NULL,
	created_at timestamptz DEFAULT now() NOT NULL,
	created_by_actor varchar(20) NULL,
	created_by_id int8 NULL,
	CONSTRAINT agent_action_simulation_pkey PRIMARY KEY (simulation_id)
);
CREATE INDEX ix_agent_action_simulation_case ON dwp_aura.agent_action_simulation USING btree (tenant_id, case_id, created_at DESC);
CREATE INDEX ix_agent_action_simulation_tenant_created ON dwp_aura.agent_action_simulation USING btree (tenant_id, created_at DESC);
COMMENT ON TABLE dwp_aura.agent_action_simulation IS 'Agent Tool simulate API 전용. propose/execute 전 단독 시뮬레이션 결과 저장.';

-- Permissions

ALTER TABLE dwp_aura.agent_action_simulation OWNER TO dwp_user;
GRANT ALL ON TABLE dwp_aura.agent_action_simulation TO dwp_user;


-- dwp_aura.agent_activity_log definition

-- Drop table

-- DROP TABLE dwp_aura.agent_activity_log;

CREATE TABLE dwp_aura.agent_activity_log (
	activity_id bigserial NOT NULL,
	tenant_id int8 NOT NULL,
	stage text NOT NULL,
	event_type text NULL,
	resource_type text NULL,
	resource_id text NULL,
	occurred_at timestamptz DEFAULT now() NOT NULL,
	actor_agent_id text NULL,
	actor_user_id int8 NULL,
	actor_display_name text NULL,
	metadata_json jsonb NULL,
	created_at timestamptz DEFAULT now() NOT NULL,
	created_by int8 NULL, -- 생성자 user_id (논리적 참조: com_users.user_id)
	updated_at timestamptz DEFAULT now() NOT NULL, -- 수정일시
	updated_by int8 NULL, -- 수정자 user_id (논리적 참조: com_users.user_id)
	CONSTRAINT agent_activity_log_pkey PRIMARY KEY (activity_id)
);
CREATE INDEX ix_agent_activity_log_tenant_case_occurred ON dwp_aura.agent_activity_log USING btree (tenant_id, resource_type, resource_id, occurred_at);
CREATE INDEX ix_agent_activity_log_tenant_meta_event_type ON dwp_aura.agent_activity_log USING btree (tenant_id, ((metadata_json ->> 'event_type'::text)));
CREATE INDEX ix_agent_activity_log_tenant_meta_input_hash ON dwp_aura.agent_activity_log USING btree (tenant_id, ((metadata_json ->> 'input_hash'::text)));
CREATE INDEX ix_agent_activity_log_tenant_meta_run_id ON dwp_aura.agent_activity_log USING btree (tenant_id, ((metadata_json ->> 'run_id'::text)));
CREATE INDEX ix_agent_activity_log_tenant_occurred ON dwp_aura.agent_activity_log USING btree (tenant_id, occurred_at DESC);
CREATE INDEX ix_agent_activity_log_tenant_resource ON dwp_aura.agent_activity_log USING btree (tenant_id, resource_type, resource_id);
COMMENT ON TABLE dwp_aura.agent_activity_log IS 'Aura 에이전트 활동 스트림. event_type→stage 매핑 적용.';

-- Column comments

COMMENT ON COLUMN dwp_aura.agent_activity_log.created_by IS '생성자 user_id (논리적 참조: com_users.user_id)';
COMMENT ON COLUMN dwp_aura.agent_activity_log.updated_at IS '수정일시';
COMMENT ON COLUMN dwp_aura.agent_activity_log.updated_by IS '수정자 user_id (논리적 참조: com_users.user_id)';

-- Permissions

ALTER TABLE dwp_aura.agent_activity_log OWNER TO dwp_user;
GRANT ALL ON TABLE dwp_aura.agent_activity_log TO dwp_user;


-- dwp_aura.agent_master definition

-- Drop table

-- DROP TABLE dwp_aura.agent_master;

CREATE TABLE dwp_aura.agent_master (
	agent_id bigserial NOT NULL, -- 에이전트 식별자 (PK)
	tenant_id int8 NOT NULL, -- 테넌트 식별자 (격리 필수)
	"name" varchar(255) NOT NULL, -- 에이전트 표시명
	"domain" varchar(100) NULL, -- 도메인(예: FINANCE, COMPLIANCE)
	model_name varchar(255) NULL, -- LLM 모델명
	temperature numeric(5, 4) NULL, -- 생성 온도
	max_tokens int4 NULL, -- 최대 토큰
	is_active bool DEFAULT true NOT NULL, -- 활성 여부
	created_at timestamptz DEFAULT now() NOT NULL,
	updated_at timestamptz DEFAULT now() NOT NULL,
	agent_key varchar(100) NOT NULL, -- 에이전트 키 (Snake Case, Aura 호출 시 사용). 예: finance_aura, hr_aura. tenant 내 unique.
	CONSTRAINT agent_master_pkey PRIMARY KEY (agent_id)
);
CREATE INDEX ix_agent_master_tenant_active ON dwp_aura.agent_master USING btree (tenant_id, is_active);
CREATE INDEX ix_agent_master_tenant_id ON dwp_aura.agent_master USING btree (tenant_id);
CREATE UNIQUE INDEX ux_agent_master_tenant_agent_key ON dwp_aura.agent_master USING btree (tenant_id, agent_key);
COMMENT ON TABLE dwp_aura.agent_master IS '에이전트 스튜디오: 에이전트 마스터. Aura 런타임 조립용.';

-- Column comments

COMMENT ON COLUMN dwp_aura.agent_master.agent_id IS '에이전트 식별자 (PK)';
COMMENT ON COLUMN dwp_aura.agent_master.tenant_id IS '테넌트 식별자 (격리 필수)';
COMMENT ON COLUMN dwp_aura.agent_master."name" IS '에이전트 표시명';
COMMENT ON COLUMN dwp_aura.agent_master."domain" IS '도메인(예: FINANCE, COMPLIANCE)';
COMMENT ON COLUMN dwp_aura.agent_master.model_name IS 'LLM 모델명';
COMMENT ON COLUMN dwp_aura.agent_master.temperature IS '생성 온도';
COMMENT ON COLUMN dwp_aura.agent_master.max_tokens IS '최대 토큰';
COMMENT ON COLUMN dwp_aura.agent_master.is_active IS '활성 여부';
COMMENT ON COLUMN dwp_aura.agent_master.agent_key IS '에이전트 키 (Snake Case, Aura 호출 시 사용). 예: finance_aura, hr_aura. tenant 내 unique.';

-- Permissions

ALTER TABLE dwp_aura.agent_master OWNER TO dwp_user;
GRANT ALL ON TABLE dwp_aura.agent_master TO dwp_user;


-- dwp_aura.agent_tool_inventory definition

-- Drop table

-- DROP TABLE dwp_aura.agent_tool_inventory;

CREATE TABLE dwp_aura.agent_tool_inventory (
	tool_id bigserial NOT NULL, -- 도구 식별자 (PK)
	tool_name varchar(255) NOT NULL, -- 도구명 (Aura 엔진 등록명과 동일)
	description text NULL, -- 도구 설명
	schema_json jsonb NULL, -- 파라미터 규격 (JSON Schema 등)
	created_at timestamptz DEFAULT now() NOT NULL,
	updated_at timestamptz DEFAULT now() NOT NULL,
	CONSTRAINT agent_tool_inventory_pkey PRIMARY KEY (tool_id),
	CONSTRAINT agent_tool_inventory_tool_name_key UNIQUE (tool_name)
);
COMMENT ON TABLE dwp_aura.agent_tool_inventory IS '에이전트 스튜디오: 도구 카탈로그. tool_name은 Aura FINANCE_TOOLS 함수명과 100% 일치.';

-- Column comments

COMMENT ON COLUMN dwp_aura.agent_tool_inventory.tool_id IS '도구 식별자 (PK)';
COMMENT ON COLUMN dwp_aura.agent_tool_inventory.tool_name IS '도구명 (Aura 엔진 등록명과 동일)';
COMMENT ON COLUMN dwp_aura.agent_tool_inventory.description IS '도구 설명';
COMMENT ON COLUMN dwp_aura.agent_tool_inventory.schema_json IS '파라미터 규격 (JSON Schema 등)';

-- Permissions

ALTER TABLE dwp_aura.agent_tool_inventory OWNER TO dwp_user;
GRANT ALL ON TABLE dwp_aura.agent_tool_inventory TO dwp_user;


-- dwp_aura.analysis_replay_gate_run definition

-- Drop table

-- DROP TABLE dwp_aura.analysis_replay_gate_run;

CREATE TABLE dwp_aura.analysis_replay_gate_run (
	id bigserial NOT NULL,
	tenant_id int8 NOT NULL,
	run_key varchar(128) NOT NULL,
	gate_passed bool NOT NULL,
	result_json jsonb NOT NULL,
	created_at timestamptz DEFAULT now() NOT NULL,
	CONSTRAINT analysis_replay_gate_run_pkey PRIMARY KEY (id)
);
CREATE INDEX ix_analysis_replay_gate_run_tenant_created ON dwp_aura.analysis_replay_gate_run USING btree (tenant_id, created_at DESC);

-- Permissions

ALTER TABLE dwp_aura.analysis_replay_gate_run OWNER TO dwp_user;
GRANT ALL ON TABLE dwp_aura.analysis_replay_gate_run TO dwp_user;


-- dwp_aura.analytics_kpi_daily definition

-- Drop table

-- DROP TABLE dwp_aura.analytics_kpi_daily;

CREATE TABLE dwp_aura.analytics_kpi_daily (
	tenant_id int8 NOT NULL, -- 테넌트 식별자
	ymd date NOT NULL, -- 집계 일자
	metric_key varchar(80) NOT NULL, -- 메트릭 키 (savings_estimate, prevented_loss 등)
	metric_value numeric(18, 4) NOT NULL, -- 메트릭 값
	dims_json jsonb DEFAULT '{}'::jsonb NULL, -- 차원 (JSONB)
	dims_hash varchar(64) DEFAULT ''::character varying NOT NULL, -- 차원 해시 (복합키)
	created_at timestamptz DEFAULT now() NOT NULL, -- 생성일시
	CONSTRAINT analytics_kpi_daily_pkey PRIMARY KEY (tenant_id, ymd, metric_key, dims_hash)
);
CREATE INDEX ix_analytics_kpi_tenant_ymd ON dwp_aura.analytics_kpi_daily USING btree (tenant_id, ymd);
COMMENT ON TABLE dwp_aura.analytics_kpi_daily IS '일별 KPI 메트릭. savings_estimate, prevented_loss, median_triage_time, automation_rate 등.';

-- Column comments

COMMENT ON COLUMN dwp_aura.analytics_kpi_daily.tenant_id IS '테넌트 식별자';
COMMENT ON COLUMN dwp_aura.analytics_kpi_daily.ymd IS '집계 일자';
COMMENT ON COLUMN dwp_aura.analytics_kpi_daily.metric_key IS '메트릭 키 (savings_estimate, prevented_loss 등)';
COMMENT ON COLUMN dwp_aura.analytics_kpi_daily.metric_value IS '메트릭 값';
COMMENT ON COLUMN dwp_aura.analytics_kpi_daily.dims_json IS '차원 (JSONB)';
COMMENT ON COLUMN dwp_aura.analytics_kpi_daily.dims_hash IS '차원 해시 (복합키)';
COMMENT ON COLUMN dwp_aura.analytics_kpi_daily.created_at IS '생성일시';

-- Permissions

ALTER TABLE dwp_aura.analytics_kpi_daily OWNER TO dwp_user;
GRANT ALL ON TABLE dwp_aura.analytics_kpi_daily TO dwp_user;


-- dwp_aura.app_code_groups definition

-- Drop table

-- DROP TABLE dwp_aura.app_code_groups;

CREATE TABLE dwp_aura.app_code_groups (
	app_code_group_id bigserial NOT NULL,
	group_key varchar(100) NOT NULL, -- 그룹 키 (예: SECURITY_ACCESS_MODEL, PII_HANDLING)
	group_name varchar(200) NULL, -- 그룹 표시명
	description varchar(500) NULL, -- 그룹 설명
	is_active bool DEFAULT true NOT NULL, -- 활성화 여부
	created_at timestamptz DEFAULT now() NOT NULL, -- 생성일시
	created_by int8 NULL, -- 생성자 user_id (논리적 참조)
	updated_at timestamptz DEFAULT now() NOT NULL, -- 수정일시
	updated_by int8 NULL, -- 수정자 user_id (논리적 참조)
	CONSTRAINT app_code_groups_pkey PRIMARY KEY (app_code_group_id),
	CONSTRAINT uk_app_code_groups_group_key UNIQUE (group_key)
);
CREATE INDEX ix_app_code_groups_active ON dwp_aura.app_code_groups USING btree (is_active);
COMMENT ON TABLE dwp_aura.app_code_groups IS 'SynapseX 앱 전용 코드 그룹 마스터';

-- Column comments

COMMENT ON COLUMN dwp_aura.app_code_groups.group_key IS '그룹 키 (예: SECURITY_ACCESS_MODEL, PII_HANDLING)';
COMMENT ON COLUMN dwp_aura.app_code_groups.group_name IS '그룹 표시명';
COMMENT ON COLUMN dwp_aura.app_code_groups.description IS '그룹 설명';
COMMENT ON COLUMN dwp_aura.app_code_groups.is_active IS '활성화 여부';
COMMENT ON COLUMN dwp_aura.app_code_groups.created_at IS '생성일시';
COMMENT ON COLUMN dwp_aura.app_code_groups.created_by IS '생성자 user_id (논리적 참조)';
COMMENT ON COLUMN dwp_aura.app_code_groups.updated_at IS '수정일시';
COMMENT ON COLUMN dwp_aura.app_code_groups.updated_by IS '수정자 user_id (논리적 참조)';

-- Permissions

ALTER TABLE dwp_aura.app_code_groups OWNER TO dwp_user;
GRANT ALL ON TABLE dwp_aura.app_code_groups TO dwp_user;


-- dwp_aura.app_codes definition

-- Drop table

-- DROP TABLE dwp_aura.app_codes;

CREATE TABLE dwp_aura.app_codes (
	app_code_id bigserial NOT NULL,
	group_key varchar(100) NOT NULL, -- 그룹 키 (논리적 참조: app_code_groups.group_key)
	code varchar(100) NOT NULL, -- 코드 값 (대문자 스네이크, 예: RBAC, ENFORCED)
	"name" varchar(200) NOT NULL, -- 기본 라벨 (UI 표시, i18n 분리 가능)
	description varchar(500) NULL,
	sort_order int4 DEFAULT 0 NOT NULL, -- 정렬 순서
	is_active bool DEFAULT true NOT NULL, -- 활성화 여부
	created_at timestamptz DEFAULT now() NOT NULL, -- 생성일시
	created_by int8 NULL, -- 생성자 user_id (논리적 참조)
	updated_at timestamptz DEFAULT now() NOT NULL, -- 수정일시
	updated_by int8 NULL, -- 수정자 user_id (논리적 참조)
	CONSTRAINT app_codes_pkey PRIMARY KEY (app_code_id),
	CONSTRAINT uk_app_codes_group_code UNIQUE (group_key, code)
);
CREATE INDEX ix_app_codes_group_active ON dwp_aura.app_codes USING btree (group_key, is_active);
CREATE INDEX ix_app_codes_group_key ON dwp_aura.app_codes USING btree (group_key);
COMMENT ON TABLE dwp_aura.app_codes IS 'SynapseX 앱 전용 코드 마스터 (UI 라벨/선택지)';

-- Column comments

COMMENT ON COLUMN dwp_aura.app_codes.group_key IS '그룹 키 (논리적 참조: app_code_groups.group_key)';
COMMENT ON COLUMN dwp_aura.app_codes.code IS '코드 값 (대문자 스네이크, 예: RBAC, ENFORCED)';
COMMENT ON COLUMN dwp_aura.app_codes."name" IS '기본 라벨 (UI 표시, i18n 분리 가능)';
COMMENT ON COLUMN dwp_aura.app_codes.sort_order IS '정렬 순서';
COMMENT ON COLUMN dwp_aura.app_codes.is_active IS '활성화 여부';
COMMENT ON COLUMN dwp_aura.app_codes.created_at IS '생성일시';
COMMENT ON COLUMN dwp_aura.app_codes.created_by IS '생성자 user_id (논리적 참조)';
COMMENT ON COLUMN dwp_aura.app_codes.updated_at IS '수정일시';
COMMENT ON COLUMN dwp_aura.app_codes.updated_by IS '수정자 user_id (논리적 참조)';

-- Permissions

ALTER TABLE dwp_aura.app_codes OWNER TO dwp_user;
GRANT ALL ON TABLE dwp_aura.app_codes TO dwp_user;


-- dwp_aura.audit_event_log definition

-- Drop table

-- DROP TABLE dwp_aura.audit_event_log;

CREATE TABLE dwp_aura.audit_event_log (
	audit_id bigserial NOT NULL, -- 감사 로그 식별자 (PK)
	tenant_id int8 NOT NULL, -- 테넌트 식별자
	event_category text NOT NULL, -- 이벤트 카테고리 (CASE, ACTION, DETECT_RUN 등)
	event_type text NOT NULL, -- 이벤트 유형
	resource_type text NULL, -- 대상 리소스 유형
	resource_id text NULL, -- 대상 리소스 ID
	created_at timestamptz DEFAULT now() NOT NULL, -- 발생 일시
	actor_type text NULL, -- 행위자 유형 (USER, AGENT, SYSTEM)
	actor_user_id int8 NULL, -- 행위자 user_id
	actor_agent_id text NULL, -- 행위자 agent_id
	actor_display_name text NULL, -- 행위자 표시명
	channel text NULL, -- 채널 (API, AGENT, BATCH 등)
	ip_address text NULL, -- 요청 IP
	user_agent text NULL, -- User-Agent
	outcome text NULL, -- 결과 (SUCCESS, FAILED)
	severity text DEFAULT 'INFO'::text NOT NULL, -- 심각도 (INFO, WARN, ERROR)
	before_json jsonb NULL, -- 변경 전 (JSONB)
	after_json jsonb NULL, -- 변경 후 (JSONB)
	diff_json jsonb NULL, -- 변경 diff (JSONB)
	evidence_json jsonb NULL, -- 증거 (JSONB)
	tags jsonb NULL, -- 태그 (JSONB)
	gateway_request_id text NULL, -- 게이트웨이 요청 ID
	trace_id text NULL, -- 추적 ID
	span_id text NULL, -- Span ID
	CONSTRAINT audit_event_log_pkey PRIMARY KEY (audit_id)
);
CREATE INDEX ix_audit_event_log_actor ON dwp_aura.audit_event_log USING btree (tenant_id, actor_type, actor_user_id, created_at DESC);
CREATE INDEX ix_audit_event_log_outcome ON dwp_aura.audit_event_log USING btree (tenant_id, outcome, created_at DESC);
CREATE INDEX ix_audit_event_log_resource ON dwp_aura.audit_event_log USING btree (tenant_id, resource_type, resource_id);
CREATE INDEX ix_audit_event_log_tenant_category_type ON dwp_aura.audit_event_log USING btree (tenant_id, event_category, event_type, created_at DESC);
CREATE INDEX ix_audit_event_log_tenant_created ON dwp_aura.audit_event_log USING btree (tenant_id, created_at DESC);
CREATE INDEX ix_audit_event_log_tenant_gateway_request_id ON dwp_aura.audit_event_log USING btree (tenant_id, gateway_request_id) WHERE (gateway_request_id IS NOT NULL);
CREATE INDEX ix_audit_event_log_tenant_resource_id ON dwp_aura.audit_event_log USING btree (tenant_id, resource_id) WHERE (resource_id IS NOT NULL);
CREATE INDEX ix_audit_event_log_tenant_span_id ON dwp_aura.audit_event_log USING btree (tenant_id, span_id) WHERE (span_id IS NOT NULL);
CREATE INDEX ix_audit_event_log_tenant_trace_id ON dwp_aura.audit_event_log USING btree (tenant_id, trace_id) WHERE (trace_id IS NOT NULL);
COMMENT ON TABLE dwp_aura.audit_event_log IS 'Synapse 도메인 감사 이벤트 SoT.';

-- Column comments

COMMENT ON COLUMN dwp_aura.audit_event_log.audit_id IS '감사 로그 식별자 (PK)';
COMMENT ON COLUMN dwp_aura.audit_event_log.tenant_id IS '테넌트 식별자';
COMMENT ON COLUMN dwp_aura.audit_event_log.event_category IS '이벤트 카테고리 (CASE, ACTION, DETECT_RUN 등)';
COMMENT ON COLUMN dwp_aura.audit_event_log.event_type IS '이벤트 유형';
COMMENT ON COLUMN dwp_aura.audit_event_log.resource_type IS '대상 리소스 유형';
COMMENT ON COLUMN dwp_aura.audit_event_log.resource_id IS '대상 리소스 ID';
COMMENT ON COLUMN dwp_aura.audit_event_log.created_at IS '발생 일시';
COMMENT ON COLUMN dwp_aura.audit_event_log.actor_type IS '행위자 유형 (USER, AGENT, SYSTEM)';
COMMENT ON COLUMN dwp_aura.audit_event_log.actor_user_id IS '행위자 user_id';
COMMENT ON COLUMN dwp_aura.audit_event_log.actor_agent_id IS '행위자 agent_id';
COMMENT ON COLUMN dwp_aura.audit_event_log.actor_display_name IS '행위자 표시명';
COMMENT ON COLUMN dwp_aura.audit_event_log.channel IS '채널 (API, AGENT, BATCH 등)';
COMMENT ON COLUMN dwp_aura.audit_event_log.ip_address IS '요청 IP';
COMMENT ON COLUMN dwp_aura.audit_event_log.user_agent IS 'User-Agent';
COMMENT ON COLUMN dwp_aura.audit_event_log.outcome IS '결과 (SUCCESS, FAILED)';
COMMENT ON COLUMN dwp_aura.audit_event_log.severity IS '심각도 (INFO, WARN, ERROR)';
COMMENT ON COLUMN dwp_aura.audit_event_log.before_json IS '변경 전 (JSONB)';
COMMENT ON COLUMN dwp_aura.audit_event_log.after_json IS '변경 후 (JSONB)';
COMMENT ON COLUMN dwp_aura.audit_event_log.diff_json IS '변경 diff (JSONB)';
COMMENT ON COLUMN dwp_aura.audit_event_log.evidence_json IS '증거 (JSONB)';
COMMENT ON COLUMN dwp_aura.audit_event_log.tags IS '태그 (JSONB)';
COMMENT ON COLUMN dwp_aura.audit_event_log.gateway_request_id IS '게이트웨이 요청 ID';
COMMENT ON COLUMN dwp_aura.audit_event_log.trace_id IS '추적 ID';
COMMENT ON COLUMN dwp_aura.audit_event_log.span_id IS 'Span ID';

-- Permissions

ALTER TABLE dwp_aura.audit_event_log OWNER TO dwp_user;
GRANT ALL ON TABLE dwp_aura.audit_event_log TO dwp_user;


-- dwp_aura.case_analysis_run definition

-- Drop table

-- DROP TABLE dwp_aura.case_analysis_run;

CREATE TABLE dwp_aura.case_analysis_run (
	run_id uuid DEFAULT gen_random_uuid() NOT NULL,
	tenant_id int8 NOT NULL,
	case_id int8 NOT NULL,
	status varchar(30) DEFAULT 'STARTED'::character varying NOT NULL, -- STARTED | RUNNING | COMPLETED | FAILED
	"mode" varchar(20) DEFAULT 'LIVE'::character varying NOT NULL, -- LIVE | SIMULATION
	requested_by varchar(20) DEFAULT 'HUMAN'::character varying NOT NULL, -- HUMAN | SYSTEM
	started_at timestamptz DEFAULT now() NOT NULL,
	finished_at timestamptz NULL,
	error_message text NULL,
	aura_trace_id varchar(100) NULL,
	created_at timestamptz DEFAULT now() NOT NULL,
	CONSTRAINT case_analysis_run_pkey PRIMARY KEY (run_id)
);
CREATE INDEX ix_case_analysis_run_status ON dwp_aura.case_analysis_run USING btree (tenant_id, status);
CREATE INDEX ix_case_analysis_run_tenant_case ON dwp_aura.case_analysis_run USING btree (tenant_id, case_id);
COMMENT ON TABLE dwp_aura.case_analysis_run IS 'Phase2: 케이스 분석 실행 단위 (Aura 연동)';

-- Column comments

COMMENT ON COLUMN dwp_aura.case_analysis_run.status IS 'STARTED | RUNNING | COMPLETED | FAILED';
COMMENT ON COLUMN dwp_aura.case_analysis_run."mode" IS 'LIVE | SIMULATION';
COMMENT ON COLUMN dwp_aura.case_analysis_run.requested_by IS 'HUMAN | SYSTEM';

-- Permissions

ALTER TABLE dwp_aura.case_analysis_run OWNER TO dwp_user;
GRANT ALL ON TABLE dwp_aura.case_analysis_run TO dwp_user;


-- dwp_aura.case_explanation definition

-- Drop table

-- DROP TABLE dwp_aura.case_explanation;

CREATE TABLE dwp_aura.case_explanation (
	explanation_id bigserial NOT NULL,
	tenant_id int8 NOT NULL,
	case_id int8 NOT NULL,
	user_id int8 NOT NULL,
	explanation_text text NOT NULL,
	evidence_attachment_id varchar(255) NULL,
	created_at timestamptz DEFAULT now() NOT NULL,
	created_by int8 NULL,
	updated_at timestamptz DEFAULT now() NOT NULL,
	updated_by int8 NULL,
	CONSTRAINT case_explanation_pkey PRIMARY KEY (explanation_id)
);
CREATE INDEX idx_case_explanation_case ON dwp_aura.case_explanation USING btree (tenant_id, case_id, user_id, created_at DESC);
CREATE INDEX idx_case_explanation_case_id ON dwp_aura.case_explanation USING btree (case_id);
COMMENT ON TABLE dwp_aura.case_explanation IS '이상 징후 전표에 대한 사용자의 소명 내역';

-- Permissions

ALTER TABLE dwp_aura.case_explanation OWNER TO dwp_user;
GRANT ALL ON TABLE dwp_aura.case_explanation TO dwp_user;


-- dwp_aura.config_profile definition

-- Drop table

-- DROP TABLE dwp_aura.config_profile;

CREATE TABLE dwp_aura.config_profile (
	profile_id bigserial NOT NULL, -- 프로파일 식별자 (PK)
	tenant_id int8 NOT NULL, -- 테넌트 식별자 (논리적 참조: com_tenants.tenant_id)
	profile_name text NOT NULL, -- 프로파일명
	description text NULL, -- 프로파일 설명
	is_default bool DEFAULT false NOT NULL, -- 테넌트 기본 프로파일 여부
	created_at timestamptz DEFAULT now() NOT NULL, -- 생성일시
	created_by int8 NULL, -- 생성자 user_id (논리적 참조: com_users.user_id)
	updated_at timestamptz DEFAULT now() NOT NULL, -- 수정일시
	updated_by int8 NULL, -- 수정자 user_id (논리적 참조: com_users.user_id)
	CONSTRAINT config_profile_pkey PRIMARY KEY (profile_id),
	CONSTRAINT config_profile_tenant_id_profile_name_key UNIQUE (tenant_id, profile_name)
);
CREATE INDEX ix_config_profile_default ON dwp_aura.config_profile USING btree (tenant_id, is_default);
CREATE INDEX ix_config_profile_tenant_id ON dwp_aura.config_profile USING btree (tenant_id);
COMMENT ON TABLE dwp_aura.config_profile IS '설정 프로파일(정책 세트). 고객사별 default/strict/pilot 등 운영 중 스위칭/AB 테스트용.';

-- Column comments

COMMENT ON COLUMN dwp_aura.config_profile.profile_id IS '프로파일 식별자 (PK)';
COMMENT ON COLUMN dwp_aura.config_profile.tenant_id IS '테넌트 식별자 (논리적 참조: com_tenants.tenant_id)';
COMMENT ON COLUMN dwp_aura.config_profile.profile_name IS '프로파일명';
COMMENT ON COLUMN dwp_aura.config_profile.description IS '프로파일 설명';
COMMENT ON COLUMN dwp_aura.config_profile.is_default IS '테넌트 기본 프로파일 여부';
COMMENT ON COLUMN dwp_aura.config_profile.created_at IS '생성일시';
COMMENT ON COLUMN dwp_aura.config_profile.created_by IS '생성자 user_id (논리적 참조: com_users.user_id)';
COMMENT ON COLUMN dwp_aura.config_profile.updated_at IS '수정일시';
COMMENT ON COLUMN dwp_aura.config_profile.updated_by IS '수정자 user_id (논리적 참조: com_users.user_id)';

-- Permissions

ALTER TABLE dwp_aura.config_profile OWNER TO dwp_user;
GRANT ALL ON TABLE dwp_aura.config_profile TO dwp_user;


-- dwp_aura.detect_run definition

-- Drop table

-- DROP TABLE dwp_aura.detect_run;

CREATE TABLE dwp_aura.detect_run (
	run_id bigserial NOT NULL, -- 실행 식별자 (PK)
	tenant_id int8 NOT NULL, -- 테넌트 식별자
	window_from timestamptz NOT NULL, -- 탐지 윈도우 시작
	window_to timestamptz NOT NULL, -- 탐지 윈도우 종료
	status varchar(20) DEFAULT 'STARTED'::character varying NOT NULL, -- STARTED | COMPLETED | FAILED
	counts_json jsonb NULL, -- {"caseCreated":N,"caseUpdated":N}
	error_message text NULL, -- 오류 메시지 (실패 시)
	started_at timestamptz DEFAULT now() NOT NULL, -- 시작 시각
	completed_at timestamptz NULL, -- 완료 시각
	created_at timestamptz DEFAULT now() NOT NULL, -- 생성일시
	created_by int8 NULL, -- 생성자 user_id (논리적 참조: com_users.user_id)
	updated_at timestamptz DEFAULT now() NOT NULL, -- 수정일시
	updated_by int8 NULL, -- 수정자 user_id (논리적 참조: com_users.user_id)
	CONSTRAINT detect_run_pkey PRIMARY KEY (run_id)
);
CREATE INDEX ix_detect_run_tenant_created ON dwp_aura.detect_run USING btree (tenant_id, started_at DESC);
CREATE INDEX ix_detect_run_tenant_status ON dwp_aura.detect_run USING btree (tenant_id, status);
COMMENT ON TABLE dwp_aura.detect_run IS 'Phase B: 탐지 배치 실행. window_from/to, case_created/updated 요약.';

-- Column comments

COMMENT ON COLUMN dwp_aura.detect_run.run_id IS '실행 식별자 (PK)';
COMMENT ON COLUMN dwp_aura.detect_run.tenant_id IS '테넌트 식별자';
COMMENT ON COLUMN dwp_aura.detect_run.window_from IS '탐지 윈도우 시작';
COMMENT ON COLUMN dwp_aura.detect_run.window_to IS '탐지 윈도우 종료';
COMMENT ON COLUMN dwp_aura.detect_run.status IS 'STARTED | COMPLETED | FAILED';
COMMENT ON COLUMN dwp_aura.detect_run.counts_json IS '{"caseCreated":N,"caseUpdated":N}';
COMMENT ON COLUMN dwp_aura.detect_run.error_message IS '오류 메시지 (실패 시)';
COMMENT ON COLUMN dwp_aura.detect_run.started_at IS '시작 시각';
COMMENT ON COLUMN dwp_aura.detect_run.completed_at IS '완료 시각';
COMMENT ON COLUMN dwp_aura.detect_run.created_at IS '생성일시';
COMMENT ON COLUMN dwp_aura.detect_run.created_by IS '생성자 user_id (논리적 참조: com_users.user_id)';
COMMENT ON COLUMN dwp_aura.detect_run.updated_at IS '수정일시';
COMMENT ON COLUMN dwp_aura.detect_run.updated_by IS '수정자 user_id (논리적 참조: com_users.user_id)';

-- Permissions

ALTER TABLE dwp_aura.detect_run OWNER TO dwp_user;
GRANT ALL ON TABLE dwp_aura.detect_run TO dwp_user;


-- dwp_aura.dictionary_term definition

-- Drop table

-- DROP TABLE dwp_aura.dictionary_term;

CREATE TABLE dwp_aura.dictionary_term (
	term_id bigserial NOT NULL, -- 용어 식별자 (PK)
	tenant_id int8 NOT NULL, -- 테넌트 식별자
	term_key varchar(120) NOT NULL, -- 용어 키
	label_ko text NULL, -- 한글 라벨
	description text NULL, -- 설명
	category varchar(50) NULL, -- 카테고리
	created_at timestamptz DEFAULT now() NOT NULL, -- 생성일시
	CONSTRAINT dictionary_term_pkey PRIMARY KEY (term_id),
	CONSTRAINT dictionary_term_tenant_id_term_key_key UNIQUE (tenant_id, term_key)
);
CREATE INDEX ix_dictionary_term_category ON dwp_aura.dictionary_term USING btree (tenant_id, category);
CREATE INDEX ix_dictionary_term_tenant ON dwp_aura.dictionary_term USING btree (tenant_id);
COMMENT ON TABLE dwp_aura.dictionary_term IS '용어 사전. term_key 기준 고유.';

-- Column comments

COMMENT ON COLUMN dwp_aura.dictionary_term.term_id IS '용어 식별자 (PK)';
COMMENT ON COLUMN dwp_aura.dictionary_term.tenant_id IS '테넌트 식별자';
COMMENT ON COLUMN dwp_aura.dictionary_term.term_key IS '용어 키';
COMMENT ON COLUMN dwp_aura.dictionary_term.label_ko IS '한글 라벨';
COMMENT ON COLUMN dwp_aura.dictionary_term.description IS '설명';
COMMENT ON COLUMN dwp_aura.dictionary_term.category IS '카테고리';
COMMENT ON COLUMN dwp_aura.dictionary_term.created_at IS '생성일시';

-- Permissions

ALTER TABLE dwp_aura.dictionary_term OWNER TO dwp_user;
GRANT ALL ON TABLE dwp_aura.dictionary_term TO dwp_user;


-- dwp_aura.flyway_schema_history definition

-- Drop table

-- DROP TABLE dwp_aura.flyway_schema_history;

CREATE TABLE dwp_aura.flyway_schema_history (
	installed_rank int4 NOT NULL,
	"version" varchar(50) NULL,
	description varchar(200) NOT NULL,
	"type" varchar(20) NOT NULL,
	script varchar(1000) NOT NULL,
	checksum int4 NULL,
	installed_by varchar(100) NOT NULL,
	installed_on timestamp DEFAULT now() NOT NULL,
	execution_time int4 NOT NULL,
	success bool NOT NULL,
	CONSTRAINT flyway_schema_history_pk PRIMARY KEY (installed_rank)
);
CREATE INDEX flyway_schema_history_s_idx ON dwp_aura.flyway_schema_history USING btree (success);

-- Permissions

ALTER TABLE dwp_aura.flyway_schema_history OWNER TO dwp_user;
GRANT ALL ON TABLE dwp_aura.flyway_schema_history TO dwp_user;


-- dwp_aura.idempotency_key definition

-- Drop table

-- DROP TABLE dwp_aura.idempotency_key;

CREATE TABLE dwp_aura.idempotency_key (
	idempotency_id bigserial NOT NULL,
	tenant_id int8 NOT NULL,
	resource_type varchar(50) NOT NULL,
	resource_id int8 NOT NULL,
	gateway_request_id varchar(100) NOT NULL,
	outcome varchar(20) NULL,
	result_snapshot jsonb NULL,
	created_at timestamptz DEFAULT now() NOT NULL,
	created_by int8 NULL, -- 생성자 user_id (논리적 참조: com_users.user_id)
	updated_at timestamptz DEFAULT now() NOT NULL, -- 수정일시
	updated_by int8 NULL, -- 수정자 user_id (논리적 참조: com_users.user_id)
	CONSTRAINT idempotency_key_pkey PRIMARY KEY (idempotency_id)
);
CREATE INDEX ix_idempotency_tenant_created ON dwp_aura.idempotency_key USING btree (tenant_id, created_at DESC);
CREATE UNIQUE INDEX ux_idempotency_tenant_resource_request ON dwp_aura.idempotency_key USING btree (tenant_id, resource_type, resource_id, gateway_request_id);
COMMENT ON TABLE dwp_aura.idempotency_key IS 'simulate/execute 멱등성. gateway_request_id 기반 중복 실행 차단.';

-- Column comments

COMMENT ON COLUMN dwp_aura.idempotency_key.created_by IS '생성자 user_id (논리적 참조: com_users.user_id)';
COMMENT ON COLUMN dwp_aura.idempotency_key.updated_at IS '수정일시';
COMMENT ON COLUMN dwp_aura.idempotency_key.updated_by IS '수정자 user_id (논리적 참조: com_users.user_id)';

-- Permissions

ALTER TABLE dwp_aura.idempotency_key OWNER TO dwp_user;
GRANT ALL ON TABLE dwp_aura.idempotency_key TO dwp_user;


-- dwp_aura.ingest_run definition

-- Drop table

-- DROP TABLE dwp_aura.ingest_run;

CREATE TABLE dwp_aura.ingest_run (
	run_id bigserial NOT NULL, -- 실행 식별자 (PK)
	tenant_id int8 NOT NULL, -- 테넌트 식별자
	batch_id text NULL, -- 배치 ID
	window_from timestamptz NULL, -- 적재 윈도우 시작
	window_to timestamptz NULL, -- 적재 윈도우 종료
	record_count int4 NULL, -- 적재 건수
	status varchar(20) DEFAULT 'STARTED'::character varying NOT NULL, -- STARTED | COMPLETED | FAILED
	error_message text NULL, -- 오류 메시지 (실패 시)
	started_at timestamptz DEFAULT now() NOT NULL, -- 시작 시각
	completed_at timestamptz NULL, -- 완료 시각
	created_at timestamptz DEFAULT now() NOT NULL, -- 생성일시
	created_by int8 NULL, -- 생성자 user_id (논리적 참조: com_users.user_id)
	updated_at timestamptz DEFAULT now() NOT NULL, -- 수정일시
	updated_by int8 NULL, -- 수정자 user_id (논리적 참조: com_users.user_id)
	CONSTRAINT ingest_run_pkey PRIMARY KEY (run_id)
);
CREATE INDEX ix_ingest_run_tenant_created ON dwp_aura.ingest_run USING btree (tenant_id, started_at DESC);
CREATE INDEX ix_ingest_run_tenant_status ON dwp_aura.ingest_run USING btree (tenant_id, status);
COMMENT ON TABLE dwp_aura.ingest_run IS '원천데이터 적재 실행 단위. window_from/to, record_count, status.';

-- Column comments

COMMENT ON COLUMN dwp_aura.ingest_run.run_id IS '실행 식별자 (PK)';
COMMENT ON COLUMN dwp_aura.ingest_run.tenant_id IS '테넌트 식별자';
COMMENT ON COLUMN dwp_aura.ingest_run.batch_id IS '배치 ID';
COMMENT ON COLUMN dwp_aura.ingest_run.window_from IS '적재 윈도우 시작';
COMMENT ON COLUMN dwp_aura.ingest_run.window_to IS '적재 윈도우 종료';
COMMENT ON COLUMN dwp_aura.ingest_run.record_count IS '적재 건수';
COMMENT ON COLUMN dwp_aura.ingest_run.status IS 'STARTED | COMPLETED | FAILED';
COMMENT ON COLUMN dwp_aura.ingest_run.error_message IS '오류 메시지 (실패 시)';
COMMENT ON COLUMN dwp_aura.ingest_run.started_at IS '시작 시각';
COMMENT ON COLUMN dwp_aura.ingest_run.completed_at IS '완료 시각';
COMMENT ON COLUMN dwp_aura.ingest_run.created_at IS '생성일시';
COMMENT ON COLUMN dwp_aura.ingest_run.created_by IS '생성자 user_id (논리적 참조: com_users.user_id)';
COMMENT ON COLUMN dwp_aura.ingest_run.updated_at IS '수정일시';
COMMENT ON COLUMN dwp_aura.ingest_run.updated_by IS '수정자 user_id (논리적 참조: com_users.user_id)';

-- Permissions

ALTER TABLE dwp_aura.ingest_run OWNER TO dwp_user;
GRANT ALL ON TABLE dwp_aura.ingest_run TO dwp_user;


-- dwp_aura.integration_outbox definition

-- Drop table

-- DROP TABLE dwp_aura.integration_outbox;

CREATE TABLE dwp_aura.integration_outbox (
	outbox_id bigserial NOT NULL, -- 아웃박스 식별자 (PK)
	tenant_id int8 NOT NULL, -- 테넌트 식별자
	target_system text NOT NULL, -- 대상 시스템 (SAP, AURA 등)
	event_type text NOT NULL, -- 이벤트 유형
	event_key text NOT NULL, -- 이벤트 키 (중복 방지)
	payload jsonb NOT NULL, -- 페이로드 (JSONB)
	status text DEFAULT 'PENDING'::text NOT NULL, -- 상태 (PENDING, SENT, FAILED)
	retry_count int4 DEFAULT 0 NOT NULL, -- 재시도 횟수
	next_retry_at timestamptz NULL, -- 다음 재시도 시각
	last_error text NULL, -- 마지막 오류
	created_at timestamptz DEFAULT now() NOT NULL, -- 생성일시
	updated_at timestamptz DEFAULT now() NOT NULL, -- 수정일시
	CONSTRAINT integration_outbox_pkey PRIMARY KEY (outbox_id)
);
CREATE INDEX ix_integration_outbox_created ON dwp_aura.integration_outbox USING btree (tenant_id, created_at DESC);
CREATE INDEX ix_integration_outbox_tenant_status ON dwp_aura.integration_outbox USING btree (tenant_id, status, next_retry_at);
CREATE INDEX ix_outbox_status ON dwp_aura.integration_outbox USING btree (tenant_id, status, next_retry_at);
CREATE UNIQUE INDEX ux_outbox_idempotent ON dwp_aura.integration_outbox USING btree (tenant_id, target_system, event_type, event_key);
COMMENT ON TABLE dwp_aura.integration_outbox IS '통합 아웃박스. 외부 시스템 전송 대기 이벤트.';

-- Column comments

COMMENT ON COLUMN dwp_aura.integration_outbox.outbox_id IS '아웃박스 식별자 (PK)';
COMMENT ON COLUMN dwp_aura.integration_outbox.tenant_id IS '테넌트 식별자';
COMMENT ON COLUMN dwp_aura.integration_outbox.target_system IS '대상 시스템 (SAP, AURA 등)';
COMMENT ON COLUMN dwp_aura.integration_outbox.event_type IS '이벤트 유형';
COMMENT ON COLUMN dwp_aura.integration_outbox.event_key IS '이벤트 키 (중복 방지)';
COMMENT ON COLUMN dwp_aura.integration_outbox.payload IS '페이로드 (JSONB)';
COMMENT ON COLUMN dwp_aura.integration_outbox.status IS '상태 (PENDING, SENT, FAILED)';
COMMENT ON COLUMN dwp_aura.integration_outbox.retry_count IS '재시도 횟수';
COMMENT ON COLUMN dwp_aura.integration_outbox.next_retry_at IS '다음 재시도 시각';
COMMENT ON COLUMN dwp_aura.integration_outbox.last_error IS '마지막 오류';
COMMENT ON COLUMN dwp_aura.integration_outbox.created_at IS '생성일시';
COMMENT ON COLUMN dwp_aura.integration_outbox.updated_at IS '수정일시';

-- Permissions

ALTER TABLE dwp_aura.integration_outbox OWNER TO dwp_user;
GRANT ALL ON TABLE dwp_aura.integration_outbox TO dwp_user;


-- dwp_aura.mcc_master definition

-- Drop table

-- DROP TABLE dwp_aura.mcc_master;

CREATE TABLE dwp_aura.mcc_master (
	mcc_code varchar(4) NOT NULL,
	mcc_name varchar(100) NOT NULL,
	risk_category varchar(20) NOT NULL, -- PROHIBITED: 무조건 탐지, CAUTION: 패턴 분석 필요, ALLOWED: 화이트리스트
	related_article varchar(100) NULL,
	created_at timestamptz DEFAULT now() NOT NULL,
	created_by int8 NULL,
	updated_at timestamptz DEFAULT now() NOT NULL,
	updated_by int8 NULL,
	mcc_id bigserial NOT NULL,
	tenant_id int8 NOT NULL,
	is_weekend_allowed bpchar(1) DEFAULT 'N'::bpchar NULL,
	limit_amount_per_use numeric(18, 2) NULL,
	CONSTRAINT mcc_master_pkey PRIMARY KEY (mcc_id),
	CONSTRAINT uq_mcc_tenant UNIQUE (tenant_id, mcc_code)
);
COMMENT ON TABLE dwp_aura.mcc_master IS '테넌트별 MCC 사용 규정 마스터';

-- Column comments

COMMENT ON COLUMN dwp_aura.mcc_master.risk_category IS 'PROHIBITED: 무조건 탐지, CAUTION: 패턴 분석 필요, ALLOWED: 화이트리스트';

-- Permissions

ALTER TABLE dwp_aura.mcc_master OWNER TO dwp_user;
GRANT ALL ON TABLE dwp_aura.mcc_master TO dwp_user;


-- dwp_aura.mcp_shadow_run_meta definition

-- Drop table

-- DROP TABLE dwp_aura.mcp_shadow_run_meta;

CREATE TABLE dwp_aura.mcp_shadow_run_meta (
	id bigserial NOT NULL,
	tenant_id int8 NOT NULL,
	run_id uuid NOT NULL,
	case_id int8 NULL,
	requested_agent_mode varchar(30) NULL,
	resolved_agent_mode varchar(30) NOT NULL,
	trace_id varchar(120) NULL,
	requested_model_version varchar(120) NULL,
	resolved_model_version varchar(120) NOT NULL,
	requested_policy_version varchar(120) NULL,
	resolved_policy_version varchar(120) NOT NULL,
	created_at timestamptz DEFAULT now() NOT NULL,
	CONSTRAINT mcp_shadow_run_meta_pkey PRIMARY KEY (id)
);
CREATE INDEX ix_mcp_shadow_meta_run ON dwp_aura.mcp_shadow_run_meta USING btree (run_id);
CREATE INDEX ix_mcp_shadow_meta_tenant_created ON dwp_aura.mcp_shadow_run_meta USING btree (tenant_id, created_at DESC);

-- Permissions

ALTER TABLE dwp_aura.mcp_shadow_run_meta OWNER TO dwp_user;
GRANT ALL ON TABLE dwp_aura.mcp_shadow_run_meta TO dwp_user;


-- dwp_aura.md_company_code definition

-- Drop table

-- DROP TABLE dwp_aura.md_company_code;

CREATE TABLE dwp_aura.md_company_code (
	tenant_id int8 NOT NULL, -- 테넌트 식별자
	bukrs varchar(4) NOT NULL, -- 회사코드
	bukrs_name text NOT NULL, -- 회사명
	country varchar(3) NULL, -- 국가코드
	default_currency varchar(5) NULL, -- 기본 통화
	is_active bool DEFAULT true NOT NULL, -- 활성 여부
	source_system text DEFAULT 'SAP'::text NOT NULL, -- 원천 시스템
	last_sync_ts timestamptz NULL, -- 마지막 동기화 시각
	created_at timestamptz DEFAULT now() NOT NULL, -- 생성일시
	updated_at timestamptz DEFAULT now() NOT NULL, -- 수정일시
	CONSTRAINT md_company_code_pkey PRIMARY KEY (tenant_id, bukrs)
);
CREATE INDEX ix_md_company_code_tenant_active ON dwp_aura.md_company_code USING btree (tenant_id, is_active);
CREATE INDEX ix_md_company_code_tenant_name ON dwp_aura.md_company_code USING btree (tenant_id, bukrs_name);
COMMENT ON TABLE dwp_aura.md_company_code IS '회사코드(BUKRS) 마스터. 표시명 등 SoT.';

-- Column comments

COMMENT ON COLUMN dwp_aura.md_company_code.tenant_id IS '테넌트 식별자';
COMMENT ON COLUMN dwp_aura.md_company_code.bukrs IS '회사코드';
COMMENT ON COLUMN dwp_aura.md_company_code.bukrs_name IS '회사명';
COMMENT ON COLUMN dwp_aura.md_company_code.country IS '국가코드';
COMMENT ON COLUMN dwp_aura.md_company_code.default_currency IS '기본 통화';
COMMENT ON COLUMN dwp_aura.md_company_code.is_active IS '활성 여부';
COMMENT ON COLUMN dwp_aura.md_company_code.source_system IS '원천 시스템';
COMMENT ON COLUMN dwp_aura.md_company_code.last_sync_ts IS '마지막 동기화 시각';
COMMENT ON COLUMN dwp_aura.md_company_code.created_at IS '생성일시';
COMMENT ON COLUMN dwp_aura.md_company_code.updated_at IS '수정일시';

-- Permissions

ALTER TABLE dwp_aura.md_company_code OWNER TO dwp_user;
GRANT ALL ON TABLE dwp_aura.md_company_code TO dwp_user;


-- dwp_aura.md_currency definition

-- Drop table

-- DROP TABLE dwp_aura.md_currency;

CREATE TABLE dwp_aura.md_currency (
	currency_code varchar(5) NOT NULL, -- 통화 코드 (PK)
	currency_name text NOT NULL, -- 통화명
	symbol text NULL, -- 통화 기호
	minor_unit int4 NULL, -- 소수 단위
	is_active bool DEFAULT true NOT NULL, -- 활성 여부
	created_at timestamptz DEFAULT now() NOT NULL, -- 생성일시
	updated_at timestamptz DEFAULT now() NOT NULL, -- 수정일시
	CONSTRAINT md_currency_pkey PRIMARY KEY (currency_code)
);
COMMENT ON TABLE dwp_aura.md_currency IS '통화 마스터(전역).';

-- Column comments

COMMENT ON COLUMN dwp_aura.md_currency.currency_code IS '통화 코드 (PK)';
COMMENT ON COLUMN dwp_aura.md_currency.currency_name IS '통화명';
COMMENT ON COLUMN dwp_aura.md_currency.symbol IS '통화 기호';
COMMENT ON COLUMN dwp_aura.md_currency.minor_unit IS '소수 단위';
COMMENT ON COLUMN dwp_aura.md_currency.is_active IS '활성 여부';
COMMENT ON COLUMN dwp_aura.md_currency.created_at IS '생성일시';
COMMENT ON COLUMN dwp_aura.md_currency.updated_at IS '수정일시';

-- Permissions

ALTER TABLE dwp_aura.md_currency OWNER TO dwp_user;
GRANT ALL ON TABLE dwp_aura.md_currency TO dwp_user;


-- dwp_aura.policy_doc_metadata definition

-- Drop table

-- DROP TABLE dwp_aura.policy_doc_metadata;

CREATE TABLE dwp_aura.policy_doc_metadata (
	doc_id bigserial NOT NULL, -- 문서 식별자 (PK)
	tenant_id int8 NOT NULL, -- 테넌트 식별자
	policy_id text NOT NULL, -- 정책 ID
	category text NOT NULL, -- 카테고리
	effective_date date NULL, -- 시행일
	priority int4 DEFAULT 100 NOT NULL, -- 우선순위
	title text NULL, -- 제목
	content_hash text NULL, -- 내용 해시
	source_uri text NULL, -- 원본 URI
	created_at timestamptz DEFAULT now() NOT NULL, -- 생성일시
	updated_at timestamptz DEFAULT now() NOT NULL, -- 수정일시
	CONSTRAINT policy_doc_metadata_pkey PRIMARY KEY (doc_id),
	CONSTRAINT policy_doc_metadata_tenant_id_policy_id_key UNIQUE (tenant_id, policy_id)
);
COMMENT ON TABLE dwp_aura.policy_doc_metadata IS '정책 문서 메타데이터. RAG/정책 문서 참조.';

-- Column comments

COMMENT ON COLUMN dwp_aura.policy_doc_metadata.doc_id IS '문서 식별자 (PK)';
COMMENT ON COLUMN dwp_aura.policy_doc_metadata.tenant_id IS '테넌트 식별자';
COMMENT ON COLUMN dwp_aura.policy_doc_metadata.policy_id IS '정책 ID';
COMMENT ON COLUMN dwp_aura.policy_doc_metadata.category IS '카테고리';
COMMENT ON COLUMN dwp_aura.policy_doc_metadata.effective_date IS '시행일';
COMMENT ON COLUMN dwp_aura.policy_doc_metadata.priority IS '우선순위';
COMMENT ON COLUMN dwp_aura.policy_doc_metadata.title IS '제목';
COMMENT ON COLUMN dwp_aura.policy_doc_metadata.content_hash IS '내용 해시';
COMMENT ON COLUMN dwp_aura.policy_doc_metadata.source_uri IS '원본 URI';
COMMENT ON COLUMN dwp_aura.policy_doc_metadata.created_at IS '생성일시';
COMMENT ON COLUMN dwp_aura.policy_doc_metadata.updated_at IS '수정일시';

-- Permissions

ALTER TABLE dwp_aura.policy_doc_metadata OWNER TO dwp_user;
GRANT ALL ON TABLE dwp_aura.policy_doc_metadata TO dwp_user;


-- dwp_aura.policy_guardrail definition

-- Drop table

-- DROP TABLE dwp_aura.policy_guardrail;

CREATE TABLE dwp_aura.policy_guardrail (
	guardrail_id bigserial NOT NULL, -- 가드레일 식별자 (PK)
	tenant_id int8 NOT NULL, -- 테넌트 식별자
	"name" varchar(120) NOT NULL, -- 가드레일명
	"scope" varchar(50) NOT NULL, -- 적용 범위 (case_type, action_type 등)
	rule_json jsonb DEFAULT '{}'::jsonb NOT NULL, -- 규칙 (JSONB)
	is_enabled bool DEFAULT true NOT NULL, -- 활성 여부
	created_at timestamptz DEFAULT now() NOT NULL, -- 생성일시
	updated_at timestamptz DEFAULT now() NOT NULL, -- 수정일시
	CONSTRAINT policy_guardrail_pkey PRIMARY KEY (guardrail_id)
);
CREATE INDEX ix_policy_guardrail_scope ON dwp_aura.policy_guardrail USING btree (tenant_id, scope, is_enabled);
CREATE INDEX ix_policy_guardrail_tenant ON dwp_aura.policy_guardrail USING btree (tenant_id);
COMMENT ON TABLE dwp_aura.policy_guardrail IS '가드레일 규칙. scope: case_type|action_type 등. rule_json에 조건/허용액/승인레벨 등.';

-- Column comments

COMMENT ON COLUMN dwp_aura.policy_guardrail.guardrail_id IS '가드레일 식별자 (PK)';
COMMENT ON COLUMN dwp_aura.policy_guardrail.tenant_id IS '테넌트 식별자';
COMMENT ON COLUMN dwp_aura.policy_guardrail."name" IS '가드레일명';
COMMENT ON COLUMN dwp_aura.policy_guardrail."scope" IS '적용 범위 (case_type, action_type 등)';
COMMENT ON COLUMN dwp_aura.policy_guardrail.rule_json IS '규칙 (JSONB)';
COMMENT ON COLUMN dwp_aura.policy_guardrail.is_enabled IS '활성 여부';
COMMENT ON COLUMN dwp_aura.policy_guardrail.created_at IS '생성일시';
COMMENT ON COLUMN dwp_aura.policy_guardrail.updated_at IS '수정일시';

-- Permissions

ALTER TABLE dwp_aura.policy_guardrail OWNER TO dwp_user;
GRANT ALL ON TABLE dwp_aura.policy_guardrail TO dwp_user;


-- dwp_aura.rag_document definition

-- Drop table

-- DROP TABLE dwp_aura.rag_document;

CREATE TABLE dwp_aura.rag_document (
	doc_id bigserial NOT NULL, -- 문서 식별자 (PK)
	tenant_id int8 NOT NULL, -- 테넌트 식별자
	title text NOT NULL, -- 문서 제목
	status varchar(20) DEFAULT 'PENDING'::character varying NOT NULL, -- 처리 상태: RAG_PROC_STATUS(READY, PROCESSING, COMPLETED, FAILED). 레거시 PENDING 허용.
	doc_type varchar(30) NULL, -- 문서 유형: DOC_TYPE. HIERARCHICAL|SEQUENTIAL|POLICY|GENERAL 및 호환용 REGULATION|MANUAL.
	source_type varchar(50) DEFAULT 'UPLOAD'::character varying NOT NULL, -- 소스 유형 (UPLOAD, S3, URL)
	effective_from date NULL, -- 해당 버전 효력 시작일시
	effective_to date NULL, -- 해당 버전 효력 종료일시 (NULL이면 현재 유효)
	lifecycle_status varchar(20) DEFAULT 'ACTIVE'::character varying NOT NULL, -- 문서 생명주기 상태 (ACTIVE, INACTIVE, DEPRECATED 등)
	active_from timestamptz NULL, -- 시스템에서 실제 활성화된 시작일시
	active_to timestamptz NULL, -- 시스템에서 실제 비활성화된 종료일시
	file_path text NULL, -- 로컬 절대 경로. source_type=UPLOAD 시 사용, Aura document_path로 전달.
	s3_key text NULL, -- S3 객체 키
	url text NULL, -- URL
	created_at timestamptz DEFAULT now() NOT NULL, -- 생성일시
	updated_at timestamptz DEFAULT now() NOT NULL, -- 마지막 수정 시각 (감사/동기화 기준 시각)
	checksum varchar(64) NULL, -- 체크섬
	"version" varchar(64) NULL, -- 문서/규정 버전 식별자 (예: v2.0)
	quality_gate_passed bool DEFAULT false NOT NULL, -- 품질 게이트 통과 여부 (true/false)
	last_quality_score numeric(5, 4) NULL, -- 최근 품질 평가 점수(요약 수치)
	last_quality_report_json jsonb NULL, -- 최근 품질 리포트 원문(JSON)
	CONSTRAINT chk_rag_document_new_doc_type CHECK (((doc_type IS NULL) OR ((doc_type)::text = ANY ((ARRAY['HIERARCHICAL'::character varying, 'SEQUENTIAL'::character varying, 'POLICY'::character varying, 'GENERAL'::character varying, 'REGULATION'::character varying, 'MANUAL'::character varying])::text[])))),
	CONSTRAINT chk_rag_document_new_lifecycle_status CHECK (((lifecycle_status)::text = ANY ((ARRAY['ACTIVE'::character varying, 'INACTIVE'::character varying, 'DEPRECATED'::character varying])::text[]))),
	CONSTRAINT chk_rag_document_new_status CHECK (((status)::text = ANY ((ARRAY['READY'::character varying, 'PROCESSING'::character varying, 'COMPLETED'::character varying, 'FAILED'::character varying, 'PENDING'::character varying])::text[]))),
	CONSTRAINT rag_document_new_pkey PRIMARY KEY (doc_id)
);
CREATE INDEX ix_rag_document_status ON dwp_aura.rag_document USING btree (tenant_id, status);
CREATE INDEX ix_rag_document_tenant ON dwp_aura.rag_document USING btree (tenant_id);
COMMENT ON TABLE dwp_aura.rag_document IS 'RAG 문서 메타데이터. UPLOAD|S3|URL 등 소스.';

-- Column comments

COMMENT ON COLUMN dwp_aura.rag_document.doc_id IS '문서 식별자 (PK)';
COMMENT ON COLUMN dwp_aura.rag_document.tenant_id IS '테넌트 식별자';
COMMENT ON COLUMN dwp_aura.rag_document.title IS '문서 제목';
COMMENT ON COLUMN dwp_aura.rag_document.status IS '처리 상태: RAG_PROC_STATUS(READY, PROCESSING, COMPLETED, FAILED). 레거시 PENDING 허용.';
COMMENT ON COLUMN dwp_aura.rag_document.doc_type IS '문서 유형: DOC_TYPE. HIERARCHICAL|SEQUENTIAL|POLICY|GENERAL 및 호환용 REGULATION|MANUAL.';
COMMENT ON COLUMN dwp_aura.rag_document.source_type IS '소스 유형 (UPLOAD, S3, URL)';
COMMENT ON COLUMN dwp_aura.rag_document.effective_from IS '해당 버전 효력 시작일시';
COMMENT ON COLUMN dwp_aura.rag_document.effective_to IS '해당 버전 효력 종료일시 (NULL이면 현재 유효)';
COMMENT ON COLUMN dwp_aura.rag_document.lifecycle_status IS '문서 생명주기 상태 (ACTIVE, INACTIVE, DEPRECATED 등)';
COMMENT ON COLUMN dwp_aura.rag_document.active_from IS '시스템에서 실제 활성화된 시작일시';
COMMENT ON COLUMN dwp_aura.rag_document.active_to IS '시스템에서 실제 비활성화된 종료일시';
COMMENT ON COLUMN dwp_aura.rag_document.file_path IS '로컬 절대 경로. source_type=UPLOAD 시 사용, Aura document_path로 전달.';
COMMENT ON COLUMN dwp_aura.rag_document.s3_key IS 'S3 객체 키';
COMMENT ON COLUMN dwp_aura.rag_document.url IS 'URL';
COMMENT ON COLUMN dwp_aura.rag_document.created_at IS '생성일시';
COMMENT ON COLUMN dwp_aura.rag_document.updated_at IS '마지막 수정 시각 (감사/동기화 기준 시각)';
COMMENT ON COLUMN dwp_aura.rag_document.checksum IS '체크섬';
COMMENT ON COLUMN dwp_aura.rag_document."version" IS '문서/규정 버전 식별자 (예: v2.0)';
COMMENT ON COLUMN dwp_aura.rag_document.quality_gate_passed IS '품질 게이트 통과 여부 (true/false)';
COMMENT ON COLUMN dwp_aura.rag_document.last_quality_score IS '최근 품질 평가 점수(요약 수치)';
COMMENT ON COLUMN dwp_aura.rag_document.last_quality_report_json IS '최근 품질 리포트 원문(JSON)';

-- Permissions

ALTER TABLE dwp_aura.rag_document OWNER TO dwp_user;
GRANT ALL ON TABLE dwp_aura.rag_document TO dwp_user;


-- dwp_aura.rag_eval_run definition

-- Drop table

-- DROP TABLE dwp_aura.rag_eval_run;

CREATE TABLE dwp_aura.rag_eval_run (
	id bigserial NOT NULL,
	tenant_id int8 NOT NULL,
	run_key varchar(128) NOT NULL,
	zero_rate numeric(5, 4) NOT NULL,
	hit_at_k numeric(5, 4) NOT NULL,
	strict_hit_top1 numeric(5, 4) NOT NULL,
	total_cases int4 NOT NULL,
	result_json jsonb NOT NULL,
	gate_passed bool NOT NULL,
	created_at timestamptz DEFAULT now() NOT NULL,
	CONSTRAINT rag_eval_run_pkey PRIMARY KEY (id)
);
CREATE INDEX ix_rag_eval_run_tenant_created ON dwp_aura.rag_eval_run USING btree (tenant_id, created_at DESC);

-- Permissions

ALTER TABLE dwp_aura.rag_eval_run OWNER TO dwp_user;
GRANT ALL ON TABLE dwp_aura.rag_eval_run TO dwp_user;


-- dwp_aura.recon_run definition

-- Drop table

-- DROP TABLE dwp_aura.recon_run;

CREATE TABLE dwp_aura.recon_run (
	run_id bigserial NOT NULL, -- 실행 식별자 (PK)
	tenant_id int8 NOT NULL, -- 테넌트 식별자
	run_type varchar(50) NOT NULL, -- 실행 유형 (DOC_OPENITEM_MATCH, ACTION_EFFECT 등)
	started_at timestamptz DEFAULT now() NOT NULL, -- 시작 시각
	ended_at timestamptz NULL, -- 종료 시각
	status varchar(20) DEFAULT 'RUNNING'::character varying NOT NULL, -- RUNNING | COMPLETED | FAILED
	summary_json jsonb NULL, -- 요약 (JSONB)
	CONSTRAINT recon_run_pkey PRIMARY KEY (run_id)
);
CREATE INDEX ix_recon_run_started ON dwp_aura.recon_run USING btree (tenant_id, started_at DESC);
CREATE INDEX ix_recon_run_tenant ON dwp_aura.recon_run USING btree (tenant_id);
COMMENT ON TABLE dwp_aura.recon_run IS 'Reconciliation 실행. run_type: DOC_OPENITEM_MATCH, ACTION_EFFECT, etc.';

-- Column comments

COMMENT ON COLUMN dwp_aura.recon_run.run_id IS '실행 식별자 (PK)';
COMMENT ON COLUMN dwp_aura.recon_run.tenant_id IS '테넌트 식별자';
COMMENT ON COLUMN dwp_aura.recon_run.run_type IS '실행 유형 (DOC_OPENITEM_MATCH, ACTION_EFFECT 등)';
COMMENT ON COLUMN dwp_aura.recon_run.started_at IS '시작 시각';
COMMENT ON COLUMN dwp_aura.recon_run.ended_at IS '종료 시각';
COMMENT ON COLUMN dwp_aura.recon_run.status IS 'RUNNING | COMPLETED | FAILED';
COMMENT ON COLUMN dwp_aura.recon_run.summary_json IS '요약 (JSONB)';

-- Permissions

ALTER TABLE dwp_aura.recon_run OWNER TO dwp_user;
GRANT ALL ON TABLE dwp_aura.recon_run TO dwp_user;


-- dwp_aura.sap_raw_events definition

-- Drop table

-- DROP TABLE dwp_aura.sap_raw_events;

CREATE TABLE dwp_aura.sap_raw_events (
	id bigserial NOT NULL, -- Raw 이벤트 식별자 (PK)
	tenant_id int8 NOT NULL, -- 테넌트 식별자 (논리적 참조: com_tenants.tenant_id)
	source_system text NOT NULL, -- 원천 시스템 (SAP_ECC, S4HANA 등)
	interface_name text NOT NULL, -- 인터페이스명 (예: FI_DOCUMENT, FI_OPEN_ITEM)
	extract_date date NOT NULL, -- 추출 일자
	payload_format text NOT NULL, -- 페이로드 형식 (JSON, XML 등)
	s3_object_key text NULL, -- S3 객체 키 (선택)
	payload_json jsonb NULL, -- 원본 페이로드 (JSONB)
	checksum text NULL, -- 중복 방지 체크섬
	status text DEFAULT 'RECEIVED'::text NOT NULL, -- 상태 (RECEIVED, PROCESSED, FAILED)
	error_message text NULL, -- 오류 메시지 (실패 시)
	created_at timestamptz DEFAULT now() NOT NULL, -- 수신 일시
	CONSTRAINT sap_raw_events_pkey PRIMARY KEY (id)
);
CREATE INDEX ix_sap_raw_events_extract_date ON dwp_aura.sap_raw_events USING btree (tenant_id, interface_name, extract_date);
CREATE UNIQUE INDEX ux_sap_raw_events_idempotent ON dwp_aura.sap_raw_events USING btree (tenant_id, source_system, interface_name, extract_date, checksum);
COMMENT ON TABLE dwp_aura.sap_raw_events IS 'SAP 원천 Raw 이벤트. 재처리/감사용. 적재 파이프라인 입력.';

-- Column comments

COMMENT ON COLUMN dwp_aura.sap_raw_events.id IS 'Raw 이벤트 식별자 (PK)';
COMMENT ON COLUMN dwp_aura.sap_raw_events.tenant_id IS '테넌트 식별자 (논리적 참조: com_tenants.tenant_id)';
COMMENT ON COLUMN dwp_aura.sap_raw_events.source_system IS '원천 시스템 (SAP_ECC, S4HANA 등)';
COMMENT ON COLUMN dwp_aura.sap_raw_events.interface_name IS '인터페이스명 (예: FI_DOCUMENT, FI_OPEN_ITEM)';
COMMENT ON COLUMN dwp_aura.sap_raw_events.extract_date IS '추출 일자';
COMMENT ON COLUMN dwp_aura.sap_raw_events.payload_format IS '페이로드 형식 (JSON, XML 등)';
COMMENT ON COLUMN dwp_aura.sap_raw_events.s3_object_key IS 'S3 객체 키 (선택)';
COMMENT ON COLUMN dwp_aura.sap_raw_events.payload_json IS '원본 페이로드 (JSONB)';
COMMENT ON COLUMN dwp_aura.sap_raw_events.checksum IS '중복 방지 체크섬';
COMMENT ON COLUMN dwp_aura.sap_raw_events.status IS '상태 (RECEIVED, PROCESSED, FAILED)';
COMMENT ON COLUMN dwp_aura.sap_raw_events.error_message IS '오류 메시지 (실패 시)';
COMMENT ON COLUMN dwp_aura.sap_raw_events.created_at IS '수신 일시';

-- Permissions

ALTER TABLE dwp_aura.sap_raw_events OWNER TO dwp_user;
GRANT ALL ON TABLE dwp_aura.sap_raw_events TO dwp_user;


-- dwp_aura.sys_notifications definition

-- Drop table

-- DROP TABLE dwp_aura.sys_notifications;

CREATE TABLE dwp_aura.sys_notifications (
	id bigserial NOT NULL,
	tenant_id int8 NOT NULL,
	user_id int8 NULL,
	title varchar(255) NOT NULL,
	"content" text NULL,
	"type" varchar(64) NOT NULL, -- 알림 유형: CASE_ACTION, RAG_STATUS 등
	channel varchar(128) NOT NULL, -- 발생 소스 Redis 채널
	occurred_at timestamptz DEFAULT (now() AT TIME ZONE 'UTC'::text) NOT NULL,
	created_at timestamptz DEFAULT (now() AT TIME ZONE 'UTC'::text) NOT NULL,
	read_at timestamptz NULL,
	payload_json jsonb NULL,
	CONSTRAINT ch_sys_notifications_channel CHECK ((char_length((channel)::text) > 0)),
	CONSTRAINT ch_sys_notifications_type CHECK ((char_length((type)::text) > 0)),
	CONSTRAINT sys_notifications_pkey PRIMARY KEY (id)
);
CREATE INDEX idx_sys_notifications_tenant_created ON dwp_aura.sys_notifications USING btree (tenant_id, created_at DESC);
CREATE INDEX idx_sys_notifications_tenant_user_unread ON dwp_aura.sys_notifications USING btree (tenant_id, user_id) WHERE (read_at IS NULL);
COMMENT ON TABLE dwp_aura.sys_notifications IS '알림 센터: Redis 실시간 이벤트 브로드캐스트 후 저장 (나중에 조회용)';

-- Column comments

COMMENT ON COLUMN dwp_aura.sys_notifications."type" IS '알림 유형: CASE_ACTION, RAG_STATUS 등';
COMMENT ON COLUMN dwp_aura.sys_notifications.channel IS '발생 소스 Redis 채널';

-- Permissions

ALTER TABLE dwp_aura.sys_notifications OWNER TO dwp_user;
GRANT ALL ON TABLE dwp_aura.sys_notifications TO dwp_user;


-- dwp_aura.tenant_company_code_scope definition

-- Drop table

-- DROP TABLE dwp_aura.tenant_company_code_scope;

CREATE TABLE dwp_aura.tenant_company_code_scope (
	tenant_id int8 NOT NULL, -- 테넌트 식별자
	bukrs varchar(4) NOT NULL, -- 회사코드
	is_enabled bool DEFAULT true NOT NULL, -- 활성 여부
	"source" varchar(16) DEFAULT 'MANUAL'::character varying NOT NULL, -- MANUAL | SAP | SEED
	created_at timestamptz DEFAULT now() NOT NULL, -- 생성일시
	updated_at timestamptz DEFAULT now() NOT NULL, -- 수정일시
	CONSTRAINT tenant_company_code_scope_pkey PRIMARY KEY (tenant_id, bukrs)
);
CREATE INDEX ix_tenant_company_code_scope_tenant_enabled ON dwp_aura.tenant_company_code_scope USING btree (tenant_id, is_enabled);
COMMENT ON TABLE dwp_aura.tenant_company_code_scope IS 'Tenant별 회사코드(BUKRS) 스코프. on/off 토글.';

-- Column comments

COMMENT ON COLUMN dwp_aura.tenant_company_code_scope.tenant_id IS '테넌트 식별자';
COMMENT ON COLUMN dwp_aura.tenant_company_code_scope.bukrs IS '회사코드';
COMMENT ON COLUMN dwp_aura.tenant_company_code_scope.is_enabled IS '활성 여부';
COMMENT ON COLUMN dwp_aura.tenant_company_code_scope."source" IS 'MANUAL | SAP | SEED';
COMMENT ON COLUMN dwp_aura.tenant_company_code_scope.created_at IS '생성일시';
COMMENT ON COLUMN dwp_aura.tenant_company_code_scope.updated_at IS '수정일시';

-- Permissions

ALTER TABLE dwp_aura.tenant_company_code_scope OWNER TO dwp_user;
GRANT ALL ON TABLE dwp_aura.tenant_company_code_scope TO dwp_user;


-- dwp_aura.tenant_currency_scope definition

-- Drop table

-- DROP TABLE dwp_aura.tenant_currency_scope;

CREATE TABLE dwp_aura.tenant_currency_scope (
	tenant_id int8 NOT NULL, -- 테넌트 식별자
	waers varchar(5) NOT NULL, -- 통화 코드
	is_enabled bool DEFAULT true NOT NULL, -- 활성 여부
	fx_control_mode varchar(16) DEFAULT 'ALLOW'::character varying NOT NULL, -- ALLOW | FX_REQUIRED | FX_LOCKED
	created_at timestamptz DEFAULT now() NOT NULL, -- 생성일시
	updated_at timestamptz DEFAULT now() NOT NULL, -- 수정일시
	CONSTRAINT tenant_currency_scope_pkey PRIMARY KEY (tenant_id, waers)
);
CREATE INDEX ix_tenant_currency_scope_tenant_enabled ON dwp_aura.tenant_currency_scope USING btree (tenant_id, is_enabled);
COMMENT ON TABLE dwp_aura.tenant_currency_scope IS 'Tenant별 통화(WAERS) 스코프. on/off + FX 제어.';

-- Column comments

COMMENT ON COLUMN dwp_aura.tenant_currency_scope.tenant_id IS '테넌트 식별자';
COMMENT ON COLUMN dwp_aura.tenant_currency_scope.waers IS '통화 코드';
COMMENT ON COLUMN dwp_aura.tenant_currency_scope.is_enabled IS '활성 여부';
COMMENT ON COLUMN dwp_aura.tenant_currency_scope.fx_control_mode IS 'ALLOW | FX_REQUIRED | FX_LOCKED';
COMMENT ON COLUMN dwp_aura.tenant_currency_scope.created_at IS '생성일시';
COMMENT ON COLUMN dwp_aura.tenant_currency_scope.updated_at IS '수정일시';

-- Permissions

ALTER TABLE dwp_aura.tenant_currency_scope OWNER TO dwp_user;
GRANT ALL ON TABLE dwp_aura.tenant_currency_scope TO dwp_user;


-- dwp_aura.tenant_scope_seed_state definition

-- Drop table

-- DROP TABLE dwp_aura.tenant_scope_seed_state;

CREATE TABLE dwp_aura.tenant_scope_seed_state (
	tenant_id int8 NOT NULL, -- 테넌트 식별자 (PK)
	seeded_at timestamptz DEFAULT now() NOT NULL, -- 시드 실행 시각
	seed_version varchar(16) DEFAULT 'v1'::character varying NOT NULL, -- 시드 버전
	CONSTRAINT tenant_scope_seed_state_pkey PRIMARY KEY (tenant_id)
);
COMMENT ON TABLE dwp_aura.tenant_scope_seed_state IS 'Tenant Scope 시드 완료 상태. 첫 GET 시 idempotent 시드용.';

-- Column comments

COMMENT ON COLUMN dwp_aura.tenant_scope_seed_state.tenant_id IS '테넌트 식별자 (PK)';
COMMENT ON COLUMN dwp_aura.tenant_scope_seed_state.seeded_at IS '시드 실행 시각';
COMMENT ON COLUMN dwp_aura.tenant_scope_seed_state.seed_version IS '시드 버전';

-- Permissions

ALTER TABLE dwp_aura.tenant_scope_seed_state OWNER TO dwp_user;
GRANT ALL ON TABLE dwp_aura.tenant_scope_seed_state TO dwp_user;


-- dwp_aura.tenant_sod_rule definition

-- Drop table

-- DROP TABLE dwp_aura.tenant_sod_rule;

CREATE TABLE dwp_aura.tenant_sod_rule (
	rule_id bigserial NOT NULL, -- 규칙 식별자 (PK)
	tenant_id int8 NOT NULL, -- 테넌트 식별자
	rule_key varchar(64) NOT NULL, -- 규칙 키
	title varchar(120) NOT NULL, -- 규칙 제목
	description text NULL, -- 규칙 설명
	is_enabled bool DEFAULT true NOT NULL, -- 활성 여부
	severity varchar(16) DEFAULT 'WARN'::character varying NOT NULL, -- INFO | WARN | BLOCK
	applies_to jsonb DEFAULT '[]'::jsonb NOT NULL, -- 적용 대상 액션 목록 (JSONB)
	created_at timestamptz DEFAULT now() NOT NULL, -- 생성일시
	updated_at timestamptz DEFAULT now() NOT NULL, -- 수정일시
	CONSTRAINT tenant_sod_rule_pkey PRIMARY KEY (rule_id),
	CONSTRAINT tenant_sod_rule_tenant_id_rule_key_key UNIQUE (tenant_id, rule_key)
);
CREATE INDEX ix_tenant_sod_rule_tenant_enabled ON dwp_aura.tenant_sod_rule USING btree (tenant_id, is_enabled);
COMMENT ON TABLE dwp_aura.tenant_sod_rule IS 'Tenant별 SoD(Segregation of Duties) 규칙.';

-- Column comments

COMMENT ON COLUMN dwp_aura.tenant_sod_rule.rule_id IS '규칙 식별자 (PK)';
COMMENT ON COLUMN dwp_aura.tenant_sod_rule.tenant_id IS '테넌트 식별자';
COMMENT ON COLUMN dwp_aura.tenant_sod_rule.rule_key IS '규칙 키';
COMMENT ON COLUMN dwp_aura.tenant_sod_rule.title IS '규칙 제목';
COMMENT ON COLUMN dwp_aura.tenant_sod_rule.description IS '규칙 설명';
COMMENT ON COLUMN dwp_aura.tenant_sod_rule.is_enabled IS '활성 여부';
COMMENT ON COLUMN dwp_aura.tenant_sod_rule.severity IS 'INFO | WARN | BLOCK';
COMMENT ON COLUMN dwp_aura.tenant_sod_rule.applies_to IS '적용 대상 액션 목록 (JSONB)';
COMMENT ON COLUMN dwp_aura.tenant_sod_rule.created_at IS '생성일시';
COMMENT ON COLUMN dwp_aura.tenant_sod_rule.updated_at IS '수정일시';

-- Permissions

ALTER TABLE dwp_aura.tenant_sod_rule OWNER TO dwp_user;
GRANT ALL ON TABLE dwp_aura.tenant_sod_rule TO dwp_user;


-- dwp_aura.user_expense_patterns definition

-- Drop table

-- DROP TABLE dwp_aura.user_expense_patterns;

CREATE TABLE dwp_aura.user_expense_patterns (
	pattern_id bigserial NOT NULL,
	tenant_id int8 NOT NULL,
	user_id int8 NOT NULL,
	mcc_code varchar(4) NULL,
	avg_amount numeric(18, 2) DEFAULT 0 NULL,
	max_amount numeric(18, 2) DEFAULT 0 NULL,
	frequency_count int4 DEFAULT 0 NULL,
	last_analyzed_at timestamptz NULL,
	created_at timestamptz DEFAULT now() NOT NULL,
	created_by int8 NULL,
	updated_at timestamptz DEFAULT now() NOT NULL,
	updated_by int8 NULL,
	CONSTRAINT user_expense_patterns_pkey PRIMARY KEY (pattern_id)
);
CREATE INDEX idx_user_expense_patterns_owner ON dwp_aura.user_expense_patterns USING btree (tenant_id, user_id, mcc_code);
COMMENT ON TABLE dwp_aura.user_expense_patterns IS '사용자별/업종별 과거 지출 패턴 통계';

-- Permissions

ALTER TABLE dwp_aura.user_expense_patterns OWNER TO dwp_user;
GRANT ALL ON TABLE dwp_aura.user_expense_patterns TO dwp_user;


-- dwp_aura.user_hr_calendar definition

-- Drop table

-- DROP TABLE dwp_aura.user_hr_calendar;

CREATE TABLE dwp_aura.user_hr_calendar (
	calendar_id bigserial NOT NULL,
	tenant_id int8 NOT NULL,
	user_id int8 NOT NULL,
	event_date date NOT NULL,
	status_code varchar(20) NOT NULL,
	description varchar(200) NULL,
	created_at timestamptz DEFAULT now() NOT NULL,
	created_by int8 NULL,
	updated_at timestamptz DEFAULT now() NOT NULL,
	updated_by int8 NULL,
	CONSTRAINT uq_user_date UNIQUE (user_id, event_date),
	CONSTRAINT user_hr_calendar_pkey PRIMARY KEY (calendar_id)
);
COMMENT ON TABLE dwp_aura.user_hr_calendar IS '사용자별 일자별 근태/휴가 정보 (Aura 분석 핵심 데이터)';

-- Permissions

ALTER TABLE dwp_aura.user_hr_calendar OWNER TO dwp_user;
GRANT ALL ON TABLE dwp_aura.user_hr_calendar TO dwp_user;


-- dwp_aura.agent_case definition

-- Drop table

-- DROP TABLE dwp_aura.agent_case;

CREATE TABLE dwp_aura.agent_case (
	case_id bigserial NOT NULL, -- 케이스 식별자 (PK)
	tenant_id int8 NOT NULL, -- 테넌트 식별자
	detected_at timestamptz DEFAULT now() NOT NULL, -- 탐지 시각
	bukrs varchar(4) NULL, -- 회사코드 (전표/오픈아이템 연결)
	belnr varchar(10) NULL, -- 전표번호
	gjahr varchar(4) NULL, -- 회계연도
	buzei varchar(3) NULL, -- 라인번호
	case_type varchar(50) NOT NULL, -- 케이스 유형 (DUPLICATE_INVOICE, ANOMALY_AMOUNT 등)
	severity varchar(10) NOT NULL, -- 심각도 (LOW, MEDIUM, HIGH)
	score numeric(6, 4) NULL, -- 리스크 점수
	reason_text text NULL, -- 탐지 사유 텍스트
	evidence_json jsonb NULL, -- 증거 데이터 (JSONB)
	rag_refs_json jsonb NULL, -- RAG 참조 (JSONB)
	status dwp_aura."agent_case_status" DEFAULT 'OPEN'::dwp_aura.agent_case_status NOT NULL, -- 상태 (OPEN, IN_REVIEW, APPROVED, REJECTED, ACTIONED, CLOSED, TRIAGED, IN_PROGRESS, RESOLVED, DISMISSED)
	owner_user varchar(80) NULL, -- 담당자 (User ID)
	created_at timestamptz DEFAULT now() NOT NULL, -- 생성일시
	updated_at timestamptz DEFAULT now() NOT NULL, -- 수정일시
	assignee_user_id int8 NULL,
	saved_view_key varchar(100) NULL,
	dedup_key varchar(200) NULL, -- Phase B: tenant+rule+entity 복합키. 중복 케이스 방지.
	last_detect_run_id int8 NULL, -- P1: 마지막 탐지 배치 run_id. 역추적용.
	user_id int8 NULL, -- 케이스 소유자 식별자 (전표 작성자 user_id 상속)
	CONSTRAINT agent_case_pkey PRIMARY KEY (case_id),
	CONSTRAINT agent_case_last_detect_run_id_fkey FOREIGN KEY (last_detect_run_id) REFERENCES dwp_aura.detect_run(run_id) ON DELETE SET NULL
);
CREATE INDEX idx_agent_case_user_id ON dwp_aura.agent_case USING btree (user_id);
CREATE INDEX ix_agent_case_assignee ON dwp_aura.agent_case USING btree (tenant_id, assignee_user_id) WHERE (assignee_user_id IS NOT NULL);
CREATE INDEX ix_agent_case_dedup_key ON dwp_aura.agent_case USING btree (tenant_id, dedup_key);
CREATE INDEX ix_agent_case_doc ON dwp_aura.agent_case USING btree (tenant_id, bukrs, belnr, gjahr, buzei);
CREATE INDEX ix_agent_case_last_detect_run ON dwp_aura.agent_case USING btree (last_detect_run_id) WHERE (last_detect_run_id IS NOT NULL);
CREATE INDEX ix_agent_case_status ON dwp_aura.agent_case USING btree (tenant_id, status, detected_at DESC);
CREATE INDEX ix_agent_case_tenant_status_severity ON dwp_aura.agent_case USING btree (tenant_id, status, severity, detected_at DESC);
CREATE UNIQUE INDEX ux_agent_case_dedup_key ON dwp_aura.agent_case USING btree (tenant_id, dedup_key) WHERE (dedup_key IS NOT NULL);
COMMENT ON TABLE dwp_aura.agent_case IS '에이전트 케이스. Detect 배치/룰 탐지 결과.';

-- Column comments

COMMENT ON COLUMN dwp_aura.agent_case.case_id IS '케이스 식별자 (PK)';
COMMENT ON COLUMN dwp_aura.agent_case.tenant_id IS '테넌트 식별자';
COMMENT ON COLUMN dwp_aura.agent_case.detected_at IS '탐지 시각';
COMMENT ON COLUMN dwp_aura.agent_case.bukrs IS '회사코드 (전표/오픈아이템 연결)';
COMMENT ON COLUMN dwp_aura.agent_case.belnr IS '전표번호';
COMMENT ON COLUMN dwp_aura.agent_case.gjahr IS '회계연도';
COMMENT ON COLUMN dwp_aura.agent_case.buzei IS '라인번호';
COMMENT ON COLUMN dwp_aura.agent_case.case_type IS '케이스 유형 (DUPLICATE_INVOICE, ANOMALY_AMOUNT 등)';
COMMENT ON COLUMN dwp_aura.agent_case.severity IS '심각도 (LOW, MEDIUM, HIGH)';
COMMENT ON COLUMN dwp_aura.agent_case.score IS '리스크 점수';
COMMENT ON COLUMN dwp_aura.agent_case.reason_text IS '탐지 사유 텍스트';
COMMENT ON COLUMN dwp_aura.agent_case.evidence_json IS '증거 데이터 (JSONB)';
COMMENT ON COLUMN dwp_aura.agent_case.rag_refs_json IS 'RAG 참조 (JSONB)';
COMMENT ON COLUMN dwp_aura.agent_case.status IS '상태 (OPEN, IN_REVIEW, APPROVED, REJECTED, ACTIONED, CLOSED, TRIAGED, IN_PROGRESS, RESOLVED, DISMISSED)';
COMMENT ON COLUMN dwp_aura.agent_case.owner_user IS '담당자 (User ID)';
COMMENT ON COLUMN dwp_aura.agent_case.created_at IS '생성일시';
COMMENT ON COLUMN dwp_aura.agent_case.updated_at IS '수정일시';
COMMENT ON COLUMN dwp_aura.agent_case.dedup_key IS 'Phase B: tenant+rule+entity 복합키. 중복 케이스 방지.';
COMMENT ON COLUMN dwp_aura.agent_case.last_detect_run_id IS 'P1: 마지막 탐지 배치 run_id. 역추적용.';
COMMENT ON COLUMN dwp_aura.agent_case.user_id IS '케이스 소유자 식별자 (전표 작성자 user_id 상속)';

-- Permissions

ALTER TABLE dwp_aura.agent_case OWNER TO dwp_user;
GRANT ALL ON TABLE dwp_aura.agent_case TO dwp_user;


-- dwp_aura.agent_case_action_history definition

-- Drop table

-- DROP TABLE dwp_aura.agent_case_action_history;

CREATE TABLE dwp_aura.agent_case_action_history (
	id bigserial NOT NULL,
	tenant_id int8 NOT NULL,
	case_id int8 NOT NULL,
	action_type varchar(20) NOT NULL, -- APPROVE, REJECT, HOLD, ESCALATE (app_codes CASE_DECISION_ACTION)
	actor_id varchar(50) NOT NULL, -- 조치자 식별자 (USER:userId 또는 AGENT:agentId)
	comment_text text NULL, -- 조치 사유/코멘트
	action_at timestamptz DEFAULT CURRENT_TIMESTAMP NOT NULL,
	metadata_json jsonb NULL, -- 조치 당시 전표 요약(bukrs,belnr,gjahr,status_code 등)
	created_at timestamptz DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT agent_case_action_history_pkey PRIMARY KEY (id),
	CONSTRAINT agent_case_action_history_case_id_fkey FOREIGN KEY (case_id) REFERENCES dwp_aura.agent_case(case_id) ON DELETE CASCADE
);
CREATE INDEX ix_agent_case_action_history_action_at ON dwp_aura.agent_case_action_history USING btree (tenant_id, action_at DESC);
CREATE INDEX ix_agent_case_action_history_tenant_case ON dwp_aura.agent_case_action_history USING btree (tenant_id, case_id, action_at DESC);
COMMENT ON TABLE dwp_aura.agent_case_action_history IS 'Phase 6: 조치 이력 (승인/거절/에스컬레이션). 감사 추적용.';

-- Column comments

COMMENT ON COLUMN dwp_aura.agent_case_action_history.action_type IS 'APPROVE, REJECT, HOLD, ESCALATE (app_codes CASE_DECISION_ACTION)';
COMMENT ON COLUMN dwp_aura.agent_case_action_history.actor_id IS '조치자 식별자 (USER:userId 또는 AGENT:agentId)';
COMMENT ON COLUMN dwp_aura.agent_case_action_history.comment_text IS '조치 사유/코멘트';
COMMENT ON COLUMN dwp_aura.agent_case_action_history.metadata_json IS '조치 당시 전표 요약(bukrs,belnr,gjahr,status_code 등)';

-- Permissions

ALTER TABLE dwp_aura.agent_case_action_history OWNER TO dwp_user;
GRANT ALL ON TABLE dwp_aura.agent_case_action_history TO dwp_user;


-- dwp_aura.agent_document_mapping definition

-- Drop table

-- DROP TABLE dwp_aura.agent_document_mapping;

CREATE TABLE dwp_aura.agent_document_mapping (
	agent_id int8 NOT NULL, -- 에이전트 ID (FK: agent_master)
	doc_id int8 NOT NULL, -- 문서 ID (FK: rag_document)
	tenant_id int8 NOT NULL, -- 테넌트 ID (멀티테넌시 격리)
	created_at timestamptz DEFAULT now() NOT NULL, -- 생성일시
	created_by int8 NULL, -- 생성자 user_id (논리적 참조: com_users.user_id)
	updated_at timestamptz DEFAULT now() NOT NULL, -- 수정일시
	updated_by int8 NULL, -- 수정자 user_id (논리적 참조: com_users.user_id)
	CONSTRAINT agent_document_mapping_pkey PRIMARY KEY (agent_id, doc_id),
	CONSTRAINT fk_agent_document_mapping_agent FOREIGN KEY (agent_id) REFERENCES dwp_aura.agent_master(agent_id) ON DELETE CASCADE,
	CONSTRAINT fk_agent_document_mapping_document FOREIGN KEY (doc_id) REFERENCES dwp_aura.rag_document(doc_id) ON DELETE CASCADE
);
CREATE INDEX ix_agent_document_mapping_agent_id ON dwp_aura.agent_document_mapping USING btree (agent_id);
CREATE INDEX ix_agent_document_mapping_doc_id ON dwp_aura.agent_document_mapping USING btree (doc_id);
CREATE INDEX ix_agent_document_mapping_tenant_id ON dwp_aura.agent_document_mapping USING btree (tenant_id);
COMMENT ON TABLE dwp_aura.agent_document_mapping IS '에이전트-문서 매핑: 에이전트가 사용하는 RAG 문서 목록';

-- Column comments

COMMENT ON COLUMN dwp_aura.agent_document_mapping.agent_id IS '에이전트 ID (FK: agent_master)';
COMMENT ON COLUMN dwp_aura.agent_document_mapping.doc_id IS '문서 ID (FK: rag_document)';
COMMENT ON COLUMN dwp_aura.agent_document_mapping.tenant_id IS '테넌트 ID (멀티테넌시 격리)';
COMMENT ON COLUMN dwp_aura.agent_document_mapping.created_at IS '생성일시';
COMMENT ON COLUMN dwp_aura.agent_document_mapping.created_by IS '생성자 user_id (논리적 참조: com_users.user_id)';
COMMENT ON COLUMN dwp_aura.agent_document_mapping.updated_at IS '수정일시';
COMMENT ON COLUMN dwp_aura.agent_document_mapping.updated_by IS '수정자 user_id (논리적 참조: com_users.user_id)';

-- Permissions

ALTER TABLE dwp_aura.agent_document_mapping OWNER TO dwp_user;
GRANT ALL ON TABLE dwp_aura.agent_document_mapping TO dwp_user;


-- dwp_aura.agent_prompt_history definition

-- Drop table

-- DROP TABLE dwp_aura.agent_prompt_history;

CREATE TABLE dwp_aura.agent_prompt_history (
	prompt_id bigserial NOT NULL, -- 프롬프트 식별자 (PK)
	agent_id int8 NOT NULL, -- 에이전트 (FK)
	system_instruction text NOT NULL, -- 시스템 지시문 (텍스트)
	"version" int4 NOT NULL, -- 버전 번호
	is_current bool DEFAULT false NOT NULL, -- 현재 사용 중인 버전 여부
	created_at timestamptz DEFAULT now() NOT NULL,
	CONSTRAINT agent_prompt_history_pkey PRIMARY KEY (prompt_id),
	CONSTRAINT agent_prompt_history_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES dwp_aura.agent_master(agent_id) ON DELETE CASCADE
);
CREATE INDEX ix_agent_prompt_history_agent_id ON dwp_aura.agent_prompt_history USING btree (agent_id);
CREATE UNIQUE INDEX ux_agent_prompt_history_agent_version ON dwp_aura.agent_prompt_history USING btree (agent_id, version);
COMMENT ON TABLE dwp_aura.agent_prompt_history IS '에이전트 스튜디오: 시스템 프롬프트 버전 이력.';

-- Column comments

COMMENT ON COLUMN dwp_aura.agent_prompt_history.prompt_id IS '프롬프트 식별자 (PK)';
COMMENT ON COLUMN dwp_aura.agent_prompt_history.agent_id IS '에이전트 (FK)';
COMMENT ON COLUMN dwp_aura.agent_prompt_history.system_instruction IS '시스템 지시문 (텍스트)';
COMMENT ON COLUMN dwp_aura.agent_prompt_history."version" IS '버전 번호';
COMMENT ON COLUMN dwp_aura.agent_prompt_history.is_current IS '현재 사용 중인 버전 여부';

-- Permissions

ALTER TABLE dwp_aura.agent_prompt_history OWNER TO dwp_user;
GRANT ALL ON TABLE dwp_aura.agent_prompt_history TO dwp_user;


-- dwp_aura.agent_tool_mapping definition

-- Drop table

-- DROP TABLE dwp_aura.agent_tool_mapping;

CREATE TABLE dwp_aura.agent_tool_mapping (
	agent_id int8 NOT NULL,
	tool_id int8 NOT NULL,
	created_at timestamptz DEFAULT now() NOT NULL,
	CONSTRAINT agent_tool_mapping_pkey PRIMARY KEY (agent_id, tool_id),
	CONSTRAINT agent_tool_mapping_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES dwp_aura.agent_master(agent_id) ON DELETE CASCADE,
	CONSTRAINT agent_tool_mapping_tool_id_fkey FOREIGN KEY (tool_id) REFERENCES dwp_aura.agent_tool_inventory(tool_id) ON DELETE CASCADE
);
CREATE INDEX ix_agent_tool_mapping_tool_id ON dwp_aura.agent_tool_mapping USING btree (tool_id);
COMMENT ON TABLE dwp_aura.agent_tool_mapping IS '에이전트–도구 M:N 매핑. tenant는 agent_master.tenant_id로 격리.';

-- Permissions

ALTER TABLE dwp_aura.agent_tool_mapping OWNER TO dwp_user;
GRANT ALL ON TABLE dwp_aura.agent_tool_mapping TO dwp_user;


-- dwp_aura.bp_party definition

-- Drop table

-- DROP TABLE dwp_aura.bp_party;

CREATE TABLE dwp_aura.bp_party (
	party_id bigserial NOT NULL, -- 거래처 식별자 (PK)
	tenant_id int8 NOT NULL, -- 테넌트 식별자
	party_type varchar(10) NOT NULL, -- 유형 (VENDOR=공급업체, CUSTOMER=고객)
	party_code varchar(40) NOT NULL, -- 거래처코드 (lifnr/kunnr)
	name_display varchar(200) NULL, -- 표시명
	country varchar(3) NULL, -- 국가코드 (3자리, ISO 3166-1 alpha-3)
	created_on date NULL, -- 생성일
	is_one_time bool DEFAULT false NOT NULL, -- 일회성 거래처 여부
	risk_flags jsonb DEFAULT '{}'::jsonb NOT NULL, -- 리스크 플래그 (JSONB, score 등)
	last_change_ts timestamptz NULL, -- 마지막 변경 시각
	raw_event_id int8 NULL, -- 원천 Raw 이벤트 ID (FK)
	updated_at timestamptz DEFAULT now() NOT NULL, -- 수정일시
	CONSTRAINT bp_party_pkey PRIMARY KEY (party_id),
	CONSTRAINT bp_party_tenant_id_party_type_party_code_key UNIQUE (tenant_id, party_type, party_code),
	CONSTRAINT bp_party_raw_event_id_fkey FOREIGN KEY (raw_event_id) REFERENCES dwp_aura.sap_raw_events(id) ON DELETE SET NULL
);
CREATE INDEX ix_bp_party_code ON dwp_aura.bp_party USING btree (tenant_id, party_type, party_code);
CREATE INDEX ix_bp_party_tenant_type_code ON dwp_aura.bp_party USING btree (tenant_id, party_type, party_code);
COMMENT ON TABLE dwp_aura.bp_party IS '거래처 마스터 (Business Partner). VENDOR/CUSTOMER.';

-- Column comments

COMMENT ON COLUMN dwp_aura.bp_party.party_id IS '거래처 식별자 (PK)';
COMMENT ON COLUMN dwp_aura.bp_party.tenant_id IS '테넌트 식별자';
COMMENT ON COLUMN dwp_aura.bp_party.party_type IS '유형 (VENDOR=공급업체, CUSTOMER=고객)';
COMMENT ON COLUMN dwp_aura.bp_party.party_code IS '거래처코드 (lifnr/kunnr)';
COMMENT ON COLUMN dwp_aura.bp_party.name_display IS '표시명';
COMMENT ON COLUMN dwp_aura.bp_party.country IS '국가코드 (3자리, ISO 3166-1 alpha-3)';
COMMENT ON COLUMN dwp_aura.bp_party.created_on IS '생성일';
COMMENT ON COLUMN dwp_aura.bp_party.is_one_time IS '일회성 거래처 여부';
COMMENT ON COLUMN dwp_aura.bp_party.risk_flags IS '리스크 플래그 (JSONB, score 등)';
COMMENT ON COLUMN dwp_aura.bp_party.last_change_ts IS '마지막 변경 시각';
COMMENT ON COLUMN dwp_aura.bp_party.raw_event_id IS '원천 Raw 이벤트 ID (FK)';
COMMENT ON COLUMN dwp_aura.bp_party.updated_at IS '수정일시';

-- Permissions

ALTER TABLE dwp_aura.bp_party OWNER TO dwp_user;
GRANT ALL ON TABLE dwp_aura.bp_party TO dwp_user;


-- dwp_aura.bp_party_pii_vault definition

-- Drop table

-- DROP TABLE dwp_aura.bp_party_pii_vault;

CREATE TABLE dwp_aura.bp_party_pii_vault (
	party_id int8 NOT NULL, -- 거래처 ID (PK, FK: bp_party.party_id)
	tenant_id int8 NOT NULL, -- 테넌트 식별자
	pii_cipher bytea NULL, -- 암호화된 PII (BYTEA)
	pii_hash text NULL, -- PII 해시 (검색용)
	updated_at timestamptz DEFAULT now() NOT NULL, -- 수정일시
	CONSTRAINT bp_party_pii_vault_pkey PRIMARY KEY (party_id),
	CONSTRAINT bp_party_pii_vault_party_id_fkey FOREIGN KEY (party_id) REFERENCES dwp_aura.bp_party(party_id) ON DELETE CASCADE
);
COMMENT ON TABLE dwp_aura.bp_party_pii_vault IS '거래처 PII 암호화 저장소. 개인정보 암호화/해시.';

-- Column comments

COMMENT ON COLUMN dwp_aura.bp_party_pii_vault.party_id IS '거래처 ID (PK, FK: bp_party.party_id)';
COMMENT ON COLUMN dwp_aura.bp_party_pii_vault.tenant_id IS '테넌트 식별자';
COMMENT ON COLUMN dwp_aura.bp_party_pii_vault.pii_cipher IS '암호화된 PII (BYTEA)';
COMMENT ON COLUMN dwp_aura.bp_party_pii_vault.pii_hash IS 'PII 해시 (검색용)';
COMMENT ON COLUMN dwp_aura.bp_party_pii_vault.updated_at IS '수정일시';

-- Permissions

ALTER TABLE dwp_aura.bp_party_pii_vault OWNER TO dwp_user;
GRANT ALL ON TABLE dwp_aura.bp_party_pii_vault TO dwp_user;


-- dwp_aura.case_action_proposal definition

-- Drop table

-- DROP TABLE dwp_aura.case_action_proposal;

CREATE TABLE dwp_aura.case_action_proposal (
	proposal_id uuid DEFAULT gen_random_uuid() NOT NULL,
	tenant_id int8 NOT NULL,
	case_id int8 NOT NULL,
	run_id uuid NULL,
	"type" varchar(50) NOT NULL,
	status varchar(20) DEFAULT 'DRAFT'::character varying NOT NULL, -- DRAFT | PROPOSED | APPROVED | REJECTED | EXECUTED | FAILED
	risk_level varchar(20) NULL,
	rationale text NULL,
	payload_json jsonb NULL,
	created_at timestamptz DEFAULT now() NOT NULL,
	updated_at timestamptz DEFAULT now() NOT NULL,
	dedup_key varchar(64) NOT NULL, -- 멱등 키: sha256(lower(type)|canonicalize(payload)|normalize(rationale))
	requires_approval bool NULL, -- 승인 필요 여부 (Aura proposals.requiresApproval)
	decided_by int8 NULL, -- 결정자 user_id (승인/거절 시)
	decided_at timestamptz NULL, -- 결정 시각
	decision_comment text NULL, -- 승인/거절 시 코멘트
	CONSTRAINT case_action_proposal_pkey PRIMARY KEY (proposal_id),
	CONSTRAINT case_action_proposal_run_id_fkey FOREIGN KEY (run_id) REFERENCES dwp_aura.case_analysis_run(run_id) ON DELETE SET NULL
);
CREATE INDEX ix_case_action_proposal_run ON dwp_aura.case_action_proposal USING btree (run_id);
CREATE INDEX ix_case_action_proposal_tenant_case ON dwp_aura.case_action_proposal USING btree (tenant_id, case_id);
CREATE UNIQUE INDEX uk_case_action_proposal_case_run_dedup ON dwp_aura.case_action_proposal USING btree (case_id, run_id, dedup_key) WHERE (run_id IS NOT NULL);
CREATE UNIQUE INDEX uk_case_action_proposal_legacy_dedup ON dwp_aura.case_action_proposal USING btree (case_id, dedup_key) WHERE (run_id IS NULL);
COMMENT ON TABLE dwp_aura.case_action_proposal IS 'Phase2: AI 권고 조치 (승인/거절/실행)';

-- Column comments

COMMENT ON COLUMN dwp_aura.case_action_proposal.status IS 'DRAFT | PROPOSED | APPROVED | REJECTED | EXECUTED | FAILED';
COMMENT ON COLUMN dwp_aura.case_action_proposal.dedup_key IS '멱등 키: sha256(lower(type)|canonicalize(payload)|normalize(rationale))';
COMMENT ON COLUMN dwp_aura.case_action_proposal.requires_approval IS '승인 필요 여부 (Aura proposals.requiresApproval)';
COMMENT ON COLUMN dwp_aura.case_action_proposal.decided_by IS '결정자 user_id (승인/거절 시)';
COMMENT ON COLUMN dwp_aura.case_action_proposal.decided_at IS '결정 시각';
COMMENT ON COLUMN dwp_aura.case_action_proposal.decision_comment IS '승인/거절 시 코멘트';

-- Permissions

ALTER TABLE dwp_aura.case_action_proposal OWNER TO dwp_user;
GRANT ALL ON TABLE dwp_aura.case_action_proposal TO dwp_user;


-- dwp_aura.case_analysis_result definition

-- Drop table

-- DROP TABLE dwp_aura.case_analysis_result;

CREATE TABLE dwp_aura.case_analysis_result (
	run_id uuid NOT NULL, -- 분석 실행 식별자 (1회 분석 단위 UUID)
	tenant_id int8 NOT NULL, -- 테넌트 식별자 (데이터 격리 키)
	score numeric(5, 2) NULL, -- 최종 위험 점수 (정규화 점수, 보통 0~1 또는 UI용 스케일 기준)
	severity varchar(20) NULL, -- 최종 위험 등급 (LOW/MEDIUM/HIGH/CRITICAL)
	reason_text text NULL, -- 사용자 노출용 최종 판단 문장
	risk_score int4 NULL, -- 운영 집계용 정수 위험점수 (예: 0~100)
	violation_clause text NULL, -- 주요 위반 조항 요약 문자열
	reasoning_summary text NULL, -- 판단 근거 요약 (감사용)
	recommended_action text NULL, -- 권고 조치 요약
	confidence_json jsonb NULL, -- 점수 구성요소/신뢰도 상세 (JSON)
	evidence_json jsonb NULL, -- 분석에 사용한 근거 목록 (JSON)
	similar_json jsonb NULL, -- 유사 케이스/유사 패턴 정보 (JSON)
	rag_refs_json jsonb NULL, -- RAG 검색 참조/인용 원본 (JSON)
	evidence_map_json jsonb NULL, -- 전표 항목↔근거 매핑 (JSON)
	sentence_citation_map jsonb NULL, -- 결론 문장별 citation 연결 결과 (JSON)
	analysis_score_breakdown jsonb NULL, -- 분석 점수 분해 상세 (JSON)
	quality_gate_codes jsonb NULL, -- 품질게이트 코드 목록 (JSON/배열)
	grounding_coverage_ratio numeric(5, 4) NULL, -- 결론 문장 근거 연결률 (0~1)
	ungrounded_claim_sentences int4 NULL, -- 근거 미연결 주장 문장 수
	created_at timestamptz DEFAULT now() NOT NULL, -- 분석 결과 생성 시각
	analysis_quality_signals jsonb NULL, -- 사용자 노출용 분석 신뢰 신호 목록(quality_gate_codes 코드명 매핑)
	CONSTRAINT case_analysis_result_new_pkey PRIMARY KEY (run_id),
	CONSTRAINT case_analysis_result_new_run_id_fkey FOREIGN KEY (run_id) REFERENCES dwp_aura.case_analysis_run(run_id) ON DELETE CASCADE
);
CREATE INDEX ix_case_analysis_result_analysis_quality_signals ON dwp_aura.case_analysis_result USING gin (analysis_quality_signals);
CREATE INDEX ix_case_analysis_result_created_at ON dwp_aura.case_analysis_result USING btree (created_at DESC);
CREATE INDEX ix_case_analysis_result_quality_gate_codes ON dwp_aura.case_analysis_result USING gin (quality_gate_codes);
CREATE INDEX ix_case_analysis_result_tenant_created ON dwp_aura.case_analysis_result USING btree (tenant_id, created_at DESC);
COMMENT ON TABLE dwp_aura.case_analysis_result IS 'Phase2: 분석 결과 (점수/근거/유사/RAG/권고). run당 1건.';

-- Column comments

COMMENT ON COLUMN dwp_aura.case_analysis_result.run_id IS '분석 실행 식별자 (1회 분석 단위 UUID)';
COMMENT ON COLUMN dwp_aura.case_analysis_result.tenant_id IS '테넌트 식별자 (데이터 격리 키)';
COMMENT ON COLUMN dwp_aura.case_analysis_result.score IS '최종 위험 점수 (정규화 점수, 보통 0~1 또는 UI용 스케일 기준)';
COMMENT ON COLUMN dwp_aura.case_analysis_result.severity IS '최종 위험 등급 (LOW/MEDIUM/HIGH/CRITICAL)';
COMMENT ON COLUMN dwp_aura.case_analysis_result.reason_text IS '사용자 노출용 최종 판단 문장';
COMMENT ON COLUMN dwp_aura.case_analysis_result.risk_score IS '운영 집계용 정수 위험점수 (예: 0~100)';
COMMENT ON COLUMN dwp_aura.case_analysis_result.violation_clause IS '주요 위반 조항 요약 문자열';
COMMENT ON COLUMN dwp_aura.case_analysis_result.reasoning_summary IS '판단 근거 요약 (감사용)';
COMMENT ON COLUMN dwp_aura.case_analysis_result.recommended_action IS '권고 조치 요약';
COMMENT ON COLUMN dwp_aura.case_analysis_result.confidence_json IS '점수 구성요소/신뢰도 상세 (JSON)';
COMMENT ON COLUMN dwp_aura.case_analysis_result.evidence_json IS '분석에 사용한 근거 목록 (JSON)';
COMMENT ON COLUMN dwp_aura.case_analysis_result.similar_json IS '유사 케이스/유사 패턴 정보 (JSON)';
COMMENT ON COLUMN dwp_aura.case_analysis_result.rag_refs_json IS 'RAG 검색 참조/인용 원본 (JSON)';
COMMENT ON COLUMN dwp_aura.case_analysis_result.evidence_map_json IS '전표 항목↔근거 매핑 (JSON)';
COMMENT ON COLUMN dwp_aura.case_analysis_result.sentence_citation_map IS '결론 문장별 citation 연결 결과 (JSON)';
COMMENT ON COLUMN dwp_aura.case_analysis_result.analysis_score_breakdown IS '분석 점수 분해 상세 (JSON)';
COMMENT ON COLUMN dwp_aura.case_analysis_result.quality_gate_codes IS '품질게이트 코드 목록 (JSON/배열)';
COMMENT ON COLUMN dwp_aura.case_analysis_result.grounding_coverage_ratio IS '결론 문장 근거 연결률 (0~1)';
COMMENT ON COLUMN dwp_aura.case_analysis_result.ungrounded_claim_sentences IS '근거 미연결 주장 문장 수';
COMMENT ON COLUMN dwp_aura.case_analysis_result.created_at IS '분석 결과 생성 시각';
COMMENT ON COLUMN dwp_aura.case_analysis_result.analysis_quality_signals IS '사용자 노출용 분석 신뢰 신호 목록(quality_gate_codes 코드명 매핑)';

-- Permissions

ALTER TABLE dwp_aura.case_analysis_result OWNER TO dwp_user;
GRANT ALL ON TABLE dwp_aura.case_analysis_result TO dwp_user;


-- dwp_aura.case_comment definition

-- Drop table

-- DROP TABLE dwp_aura.case_comment;

CREATE TABLE dwp_aura.case_comment (
	comment_id bigserial NOT NULL,
	tenant_id int8 NOT NULL,
	case_id int8 NOT NULL,
	author_user_id int8 NULL,
	author_agent_id varchar(80) NULL,
	comment_text text NOT NULL,
	created_at timestamptz DEFAULT now() NOT NULL,
	CONSTRAINT case_comment_pkey PRIMARY KEY (comment_id),
	CONSTRAINT case_comment_case_id_fkey FOREIGN KEY (case_id) REFERENCES dwp_aura.agent_case(case_id) ON DELETE CASCADE
);
CREATE INDEX ix_case_comment_case ON dwp_aura.case_comment USING btree (tenant_id, case_id, created_at DESC);
COMMENT ON TABLE dwp_aura.case_comment IS '케이스 코멘트.';

-- Permissions

ALTER TABLE dwp_aura.case_comment OWNER TO dwp_user;
GRANT ALL ON TABLE dwp_aura.case_comment TO dwp_user;


-- dwp_aura.config_kv definition

-- Drop table

-- DROP TABLE dwp_aura.config_kv;

CREATE TABLE dwp_aura.config_kv (
	tenant_id int8 NOT NULL, -- 테넌트 식별자 (논리적 참조: com_tenants.tenant_id)
	profile_id int8 NOT NULL, -- 프로파일 식별자 (논리적 참조: config_profile.profile_id)
	config_key text NOT NULL, -- 설정 키
	config_value jsonb NOT NULL, -- 설정 값 (JSONB)
	created_at timestamptz DEFAULT now() NOT NULL, -- 생성일시
	created_by int8 NULL, -- 생성자 user_id (논리적 참조: com_users.user_id)
	updated_at timestamptz DEFAULT now() NOT NULL, -- 수정일시
	updated_by int8 NULL, -- 수정자 user_id (논리적 참조: com_users.user_id)
	CONSTRAINT config_kv_pkey PRIMARY KEY (tenant_id, profile_id, config_key),
	CONSTRAINT config_kv_profile_id_fkey FOREIGN KEY (profile_id) REFERENCES dwp_aura.config_profile(profile_id) ON DELETE CASCADE
);
CREATE INDEX ix_config_kv_tenant_id ON dwp_aura.config_kv USING btree (tenant_id);
COMMENT ON TABLE dwp_aura.config_kv IS 'Key-Value 설정. 규정/정책 추가 시 컬럼 확장 없이 확장용.';

-- Column comments

COMMENT ON COLUMN dwp_aura.config_kv.tenant_id IS '테넌트 식별자 (논리적 참조: com_tenants.tenant_id)';
COMMENT ON COLUMN dwp_aura.config_kv.profile_id IS '프로파일 식별자 (논리적 참조: config_profile.profile_id)';
COMMENT ON COLUMN dwp_aura.config_kv.config_key IS '설정 키';
COMMENT ON COLUMN dwp_aura.config_kv.config_value IS '설정 값 (JSONB)';
COMMENT ON COLUMN dwp_aura.config_kv.created_at IS '생성일시';
COMMENT ON COLUMN dwp_aura.config_kv.created_by IS '생성자 user_id (논리적 참조: com_users.user_id)';
COMMENT ON COLUMN dwp_aura.config_kv.updated_at IS '수정일시';
COMMENT ON COLUMN dwp_aura.config_kv.updated_by IS '수정자 user_id (논리적 참조: com_users.user_id)';

-- Permissions

ALTER TABLE dwp_aura.config_kv OWNER TO dwp_user;
GRANT ALL ON TABLE dwp_aura.config_kv TO dwp_user;


-- dwp_aura.feedback_label definition

-- Drop table

-- DROP TABLE dwp_aura.feedback_label;

CREATE TABLE dwp_aura.feedback_label (
	feedback_id bigserial NOT NULL, -- 피드백 식별자 (PK)
	tenant_id int8 NOT NULL, -- 테넌트 식별자
	target_type varchar(30) NOT NULL, -- 대상 유형 (CASE, DOC, ENTITY)
	target_id text NOT NULL, -- 대상 ID
	"label" varchar(30) NOT NULL, -- 라벨 (VALID, INVALID, NEEDS_REVIEW)
	"comment" text NULL, -- 코멘트
	created_by int8 NULL, -- 생성자 user_id
	created_at timestamptz DEFAULT now() NOT NULL, -- 생성일시
	correct_action varchar(100) NULL,
	case_id int8 NULL,
	CONSTRAINT feedback_label_pkey PRIMARY KEY (feedback_id),
	CONSTRAINT feedback_label_case_id_fkey FOREIGN KEY (case_id) REFERENCES dwp_aura.agent_case(case_id) ON DELETE SET NULL
);
CREATE INDEX ix_feedback_label_case ON dwp_aura.feedback_label USING btree (tenant_id, case_id) WHERE (case_id IS NOT NULL);
CREATE INDEX ix_feedback_label_target ON dwp_aura.feedback_label USING btree (tenant_id, target_type, target_id);
CREATE INDEX ix_feedback_label_tenant ON dwp_aura.feedback_label USING btree (tenant_id);
COMMENT ON TABLE dwp_aura.feedback_label IS '피드백 라벨. target_type: CASE|DOC|ENTITY. label: VALID|INVALID|NEEDS_REVIEW.';

-- Column comments

COMMENT ON COLUMN dwp_aura.feedback_label.feedback_id IS '피드백 식별자 (PK)';
COMMENT ON COLUMN dwp_aura.feedback_label.tenant_id IS '테넌트 식별자';
COMMENT ON COLUMN dwp_aura.feedback_label.target_type IS '대상 유형 (CASE, DOC, ENTITY)';
COMMENT ON COLUMN dwp_aura.feedback_label.target_id IS '대상 ID';
COMMENT ON COLUMN dwp_aura.feedback_label."label" IS '라벨 (VALID, INVALID, NEEDS_REVIEW)';
COMMENT ON COLUMN dwp_aura.feedback_label."comment" IS '코멘트';
COMMENT ON COLUMN dwp_aura.feedback_label.created_by IS '생성자 user_id';
COMMENT ON COLUMN dwp_aura.feedback_label.created_at IS '생성일시';

-- Permissions

ALTER TABLE dwp_aura.feedback_label OWNER TO dwp_user;
GRANT ALL ON TABLE dwp_aura.feedback_label TO dwp_user;


-- dwp_aura.fi_doc_header definition

-- Drop table

-- DROP TABLE dwp_aura.fi_doc_header;

CREATE TABLE dwp_aura.fi_doc_header (
	tenant_id int8 NOT NULL, -- 테넌트 식별자
	bukrs varchar(4) NOT NULL, -- 회사코드 (SAP BUKRS)
	belnr varchar(10) NOT NULL, -- 전표번호 (SAP BELNR)
	gjahr varchar(4) NOT NULL, -- 회계연도 (SAP GJAHR)
	doc_source varchar(10) NOT NULL, -- 전표 원천 (SAP, MANUAL 등)
	budat date NOT NULL, -- 전기일 (Posting Date)
	bldat date NULL, -- 증빙일 (Document Date)
	cpudt date NULL, -- 처리일 (CPU Date)
	cputm time NULL, -- 처리시간 (CPU Time)
	usnam varchar(12) NULL, -- 생성자 (User Name)
	tcode varchar(20) NULL, -- 트랜잭션 코드 (SAP TCODE)
	blart varchar(2) NULL, -- 전표유형 (Document Type)
	waers varchar(5) NULL, -- 통화 (Currency)
	kursf numeric(18, 6) NULL, -- 환율
	xblnr varchar(30) NULL, -- 참조번호 (External Document No)
	bktxt varchar(200) NULL, -- 헤더텍스트
	status_code varchar(20) NULL, -- 전표 상태 (POSTED, PARKED 등)
	reversal_belnr varchar(10) NULL, -- 역분개 전표번호
	last_change_ts timestamptz NULL, -- 마지막 변경 시각
	raw_event_id int8 NULL, -- 원천 Raw 이벤트 ID (FK)
	created_at timestamptz DEFAULT now() NOT NULL, -- 생성일시 (Detect 배치 윈도우 기준)
	updated_at timestamptz DEFAULT now() NOT NULL, -- 수정일시
	intended_risk_type varchar(50) NULL, -- 데모 생성 시 시나리오 유형(예: SPLIT_PAYMENT, HOLIDAY_USAGE). Aura 엔진에 evidence_json.intended_risk_type으로 전달.
	hr_status varchar(20) NULL, -- 규정 v2.0: 근무/휴가 (WORK, LEAVE 등). Aura evidence_json/metadata 전달용.
	mcc_code varchar(20) NULL, -- 규정 v2.0: 업종 코드 또는 라벨 (예: RESTAURANT, BAR, GOLF). Aura 전달용.
	user_id int8 NULL, -- 전표 소유자 식별자 (public.com_users.user_id)
	budget_exceeded_flag bpchar(1) DEFAULT 'N'::bpchar NULL, -- 예산 초과 여부 (Y/N)
	created_by int8 NULL, -- 생성자 user_id (논리적 참조: com_users.user_id)
	updated_by int8 NULL, -- 수정자 user_id (논리적 참조: com_users.user_id)
	CONSTRAINT fi_doc_header_pkey PRIMARY KEY (tenant_id, bukrs, belnr, gjahr),
	CONSTRAINT fi_doc_header_raw_event_id_fkey FOREIGN KEY (raw_event_id) REFERENCES dwp_aura.sap_raw_events(id) ON DELETE SET NULL
);
CREATE INDEX idx_fi_doc_header_owner ON dwp_aura.fi_doc_header USING btree (user_id, tenant_id);
CREATE INDEX ix_fi_doc_header_budat ON dwp_aura.fi_doc_header USING btree (tenant_id, budat);
CREATE INDEX ix_fi_doc_header_tenant_bukrs_gjahr_belnr ON dwp_aura.fi_doc_header USING btree (tenant_id, bukrs, gjahr, belnr);
CREATE INDEX ix_fi_doc_header_user_time ON dwp_aura.fi_doc_header USING btree (tenant_id, usnam, cpudt);
CREATE INDEX ix_fi_doc_header_xblnr ON dwp_aura.fi_doc_header USING btree (tenant_id, xblnr);
COMMENT ON TABLE dwp_aura.fi_doc_header IS 'FI 전표 헤더 (Canonical). SAP ECC/S4 전표 원천.';

-- Column comments

COMMENT ON COLUMN dwp_aura.fi_doc_header.tenant_id IS '테넌트 식별자';
COMMENT ON COLUMN dwp_aura.fi_doc_header.bukrs IS '회사코드 (SAP BUKRS)';
COMMENT ON COLUMN dwp_aura.fi_doc_header.belnr IS '전표번호 (SAP BELNR)';
COMMENT ON COLUMN dwp_aura.fi_doc_header.gjahr IS '회계연도 (SAP GJAHR)';
COMMENT ON COLUMN dwp_aura.fi_doc_header.doc_source IS '전표 원천 (SAP, MANUAL 등)';
COMMENT ON COLUMN dwp_aura.fi_doc_header.budat IS '전기일 (Posting Date)';
COMMENT ON COLUMN dwp_aura.fi_doc_header.bldat IS '증빙일 (Document Date)';
COMMENT ON COLUMN dwp_aura.fi_doc_header.cpudt IS '처리일 (CPU Date)';
COMMENT ON COLUMN dwp_aura.fi_doc_header.cputm IS '처리시간 (CPU Time)';
COMMENT ON COLUMN dwp_aura.fi_doc_header.usnam IS '생성자 (User Name)';
COMMENT ON COLUMN dwp_aura.fi_doc_header.tcode IS '트랜잭션 코드 (SAP TCODE)';
COMMENT ON COLUMN dwp_aura.fi_doc_header.blart IS '전표유형 (Document Type)';
COMMENT ON COLUMN dwp_aura.fi_doc_header.waers IS '통화 (Currency)';
COMMENT ON COLUMN dwp_aura.fi_doc_header.kursf IS '환율';
COMMENT ON COLUMN dwp_aura.fi_doc_header.xblnr IS '참조번호 (External Document No)';
COMMENT ON COLUMN dwp_aura.fi_doc_header.bktxt IS '헤더텍스트';
COMMENT ON COLUMN dwp_aura.fi_doc_header.status_code IS '전표 상태 (POSTED, PARKED 등)';
COMMENT ON COLUMN dwp_aura.fi_doc_header.reversal_belnr IS '역분개 전표번호';
COMMENT ON COLUMN dwp_aura.fi_doc_header.last_change_ts IS '마지막 변경 시각';
COMMENT ON COLUMN dwp_aura.fi_doc_header.raw_event_id IS '원천 Raw 이벤트 ID (FK)';
COMMENT ON COLUMN dwp_aura.fi_doc_header.created_at IS '생성일시 (Detect 배치 윈도우 기준)';
COMMENT ON COLUMN dwp_aura.fi_doc_header.updated_at IS '수정일시';
COMMENT ON COLUMN dwp_aura.fi_doc_header.intended_risk_type IS '데모 생성 시 시나리오 유형(예: SPLIT_PAYMENT, HOLIDAY_USAGE). Aura 엔진에 evidence_json.intended_risk_type으로 전달.';
COMMENT ON COLUMN dwp_aura.fi_doc_header.hr_status IS '규정 v2.0: 근무/휴가 (WORK, LEAVE 등). Aura evidence_json/metadata 전달용.';
COMMENT ON COLUMN dwp_aura.fi_doc_header.mcc_code IS '규정 v2.0: 업종 코드 또는 라벨 (예: RESTAURANT, BAR, GOLF). Aura 전달용.';
COMMENT ON COLUMN dwp_aura.fi_doc_header.user_id IS '전표 소유자 식별자 (public.com_users.user_id)';
COMMENT ON COLUMN dwp_aura.fi_doc_header.budget_exceeded_flag IS '예산 초과 여부 (Y/N)';
COMMENT ON COLUMN dwp_aura.fi_doc_header.created_by IS '생성자 user_id (논리적 참조: com_users.user_id)';
COMMENT ON COLUMN dwp_aura.fi_doc_header.updated_by IS '수정자 user_id (논리적 참조: com_users.user_id)';

-- Permissions

ALTER TABLE dwp_aura.fi_doc_header OWNER TO dwp_user;
GRANT ALL ON TABLE dwp_aura.fi_doc_header TO dwp_user;


-- dwp_aura.fi_doc_item definition

-- Drop table

-- DROP TABLE dwp_aura.fi_doc_item;

CREATE TABLE dwp_aura.fi_doc_item (
	tenant_id int8 NOT NULL, -- 테넌트 식별자
	bukrs varchar(4) NOT NULL, -- 회사코드
	belnr varchar(10) NOT NULL, -- 전표번호
	gjahr varchar(4) NOT NULL, -- 회계연도
	buzei varchar(3) NOT NULL, -- 라인번호 (Line Item)
	hkont varchar(10) NOT NULL, -- 계정 (G/L Account)
	bschl varchar(2) NULL, -- 전기키 (Posting Key)
	shkzg varchar(1) NULL, -- 차대구분 (S=차변, H=대변)
	lifnr varchar(20) NULL, -- 공급업체코드 (Vendor)
	kunnr varchar(20) NULL, -- 고객코드 (Customer)
	wrbtr numeric(18, 2) NULL, -- 금액 (Transaction Currency)
	dmbtr numeric(18, 2) NULL, -- 로컬통화 금액
	waers varchar(5) NULL, -- 통화
	mwskz varchar(2) NULL, -- 부가세코드
	kostl varchar(10) NULL, -- 코스트센터
	prctr varchar(10) NULL, -- 손익센터
	aufnr varchar(12) NULL, -- 오더번호
	zterm varchar(4) NULL, -- 지급조건
	zfbdt date NULL, -- 기본만기일
	due_date date NULL, -- 만기일
	payment_block bool DEFAULT false NOT NULL, -- 지급블록 여부
	dispute_flag bool DEFAULT false NOT NULL, -- 분쟁 플래그
	zuonr varchar(18) NULL, -- 할당번호
	sgtxt varchar(200) NULL, -- 라인텍스트
	last_change_ts timestamptz NULL, -- 마지막 변경 시각
	raw_event_id int8 NULL, -- 원천 Raw 이벤트 ID (FK)
	created_at timestamptz DEFAULT now() NOT NULL, -- 생성일시
	CONSTRAINT fi_doc_item_pkey PRIMARY KEY (tenant_id, bukrs, belnr, gjahr, buzei),
	CONSTRAINT fi_doc_item_raw_event_id_fkey FOREIGN KEY (raw_event_id) REFERENCES dwp_aura.sap_raw_events(id) ON DELETE SET NULL,
	CONSTRAINT fi_doc_item_tenant_id_bukrs_belnr_gjahr_fkey FOREIGN KEY (tenant_id,bukrs,belnr,gjahr) REFERENCES dwp_aura.fi_doc_header(tenant_id,bukrs,belnr,gjahr) ON DELETE CASCADE
);
CREATE INDEX ix_fi_doc_item_amount ON dwp_aura.fi_doc_item USING btree (tenant_id, wrbtr);
CREATE INDEX ix_fi_doc_item_hkont ON dwp_aura.fi_doc_item USING btree (tenant_id, hkont);
CREATE INDEX ix_fi_doc_item_partner ON dwp_aura.fi_doc_item USING btree (tenant_id, lifnr, kunnr);
COMMENT ON TABLE dwp_aura.fi_doc_item IS 'FI 전표 라인 (Canonical). fi_doc_header 자식.';

-- Column comments

COMMENT ON COLUMN dwp_aura.fi_doc_item.tenant_id IS '테넌트 식별자';
COMMENT ON COLUMN dwp_aura.fi_doc_item.bukrs IS '회사코드';
COMMENT ON COLUMN dwp_aura.fi_doc_item.belnr IS '전표번호';
COMMENT ON COLUMN dwp_aura.fi_doc_item.gjahr IS '회계연도';
COMMENT ON COLUMN dwp_aura.fi_doc_item.buzei IS '라인번호 (Line Item)';
COMMENT ON COLUMN dwp_aura.fi_doc_item.hkont IS '계정 (G/L Account)';
COMMENT ON COLUMN dwp_aura.fi_doc_item.bschl IS '전기키 (Posting Key)';
COMMENT ON COLUMN dwp_aura.fi_doc_item.shkzg IS '차대구분 (S=차변, H=대변)';
COMMENT ON COLUMN dwp_aura.fi_doc_item.lifnr IS '공급업체코드 (Vendor)';
COMMENT ON COLUMN dwp_aura.fi_doc_item.kunnr IS '고객코드 (Customer)';
COMMENT ON COLUMN dwp_aura.fi_doc_item.wrbtr IS '금액 (Transaction Currency)';
COMMENT ON COLUMN dwp_aura.fi_doc_item.dmbtr IS '로컬통화 금액';
COMMENT ON COLUMN dwp_aura.fi_doc_item.waers IS '통화';
COMMENT ON COLUMN dwp_aura.fi_doc_item.mwskz IS '부가세코드';
COMMENT ON COLUMN dwp_aura.fi_doc_item.kostl IS '코스트센터';
COMMENT ON COLUMN dwp_aura.fi_doc_item.prctr IS '손익센터';
COMMENT ON COLUMN dwp_aura.fi_doc_item.aufnr IS '오더번호';
COMMENT ON COLUMN dwp_aura.fi_doc_item.zterm IS '지급조건';
COMMENT ON COLUMN dwp_aura.fi_doc_item.zfbdt IS '기본만기일';
COMMENT ON COLUMN dwp_aura.fi_doc_item.due_date IS '만기일';
COMMENT ON COLUMN dwp_aura.fi_doc_item.payment_block IS '지급블록 여부';
COMMENT ON COLUMN dwp_aura.fi_doc_item.dispute_flag IS '분쟁 플래그';
COMMENT ON COLUMN dwp_aura.fi_doc_item.zuonr IS '할당번호';
COMMENT ON COLUMN dwp_aura.fi_doc_item.sgtxt IS '라인텍스트';
COMMENT ON COLUMN dwp_aura.fi_doc_item.last_change_ts IS '마지막 변경 시각';
COMMENT ON COLUMN dwp_aura.fi_doc_item.raw_event_id IS '원천 Raw 이벤트 ID (FK)';
COMMENT ON COLUMN dwp_aura.fi_doc_item.created_at IS '생성일시';

-- Permissions

ALTER TABLE dwp_aura.fi_doc_item OWNER TO dwp_user;
GRANT ALL ON TABLE dwp_aura.fi_doc_item TO dwp_user;


-- dwp_aura.fi_open_item definition

-- Drop table

-- DROP TABLE dwp_aura.fi_open_item;

CREATE TABLE dwp_aura.fi_open_item (
	tenant_id int8 NOT NULL, -- 테넌트 식별자
	bukrs varchar(4) NOT NULL, -- 회사코드
	belnr varchar(10) NOT NULL, -- 전표번호
	gjahr varchar(4) NOT NULL, -- 회계연도
	buzei varchar(3) NOT NULL, -- 라인번호
	item_type varchar(10) NOT NULL, -- 유형 (AP=매입채무, AR=매출채권)
	lifnr varchar(20) NULL, -- 공급업체코드
	kunnr varchar(20) NULL, -- 고객코드
	baseline_date date NULL, -- 기준일
	zterm varchar(4) NULL, -- 지급조건
	due_date date NOT NULL, -- 만기일
	open_amount numeric(18, 2) NOT NULL, -- 미결금액
	currency varchar(5) NOT NULL, -- 통화
	cleared bool DEFAULT false NOT NULL, -- 청산 여부
	clearing_date date NULL, -- 청산일
	payment_block bool DEFAULT false NOT NULL, -- 지급블록 여부
	dispute_flag bool DEFAULT false NOT NULL, -- 분쟁 플래그
	last_change_ts timestamptz NULL, -- 마지막 변경 시각
	raw_event_id int8 NULL, -- 원천 Raw 이벤트 ID (FK)
	last_update_ts timestamptz DEFAULT now() NOT NULL, -- 마지막 업데이트 시각 (Detect 배치 윈도우 기준)
	CONSTRAINT fi_open_item_pkey PRIMARY KEY (tenant_id, bukrs, belnr, gjahr, buzei),
	CONSTRAINT fi_open_item_raw_event_id_fkey FOREIGN KEY (raw_event_id) REFERENCES dwp_aura.sap_raw_events(id) ON DELETE SET NULL
);
CREATE INDEX ix_fi_open_item_due ON dwp_aura.fi_open_item USING btree (tenant_id, due_date, cleared);
CREATE INDEX ix_fi_open_item_partner ON dwp_aura.fi_open_item USING btree (tenant_id, item_type, lifnr, kunnr);
CREATE INDEX ix_fi_open_item_tenant_type_due ON dwp_aura.fi_open_item USING btree (tenant_id, item_type, due_date) WHERE (cleared = false);
COMMENT ON TABLE dwp_aura.fi_open_item IS 'FI 미결항목 (AP/AR). Open Item Management.';

-- Column comments

COMMENT ON COLUMN dwp_aura.fi_open_item.tenant_id IS '테넌트 식별자';
COMMENT ON COLUMN dwp_aura.fi_open_item.bukrs IS '회사코드';
COMMENT ON COLUMN dwp_aura.fi_open_item.belnr IS '전표번호';
COMMENT ON COLUMN dwp_aura.fi_open_item.gjahr IS '회계연도';
COMMENT ON COLUMN dwp_aura.fi_open_item.buzei IS '라인번호';
COMMENT ON COLUMN dwp_aura.fi_open_item.item_type IS '유형 (AP=매입채무, AR=매출채권)';
COMMENT ON COLUMN dwp_aura.fi_open_item.lifnr IS '공급업체코드';
COMMENT ON COLUMN dwp_aura.fi_open_item.kunnr IS '고객코드';
COMMENT ON COLUMN dwp_aura.fi_open_item.baseline_date IS '기준일';
COMMENT ON COLUMN dwp_aura.fi_open_item.zterm IS '지급조건';
COMMENT ON COLUMN dwp_aura.fi_open_item.due_date IS '만기일';
COMMENT ON COLUMN dwp_aura.fi_open_item.open_amount IS '미결금액';
COMMENT ON COLUMN dwp_aura.fi_open_item.currency IS '통화';
COMMENT ON COLUMN dwp_aura.fi_open_item.cleared IS '청산 여부';
COMMENT ON COLUMN dwp_aura.fi_open_item.clearing_date IS '청산일';
COMMENT ON COLUMN dwp_aura.fi_open_item.payment_block IS '지급블록 여부';
COMMENT ON COLUMN dwp_aura.fi_open_item.dispute_flag IS '분쟁 플래그';
COMMENT ON COLUMN dwp_aura.fi_open_item.last_change_ts IS '마지막 변경 시각';
COMMENT ON COLUMN dwp_aura.fi_open_item.raw_event_id IS '원천 Raw 이벤트 ID (FK)';
COMMENT ON COLUMN dwp_aura.fi_open_item.last_update_ts IS '마지막 업데이트 시각 (Detect 배치 윈도우 기준)';

-- Permissions

ALTER TABLE dwp_aura.fi_open_item OWNER TO dwp_user;
GRANT ALL ON TABLE dwp_aura.fi_open_item TO dwp_user;


-- dwp_aura.ingestion_errors definition

-- Drop table

-- DROP TABLE dwp_aura.ingestion_errors;

CREATE TABLE dwp_aura.ingestion_errors (
	id bigserial NOT NULL, -- 오류 로그 식별자 (PK)
	raw_event_id int8 NULL, -- 원본 Raw 이벤트 ID (FK: sap_raw_events.id)
	tenant_id int8 NOT NULL, -- 테넌트 식별자
	dataset_id text NOT NULL, -- 데이터셋 ID (fi_doc_header, fi_open_item 등)
	record_key text NULL, -- 레코드 키 (적재 실패 레코드 식별)
	error_code text NOT NULL, -- 오류 코드
	error_detail text NOT NULL, -- 오류 상세
	record_json jsonb NULL, -- 실패 레코드 원본 (JSONB)
	created_at timestamptz DEFAULT now() NOT NULL, -- 발생 일시
	CONSTRAINT ingestion_errors_pkey PRIMARY KEY (id),
	CONSTRAINT ingestion_errors_raw_event_id_fkey FOREIGN KEY (raw_event_id) REFERENCES dwp_aura.sap_raw_events(id) ON DELETE SET NULL
);
CREATE INDEX ix_ingestion_errors_tenant_time ON dwp_aura.ingestion_errors USING btree (tenant_id, created_at DESC);
COMMENT ON TABLE dwp_aura.ingestion_errors IS '적재 오류 로그. Raw 이벤트 처리 실패 시 기록.';

-- Column comments

COMMENT ON COLUMN dwp_aura.ingestion_errors.id IS '오류 로그 식별자 (PK)';
COMMENT ON COLUMN dwp_aura.ingestion_errors.raw_event_id IS '원본 Raw 이벤트 ID (FK: sap_raw_events.id)';
COMMENT ON COLUMN dwp_aura.ingestion_errors.tenant_id IS '테넌트 식별자';
COMMENT ON COLUMN dwp_aura.ingestion_errors.dataset_id IS '데이터셋 ID (fi_doc_header, fi_open_item 등)';
COMMENT ON COLUMN dwp_aura.ingestion_errors.record_key IS '레코드 키 (적재 실패 레코드 식별)';
COMMENT ON COLUMN dwp_aura.ingestion_errors.error_code IS '오류 코드';
COMMENT ON COLUMN dwp_aura.ingestion_errors.error_detail IS '오류 상세';
COMMENT ON COLUMN dwp_aura.ingestion_errors.record_json IS '실패 레코드 원본 (JSONB)';
COMMENT ON COLUMN dwp_aura.ingestion_errors.created_at IS '발생 일시';

-- Permissions

ALTER TABLE dwp_aura.ingestion_errors OWNER TO dwp_user;
GRANT ALL ON TABLE dwp_aura.ingestion_errors TO dwp_user;


-- dwp_aura.policy_action_guardrail definition

-- Drop table

-- DROP TABLE dwp_aura.policy_action_guardrail;

CREATE TABLE dwp_aura.policy_action_guardrail (
	guardrail_id bigserial NOT NULL, -- 가드레일 식별자 (PK)
	tenant_id int8 NOT NULL, -- 테넌트 식별자 (논리적 참조: com_tenants.tenant_id)
	profile_id int8 NOT NULL, -- 프로파일 식별자 (논리적 참조: config_profile.profile_id)
	severity text NOT NULL, -- 심각도(LOW/MEDIUM/HIGH)
	allow_actions jsonb NOT NULL, -- 허용 조치 목록(JSON 배열, 예: SEND_NUDGE,CREATE_TICKET)
	require_human_approval bool DEFAULT true NOT NULL, -- 인가 승인 필수 여부
	created_at timestamptz DEFAULT now() NOT NULL, -- 생성일시
	created_by int8 NULL, -- 생성자 user_id (논리적 참조: com_users.user_id)
	updated_at timestamptz DEFAULT now() NOT NULL, -- 수정일시
	updated_by int8 NULL, -- 수정자 user_id (논리적 참조: com_users.user_id)
	CONSTRAINT policy_action_guardrail_pkey PRIMARY KEY (guardrail_id),
	CONSTRAINT policy_action_guardrail_tenant_id_profile_id_severity_key UNIQUE (tenant_id, profile_id, severity),
	CONSTRAINT policy_action_guardrail_profile_id_fkey FOREIGN KEY (profile_id) REFERENCES dwp_aura.config_profile(profile_id) ON DELETE CASCADE
);
CREATE INDEX ix_policy_action_guardrail_tenant_id ON dwp_aura.policy_action_guardrail USING btree (tenant_id);
COMMENT ON TABLE dwp_aura.policy_action_guardrail IS '조치 정책(Severity별 허용 조치·인가 승인). Agentic AI 가드레일.';

-- Column comments

COMMENT ON COLUMN dwp_aura.policy_action_guardrail.guardrail_id IS '가드레일 식별자 (PK)';
COMMENT ON COLUMN dwp_aura.policy_action_guardrail.tenant_id IS '테넌트 식별자 (논리적 참조: com_tenants.tenant_id)';
COMMENT ON COLUMN dwp_aura.policy_action_guardrail.profile_id IS '프로파일 식별자 (논리적 참조: config_profile.profile_id)';
COMMENT ON COLUMN dwp_aura.policy_action_guardrail.severity IS '심각도(LOW/MEDIUM/HIGH)';
COMMENT ON COLUMN dwp_aura.policy_action_guardrail.allow_actions IS '허용 조치 목록(JSON 배열, 예: SEND_NUDGE,CREATE_TICKET)';
COMMENT ON COLUMN dwp_aura.policy_action_guardrail.require_human_approval IS '인가 승인 필수 여부';
COMMENT ON COLUMN dwp_aura.policy_action_guardrail.created_at IS '생성일시';
COMMENT ON COLUMN dwp_aura.policy_action_guardrail.created_by IS '생성자 user_id (논리적 참조: com_users.user_id)';
COMMENT ON COLUMN dwp_aura.policy_action_guardrail.updated_at IS '수정일시';
COMMENT ON COLUMN dwp_aura.policy_action_guardrail.updated_by IS '수정자 user_id (논리적 참조: com_users.user_id)';

-- Permissions

ALTER TABLE dwp_aura.policy_action_guardrail OWNER TO dwp_user;
GRANT ALL ON TABLE dwp_aura.policy_action_guardrail TO dwp_user;


-- dwp_aura.policy_data_protection definition

-- Drop table

-- DROP TABLE dwp_aura.policy_data_protection;

CREATE TABLE dwp_aura.policy_data_protection (
	protection_id bigserial NOT NULL, -- 보호 정책 식별자 (PK)
	tenant_id int8 NOT NULL, -- 테넌트 식별자
	profile_id int8 NOT NULL, -- 프로파일 ID (FK)
	at_rest_encryption_enabled bool DEFAULT false NOT NULL, -- 저장 시 암호화 여부
	key_provider varchar(20) DEFAULT 'KMS_MOCK'::character varying NOT NULL, -- KMS_MOCK | KMS | HSM
	audit_retention_years int4 DEFAULT 7 NOT NULL, -- 감사 보존 연수
	export_requires_approval bool DEFAULT true NOT NULL, -- 내보내기 승인 필수 여부
	export_mode varchar(20) DEFAULT 'ZIP'::character varying NOT NULL, -- ZIP | CSV
	updated_at timestamptz DEFAULT now() NOT NULL, -- 수정일시
	kms_mode text DEFAULT 'KMS_MANAGED_KEYS'::text NOT NULL, -- KMS 모드
	CONSTRAINT policy_data_protection_pkey PRIMARY KEY (protection_id),
	CONSTRAINT policy_data_protection_tenant_id_profile_id_key UNIQUE (tenant_id, profile_id),
	CONSTRAINT policy_data_protection_profile_id_fkey FOREIGN KEY (profile_id) REFERENCES dwp_aura.config_profile(profile_id) ON DELETE CASCADE
);
CREATE INDEX ix_policy_data_protection_tenant ON dwp_aura.policy_data_protection USING btree (tenant_id);
COMMENT ON TABLE dwp_aura.policy_data_protection IS '데이터 보호 정책(암호화, 보존기간, 내보내기 제어).';

-- Column comments

COMMENT ON COLUMN dwp_aura.policy_data_protection.protection_id IS '보호 정책 식별자 (PK)';
COMMENT ON COLUMN dwp_aura.policy_data_protection.tenant_id IS '테넌트 식별자';
COMMENT ON COLUMN dwp_aura.policy_data_protection.profile_id IS '프로파일 ID (FK)';
COMMENT ON COLUMN dwp_aura.policy_data_protection.at_rest_encryption_enabled IS '저장 시 암호화 여부';
COMMENT ON COLUMN dwp_aura.policy_data_protection.key_provider IS 'KMS_MOCK | KMS | HSM';
COMMENT ON COLUMN dwp_aura.policy_data_protection.audit_retention_years IS '감사 보존 연수';
COMMENT ON COLUMN dwp_aura.policy_data_protection.export_requires_approval IS '내보내기 승인 필수 여부';
COMMENT ON COLUMN dwp_aura.policy_data_protection.export_mode IS 'ZIP | CSV';
COMMENT ON COLUMN dwp_aura.policy_data_protection.updated_at IS '수정일시';
COMMENT ON COLUMN dwp_aura.policy_data_protection.kms_mode IS 'KMS 모드';

-- Permissions

ALTER TABLE dwp_aura.policy_data_protection OWNER TO dwp_user;
GRANT ALL ON TABLE dwp_aura.policy_data_protection TO dwp_user;


-- dwp_aura.policy_notification_channel definition

-- Drop table

-- DROP TABLE dwp_aura.policy_notification_channel;

CREATE TABLE dwp_aura.policy_notification_channel (
	channel_id bigserial NOT NULL, -- 채널 식별자 (PK)
	tenant_id int8 NOT NULL, -- 테넌트 식별자 (논리적 참조: com_tenants.tenant_id)
	profile_id int8 NOT NULL, -- 프로파일 식별자 (논리적 참조: config_profile.profile_id)
	channel_type text NOT NULL, -- 채널 유형(EMAIL|SMS|MESSENGER|PORTAL|WEBHOOK)
	is_enabled bool DEFAULT true NOT NULL, -- 활성 여부
	config_json jsonb NOT NULL, -- 채널 설정(endpoint, auth, templates, throttle 등 JSON)
	created_at timestamptz DEFAULT now() NOT NULL, -- 생성일시
	created_by int8 NULL, -- 생성자 user_id (논리적 참조: com_users.user_id)
	updated_at timestamptz DEFAULT now() NOT NULL, -- 수정일시
	updated_by int8 NULL, -- 수정자 user_id (논리적 참조: com_users.user_id)
	CONSTRAINT policy_notification_channel_pkey PRIMARY KEY (channel_id),
	CONSTRAINT policy_notification_channel_tenant_id_profile_id_channel_ty_key UNIQUE (tenant_id, profile_id, channel_type),
	CONSTRAINT policy_notification_channel_profile_id_fkey FOREIGN KEY (profile_id) REFERENCES dwp_aura.config_profile(profile_id) ON DELETE CASCADE
);
CREATE INDEX ix_policy_notification_channel_tenant_id ON dwp_aura.policy_notification_channel USING btree (tenant_id);
COMMENT ON TABLE dwp_aura.policy_notification_channel IS '알림 채널 정책(고객사별 옵션). EMAIL|SMS|MESSENGER|PORTAL|WEBHOOK 등.';

-- Column comments

COMMENT ON COLUMN dwp_aura.policy_notification_channel.channel_id IS '채널 식별자 (PK)';
COMMENT ON COLUMN dwp_aura.policy_notification_channel.tenant_id IS '테넌트 식별자 (논리적 참조: com_tenants.tenant_id)';
COMMENT ON COLUMN dwp_aura.policy_notification_channel.profile_id IS '프로파일 식별자 (논리적 참조: config_profile.profile_id)';
COMMENT ON COLUMN dwp_aura.policy_notification_channel.channel_type IS '채널 유형(EMAIL|SMS|MESSENGER|PORTAL|WEBHOOK)';
COMMENT ON COLUMN dwp_aura.policy_notification_channel.is_enabled IS '활성 여부';
COMMENT ON COLUMN dwp_aura.policy_notification_channel.config_json IS '채널 설정(endpoint, auth, templates, throttle 등 JSON)';
COMMENT ON COLUMN dwp_aura.policy_notification_channel.created_at IS '생성일시';
COMMENT ON COLUMN dwp_aura.policy_notification_channel.created_by IS '생성자 user_id (논리적 참조: com_users.user_id)';
COMMENT ON COLUMN dwp_aura.policy_notification_channel.updated_at IS '수정일시';
COMMENT ON COLUMN dwp_aura.policy_notification_channel.updated_by IS '수정자 user_id (논리적 참조: com_users.user_id)';

-- Permissions

ALTER TABLE dwp_aura.policy_notification_channel OWNER TO dwp_user;
GRANT ALL ON TABLE dwp_aura.policy_notification_channel TO dwp_user;


-- dwp_aura.policy_pii_field definition

-- Drop table

-- DROP TABLE dwp_aura.policy_pii_field;

CREATE TABLE dwp_aura.policy_pii_field (
	pii_id bigserial NOT NULL, -- PII 정책 식별자 (PK)
	tenant_id int8 NOT NULL, -- 테넌트 식별자 (논리적 참조: com_tenants.tenant_id)
	profile_id int8 NOT NULL, -- 프로파일 식별자 (논리적 참조: config_profile.profile_id)
	field_name text NOT NULL, -- 필드명
	handling text NOT NULL, -- 저장 정책(ALLOW|MASK|HASH_ONLY|ENCRYPT|FORBID)
	note text NULL, -- 비고
	created_at timestamptz DEFAULT now() NOT NULL, -- 생성일시
	created_by int8 NULL, -- 생성자 user_id (논리적 참조: com_users.user_id)
	updated_at timestamptz DEFAULT now() NOT NULL, -- 수정일시
	updated_by int8 NULL, -- 수정자 user_id (논리적 참조: com_users.user_id)
	mask_rule text NULL, -- 마스킹 규칙(예: PARTIAL_4_4, FULL)
	hash_rule text NULL, -- 해시 규칙(예: SHA256)
	encrypt_rule text NULL, -- 암호화 규칙(예: AES256)
	CONSTRAINT policy_pii_field_pkey PRIMARY KEY (pii_id),
	CONSTRAINT policy_pii_field_tenant_id_profile_id_field_name_key UNIQUE (tenant_id, profile_id, field_name),
	CONSTRAINT policy_pii_field_profile_id_fkey FOREIGN KEY (profile_id) REFERENCES dwp_aura.config_profile(profile_id) ON DELETE CASCADE
);
CREATE INDEX ix_policy_pii_field_tenant_id ON dwp_aura.policy_pii_field USING btree (tenant_id);
COMMENT ON TABLE dwp_aura.policy_pii_field IS 'PII 정책(필드별 저장 정책). 운영 전환 시 ALLOW→ENCRYPT/HASH_ONLY 등 강화 가능.';

-- Column comments

COMMENT ON COLUMN dwp_aura.policy_pii_field.pii_id IS 'PII 정책 식별자 (PK)';
COMMENT ON COLUMN dwp_aura.policy_pii_field.tenant_id IS '테넌트 식별자 (논리적 참조: com_tenants.tenant_id)';
COMMENT ON COLUMN dwp_aura.policy_pii_field.profile_id IS '프로파일 식별자 (논리적 참조: config_profile.profile_id)';
COMMENT ON COLUMN dwp_aura.policy_pii_field.field_name IS '필드명';
COMMENT ON COLUMN dwp_aura.policy_pii_field.handling IS '저장 정책(ALLOW|MASK|HASH_ONLY|ENCRYPT|FORBID)';
COMMENT ON COLUMN dwp_aura.policy_pii_field.note IS '비고';
COMMENT ON COLUMN dwp_aura.policy_pii_field.created_at IS '생성일시';
COMMENT ON COLUMN dwp_aura.policy_pii_field.created_by IS '생성자 user_id (논리적 참조: com_users.user_id)';
COMMENT ON COLUMN dwp_aura.policy_pii_field.updated_at IS '수정일시';
COMMENT ON COLUMN dwp_aura.policy_pii_field.updated_by IS '수정자 user_id (논리적 참조: com_users.user_id)';
COMMENT ON COLUMN dwp_aura.policy_pii_field.mask_rule IS '마스킹 규칙(예: PARTIAL_4_4, FULL)';
COMMENT ON COLUMN dwp_aura.policy_pii_field.hash_rule IS '해시 규칙(예: SHA256)';
COMMENT ON COLUMN dwp_aura.policy_pii_field.encrypt_rule IS '암호화 규칙(예: AES256)';

-- Permissions

ALTER TABLE dwp_aura.policy_pii_field OWNER TO dwp_user;
GRANT ALL ON TABLE dwp_aura.policy_pii_field TO dwp_user;


-- dwp_aura.policy_scope_company definition

-- Drop table

-- DROP TABLE dwp_aura.policy_scope_company;

CREATE TABLE dwp_aura.policy_scope_company (
	scope_id bigserial NOT NULL, -- 스코프 식별자 (PK)
	tenant_id int8 NOT NULL, -- 테넌트 식별자
	profile_id int8 NOT NULL, -- 프로파일 ID (FK)
	bukrs varchar(4) NOT NULL, -- 회사코드
	included bool DEFAULT true NOT NULL, -- 스코프 포함 여부
	created_at timestamptz DEFAULT now() NOT NULL, -- 생성일시
	created_by int8 NULL, -- 생성자 user_id
	updated_at timestamptz DEFAULT now() NOT NULL, -- 수정일시
	updated_by int8 NULL, -- 수정자 user_id
	CONSTRAINT policy_scope_company_pkey PRIMARY KEY (scope_id),
	CONSTRAINT policy_scope_company_tenant_id_profile_id_bukrs_key UNIQUE (tenant_id, profile_id, bukrs),
	CONSTRAINT policy_scope_company_profile_id_fkey FOREIGN KEY (profile_id) REFERENCES dwp_aura.config_profile(profile_id) ON DELETE CASCADE
);
CREATE INDEX ix_policy_scope_company_tenant_profile ON dwp_aura.policy_scope_company USING btree (tenant_id, profile_id);
COMMENT ON TABLE dwp_aura.policy_scope_company IS 'Profile별 회사코드(BUKRS) 스코프. included=true면 scope 내.';

-- Column comments

COMMENT ON COLUMN dwp_aura.policy_scope_company.scope_id IS '스코프 식별자 (PK)';
COMMENT ON COLUMN dwp_aura.policy_scope_company.tenant_id IS '테넌트 식별자';
COMMENT ON COLUMN dwp_aura.policy_scope_company.profile_id IS '프로파일 ID (FK)';
COMMENT ON COLUMN dwp_aura.policy_scope_company.bukrs IS '회사코드';
COMMENT ON COLUMN dwp_aura.policy_scope_company.included IS '스코프 포함 여부';
COMMENT ON COLUMN dwp_aura.policy_scope_company.created_at IS '생성일시';
COMMENT ON COLUMN dwp_aura.policy_scope_company.created_by IS '생성자 user_id';
COMMENT ON COLUMN dwp_aura.policy_scope_company.updated_at IS '수정일시';
COMMENT ON COLUMN dwp_aura.policy_scope_company.updated_by IS '수정자 user_id';

-- Permissions

ALTER TABLE dwp_aura.policy_scope_company OWNER TO dwp_user;
GRANT ALL ON TABLE dwp_aura.policy_scope_company TO dwp_user;


-- dwp_aura.policy_scope_currency definition

-- Drop table

-- DROP TABLE dwp_aura.policy_scope_currency;

CREATE TABLE dwp_aura.policy_scope_currency (
	scope_id bigserial NOT NULL, -- 스코프 식별자 (PK)
	tenant_id int8 NOT NULL, -- 테넌트 식별자
	profile_id int8 NOT NULL, -- 프로파일 ID (FK)
	currency_code varchar(5) NOT NULL, -- 통화 코드 (FK)
	included bool DEFAULT true NOT NULL, -- 스코프 포함 여부
	created_at timestamptz DEFAULT now() NOT NULL, -- 생성일시
	created_by int8 NULL, -- 생성자 user_id
	updated_at timestamptz DEFAULT now() NOT NULL, -- 수정일시
	updated_by int8 NULL, -- 수정자 user_id
	fx_control_mode varchar(16) DEFAULT 'ALLOW'::character varying NOT NULL, -- ALLOW | FX_REQUIRED | FX_LOCKED
	CONSTRAINT policy_scope_currency_pkey PRIMARY KEY (scope_id),
	CONSTRAINT policy_scope_currency_tenant_id_profile_id_currency_code_key UNIQUE (tenant_id, profile_id, currency_code),
	CONSTRAINT policy_scope_currency_currency_code_fkey FOREIGN KEY (currency_code) REFERENCES dwp_aura.md_currency(currency_code),
	CONSTRAINT policy_scope_currency_profile_id_fkey FOREIGN KEY (profile_id) REFERENCES dwp_aura.config_profile(profile_id) ON DELETE CASCADE
);
CREATE INDEX ix_policy_scope_currency_tenant_profile ON dwp_aura.policy_scope_currency USING btree (tenant_id, profile_id);
COMMENT ON TABLE dwp_aura.policy_scope_currency IS 'Profile별 통화 스코프. included=true면 scope 내.';

-- Column comments

COMMENT ON COLUMN dwp_aura.policy_scope_currency.scope_id IS '스코프 식별자 (PK)';
COMMENT ON COLUMN dwp_aura.policy_scope_currency.tenant_id IS '테넌트 식별자';
COMMENT ON COLUMN dwp_aura.policy_scope_currency.profile_id IS '프로파일 ID (FK)';
COMMENT ON COLUMN dwp_aura.policy_scope_currency.currency_code IS '통화 코드 (FK)';
COMMENT ON COLUMN dwp_aura.policy_scope_currency.included IS '스코프 포함 여부';
COMMENT ON COLUMN dwp_aura.policy_scope_currency.created_at IS '생성일시';
COMMENT ON COLUMN dwp_aura.policy_scope_currency.created_by IS '생성자 user_id';
COMMENT ON COLUMN dwp_aura.policy_scope_currency.updated_at IS '수정일시';
COMMENT ON COLUMN dwp_aura.policy_scope_currency.updated_by IS '수정자 user_id';
COMMENT ON COLUMN dwp_aura.policy_scope_currency.fx_control_mode IS 'ALLOW | FX_REQUIRED | FX_LOCKED';

-- Permissions

ALTER TABLE dwp_aura.policy_scope_currency OWNER TO dwp_user;
GRANT ALL ON TABLE dwp_aura.policy_scope_currency TO dwp_user;


-- dwp_aura.policy_sod_rule definition

-- Drop table

-- DROP TABLE dwp_aura.policy_sod_rule;

CREATE TABLE dwp_aura.policy_sod_rule (
	tenant_id int8 NOT NULL, -- 테넌트 식별자
	profile_id int8 NOT NULL, -- 프로파일 ID (FK)
	rule_key text NOT NULL, -- 규칙 키
	title text NOT NULL, -- 규칙 제목
	description text DEFAULT ''::text NOT NULL, -- 규칙 설명
	is_enabled bool DEFAULT true NOT NULL, -- 활성 여부
	config_json jsonb DEFAULT '{}'::jsonb NOT NULL, -- 규칙 설정 (JSONB)
	created_at timestamptz DEFAULT now() NOT NULL, -- 생성일시
	created_by int8 NULL, -- 생성자 user_id
	updated_at timestamptz DEFAULT now() NOT NULL, -- 수정일시
	updated_by int8 NULL, -- 수정자 user_id
	severity varchar(16) DEFAULT 'WARN'::character varying NOT NULL, -- INFO | WARN | BLOCK
	CONSTRAINT policy_sod_rule_pkey PRIMARY KEY (tenant_id, profile_id, rule_key),
	CONSTRAINT policy_sod_rule_profile_id_fkey FOREIGN KEY (profile_id) REFERENCES dwp_aura.config_profile(profile_id) ON DELETE CASCADE
);
CREATE INDEX ix_policy_sod_rule_tenant_profile ON dwp_aura.policy_sod_rule USING btree (tenant_id, profile_id);
COMMENT ON TABLE dwp_aura.policy_sod_rule IS 'Profile별 SoD 규칙. NO_SELF_APPROVE, DUAL_CONTROL, FINANCE_VS_SECURITY 등.';

-- Column comments

COMMENT ON COLUMN dwp_aura.policy_sod_rule.tenant_id IS '테넌트 식별자';
COMMENT ON COLUMN dwp_aura.policy_sod_rule.profile_id IS '프로파일 ID (FK)';
COMMENT ON COLUMN dwp_aura.policy_sod_rule.rule_key IS '규칙 키';
COMMENT ON COLUMN dwp_aura.policy_sod_rule.title IS '규칙 제목';
COMMENT ON COLUMN dwp_aura.policy_sod_rule.description IS '규칙 설명';
COMMENT ON COLUMN dwp_aura.policy_sod_rule.is_enabled IS '활성 여부';
COMMENT ON COLUMN dwp_aura.policy_sod_rule.config_json IS '규칙 설정 (JSONB)';
COMMENT ON COLUMN dwp_aura.policy_sod_rule.created_at IS '생성일시';
COMMENT ON COLUMN dwp_aura.policy_sod_rule.created_by IS '생성자 user_id';
COMMENT ON COLUMN dwp_aura.policy_sod_rule.updated_at IS '수정일시';
COMMENT ON COLUMN dwp_aura.policy_sod_rule.updated_by IS '수정자 user_id';
COMMENT ON COLUMN dwp_aura.policy_sod_rule.severity IS 'INFO | WARN | BLOCK';

-- Permissions

ALTER TABLE dwp_aura.policy_sod_rule OWNER TO dwp_user;
GRANT ALL ON TABLE dwp_aura.policy_sod_rule TO dwp_user;


-- dwp_aura.policy_suggestion definition

-- Drop table

-- DROP TABLE dwp_aura.policy_suggestion;

CREATE TABLE dwp_aura.policy_suggestion (
	suggestion_id bigserial NOT NULL,
	tenant_id int8 NOT NULL,
	case_id int8 NULL,
	suggested_action varchar(100) NULL,
	suggested_rule text NULL,
	"comment" text NULL,
	created_by int8 NULL,
	created_at timestamptz DEFAULT now() NOT NULL,
	status varchar(20) DEFAULT 'PENDING'::character varying NOT NULL,
	CONSTRAINT policy_suggestion_pkey PRIMARY KEY (suggestion_id),
	CONSTRAINT policy_suggestion_case_id_fkey FOREIGN KEY (case_id) REFERENCES dwp_aura.agent_case(case_id) ON DELETE SET NULL
);
CREATE INDEX ix_policy_suggestion_tenant ON dwp_aura.policy_suggestion USING btree (tenant_id, created_at DESC);
COMMENT ON TABLE dwp_aura.policy_suggestion IS '정책 제안. feedback 확장.';

-- Permissions

ALTER TABLE dwp_aura.policy_suggestion OWNER TO dwp_user;
GRANT ALL ON TABLE dwp_aura.policy_suggestion TO dwp_user;


-- dwp_aura.rag_chunk definition

-- Drop table

-- DROP TABLE dwp_aura.rag_chunk;

CREATE TABLE dwp_aura.rag_chunk (
	chunk_id bigserial NOT NULL, -- 청크 식별자 (PK)
	tenant_id int8 NOT NULL, -- 테넌트 식별자
	doc_id int8 NOT NULL, -- 문서 ID (FK)
	chunk_text text NOT NULL, -- 청크 텍스트
	search_text text NULL, -- prefix 제거된 정제 본문 (BM25/임베딩 검색용)
	regulation_article varchar(100) NULL, -- 규정 조항 (예: "제11조", "제3장")
	regulation_clause varchar(100) NULL, -- 규정 항목 (예: "2항", "제1호")
	parent_article varchar(64) NULL, -- 상위 조문 번호
	parent_title varchar(255) NULL, -- 상위 조문 제목
	node_type varchar(20) NULL, -- 노드 유형: ARTICLE(조문), CLAUSE(항/호), PARAGRAPH(문단)
	parent_id int8 NULL, -- 부모 청크 ID (조문-항/호 Parent-Child 관계)
	parent_chunk_id varchar(128) NULL,
	child_index int4 NULL,
	chunk_level varchar(16) NULL,
	chunk_index int4 NULL, -- 문서 내 청크 순서 (추론 시 문맥 파악용)
	page_no int4 DEFAULT 1 NOT NULL, -- 페이지 번호
	embedding_id text NULL, -- 임베딩 ID (벡터 DB 연동)
	embedding public.vector NULL, -- OpenAI embedding 벡터 (1536차원, pgvector)
	search_tsv tsvector NULL, -- tsvector 컬럼 (BM25 검색 인덱스)
	metadata_json jsonb NULL, -- 페이지 번호, 파일 경로 등 부가 메타데이터
	"version" varchar(64) NULL,
	effective_from date NULL,
	effective_to date NULL,
	is_active bool DEFAULT true NOT NULL,
	created_at timestamptz DEFAULT now() NOT NULL, -- 생성일시
	CONSTRAINT chk_rag_chunk_level CHECK (((chunk_level IS NULL) OR ((chunk_level)::text = ANY ((ARRAY['root'::character varying, 'child'::character varying])::text[])))),
	CONSTRAINT rag_chunk_pkey PRIMARY KEY (chunk_id),
	CONSTRAINT rag_chunk_doc_id_fkey FOREIGN KEY (doc_id) REFERENCES dwp_aura.rag_document(doc_id) ON DELETE CASCADE
);
CREATE INDEX ix_rag_chunk_doc_id ON dwp_aura.rag_chunk USING btree (doc_id);
CREATE INDEX ix_rag_chunk_node_type ON dwp_aura.rag_chunk USING btree (tenant_id, node_type) WHERE (node_type IS NOT NULL);
CREATE INDEX ix_rag_chunk_parent ON dwp_aura.rag_chunk USING btree (tenant_id, parent_id) WHERE (parent_id IS NOT NULL);
CREATE INDEX ix_rag_chunk_regulation ON dwp_aura.rag_chunk USING btree (tenant_id, regulation_article, regulation_clause) WHERE (regulation_article IS NOT NULL);
CREATE INDEX ix_rag_chunk_regulation_filter ON dwp_aura.rag_chunk USING btree (tenant_id, doc_id, regulation_article, regulation_clause) WHERE (regulation_article IS NOT NULL);
CREATE INDEX ix_rag_chunk_search_tsv ON dwp_aura.rag_chunk USING gin (search_tsv);
CREATE INDEX ix_rag_chunk_tenant_doc ON dwp_aura.rag_chunk USING btree (tenant_id, doc_id);
CREATE INDEX ix_rag_chunk_tenant_doc_active ON dwp_aura.rag_chunk USING btree (tenant_id, doc_id, is_active);
CREATE INDEX ix_rag_chunk_tenant_effective_range ON dwp_aura.rag_chunk USING btree (tenant_id, effective_from, effective_to);
CREATE INDEX ix_rag_chunk_tenant_meta_article ON dwp_aura.rag_chunk USING btree (tenant_id, ((metadata_json ->> 'regulation_article'::text)));
CREATE INDEX ix_rag_chunk_tenant_version ON dwp_aura.rag_chunk USING btree (tenant_id, version);
CREATE INDEX ix_rag_chunk_text_gin ON dwp_aura.rag_chunk USING gin (to_tsvector('simple'::regconfig, chunk_text));
COMMENT ON TABLE dwp_aura.rag_chunk IS 'RAG 청크. chunk_text 검색용. embedding_id는 벡터 DB 연동 시 사용.';

-- Column comments

COMMENT ON COLUMN dwp_aura.rag_chunk.chunk_id IS '청크 식별자 (PK)';
COMMENT ON COLUMN dwp_aura.rag_chunk.tenant_id IS '테넌트 식별자';
COMMENT ON COLUMN dwp_aura.rag_chunk.doc_id IS '문서 ID (FK)';
COMMENT ON COLUMN dwp_aura.rag_chunk.chunk_text IS '청크 텍스트';
COMMENT ON COLUMN dwp_aura.rag_chunk.search_text IS 'prefix 제거된 정제 본문 (BM25/임베딩 검색용)';
COMMENT ON COLUMN dwp_aura.rag_chunk.regulation_article IS '규정 조항 (예: "제11조", "제3장")';
COMMENT ON COLUMN dwp_aura.rag_chunk.regulation_clause IS '규정 항목 (예: "2항", "제1호")';
COMMENT ON COLUMN dwp_aura.rag_chunk.parent_article IS '상위 조문 번호';
COMMENT ON COLUMN dwp_aura.rag_chunk.parent_title IS '상위 조문 제목';
COMMENT ON COLUMN dwp_aura.rag_chunk.node_type IS '노드 유형: ARTICLE(조문), CLAUSE(항/호), PARAGRAPH(문단)';
COMMENT ON COLUMN dwp_aura.rag_chunk.parent_id IS '부모 청크 ID (조문-항/호 Parent-Child 관계)';
COMMENT ON COLUMN dwp_aura.rag_chunk.chunk_index IS '문서 내 청크 순서 (추론 시 문맥 파악용)';
COMMENT ON COLUMN dwp_aura.rag_chunk.page_no IS '페이지 번호';
COMMENT ON COLUMN dwp_aura.rag_chunk.embedding_id IS '임베딩 ID (벡터 DB 연동)';
COMMENT ON COLUMN dwp_aura.rag_chunk.embedding IS 'OpenAI embedding 벡터 (1536차원, pgvector)';
COMMENT ON COLUMN dwp_aura.rag_chunk.search_tsv IS 'tsvector 컬럼 (BM25 검색 인덱스)';
COMMENT ON COLUMN dwp_aura.rag_chunk.metadata_json IS '페이지 번호, 파일 경로 등 부가 메타데이터';
COMMENT ON COLUMN dwp_aura.rag_chunk.created_at IS '생성일시';

-- Permissions

ALTER TABLE dwp_aura.rag_chunk OWNER TO dwp_user;
GRANT ALL ON TABLE dwp_aura.rag_chunk TO dwp_user;


-- dwp_aura.rag_document_quality_report definition

-- Drop table

-- DROP TABLE dwp_aura.rag_document_quality_report;

CREATE TABLE dwp_aura.rag_document_quality_report (
	id bigserial NOT NULL, -- 품질 리포트 PK
	tenant_id int8 NOT NULL, -- 테넌트 식별자
	doc_id int8 NOT NULL, -- 대상 RAG 문서 ID (FK: rag_document.doc_id)
	run_id uuid NULL, -- 리포트 생성 실행(run) 식별자 (없으면 NULL 가능)
	quality_gate_passed bool NOT NULL, -- 품질게이트 통과 여부
	input_chunks int4 NOT NULL, -- 게이트 적용 전 입력 청크 수
	final_chunks int4 NOT NULL, -- 게이트 적용 후 최종 청크 수
	article_coverage numeric(5, 4) NOT NULL, -- 조항 메타(regulation_article) 커버리지 비율
	noise_rate numeric(5, 4) NOT NULL, -- 잔존 노이즈 비율
	duplicate_rate numeric(5, 4) NOT NULL, -- 중복 비율
	short_chunk_rate numeric(5, 4) NOT NULL, -- 짧은/heading-only 청크 비율
	removed_empty int4 DEFAULT 0 NOT NULL, -- 빈 청크 제거 건수
	removed_heading_only int4 DEFAULT 0 NOT NULL, -- 제목-only 청크 제거 건수
	removed_duplicate_exact int4 DEFAULT 0 NOT NULL, -- 완전중복 제거 건수
	removed_duplicate_near int4 DEFAULT 0 NOT NULL, -- 유사중복 제거 건수
	missing_required jsonb NULL, -- 필수 메타 누락 키 목록
	errors jsonb NULL, -- 품질게이트 오류 코드 목록
	raw_report_json jsonb NOT NULL, -- 원본 품질 리포트 JSON
	created_at timestamptz DEFAULT now() NOT NULL, -- 리포트 생성 시각
	CONSTRAINT rag_document_quality_report_pkey PRIMARY KEY (id),
	CONSTRAINT rag_document_quality_report_doc_id_fkey FOREIGN KEY (doc_id) REFERENCES dwp_aura.rag_document(doc_id) ON DELETE CASCADE
);
CREATE INDEX ix_rag_doc_quality_tenant_doc_created ON dwp_aura.rag_document_quality_report USING btree (tenant_id, doc_id, created_at DESC);

-- Column comments

COMMENT ON COLUMN dwp_aura.rag_document_quality_report.id IS '품질 리포트 PK';
COMMENT ON COLUMN dwp_aura.rag_document_quality_report.tenant_id IS '테넌트 식별자';
COMMENT ON COLUMN dwp_aura.rag_document_quality_report.doc_id IS '대상 RAG 문서 ID (FK: rag_document.doc_id)';
COMMENT ON COLUMN dwp_aura.rag_document_quality_report.run_id IS '리포트 생성 실행(run) 식별자 (없으면 NULL 가능)';
COMMENT ON COLUMN dwp_aura.rag_document_quality_report.quality_gate_passed IS '품질게이트 통과 여부';
COMMENT ON COLUMN dwp_aura.rag_document_quality_report.input_chunks IS '게이트 적용 전 입력 청크 수';
COMMENT ON COLUMN dwp_aura.rag_document_quality_report.final_chunks IS '게이트 적용 후 최종 청크 수';
COMMENT ON COLUMN dwp_aura.rag_document_quality_report.article_coverage IS '조항 메타(regulation_article) 커버리지 비율';
COMMENT ON COLUMN dwp_aura.rag_document_quality_report.noise_rate IS '잔존 노이즈 비율';
COMMENT ON COLUMN dwp_aura.rag_document_quality_report.duplicate_rate IS '중복 비율';
COMMENT ON COLUMN dwp_aura.rag_document_quality_report.short_chunk_rate IS '짧은/heading-only 청크 비율';
COMMENT ON COLUMN dwp_aura.rag_document_quality_report.removed_empty IS '빈 청크 제거 건수';
COMMENT ON COLUMN dwp_aura.rag_document_quality_report.removed_heading_only IS '제목-only 청크 제거 건수';
COMMENT ON COLUMN dwp_aura.rag_document_quality_report.removed_duplicate_exact IS '완전중복 제거 건수';
COMMENT ON COLUMN dwp_aura.rag_document_quality_report.removed_duplicate_near IS '유사중복 제거 건수';
COMMENT ON COLUMN dwp_aura.rag_document_quality_report.missing_required IS '필수 메타 누락 키 목록';
COMMENT ON COLUMN dwp_aura.rag_document_quality_report.errors IS '품질게이트 오류 코드 목록';
COMMENT ON COLUMN dwp_aura.rag_document_quality_report.raw_report_json IS '원본 품질 리포트 JSON';
COMMENT ON COLUMN dwp_aura.rag_document_quality_report.created_at IS '리포트 생성 시각';

-- Permissions

ALTER TABLE dwp_aura.rag_document_quality_report OWNER TO dwp_user;
GRANT ALL ON TABLE dwp_aura.rag_document_quality_report TO dwp_user;


-- dwp_aura.recon_result definition

-- Drop table

-- DROP TABLE dwp_aura.recon_result;

CREATE TABLE dwp_aura.recon_result (
	result_id bigserial NOT NULL, -- 결과 식별자 (PK)
	tenant_id int8 NOT NULL, -- 테넌트 식별자
	run_id int8 NOT NULL, -- 실행 ID (FK)
	resource_type varchar(50) NOT NULL, -- 리소스 유형
	resource_key text NOT NULL, -- bukrs-belnr-gjahr-buzei 등 리소스 식별 키
	status varchar(10) NOT NULL, -- 상태 (PASS, FAIL)
	detail_json jsonb NULL, -- 상세 (JSONB)
	CONSTRAINT recon_result_pkey PRIMARY KEY (result_id),
	CONSTRAINT recon_result_run_id_fkey FOREIGN KEY (run_id) REFERENCES dwp_aura.recon_run(run_id) ON DELETE CASCADE
);
CREATE INDEX ix_recon_result_run ON dwp_aura.recon_result USING btree (run_id);
CREATE INDEX ix_recon_result_tenant_run ON dwp_aura.recon_result USING btree (tenant_id, run_id);
COMMENT ON TABLE dwp_aura.recon_result IS 'Reconciliation 결과. status: PASS | FAIL.';

-- Column comments

COMMENT ON COLUMN dwp_aura.recon_result.result_id IS '결과 식별자 (PK)';
COMMENT ON COLUMN dwp_aura.recon_result.tenant_id IS '테넌트 식별자';
COMMENT ON COLUMN dwp_aura.recon_result.run_id IS '실행 ID (FK)';
COMMENT ON COLUMN dwp_aura.recon_result.resource_type IS '리소스 유형';
COMMENT ON COLUMN dwp_aura.recon_result.resource_key IS 'bukrs-belnr-gjahr-buzei 등 리소스 식별 키';
COMMENT ON COLUMN dwp_aura.recon_result.status IS '상태 (PASS, FAIL)';
COMMENT ON COLUMN dwp_aura.recon_result.detail_json IS '상세 (JSONB)';

-- Permissions

ALTER TABLE dwp_aura.recon_result OWNER TO dwp_user;
GRANT ALL ON TABLE dwp_aura.recon_result TO dwp_user;


-- dwp_aura.rule_duplicate_invoice definition

-- Drop table

-- DROP TABLE dwp_aura.rule_duplicate_invoice;

CREATE TABLE dwp_aura.rule_duplicate_invoice (
	rule_id bigserial NOT NULL, -- 룰 식별자 (PK)
	tenant_id int8 NOT NULL, -- 테넌트 식별자 (논리적 참조: com_tenants.tenant_id)
	profile_id int8 NOT NULL, -- 프로파일 식별자 (논리적 참조: config_profile.profile_id)
	rule_name text NOT NULL, -- 룰명
	is_enabled bool DEFAULT true NOT NULL, -- 활성 여부
	key_fields jsonb NOT NULL, -- 중복 판단 키 필드 세트(JSON 배열, 예: lifnr,xblnr,waers,wrbtr)
	amount_tolerance_pct numeric(6, 3) DEFAULT 0 NOT NULL, -- 금액 허용 오차(%)
	date_tolerance_days int4 DEFAULT 0 NOT NULL, -- 날짜 허용 오차(일)
	split_window_days int4 DEFAULT 0 NOT NULL, -- split 회피 탐지: 기간(일)
	split_count_threshold int4 DEFAULT 0 NOT NULL, -- split 회피 탐지: 건수 임계치
	split_amount_threshold numeric(18, 2) DEFAULT 0 NOT NULL, -- split 회피 탐지: 금액 임계치
	severity_on_match text DEFAULT 'HIGH'::text NOT NULL, -- 매칭 시 심각도
	action_on_match text DEFAULT 'SET_PAYMENT_BLOCK_AND_NOTIFY'::text NOT NULL, -- 매칭 시 조치
	created_at timestamptz DEFAULT now() NOT NULL, -- 생성일시
	created_by int8 NULL, -- 생성자 user_id (논리적 참조: com_users.user_id)
	updated_at timestamptz DEFAULT now() NOT NULL, -- 수정일시
	updated_by int8 NULL, -- 수정자 user_id (논리적 참조: com_users.user_id)
	CONSTRAINT rule_duplicate_invoice_pkey PRIMARY KEY (rule_id),
	CONSTRAINT rule_duplicate_invoice_tenant_id_profile_id_rule_name_key UNIQUE (tenant_id, profile_id, rule_name),
	CONSTRAINT rule_duplicate_invoice_profile_id_fkey FOREIGN KEY (profile_id) REFERENCES dwp_aura.config_profile(profile_id) ON DELETE CASCADE
);
CREATE INDEX ix_rule_duplicate_invoice_enabled ON dwp_aura.rule_duplicate_invoice USING btree (tenant_id, profile_id, is_enabled);
CREATE INDEX ix_rule_duplicate_invoice_tenant_id ON dwp_aura.rule_duplicate_invoice USING btree (tenant_id);
COMMENT ON TABLE dwp_aura.rule_duplicate_invoice IS '중복송장 정의(룰셋). 회사/고객사별 중복 정의 옵션화.';

-- Column comments

COMMENT ON COLUMN dwp_aura.rule_duplicate_invoice.rule_id IS '룰 식별자 (PK)';
COMMENT ON COLUMN dwp_aura.rule_duplicate_invoice.tenant_id IS '테넌트 식별자 (논리적 참조: com_tenants.tenant_id)';
COMMENT ON COLUMN dwp_aura.rule_duplicate_invoice.profile_id IS '프로파일 식별자 (논리적 참조: config_profile.profile_id)';
COMMENT ON COLUMN dwp_aura.rule_duplicate_invoice.rule_name IS '룰명';
COMMENT ON COLUMN dwp_aura.rule_duplicate_invoice.is_enabled IS '활성 여부';
COMMENT ON COLUMN dwp_aura.rule_duplicate_invoice.key_fields IS '중복 판단 키 필드 세트(JSON 배열, 예: lifnr,xblnr,waers,wrbtr)';
COMMENT ON COLUMN dwp_aura.rule_duplicate_invoice.amount_tolerance_pct IS '금액 허용 오차(%)';
COMMENT ON COLUMN dwp_aura.rule_duplicate_invoice.date_tolerance_days IS '날짜 허용 오차(일)';
COMMENT ON COLUMN dwp_aura.rule_duplicate_invoice.split_window_days IS 'split 회피 탐지: 기간(일)';
COMMENT ON COLUMN dwp_aura.rule_duplicate_invoice.split_count_threshold IS 'split 회피 탐지: 건수 임계치';
COMMENT ON COLUMN dwp_aura.rule_duplicate_invoice.split_amount_threshold IS 'split 회피 탐지: 금액 임계치';
COMMENT ON COLUMN dwp_aura.rule_duplicate_invoice.severity_on_match IS '매칭 시 심각도';
COMMENT ON COLUMN dwp_aura.rule_duplicate_invoice.action_on_match IS '매칭 시 조치';
COMMENT ON COLUMN dwp_aura.rule_duplicate_invoice.created_at IS '생성일시';
COMMENT ON COLUMN dwp_aura.rule_duplicate_invoice.created_by IS '생성자 user_id (논리적 참조: com_users.user_id)';
COMMENT ON COLUMN dwp_aura.rule_duplicate_invoice.updated_at IS '수정일시';
COMMENT ON COLUMN dwp_aura.rule_duplicate_invoice.updated_by IS '수정자 user_id (논리적 참조: com_users.user_id)';

-- Permissions

ALTER TABLE dwp_aura.rule_duplicate_invoice OWNER TO dwp_user;
GRANT ALL ON TABLE dwp_aura.rule_duplicate_invoice TO dwp_user;


-- dwp_aura.rule_threshold definition

-- Drop table

-- DROP TABLE dwp_aura.rule_threshold;

CREATE TABLE dwp_aura.rule_threshold (
	threshold_id bigserial NOT NULL, -- 한도 식별자 (PK)
	tenant_id int8 NOT NULL, -- 테넌트 식별자 (논리적 참조: com_tenants.tenant_id)
	profile_id int8 NOT NULL, -- 프로파일 식별자 (논리적 참조: config_profile.profile_id)
	policy_doc_id text NULL, -- RAG 정책 문서 연결(선택, 예: finance_compliance_docs)
	dimension text NOT NULL, -- 차원(HKONT|CATEGORY|COSTCENTER 등)
	dimension_key text NOT NULL, -- 차원 키(예: 510030)
	waers text DEFAULT 'KRW'::text NOT NULL, -- 통화 코드
	threshold_amount numeric(18, 2) NOT NULL, -- 한도 금액
	require_evidence bool DEFAULT false NOT NULL, -- 증빙 필수 여부
	evidence_types jsonb NULL, -- 증빙 유형(JSON 배열, 예: receipt,attendee_list)
	severity_on_breach text DEFAULT 'MEDIUM'::text NOT NULL, -- 위반 시 심각도
	action_on_breach text DEFAULT 'FLAG_FOR_REVIEW'::text NOT NULL, -- 위반 시 조치
	created_at timestamptz DEFAULT now() NOT NULL, -- 생성일시
	created_by int8 NULL, -- 생성자 user_id (논리적 참조: com_users.user_id)
	updated_at timestamptz DEFAULT now() NOT NULL, -- 수정일시
	updated_by int8 NULL, -- 수정자 user_id (논리적 참조: com_users.user_id)
	CONSTRAINT rule_threshold_pkey PRIMARY KEY (threshold_id),
	CONSTRAINT rule_threshold_tenant_id_profile_id_dimension_dimension_key_key UNIQUE (tenant_id, profile_id, dimension, dimension_key, waers),
	CONSTRAINT rule_threshold_profile_id_fkey FOREIGN KEY (profile_id) REFERENCES dwp_aura.config_profile(profile_id) ON DELETE CASCADE
);
CREATE INDEX ix_rule_threshold_dim ON dwp_aura.rule_threshold USING btree (tenant_id, profile_id, dimension, dimension_key);
CREATE INDEX ix_rule_threshold_tenant_id ON dwp_aura.rule_threshold USING btree (tenant_id);
COMMENT ON TABLE dwp_aura.rule_threshold IS '한도/정책(계정·카테고리·코스트센터 등). 예: 접대비 50만원 이상 증빙 필수.';

-- Column comments

COMMENT ON COLUMN dwp_aura.rule_threshold.threshold_id IS '한도 식별자 (PK)';
COMMENT ON COLUMN dwp_aura.rule_threshold.tenant_id IS '테넌트 식별자 (논리적 참조: com_tenants.tenant_id)';
COMMENT ON COLUMN dwp_aura.rule_threshold.profile_id IS '프로파일 식별자 (논리적 참조: config_profile.profile_id)';
COMMENT ON COLUMN dwp_aura.rule_threshold.policy_doc_id IS 'RAG 정책 문서 연결(선택, 예: finance_compliance_docs)';
COMMENT ON COLUMN dwp_aura.rule_threshold.dimension IS '차원(HKONT|CATEGORY|COSTCENTER 등)';
COMMENT ON COLUMN dwp_aura.rule_threshold.dimension_key IS '차원 키(예: 510030)';
COMMENT ON COLUMN dwp_aura.rule_threshold.waers IS '통화 코드';
COMMENT ON COLUMN dwp_aura.rule_threshold.threshold_amount IS '한도 금액';
COMMENT ON COLUMN dwp_aura.rule_threshold.require_evidence IS '증빙 필수 여부';
COMMENT ON COLUMN dwp_aura.rule_threshold.evidence_types IS '증빙 유형(JSON 배열, 예: receipt,attendee_list)';
COMMENT ON COLUMN dwp_aura.rule_threshold.severity_on_breach IS '위반 시 심각도';
COMMENT ON COLUMN dwp_aura.rule_threshold.action_on_breach IS '위반 시 조치';
COMMENT ON COLUMN dwp_aura.rule_threshold.created_at IS '생성일시';
COMMENT ON COLUMN dwp_aura.rule_threshold.created_by IS '생성자 user_id (논리적 참조: com_users.user_id)';
COMMENT ON COLUMN dwp_aura.rule_threshold.updated_at IS '수정일시';
COMMENT ON COLUMN dwp_aura.rule_threshold.updated_by IS '수정자 user_id (논리적 참조: com_users.user_id)';

-- Permissions

ALTER TABLE dwp_aura.rule_threshold OWNER TO dwp_user;
GRANT ALL ON TABLE dwp_aura.rule_threshold TO dwp_user;


-- dwp_aura.sap_change_log definition

-- Drop table

-- DROP TABLE dwp_aura.sap_change_log;

CREATE TABLE dwp_aura.sap_change_log (
	tenant_id int8 NOT NULL, -- 테넌트 식별자
	objectclas varchar(15) NOT NULL, -- 객체 클래스
	objectid varchar(90) NOT NULL, -- 객체 ID (party_code 등)
	changenr varchar(10) NOT NULL, -- 변경번호
	username varchar(12) NULL, -- 변경 사용자
	udate date NULL, -- 변경일
	utime time NULL, -- 변경시간
	tabname varchar(30) NOT NULL, -- 테이블명
	fname varchar(30) NOT NULL, -- 필드명
	value_old text NULL, -- 변경 전 값
	value_new text NULL, -- 변경 후 값
	last_change_ts timestamptz NULL, -- 마지막 변경 시각
	raw_event_id int8 NULL, -- 원천 Raw 이벤트 ID (FK)
	CONSTRAINT sap_change_log_pkey PRIMARY KEY (tenant_id, objectclas, objectid, changenr, tabname, fname),
	CONSTRAINT sap_change_log_raw_event_id_fkey FOREIGN KEY (raw_event_id) REFERENCES dwp_aura.sap_raw_events(id) ON DELETE SET NULL
);
CREATE INDEX ix_sap_change_log_obj ON dwp_aura.sap_change_log USING btree (tenant_id, objectclas, objectid);
CREATE INDEX ix_sap_change_log_time ON dwp_aura.sap_change_log USING btree (tenant_id, udate, utime);
COMMENT ON TABLE dwp_aura.sap_change_log IS 'SAP 변경 이력 (CDHDR/CDPOS 유사). 객체별 변경 추적.';

-- Column comments

COMMENT ON COLUMN dwp_aura.sap_change_log.tenant_id IS '테넌트 식별자';
COMMENT ON COLUMN dwp_aura.sap_change_log.objectclas IS '객체 클래스';
COMMENT ON COLUMN dwp_aura.sap_change_log.objectid IS '객체 ID (party_code 등)';
COMMENT ON COLUMN dwp_aura.sap_change_log.changenr IS '변경번호';
COMMENT ON COLUMN dwp_aura.sap_change_log.username IS '변경 사용자';
COMMENT ON COLUMN dwp_aura.sap_change_log.udate IS '변경일';
COMMENT ON COLUMN dwp_aura.sap_change_log.utime IS '변경시간';
COMMENT ON COLUMN dwp_aura.sap_change_log.tabname IS '테이블명';
COMMENT ON COLUMN dwp_aura.sap_change_log.fname IS '필드명';
COMMENT ON COLUMN dwp_aura.sap_change_log.value_old IS '변경 전 값';
COMMENT ON COLUMN dwp_aura.sap_change_log.value_new IS '변경 후 값';
COMMENT ON COLUMN dwp_aura.sap_change_log.last_change_ts IS '마지막 변경 시각';
COMMENT ON COLUMN dwp_aura.sap_change_log.raw_event_id IS '원천 Raw 이벤트 ID (FK)';

-- Permissions

ALTER TABLE dwp_aura.sap_change_log OWNER TO dwp_user;
GRANT ALL ON TABLE dwp_aura.sap_change_log TO dwp_user;


-- dwp_aura.thought_chain_log definition

-- Drop table

-- DROP TABLE dwp_aura.thought_chain_log;

CREATE TABLE dwp_aura.thought_chain_log (
	log_id bigserial NOT NULL,
	run_id uuid NOT NULL,
	tenant_id int8 NOT NULL,
	case_id int8 NOT NULL,
	event_type varchar(50) NOT NULL,
	"data" text NULL,
	created_at timestamptz DEFAULT now() NOT NULL,
	CONSTRAINT thought_chain_log_pkey PRIMARY KEY (log_id),
	CONSTRAINT thought_chain_log_run_id_fkey FOREIGN KEY (run_id) REFERENCES dwp_aura.case_analysis_run(run_id) ON DELETE CASCADE
);
CREATE INDEX ix_thought_chain_log_run ON dwp_aura.thought_chain_log USING btree (run_id, created_at);
CREATE INDEX ix_thought_chain_log_tenant_case ON dwp_aura.thought_chain_log USING btree (tenant_id, case_id);
COMMENT ON TABLE dwp_aura.thought_chain_log IS 'AI 분석 시 사고 과정(Thought Chain) 로그 — run별 시간순 저장';

-- Permissions

ALTER TABLE dwp_aura.thought_chain_log OWNER TO dwp_user;
GRANT ALL ON TABLE dwp_aura.thought_chain_log TO dwp_user;


-- dwp_aura.agent_action definition

-- Drop table

-- DROP TABLE dwp_aura.agent_action;

CREATE TABLE dwp_aura.agent_action (
	action_id bigserial NOT NULL, -- 액션 식별자 (PK)
	tenant_id int8 NOT NULL, -- 테넌트 식별자
	case_id int8 NOT NULL, -- 케이스 ID (FK: agent_case.case_id)
	action_type varchar(50) NOT NULL, -- 액션 유형 (PAYMENT_BLOCK, SEND_NUDGE 등)
	action_payload jsonb NULL, -- 액션 페이로드 (JSONB)
	planned_at timestamptz DEFAULT now() NOT NULL, -- 계획 시각
	executed_at timestamptz NULL, -- 실행 시각
	status dwp_aura."agent_action_status" DEFAULT 'PLANNED'::dwp_aura.agent_action_status NOT NULL, -- 상태 (PLANNED, PROPOSED, PENDING_APPROVAL, APPROVED, EXECUTING, EXECUTED, FAILED, CANCELED 등)
	executed_by varchar(50) NULL, -- 실행자 (PENDING, USER_ID 등)
	error_message text NULL, -- 오류 메시지
	requested_by_user_id int8 NULL, -- 요청자 user_id
	requested_by_actor_type varchar(20) DEFAULT 'USER'::character varying NULL, -- 요청자 유형 (USER, AGENT, SYSTEM)
	payload_json jsonb NULL, -- 페이로드 JSON
	simulation_before jsonb NULL, -- 시뮬레이션 전 상태
	simulation_after jsonb NULL, -- 시뮬레이션 후 상태
	diff_json jsonb NULL, -- 변경 diff
	failure_reason text NULL, -- 실패 사유
	created_at timestamptz DEFAULT now() NULL, -- 생성일시
	updated_at timestamptz DEFAULT now() NULL, -- 수정일시
	CONSTRAINT agent_action_pkey PRIMARY KEY (action_id),
	CONSTRAINT agent_action_case_id_fkey FOREIGN KEY (case_id) REFERENCES dwp_aura.agent_case(case_id) ON DELETE CASCADE
);
CREATE INDEX ix_agent_action_case ON dwp_aura.agent_action USING btree (tenant_id, case_id);
CREATE INDEX ix_agent_action_status_created ON dwp_aura.agent_action USING btree (tenant_id, status, created_at DESC);
CREATE INDEX ix_agent_action_tenant_status_created ON dwp_aura.agent_action USING btree (tenant_id, status, created_at DESC);
COMMENT ON TABLE dwp_aura.agent_action IS '에이전트 액션. 케이스별 제안/승인/실행.';

-- Column comments

COMMENT ON COLUMN dwp_aura.agent_action.action_id IS '액션 식별자 (PK)';
COMMENT ON COLUMN dwp_aura.agent_action.tenant_id IS '테넌트 식별자';
COMMENT ON COLUMN dwp_aura.agent_action.case_id IS '케이스 ID (FK: agent_case.case_id)';
COMMENT ON COLUMN dwp_aura.agent_action.action_type IS '액션 유형 (PAYMENT_BLOCK, SEND_NUDGE 등)';
COMMENT ON COLUMN dwp_aura.agent_action.action_payload IS '액션 페이로드 (JSONB)';
COMMENT ON COLUMN dwp_aura.agent_action.planned_at IS '계획 시각';
COMMENT ON COLUMN dwp_aura.agent_action.executed_at IS '실행 시각';
COMMENT ON COLUMN dwp_aura.agent_action.status IS '상태 (PLANNED, PROPOSED, PENDING_APPROVAL, APPROVED, EXECUTING, EXECUTED, FAILED, CANCELED 등)';
COMMENT ON COLUMN dwp_aura.agent_action.executed_by IS '실행자 (PENDING, USER_ID 등)';
COMMENT ON COLUMN dwp_aura.agent_action.error_message IS '오류 메시지';
COMMENT ON COLUMN dwp_aura.agent_action.requested_by_user_id IS '요청자 user_id';
COMMENT ON COLUMN dwp_aura.agent_action.requested_by_actor_type IS '요청자 유형 (USER, AGENT, SYSTEM)';
COMMENT ON COLUMN dwp_aura.agent_action.payload_json IS '페이로드 JSON';
COMMENT ON COLUMN dwp_aura.agent_action.simulation_before IS '시뮬레이션 전 상태';
COMMENT ON COLUMN dwp_aura.agent_action.simulation_after IS '시뮬레이션 후 상태';
COMMENT ON COLUMN dwp_aura.agent_action.diff_json IS '변경 diff';
COMMENT ON COLUMN dwp_aura.agent_action.failure_reason IS '실패 사유';
COMMENT ON COLUMN dwp_aura.agent_action.created_at IS '생성일시';
COMMENT ON COLUMN dwp_aura.agent_action.updated_at IS '수정일시';

-- Permissions

ALTER TABLE dwp_aura.agent_action OWNER TO dwp_user;
GRANT ALL ON TABLE dwp_aura.agent_action TO dwp_user;


-- dwp_aura.case_action_execution definition

-- Drop table

-- DROP TABLE dwp_aura.case_action_execution;

CREATE TABLE dwp_aura.case_action_execution (
	execution_id uuid DEFAULT gen_random_uuid() NOT NULL,
	tenant_id int8 NOT NULL,
	case_id int8 NOT NULL,
	run_id uuid NULL,
	proposal_id uuid NULL,
	"mode" varchar(20) DEFAULT 'SIMULATION'::character varying NOT NULL, -- SIMULATION | LIVE
	status varchar(20) DEFAULT 'COMPLETED'::character varying NOT NULL, -- COMPLETED | FAILED
	result_json jsonb NULL,
	error_message text NULL,
	executed_by int8 NULL,
	executed_at timestamptz DEFAULT now() NOT NULL,
	created_at timestamptz DEFAULT now() NOT NULL,
	gateway_request_id varchar(255) NULL, -- 요청 추적/멱등용 (FE 또는 Gateway에서 전달)
	request_json jsonb NULL, -- 요청 본문(멱등/감사용)
	action_type varchar(64) NULL, -- 실행한 액션 유형(PAYMENT_BLOCK 등), proposal 없을 때 필수
	CONSTRAINT case_action_execution_pkey PRIMARY KEY (execution_id),
	CONSTRAINT case_action_execution_proposal_id_fkey FOREIGN KEY (proposal_id) REFERENCES dwp_aura.case_action_proposal(proposal_id) ON DELETE CASCADE,
	CONSTRAINT case_action_execution_run_id_fkey FOREIGN KEY (run_id) REFERENCES dwp_aura.case_analysis_run(run_id) ON DELETE SET NULL
);
CREATE INDEX ix_case_action_execution_proposal ON dwp_aura.case_action_execution USING btree (proposal_id);
CREATE INDEX ix_case_action_execution_tenant_case ON dwp_aura.case_action_execution USING btree (tenant_id, case_id);
CREATE INDEX ix_case_action_execution_tenant_case_run ON dwp_aura.case_action_execution USING btree (tenant_id, case_id, run_id, executed_at DESC);
CREATE UNIQUE INDEX uq_case_action_execution_tenant_gateway_request_id ON dwp_aura.case_action_execution USING btree (tenant_id, gateway_request_id) WHERE (gateway_request_id IS NOT NULL);
COMMENT ON TABLE dwp_aura.case_action_execution IS 'Phase3: 액션 제안 실행(시뮬) 결과';

-- Column comments

COMMENT ON COLUMN dwp_aura.case_action_execution."mode" IS 'SIMULATION | LIVE';
COMMENT ON COLUMN dwp_aura.case_action_execution.status IS 'COMPLETED | FAILED';
COMMENT ON COLUMN dwp_aura.case_action_execution.gateway_request_id IS '요청 추적/멱등용 (FE 또는 Gateway에서 전달)';
COMMENT ON COLUMN dwp_aura.case_action_execution.request_json IS '요청 본문(멱등/감사용)';
COMMENT ON COLUMN dwp_aura.case_action_execution.action_type IS '실행한 액션 유형(PAYMENT_BLOCK 등), proposal 없을 때 필수';

-- Permissions

ALTER TABLE dwp_aura.case_action_execution OWNER TO dwp_user;
GRANT ALL ON TABLE dwp_aura.case_action_execution TO dwp_user;


여긴 aura 테이블 스키마 정보가 있습니다. @docs/aura_db.md
그리고 /Users/joonbinchoi/Work/dwp/FE,BE,AURA  경로 밑에는 이 프로젝트를 POC 로 만들기 위해 가져온 원본 소스 폴더가 각각 있습니다.
따라서 모듈별로 참고할만한 소스가 필요시 참고하면 됩니다.
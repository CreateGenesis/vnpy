export type GatewayName = "XTP" | "TORA";
export type ConnectionState = "connecting" | "connected" | "reconnecting" | "disconnected";
export type ServiceName = "research" | "model_xtp" | "model_tora" | "rqdata_fetcher";
export type ServiceAction = "start" | "stop" | "restart";
export type GatewayAction = "start" | "stop" | "reconnect" | "select";

export interface ActionBlocker {
  code: string;
  parameters: Record<string, string | number | boolean | null>;
}

export interface ActionState {
  contract_version: 2;
  action_id: string;
  target: string;
  state: "enabled" | "blocked" | "running";
  blockers: ActionBlocker[];
  remediation: string[];
  expected_revision: number;
}

export interface ServiceProjection {
  service: ServiceName;
  state: string;
  revision: number;
  error_code: string | null;
}

export interface GatewayStatusProjection {
  gateway: GatewayName;
  state: string;
  selected: boolean;
  error_code: string | null;
  updated_at_ms: number;
}

export interface SystemProjection {
  contract_version: 2;
  revision: number;
  configuration: {
    state: string;
    active_version: number;
    draft_revision: number;
  };
  services: ServiceProjection[];
  gateways: GatewayStatusProjection[];
  actions: ActionState[];
}

export interface ConfigurationTestReceipt {
  passed: boolean;
  code: string;
  expires_at_ms: number;
  fingerprint?: string;
}

export interface ConfigurationDraftProjection {
  revision: number;
  sections: Record<string, Record<string, unknown>>;
  changed_sections: string[];
  test_receipts: Record<string, ConfigurationTestReceipt>;
  secret_status: Record<string, { configured: boolean; mask?: string }>;
}

export interface ConfigurationDraftUpdate {
  expected_revision: number;
  sections: Record<string, Record<string, unknown>>;
  secret_updates: Record<string, string>;
  clear_secrets: string[];
}

export interface ConfigurationActivationReceipt {
  state: string;
  version?: number;
  revision?: number;
  next_url?: string;
  error_code?: string;
}

export interface OperationReceipt {
  state: string;
  revision?: number;
  service?: ServiceName;
  gateway?: GatewayName;
  selected?: boolean;
  receipt_digest?: string;
  next_url?: string;
}

export interface ResearchTaskProjection {
  contract_version: 1;
  task_id: string;
  task_digest: string;
  source: "operator" | "side_master_proposal";
  objective: string;
  priority: string;
  state: string;
  created_at_ms: number;
  expires_at_ms: number;
  not_before_boundary: string;
}

export interface ResearchProjection {
  revision: number;
  tasks: ResearchTaskProjection[];
}

export interface ResearchTaskCreate {
  mission_id: string;
  objective: string;
  constraints: string[];
  data_references: string[];
  priority: "routine" | "high";
  expires_at_ms: number;
}

export interface ModelRunProjection {
  run_id: string;
  family: "rule" | "factor" | "lasso" | "lightgbm" | "mlp" | string;
  state: string;
  progress_percent: number;
  artifact_digest: string | null;
  error_code: string | null;
}

export interface ModelPipelineProjection {
  revision: number;
  current_candidate: null | {
    state: string;
    candidate_digest: string;
    package_digest: string;
    family: string;
    publication_revision: number;
  };
  runs: ModelRunProjection[];
}

export interface DemoReadiness {
  state: "ready" | "blocked";
  ready: boolean;
  candidate_digest: string;
  components: Array<{
    name: string;
    state: string;
  }>;
  blockers: Array<{
    code: string;
    detail: string;
  }>;
}

export interface LatencyProjection {
  count: number;
  p50: number;
  p95: number;
  p99: number;
  max: number;
}

export interface PositionProjection {
  symbol: string;
  quantity: number;
  available_quantity: number;
  marked_value_minor: number;
  unrealized_profit_minor: number;
  t_plus_one_locked_quantity: number;
}

export interface GatewayProjection {
  gateway: GatewayName;
  run_digest: string;
  state: string;
  connection_state: string;
  reconciliation_state: string;
  net_profit_minor: number;
  realized_profit_minor: number;
  unrealized_profit_minor: number;
  fees_minor: number;
  return_bps: number;
  max_drawdown_bps: number;
  fill_count: number;
  positions: PositionProjection[];
  gross_exposure_minor: number;
  risk_headroom_minor: number;
  local_latency_us: LatencyProjection;
  broker_latency_us: LatencyProjection;
  incidents: string[];
  residual_exposure_minor: number;
  working_order_count: number;
  unresolved_outcomes: number;
  permitted_next_action: string;
}

export interface HistoricalGatewayProjection {
  gateway: GatewayName;
  net_profit_minor: number;
  reconciled: boolean;
  hard_limit_breaches: number;
  unresolved_outcomes: number;
}

export interface HistoricalEvidenceProjection {
  label: "historical_broker_simulation_evidence";
  campaign_digest: string;
  candidate_digest: string;
  evidence_digest: string;
  sessions: string[];
  ready: boolean;
  gateways: HistoricalGatewayProjection[];
  retained_at_ms: number;
}

export interface DemoProjection {
  contract_version: 1;
  entity_type: "investor_demo_projection";
  revision: number;
  source_revision: number;
  source_digest: string;
  projection_digest: string;
  previous_projection_digest: string | null;
  updated_at_ms: number;
  performance_scope: "broker_simulation";
  candidate: {
    candidate_digest: string;
    author_lineage_digest: string;
    package_digest: string;
    readiness: string;
  };
  current: {
    label: "current_broker_simulation";
    campaign_id: string | null;
    campaign_digest: string | null;
    campaign_state: string;
    gateways: GatewayProjection[];
  };
  history: HistoricalEvidenceProjection[];
  risk_state: string;
  permitted_actions: Array<"start" | "pause" | "emergency_stop">;
}

export interface ControlGatewayReceipt {
  gateway: GatewayName;
  state: string;
  receipt_digest?: string;
  error_code?: string;
  data?: Record<string, unknown>;
}

export interface ControlReceipt {
  contract_version?: 1;
  action?: "pause" | "emergency_stop";
  state: string;
  request_digest?: string;
  started_at_ns?: number;
  completed_at_ns?: number;
  hard_stop_deadline_met?: boolean;
  gateways?: ControlGatewayReceipt[];
  receipt_digest?: string;
  [key: string]: unknown;
}

export interface DynamicContent {
  media_type: "text/plain; charset=utf-8" | "application/json";
  body: unknown;
  canonical_body_base64: string;
  body_digest: string;
}

export type ProposalState = "pending" | "confirmed" | "rejected" | "expired" | "uncertain";

export interface SideMasterProposal {
  contract_version: 1;
  entity_type: "side_master_approval_proposal";
  proposal_id: string;
  session_id: string;
  mission_id: string;
  side_master_identity: string;
  source_turn_digest: string;
  material_direction_change: true;
  interpretation: string;
  proposed_guidance: string;
  provider_outcome: "certain" | "uncertain";
  state: ProposalState;
  created_at_ms: number;
  expires_at_ms: number;
  proposal_digest: string;
}

export interface SideMasterChatResult {
  contract_version: 1;
  entity_type: "demo_side_master_chat_result";
  session_id: string;
  mission_id: string;
  state: "completed" | "uncertain";
  reply: DynamicContent | null;
  proposal: SideMasterProposal | null;
  provider_outcome: "certain" | "uncertain";
  result_digest: string;
}

export interface GuidanceRevision {
  contract_version: 1;
  entity_type: "confirmed_future_research_guidance";
  guidance_id: string;
  proposal_id: string;
  proposal_digest: string;
  mission_id: string;
  guidance: string;
  operator_identity_digest: string;
  confirmed_at_ms: number;
  scope: "future_research_only";
  not_before_safe_boundary_revision: number;
  delivery_id: string;
  active_campaign_immutable: boolean;
  signer_id: string;
  verifying_key: string;
  guidance_digest: string;
  signature: string;
}

export interface ProposalDecisionReceipt {
  proposal: SideMasterProposal;
  guidance: GuidanceRevision | null;
  idempotency_key: string;
  decision_digest: string;
}

export interface SideMasterApi {
  sendSideMasterMessage(
    sessionId: string,
    missionId: string,
    content: string,
    idempotencyKey: string,
  ): Promise<SideMasterChatResult>;
  decideSideMasterProposal(
    proposalId: string,
    expectedProposalDigest: string,
    decision: "confirm" | "reject",
    idempotencyKey: string,
  ): Promise<ProposalDecisionReceipt>;
}

export interface DemoApi extends SideMasterApi {
  getSystem(): Promise<SystemProjection>;
  getConfigurationDraft(): Promise<ConfigurationDraftProjection>;
  updateConfigurationDraft(command: ConfigurationDraftUpdate): Promise<ConfigurationDraftProjection>;
  testConfigurationSection(section: string, expectedRevision: number): Promise<ConfigurationTestReceipt>;
  activateConfigurationDraft(expectedRevision: number): Promise<ConfigurationActivationReceipt>;
  controlService(service: ServiceName, action: ServiceAction, expectedRevision: number): Promise<OperationReceipt>;
  controlGateway(
    gateway: GatewayName,
    action: GatewayAction,
    expectedRevision: number,
    selected?: boolean,
  ): Promise<OperationReceipt>;
  getResearchTasks(): Promise<ResearchProjection>;
  createResearchTask(command: ResearchTaskCreate): Promise<{ revision: number; task: ResearchTaskProjection }>;
  cancelResearchTask(taskId: string, taskDigest: string): Promise<{ revision: number; task: ResearchTaskProjection }>;
  getModels(): Promise<ModelPipelineProjection>;
  getEvidence(campaignId: string): Promise<Record<string, unknown>>;
  getReadiness(): Promise<DemoReadiness>;
  getProjection(): Promise<DemoProjection>;
  startCampaign(candidateDigest: string, gateways: GatewayName[]): Promise<ControlReceipt>;
  pauseCampaign(campaignId: string): Promise<ControlReceipt>;
  emergencyStop(): Promise<ControlReceipt>;
  subscribe(
    onProjection: (projection: DemoProjection) => void,
    onConnection: (state: ConnectionState, attempt: number) => void,
  ): () => void;
}

interface ApiEnvelope<T> {
  contract_version: 1;
  status: "ok" | "accepted" | "blocked" | "error";
  data: T;
  errors?: Array<{ code: string; message: string }>;
}

interface ProjectionEvent {
  event: "projection.snapshot";
  data: DemoProjection;
}

const csrfToken = (): string =>
  document.querySelector<HTMLMetaElement>('meta[name="auto-trade-csrf"]')?.content ?? "";

const idempotencyKey = (): string => `${crypto.randomUUID()}-${crypto.randomUUID()}`;

const decodeEnvelope = async <T>(response: Response): Promise<T> => {
  const envelope = (await response.json()) as ApiEnvelope<T>;
  if (!response.ok || !["ok", "accepted"].includes(envelope.status)) {
    throw new Error(envelope.errors?.[0]?.code ?? `HTTP_${response.status}`);
  }
  return envelope.data;
};

const request = async <T>(path: string, init?: RequestInit): Promise<T> => {
  const response = await fetch(path, {
    credentials: "same-origin",
    ...init,
    headers: {
      Accept: "application/json",
      ...(init?.headers ?? {}),
    },
  });
  return decodeEnvelope<T>(response);
};

const post = async <T>(path: string, body?: unknown): Promise<T> => {
  const token = csrfToken();
  if (token.length < 32) {
    throw new Error("CSRF_TOKEN_UNAVAILABLE");
  }
  return request<T>(path, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": token,
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
};

const put = async <T>(path: string, body: unknown): Promise<T> => {
  const token = csrfToken();
  if (token.length < 32) {
    throw new Error("CSRF_TOKEN_UNAVAILABLE");
  }
  return request<T>(path, {
    method: "PUT",
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": token,
    },
    body: JSON.stringify(body),
  });
};

export const createDemoApi = (): DemoApi => ({
  getSystem: () => request<SystemProjection>("/api/v1/system"),
  getConfigurationDraft: () => request<ConfigurationDraftProjection>("/api/v1/config/draft"),
  updateConfigurationDraft: (command) =>
    put<ConfigurationDraftProjection>("/api/v1/config/draft", command),
  testConfigurationSection: (section, expectedRevision) =>
    post<ConfigurationTestReceipt>("/api/v1/config/draft/test", {
      section,
      expected_revision: expectedRevision,
      idempotency_key: idempotencyKey(),
    }),
  activateConfigurationDraft: (expectedRevision) =>
    post<ConfigurationActivationReceipt>("/api/v1/config/draft/activate", {
      expected_revision: expectedRevision,
      idempotency_key: idempotencyKey(),
    }),
  controlService: (service, action, expectedRevision) =>
    post<OperationReceipt>(`/api/v1/services/${service}/${action}`, {
      expected_revision: expectedRevision,
      idempotency_key: idempotencyKey(),
    }),
  controlGateway: (gateway, action, expectedRevision, selected) =>
    post<OperationReceipt>(`/api/v1/gateways/${gateway}/${action}`, {
      expected_revision: expectedRevision,
      idempotency_key: idempotencyKey(),
      ...(action === "select" ? { selected } : {}),
    }),
  getResearchTasks: () => request<ResearchProjection>("/api/v1/research/tasks"),
  createResearchTask: (command) =>
    post<{ revision: number; task: ResearchTaskProjection }>("/api/v1/research/tasks", {
      ...command,
      idempotency_key: idempotencyKey(),
    }),
  cancelResearchTask: (taskId, taskDigest) =>
    post<{ revision: number; task: ResearchTaskProjection }>(
      `/api/v1/research/tasks/${encodeURIComponent(taskId)}/cancel`,
      { expected_task_digest: taskDigest, idempotency_key: idempotencyKey() },
    ),
  getModels: () => request<ModelPipelineProjection>("/api/v1/models"),
  getEvidence: (campaignId) =>
    request<Record<string, unknown>>(`/api/v1/evidence/${encodeURIComponent(campaignId)}`),
  getReadiness: () => request<DemoReadiness>("/api/v1/readiness"),
  getProjection: () => request<DemoProjection>("/api/v1/projection"),
  startCampaign: (candidateDigest, gateways) =>
    post<ControlReceipt>("/api/v1/campaigns", {
      candidate_digest: candidateDigest,
      gateways,
      idempotency_key: idempotencyKey(),
    }),
  pauseCampaign: (campaignId) =>
    post<ControlReceipt>(`/api/v1/campaigns/${encodeURIComponent(campaignId)}/pause`),
  emergencyStop: () => post<ControlReceipt>("/api/v1/emergency-stop"),
  sendSideMasterMessage: (sessionId, missionId, content, requestKey) =>
    post<SideMasterChatResult>("/api/v1/chat/messages", {
      session_id: sessionId,
      mission_id: missionId,
      content,
      idempotency_key: requestKey,
    }),
  decideSideMasterProposal: (proposalId, expectedProposalDigest, decision, requestKey) =>
    post<ProposalDecisionReceipt>(
      `/api/v1/chat/proposals/${encodeURIComponent(proposalId)}/${decision}`,
      {
        expected_proposal_digest: expectedProposalDigest,
        idempotency_key: requestKey,
      },
    ),
  subscribe: (onProjection, onConnection) => {
    let socket: WebSocket | null = null;
    let reconnectTimer: number | null = null;
    let attempt = 0;
    let closed = false;

    const connect = (): void => {
      if (closed) return;
      onConnection(attempt === 0 ? "connecting" : "reconnecting", attempt);
      const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      socket = new WebSocket(`${protocol}//${window.location.host}/api/v1/events`);
      socket.onopen = () => {
        attempt = 0;
        onConnection("connected", 0);
      };
      socket.onmessage = (message) => {
        const event = JSON.parse(message.data as string) as ProjectionEvent;
        if (event.event === "projection.snapshot") onProjection(event.data);
      };
      socket.onerror = () => socket?.close();
      socket.onclose = () => {
        if (closed) {
          onConnection("disconnected", attempt);
          return;
        }
        attempt += 1;
        onConnection("reconnecting", attempt);
        const delay = Math.min(10_000, 500 * 2 ** Math.min(attempt - 1, 5));
        reconnectTimer = window.setTimeout(connect, delay);
      };
    };

    connect();
    return () => {
      closed = true;
      if (reconnectTimer !== null) window.clearTimeout(reconnectTimer);
      socket?.close();
    };
  },
});

export type GatewayName = "XTP" | "TORA";
export type ConnectionState = "connecting" | "connected" | "reconnecting" | "disconnected";

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

export interface DemoApi {
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

export const createDemoApi = (): DemoApi => ({
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

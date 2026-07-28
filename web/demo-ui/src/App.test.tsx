import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { vi } from "vitest";

import { App } from "./App";
import type {
  ConnectionState,
  DemoApi,
  DemoProjection,
  DemoReadiness,
  ModelPipelineProjection,
  ResearchProjection,
  SystemProjection,
} from "./api";


const digest = (character: string): string => `sha256:${character.repeat(64)}`;

const system: SystemProjection = {
  contract_version: 2,
  revision: 8,
  configuration: { state: "unconfigured", active_version: 0, draft_revision: 1 },
  services: [
    { service: "research", state: "stopped", revision: 2, error_code: null },
    { service: "model_xtp", state: "unconfigured", revision: 0, error_code: null },
    { service: "model_tora", state: "unavailable", revision: 0, error_code: "SERVICE_NOT_CONFIGURED" },
    { service: "rqdata_fetcher", state: "stopped", revision: 1, error_code: null },
  ],
  gateways: [
    { gateway: "XTP", state: "stopped", selected: false, error_code: null, updated_at_ms: 1_753_600_000_000 },
    { gateway: "TORA", state: "unavailable", selected: false, error_code: "GATEWAY_CONFIGURATION_NOT_ACTIVE", updated_at_ms: 1_753_600_000_000 },
  ],
  actions: [
    {
      contract_version: 2,
      action_id: "configuration.save",
      target: "configuration",
      state: "enabled",
      blockers: [],
      remediation: [],
      expected_revision: 1,
    },
    ...[
      "service.research.start",
      "service.research.stop",
      "gateway.xtp.start",
      "gateway.xtp.stop",
      "gateway.xtp.reconnect",
      "gateway.xtp.select",
      "gateway.tora.start",
      "gateway.tora.stop",
      "gateway.tora.reconnect",
      "gateway.tora.select",
      "campaign.start",
      "campaign.pause",
    ].map((action_id) => ({
      contract_version: 2 as const,
      action_id,
      target: action_id.split(".").slice(0, -1).join("."),
      state: "blocked" as const,
      blockers: [{ code: "CONFIGURATION_NOT_ACTIVE", parameters: {} }],
      remediation: ["open_settings"],
      expected_revision: 8,
    })),
    {
      contract_version: 2,
      action_id: "campaign.emergency_stop",
      target: "campaign",
      state: "enabled",
      blockers: [],
      remediation: [],
      expected_revision: 8,
    },
  ],
};

const projection: DemoProjection = {
  contract_version: 1,
  entity_type: "investor_demo_projection",
  revision: 4,
  source_revision: 18,
  source_digest: digest("1"),
  projection_digest: digest("2"),
  previous_projection_digest: digest("3"),
  updated_at_ms: 1_753_600_001_000,
  performance_scope: "broker_simulation",
  candidate: {
    candidate_digest: digest("a"),
    author_lineage_digest: digest("b"),
    package_digest: digest("c"),
    readiness: "ready",
  },
  current: {
    label: "current_broker_simulation",
    campaign_id: null,
    campaign_digest: null,
    campaign_state: "unavailable",
    gateways: [],
  },
  history: [
    {
      label: "historical_broker_simulation_evidence",
      campaign_digest: digest("9"),
      candidate_digest: digest("a"),
      evidence_digest: digest("8"),
      sessions: ["2026-07-20", "2026-07-21", "2026-07-22", "2026-07-23", "2026-07-24"],
      ready: true,
      gateways: [
        { gateway: "XTP", net_profit_minor: 10_000, reconciled: true, hard_limit_breaches: 0, unresolved_outcomes: 0 },
        { gateway: "TORA", net_profit_minor: 8_000, reconciled: true, hard_limit_breaches: 0, unresolved_outcomes: 0 },
      ],
      retained_at_ms: 1_753_600_000_000,
    },
  ],
  risk_state: "normal",
  permitted_actions: ["emergency_stop"],
};

const readiness: DemoReadiness = {
  state: "blocked",
  ready: false,
  candidate_digest: digest("a"),
  components: [
    { name: "run-xtp", state: "stopped" },
    { name: "run-tora", state: "unavailable" },
    { name: "side-master", state: "unavailable" },
  ],
  blockers: [{ code: "CONFIGURATION_NOT_ACTIVE", detail: "Configuration is unavailable." }],
};

const research: ResearchProjection = {
  revision: 3,
  tasks: [
    {
      contract_version: 1,
      task_id: "b53bc59c-c626-4f16-8a3e-a3185c7dad23",
      task_digest: digest("d"),
      source: "side_master_proposal",
      objective: "研究低换手率回撤控制因子",
      priority: "routine",
      state: "queued",
      created_at_ms: 1_753_600_000_000,
      expires_at_ms: 1_753_686_400_000,
      not_before_boundary: "campaign_terminal",
    },
  ],
};

const models: ModelPipelineProjection = {
  revision: 5,
  current_candidate: {
    state: "ready",
    candidate_digest: digest("a"),
    package_digest: digest("c"),
    family: "lasso",
    publication_revision: 2,
  },
  runs: [
    {
      run_id: "run-lasso-01",
      family: "lasso",
      state: "evaluated",
      progress_percent: 100,
      artifact_digest: digest("f"),
      error_code: null,
    },
  ],
};

function apiMock(): {
  api: DemoApi;
  emitConnection: (state: ConnectionState, attempt?: number) => void;
} {
  let connectionListener: (state: ConnectionState, attempt: number) => void = () => undefined;
  const api: DemoApi = {
    getSystem: vi.fn().mockResolvedValue(system),
    getConfigurationDraft: vi.fn().mockResolvedValue({
      revision: 1,
      sections: {},
      changed_sections: [],
      test_receipts: {},
      secret_status: {},
    }),
    updateConfigurationDraft: vi.fn(),
    testConfigurationSection: vi.fn(),
    activateConfigurationDraft: vi.fn(),
    controlService: vi.fn(),
    controlGateway: vi.fn(),
    getResearchTasks: vi.fn().mockResolvedValue(research),
    createResearchTask: vi.fn(),
    cancelResearchTask: vi.fn(),
    getModels: vi.fn().mockResolvedValue(models),
    getEvidence: vi.fn(),
    getReadiness: vi.fn().mockResolvedValue(readiness),
    getProjection: vi.fn().mockResolvedValue(projection),
    startCampaign: vi.fn().mockResolvedValue({ state: "starting" }),
    pauseCampaign: vi.fn().mockResolvedValue({ state: "paused" }),
    emergencyStop: vi.fn().mockResolvedValue({ state: "stopped" }),
    sendSideMasterMessage: vi.fn(),
    decideSideMasterProposal: vi.fn(),
    subscribe: vi.fn((_onProjection, onConnection) => {
      connectionListener = onConnection;
      onConnection("connected", 0);
      return () => undefined;
    }),
  };
  return {
    api,
    emitConnection: (state, attempt = 0) => connectionListener(state, attempt),
  };
}


test("renders six Simplified Chinese work views without raw backend unavailable copy", async () => {
  const mock = apiMock();
  render(<App api={mock.api} />);

  expect(await screen.findByRole("heading", { name: "运行概览" })).toBeInTheDocument();
  for (const label of ["概览", "研究", "模型", "模拟盘", "证据", "设置"]) {
    expect(screen.getByRole("tab", { name: label })).toBeInTheDocument();
  }
  expect(screen.getAllByText("Auto Trade 模拟盘控制台")).toHaveLength(2);
  expect(screen.getByText("仅限模拟交易")).toBeInTheDocument();
  expect(screen.getByText("尚未完成配置")).toBeInTheDocument();
  expect(document.body.textContent).not.toMatch(/unavailable|No active gateway runs|Risk blocking/i);
});

test("keeps every gateway and campaign action visible with a Chinese blocker and remediation", async () => {
  const mock = apiMock();
  render(<App api={mock.api} />);
  await screen.findByRole("heading", { name: "运行概览" });

  fireEvent.click(screen.getByRole("tab", { name: "模拟盘" }));
  for (const action of [
    "启动 XTP", "停止 XTP", "重连 XTP", "选择 XTP",
    "启动 TORA", "停止 TORA", "重连 TORA", "选择 TORA",
    "启动模拟盘", "暂停模拟盘", "紧急停止",
  ]) {
    expect(screen.getByRole("button", { name: action })).toBeInTheDocument();
  }
  expect(screen.getAllByText("请先在设置中完成并激活配置").length).toBeGreaterThan(0);
  expect(screen.getByRole("button", { name: "启动模拟盘" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "紧急停止" })).toBeEnabled();
});

test("formats CNY correctly and keeps long immutable identities readable on mobile", async () => {
  Object.defineProperty(window, "innerWidth", { configurable: true, value: 390 });
  window.dispatchEvent(new Event("resize"));
  const mock = apiMock();
  render(<App api={mock.api} />);
  await screen.findByRole("heading", { name: "运行概览" });

  fireEvent.click(screen.getByRole("tab", { name: "证据" }));
  expect(screen.getByTestId("history-XTP-profit")).toHaveTextContent("¥100.00");
  expect(screen.getByTestId("history-TORA-profit")).toHaveTextContent("¥80.00");
  const identity = screen.getByTestId("candidate-digest");
  expect(identity).toHaveTextContent(digest("a"));
  expect(identity).toHaveStyle({ overflowWrap: "anywhere" });
  expect(document.documentElement.scrollWidth).toBeLessThanOrEqual(window.innerWidth);
});

test("translates connection recovery states and never surfaces raw English status codes", async () => {
  const mock = apiMock();
  render(<App api={mock.api} />);
  await screen.findByText("实时连接");

  act(() => mock.emitConnection("reconnecting", 2));
  expect(screen.getByText("正在重连，第 2 次")).toBeInTheDocument();
  expect(document.body.textContent).not.toMatch(/reconnecting|attempt|connected/i);

  act(() => mock.emitConnection("disconnected"));
  await waitFor(() => expect(screen.getByText("连接已断开")).toBeInTheDocument());
});

test("supports keyboard navigation and exposes descriptive labels for icon actions", async () => {
  const mock = apiMock();
  render(<App api={mock.api} />);
  await screen.findByRole("heading", { name: "运行概览" });

  const settings = screen.getByRole("tab", { name: "设置" });
  settings.focus();
  fireEvent.keyDown(settings, { key: "Enter" });
  expect(await screen.findByRole("heading", { name: "系统设置" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "刷新系统状态" })).toHaveAttribute("title", "刷新系统状态");
  expect(screen.queryByRole("button", { name: /order|cancel|下单|撤单/i })).not.toBeInTheDocument();
});

import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { vi } from "vitest";

import { App } from "./App";
import type { ConnectionState, DemoApi, DemoProjection } from "./api";


const digest = (character: string): string => `sha256:${character.repeat(64)}`;

const projection: DemoProjection = {
  contract_version: 1,
  entity_type: "investor_demo_projection",
  revision: 4,
  source_revision: 18,
  source_digest: digest("1"),
  projection_digest: digest("2"),
  previous_projection_digest: digest("3"),
  updated_at_ms: 1_722_000_001_000,
  performance_scope: "broker_simulation",
  candidate: {
    candidate_digest: digest("a"),
    author_lineage_digest: digest("b"),
    package_digest: digest("c"),
    readiness: "ready",
  },
  current: {
    label: "current_broker_simulation",
    campaign_digest: digest("d"),
    campaign_state: "active",
    gateways: [
      {
        gateway: "XTP",
        run_digest: digest("e"),
        state: "active",
        connection_state: "connected",
        reconciliation_state: "complete",
        net_profit_minor: 3_200,
        realized_profit_minor: 3_700,
        unrealized_profit_minor: 250,
        fees_minor: 750,
        return_bps: 32,
        max_drawdown_bps: 41,
        fill_count: 17,
        positions: [
          {
            symbol: "600000.SSE",
            quantity: 100,
            available_quantity: 0,
            marked_value_minor: 102_300,
            unrealized_profit_minor: 2_300,
            t_plus_one_locked_quantity: 100,
          },
        ],
        gross_exposure_minor: 102_300,
        risk_headroom_minor: 897_700,
        local_latency_us: { count: 10_000, p50: 1_200, p95: 3_400, p99: 5_600, max: 7_800 },
        broker_latency_us: { count: 17, p50: 18_000, p95: 31_000, p99: 45_000, max: 52_000 },
        incidents: [],
        residual_exposure_minor: 0,
        working_order_count: 0,
        unresolved_outcomes: 0,
        permitted_next_action: "pause",
      },
      {
        gateway: "TORA",
        run_digest: digest("f"),
        state: "active",
        connection_state: "connected",
        reconciliation_state: "complete",
        net_profit_minor: 2_800,
        realized_profit_minor: 3_100,
        unrealized_profit_minor: 300,
        fees_minor: 600,
        return_bps: 28,
        max_drawdown_bps: 37,
        fill_count: 14,
        positions: [],
        gross_exposure_minor: 89_000,
        risk_headroom_minor: 911_000,
        local_latency_us: { count: 10_000, p50: 1_100, p95: 3_200, p99: 5_300, max: 7_100 },
        broker_latency_us: { count: 14, p50: 16_000, p95: 29_000, p99: 42_000, max: 49_000 },
        incidents: ["QUOTE_STALE_RECOVERED"],
        residual_exposure_minor: 0,
        working_order_count: 0,
        unresolved_outcomes: 0,
        permitted_next_action: "pause",
      },
    ],
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
      retained_at_ms: 1_722_000_000_000,
    },
  ],
  risk_state: "normal",
  permitted_actions: ["pause", "emergency_stop"],
};

function apiMock(): {
  api: DemoApi;
  emitProjection: (value: DemoProjection) => void;
  emitConnection: (state: ConnectionState, attempt?: number) => void;
} {
  let projectionListener: (value: DemoProjection) => void = () => undefined;
  let connectionListener: (state: ConnectionState, attempt: number) => void = () => undefined;
  const api: DemoApi = {
    getProjection: vi.fn().mockResolvedValue(projection),
    startCampaign: vi.fn().mockResolvedValue({ state: "starting" }),
    pauseCampaign: vi.fn().mockResolvedValue({ state: "paused" }),
    emergencyStop: vi.fn().mockResolvedValue({ state: "stopped" }),
    subscribe: vi.fn((onProjection, onConnection) => {
      projectionListener = onProjection;
      connectionListener = onConnection;
      onConnection("connected", 0);
      return () => undefined;
    }),
  };
  return {
    api,
    emitProjection: (value) => projectionListener(value),
    emitConnection: (state, attempt = 0) => connectionListener(state, attempt),
  };
}

test("renders current broker activity separately from historical signed evidence", async () => {
  const mock = apiMock();
  render(<App api={mock.api} />);

  expect(await screen.findByText("Current broker simulation")).toBeInTheDocument();
  expect(screen.getByText("Historical signed evidence")).toBeInTheDocument();
  expect(screen.getByTestId("current-XTP-profit")).toHaveTextContent("¥32.00");
  expect(screen.getByTestId("current-TORA-profit")).toHaveTextContent("¥28.00");
  expect(screen.getByTestId("history-XTP-profit")).toHaveTextContent("¥100.00");
  expect(screen.getByTestId("history-TORA-profit")).toHaveTextContent("¥80.00");
  expect(screen.getByText("Simulation only")).toBeInTheDocument();
});

test("shows dual gateway metrics, evidence identities, and narrow-screen-safe long values", async () => {
  Object.defineProperty(window, "innerWidth", { configurable: true, value: 390 });
  window.dispatchEvent(new Event("resize"));
  const mock = apiMock();
  render(<App api={mock.api} />);

  const candidate = await screen.findByTestId("candidate-digest");
  expect(candidate).toHaveTextContent(digest("a"));
  expect(candidate).toHaveStyle({ overflowWrap: "anywhere" });
  expect(screen.getByText("5.6 ms")).toBeInTheDocument();
  expect(screen.getByText("45.0 ms")).toBeInTheDocument();
  expect(screen.getByText("600000.SSE")).toBeInTheDocument();
  expect(screen.getByText("17 fills")).toBeInTheDocument();
  expect(screen.getByText("Signed and verified")).toBeInTheDocument();
});

test("surfaces websocket recovery and applies the recovered projection without relabeling history", async () => {
  const mock = apiMock();
  render(<App api={mock.api} />);
  await screen.findByText("Current broker simulation");

  act(() => mock.emitConnection("reconnecting", 2));
  expect(screen.getByText("Reconnecting · attempt 2")).toBeInTheDocument();

  const recovered: DemoProjection = {
    ...projection,
    revision: 5,
    current: {
      ...projection.current,
      gateways: projection.current.gateways.map((gateway) => ({
        ...gateway,
        net_profit_minor: gateway.gateway === "XTP" ? 3_500 : 3_100,
      })),
    },
  };
  act(() => {
    mock.emitProjection(recovered);
    mock.emitConnection("connected");
  });

  await waitFor(() => expect(screen.getByTestId("current-XTP-profit")).toHaveTextContent("¥35.00"));
  expect(screen.getByTestId("history-XTP-profit")).toHaveTextContent("¥100.00");
  expect(screen.getByText("Live connection")).toBeInTheDocument();
});

test("offers only campaign, pause, and emergency controls", async () => {
  const mock = apiMock();
  render(<App api={mock.api} />);
  await screen.findByText("Current broker simulation");

  fireEvent.click(screen.getByRole("button", { name: "Pause campaign" }));
  fireEvent.click(screen.getByRole("button", { name: "Emergency stop" }));

  await waitFor(() => expect(mock.api.pauseCampaign).toHaveBeenCalledOnce());
  expect(mock.api.emergencyStop).toHaveBeenCalledOnce();
  expect(screen.queryByRole("button", { name: /order|cancel/i })).not.toBeInTheDocument();
});

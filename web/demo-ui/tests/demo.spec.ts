import { expect, test, type Page, type WebSocketRoute } from "@playwright/test";

const digest = (character: string): string => `sha256:${character.repeat(64)}`;

const gateway = (name: "XTP" | "TORA", profit: number) => ({
  gateway: name,
  run_digest: digest(name === "XTP" ? "e" : "f"),
  state: "active",
  connection_state: "connected",
  reconciliation_state: "complete",
  net_profit_minor: profit,
  realized_profit_minor: profit + 500,
  unrealized_profit_minor: 250,
  fees_minor: 750,
  return_bps: name === "XTP" ? 32 : 28,
  max_drawdown_bps: name === "XTP" ? 41 : 37,
  fill_count: name === "XTP" ? 17 : 14,
  positions: [],
  gross_exposure_minor: 102_300,
  risk_headroom_minor: 897_700,
  local_latency_us: { count: 10_000, p50: 1_200, p95: 3_400, p99: 5_600, max: 7_800 },
  broker_latency_us: { count: 17, p50: 18_000, p95: 31_000, p99: 45_000, max: 52_000 },
  incidents: [],
  residual_exposure_minor: 0,
  working_order_count: 0,
  unresolved_outcomes: 0,
  permitted_next_action: "pause",
});

const projection = {
  contract_version: 1,
  entity_type: "investor_demo_projection",
  revision: 7,
  source_revision: 21,
  source_digest: digest("1"),
  projection_digest: digest("2"),
  previous_projection_digest: digest("3"),
  updated_at_ms: 1_753_600_000_000,
  performance_scope: "broker_simulation",
  candidate: {
    candidate_digest: digest("a"),
    author_lineage_digest: digest("b"),
    package_digest: digest("c"),
    readiness: "ready",
  },
  current: {
    label: "current_broker_simulation",
    campaign_id: "b53bc59c-c626-4f16-8a3e-a3185c7dad23",
    campaign_digest: digest("d"),
    campaign_state: "active",
    gateways: [gateway("XTP", 3_200), gateway("TORA", 2_800)],
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
        {
          gateway: "XTP",
          net_profit_minor: 10_000,
          reconciled: true,
          hard_limit_breaches: 0,
          unresolved_outcomes: 0,
        },
        {
          gateway: "TORA",
          net_profit_minor: 8_000,
          reconciled: true,
          hard_limit_breaches: 0,
          unresolved_outcomes: 0,
        },
      ],
      retained_at_ms: 1_753_600_000_000,
    },
  ],
  risk_state: "normal",
  permitted_actions: ["pause", "emergency_stop"],
};

const readiness = {
  state: "ready",
  ready: true,
  candidate_digest: digest("a"),
  components: [
    { name: "run-xtp", state: "configured" },
    { name: "run-tora", state: "configured" },
    { name: "side-master", state: "unavailable" },
  ],
  blockers: [],
};

const envelope = (data: unknown, status: "ok" | "accepted" = "ok") => ({
  contract_version: 1,
  request_id: "b53bc59c-c626-4f16-8a3e-a3185c7dad24",
  status,
  revision: 7,
  data,
});

const controlReceipt = (action: "pause" | "emergency_stop") => ({
  contract_version: 1,
  action,
  state: action === "pause" ? "paused" : "stopped",
  request_digest: digest("4"),
  started_at_ns: 1_000_000_000,
  completed_at_ns: 1_100_000_000,
  hard_stop_deadline_met: true,
  gateways: [
    { gateway: "XTP", state: action === "pause" ? "paused" : "stopped", receipt_digest: digest("5") },
    { gateway: "TORA", state: action === "pause" ? "paused" : "stopped", receipt_digest: digest("6") },
  ],
  receipt_digest: digest(action === "pause" ? "7" : "8"),
});

async function installDeterministicBackend(page: Page) {
  const sockets: WebSocketRoute[] = [];
  const posts: string[] = [];

  await page.routeWebSocket("**/api/v1/events", (socket) => {
    sockets.push(socket);
    socket.send(JSON.stringify({ event: "projection.snapshot", data: projection }));
  });
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === "POST") posts.push(path);
    if (path === "/api/v1/projection") {
      await route.fulfill({ json: envelope(projection) });
    } else if (path === "/api/v1/readiness") {
      await route.fulfill({ json: envelope(readiness) });
    } else if (path.endsWith("/pause")) {
      await route.fulfill({ json: envelope(controlReceipt("pause"), "accepted") });
    } else if (path === "/api/v1/emergency-stop") {
      await route.fulfill({ json: envelope(controlReceipt("emergency_stop"), "accepted") });
    } else {
      await route.fulfill({ status: 404, json: { error: "unexpected acceptance route" } });
    }
  });
  return { sockets, posts };
}

test("desktop and mobile control, reconnect, chart, and screenshot acceptance", async ({ page }, testInfo) => {
  const consoleErrors: string[] = [];
  const pageErrors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  page.on("pageerror", (error) => pageErrors.push(error.message));
  const backend = await installDeterministicBackend(page);

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Current broker simulation" })).toBeVisible();
  await page.locator('meta[name="auto-trade-csrf"]').evaluate((element) => {
    element.setAttribute("content", "acceptance-csrf-token-0123456789abcdef0123456789abcdef");
  });
  await expect(page.getByText("Live connection")).toBeVisible();

  const chart = page.getByLabel("Current and historical profit comparison").locator(".profit-chart");
  await expect(chart.locator("svg")).toBeVisible();
  await expect(chart.locator(".recharts-rectangle")).toHaveCount(4);
  const chartBox = await chart.boundingBox();
  expect(chartBox?.width ?? 0).toBeGreaterThan(100);
  expect(chartBox?.height ?? 0).toBeGreaterThan(100);

  await page.getByRole("button", { name: "Pause campaign" }).click();
  await expect(page.getByRole("dialog", { name: "Confirm pause campaign" })).toBeVisible();
  await page.getByRole("button", { name: "Confirm pause" }).click();
  await expect(page.getByRole("status", { name: "Immutable control receipt" })).toContainText("pause");

  await page.getByRole("button", { name: "Emergency stop" }).click();
  await expect(page.getByRole("dialog", { name: "Confirm emergency stop" })).toBeVisible();
  await page.getByRole("button", { name: "Confirm emergency stop" }).click();
  await expect(page.getByRole("status", { name: "Immutable control receipt" })).toContainText("emergency stop");
  expect(backend.posts).toEqual([
    "/api/v1/campaigns/b53bc59c-c626-4f16-8a3e-a3185c7dad23/pause",
    "/api/v1/emergency-stop",
  ]);

  const socketCount = backend.sockets.length;
  backend.sockets.at(-1)?.close();
  await expect(page.getByText(/Reconnecting.*attempt 1/)).toBeVisible();
  await expect.poll(() => backend.sockets.length).toBeGreaterThan(socketCount);
  await expect(page.getByText("Live connection")).toBeVisible();

  const noHorizontalOverflow = await page.evaluate(
    () => document.documentElement.scrollWidth <= window.innerWidth,
  );
  expect(noHorizontalOverflow).toBe(true);
  expect(consoleErrors).toEqual([]);
  expect(pageErrors).toEqual([]);

  await page.screenshot({
    path: testInfo.outputPath(`${testInfo.project.name}-investor-demo.png`),
    fullPage: true,
  });
});

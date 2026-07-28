import { expect, test, type Page } from "@playwright/test";


const digest = (character: string): string => `sha256:${character.repeat(64)}`;

const projection = {
  contract_version: 1,
  entity_type: "investor_demo_projection",
  revision: 9,
  source_revision: 20,
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
    campaign_state: "stopped",
    gateways: [],
  },
  history: [],
  risk_state: "normal",
  permitted_actions: ["start", "emergency_stop"],
};

const actionIds = [
  "configuration.save",
  "configuration.test",
  "configuration.activate",
  "service.research.start",
  "service.research.stop",
  "service.research.restart",
  "service.model_xtp.start",
  "service.model_xtp.stop",
  "service.model_xtp.restart",
  "service.model_tora.start",
  "service.model_tora.stop",
  "service.model_tora.restart",
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
  "campaign.emergency_stop",
];

const envelope = (data: unknown, status: "ok" | "accepted" = "ok") => ({
  contract_version: 1,
  request_id: crypto.randomUUID(),
  status,
  revision: 9,
  data,
});

async function installOperationsBackend(page: Page) {
  const posts: Array<{ path: string; body: Record<string, unknown> }> = [];
  const puts: Array<{ path: string; body: Record<string, unknown> }> = [];
  const state = {
    revision: 9,
    researchState: "stopped",
    gateways: {
      XTP: { state: "stopped", selected: false },
      TORA: { state: "stopped", selected: false },
    },
    tasks: [] as Array<Record<string, unknown>>,
    draft: {
      revision: 1,
      sections: {
        ports: { web: 8765, supervisor: 8766, agentd: 18801, model_xtp: 18811, model_tora: 18812, run_xtp: 18821, run_tora: 18822, rqdata_fetcher: 8786 },
        rqdata: { endpoint: "https://rqdata.invalid", tick_required: true },
      },
      changed_sections: [],
      test_receipts: {},
      secret_status: {
        "rqdata.username": { configured: false },
        "rqdata.password": { configured: false },
      },
    },
  };

  const system = () => ({
    contract_version: 2,
    revision: state.revision,
    configuration: { state: "active", active_version: 2, draft_revision: state.draft.revision },
    services: [
      { service: "research", state: state.researchState, revision: state.revision, error_code: null },
      { service: "model_xtp", state: "ready", revision: 2, error_code: null },
      { service: "model_tora", state: "ready", revision: 2, error_code: null },
      { service: "rqdata_fetcher", state: "ready", revision: 2, error_code: null },
    ],
    gateways: Object.entries(state.gateways).map(([gateway, value]) => ({
      gateway,
      ...value,
      error_code: null,
      updated_at_ms: 1_753_600_001_000,
    })),
    actions: actionIds.map((action_id) => ({
      contract_version: 2,
      action_id,
      target: action_id.split(".").slice(0, -1).join("."),
      state: "enabled",
      blockers: [],
      remediation: [],
      expected_revision: state.revision,
    })),
  });

  await page.routeWebSocket("**/api/v1/events", (socket) => {
    socket.send(JSON.stringify({ event: "projection.snapshot", data: projection }));
  });
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const body = request.method() === "GET" ? {} : request.postDataJSON() as Record<string, unknown>;
    if (request.method() === "POST") posts.push({ path, body });
    if (request.method() === "PUT") puts.push({ path, body });

    if (request.method() === "GET" && path === "/api/v1/system") {
      return route.fulfill({ json: envelope(system()) });
    }
    if (request.method() === "GET" && path === "/api/v1/config/draft") {
      return route.fulfill({ json: envelope(state.draft) });
    }
    if (request.method() === "PUT" && path === "/api/v1/config/draft") {
      state.draft = {
        ...state.draft,
        revision: state.draft.revision + 1,
        sections: body.sections as typeof state.draft.sections,
        changed_sections: Object.keys(body.sections as object),
        secret_status: {
          "rqdata.username": { configured: true },
          "rqdata.password": { configured: true },
        },
      };
      return route.fulfill({ json: envelope(state.draft) });
    }
    if (path === "/api/v1/config/draft/test") {
      const section = body.section as string;
      state.draft.test_receipts = {
        ...state.draft.test_receipts,
        [section]: { passed: true, code: "PASSED", expires_at_ms: 1_753_686_400_000 },
      };
      return route.fulfill({ status: 202, json: envelope({ section, passed: true, expires_at_ms: 1_753_686_400_000 }, "accepted") });
    }
    if (path === "/api/v1/config/draft/activate") {
      return route.fulfill({
        status: 202,
        json: envelope({ state: "active", version: 3, next_url: "http://127.0.0.1:8877" }, "accepted"),
      });
    }
    if (request.method() === "GET" && path === "/api/v1/readiness") {
      return route.fulfill({ json: envelope({ state: "ready", ready: true, candidate_digest: digest("a"), components: [], blockers: [] }) });
    }
    if (request.method() === "GET" && path === "/api/v1/projection") {
      return route.fulfill({ json: envelope(projection) });
    }
    if (request.method() === "GET" && path === "/api/v1/research/tasks") {
      return route.fulfill({ json: envelope({ revision: state.revision, tasks: state.tasks }) });
    }
    if (request.method() === "POST" && path === "/api/v1/research/tasks") {
      const task = {
        contract_version: 1,
        task_id: "b53bc59c-c626-4f16-8a3e-a3185c7dad23",
        task_digest: digest("d"),
        source: "operator",
        objective: body.objective,
        priority: body.priority,
        state: "queued",
        created_at_ms: 1_753_600_001_000,
        expires_at_ms: body.expires_at_ms,
        not_before_boundary: "immediate_safe_boundary",
      };
      state.tasks.push(task);
      return route.fulfill({ status: 202, json: envelope({ revision: state.revision, task }, "accepted") });
    }
    if (request.method() === "GET" && path === "/api/v1/models") {
      return route.fulfill({ json: envelope({
        revision: 5,
        current_candidate: {
          state: "ready",
          candidate_digest: digest("a"),
          package_digest: digest("c"),
          family: "lasso",
          publication_revision: 2,
        },
        runs: [
          { run_id: "rule-01", family: "rule", state: "evaluated", progress_percent: 100, artifact_digest: digest("e"), error_code: null },
          { run_id: "lasso-01", family: "lasso", state: "evaluated", progress_percent: 100, artifact_digest: digest("f"), error_code: null },
          { run_id: "lightgbm-01", family: "lightgbm", state: "running", progress_percent: 64, artifact_digest: null, error_code: null },
          { run_id: "mlp-01", family: "mlp", state: "prepared", progress_percent: 0, artifact_digest: null, error_code: null },
        ],
      }) });
    }
    const service = path.match(/^\/api\/v1\/services\/(research|model_xtp|model_tora)\/(start|stop|restart)$/);
    if (service) {
      if (service[1] === "research") state.researchState = service[2] === "stop" ? "stopped" : "ready";
      state.revision += 1;
      return route.fulfill({ status: 202, json: envelope({ service: service[1], state: state.researchState, revision: state.revision }, "accepted") });
    }
    const gateway = path.match(/^\/api\/v1\/gateways\/(XTP|TORA)\/(start|stop|reconnect|select)$/);
    if (gateway) {
      const selected = state.gateways[gateway[1] as "XTP" | "TORA"];
      if (gateway[2] === "select") selected.selected = body.selected as boolean;
      else selected.state = gateway[2] === "stop" ? "stopped" : "connected";
      state.revision += 1;
      return route.fulfill({ status: 202, json: envelope({ gateway: gateway[1], ...selected, revision: state.revision }, "accepted") });
    }
    if (path === "/api/v1/campaigns") {
      projection.current.campaign_id = "c53bc59c-c626-4f16-8a3e-a3185c7dad23";
      projection.current.campaign_state = "starting";
      return route.fulfill({ status: 202, json: envelope({ state: "starting" }, "accepted") });
    }
    return route.fulfill({ status: 404, json: { error: `unexpected route ${path}` } });
  });

  return { posts, puts, state };
}


test("settings draft, tests, activation, and port handoff are operable", async ({ page }) => {
  const backend = await installOperationsBackend(page);
  await page.goto("/");
  await page.getByRole("tab", { name: "设置" }).click();

  await page.getByLabel("Web 端口").fill("8877");
  await page.getByLabel("RQData 用户名").fill("demo-user");
  await page.getByLabel("RQData 密码").fill("write-only-secret");
  await page.getByRole("button", { name: "保存草稿" }).click();
  await expect(page.getByText("草稿已保存")).toBeVisible();
  expect(JSON.stringify(backend.puts)).toContain("demo-user");

  await page.getByRole("button", { name: "测试端口" }).click();
  await expect(page.getByText("端口测试通过")).toBeVisible();
  await page.getByRole("button", { name: "激活配置" }).click();
  const handoff = page.getByRole("link", { name: "打开新控制台地址" });
  await expect(handoff).toHaveAttribute("href", "http://127.0.0.1:8877");
  await expect(page.getByLabel("RQData 密码")).toHaveValue("");
});


test("independent gateways, selected campaign, research, model pipeline, and restart work", async ({ page }) => {
  const backend = await installOperationsBackend(page);
  await page.goto("/");

  await page.getByRole("tab", { name: "概览" }).click();
  await page.getByRole("button", { name: "启动研究服务" }).click();
  await expect(page.getByText("研究服务已就绪")).toBeVisible();

  await page.getByRole("tab", { name: "模拟盘" }).click();
  await page.getByRole("button", { name: "启动 XTP" }).click();
  await expect(page.getByTestId("gateway-XTP-state")).toHaveText("已连接");
  await expect(page.getByTestId("gateway-TORA-state")).toHaveText("已停止");
  await page.getByRole("button", { name: "选择 XTP" }).click();
  await page.getByRole("button", { name: "启动模拟盘" }).click();
  await expect(page.getByText("模拟盘正在启动")).toBeVisible();

  await page.getByRole("tab", { name: "研究" }).click();
  await page.getByLabel("研究目标").fill("研究低换手率回撤控制因子");
  await page.getByRole("button", { name: "创建研究任务" }).click();
  await expect(page.getByText("研究低换手率回撤控制因子")).toBeVisible();
  await expect(page.getByText("等待调度")).toBeVisible();

  await page.getByRole("tab", { name: "模型" }).click();
  await expect(page.getByText("Lasso")).toBeVisible();
  await expect(page.getByText("LightGBM")).toBeVisible();
  await expect(page.getByText("64%")).toBeVisible();

  await page.reload();
  await page.getByRole("tab", { name: "模拟盘" }).click();
  await expect(page.getByTestId("gateway-XTP-state")).toHaveText("已连接");
  await expect(page.getByRole("button", { name: "取消选择 XTP" })).toBeVisible();
  expect(backend.posts.map((item) => item.path)).toEqual(expect.arrayContaining([
    "/api/v1/services/research/start",
    "/api/v1/gateways/XTP/start",
    "/api/v1/gateways/XTP/select",
    "/api/v1/campaigns",
    "/api/v1/research/tasks",
  ]));

  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
  expect(overflow).toBe(false);
});

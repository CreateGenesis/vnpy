import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Activity,
  AlertTriangle,
  BarChart3,
  BookOpen,
  Bot,
  CheckCircle2,
  Clock3,
  Database,
  FileCheck2,
  Gauge,
  LayoutDashboard,
  Octagon,
  Pause,
  Play,
  Radio,
  RefreshCw,
  RotateCw,
  Settings,
  ShieldCheck,
  Square,
} from "lucide-react";
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { createDemoApi } from "./api";
import type {
  ActionState,
  ConfigurationDraftProjection,
  ConnectionState,
  DemoApi,
  DemoProjection,
  DemoReadiness,
  GatewayAction,
  GatewayName,
  HistoricalEvidenceProjection,
  ModelPipelineProjection,
  ResearchProjection,
  ServiceAction,
  ServiceName,
  SystemProjection,
} from "./api";
import { ModelsView } from "./components/ModelsView";
import { ResearchView } from "./components/ResearchView";
import { SettingsView } from "./components/SettingsView";
import { SimulationView } from "./components/SimulationView";
import {
  blockerLabel,
  cny,
  connectionLabel,
  errorLabel,
  serviceLabel,
  stateLabel,
} from "./i18n";


interface AppProps {
  api?: DemoApi;
}

type WorkView = "overview" | "research" | "models" | "simulation" | "evidence" | "settings";
type Confirmation = "pause" | "emergency";

const views: Array<{ id: WorkView; label: string; icon: typeof Gauge }> = [
  { id: "overview", label: "概览", icon: LayoutDashboard },
  { id: "research", label: "研究", icon: BookOpen },
  { id: "models", label: "模型", icon: Bot },
  { id: "simulation", label: "模拟盘", icon: Gauge },
  { id: "evidence", label: "证据", icon: FileCheck2 },
  { id: "settings", label: "设置", icon: Settings },
];

const emptyResearch: ResearchProjection = { revision: 0, tasks: [] };
const emptyModels: ModelPipelineProjection = { revision: 0, current_candidate: null, runs: [] };

const findAction = (system: SystemProjection, actionId: string): ActionState | undefined =>
  system.actions.find((action) => action.action_id === actionId);

const actionReason = (action: ActionState | undefined): string | null => {
  const blocker = action?.blockers[0];
  return blocker ? blockerLabel(blocker.code, blocker.parameters) : null;
};

export function App({ api }: AppProps) {
  const client = useMemo(() => api ?? createDemoApi(), [api]);
  const [view, setView] = useState<WorkView>("overview");
  const [system, setSystem] = useState<SystemProjection | null>(null);
  const [draft, setDraft] = useState<ConfigurationDraftProjection | null>(null);
  const [projection, setProjection] = useState<DemoProjection | null>(null);
  const [readiness, setReadiness] = useState<DemoReadiness | null>(null);
  const [research, setResearch] = useState<ResearchProjection>(emptyResearch);
  const [models, setModels] = useState<ModelPipelineProjection>(emptyModels);
  const [connection, setConnection] = useState<{ state: ConnectionState; attempt: number }>({ state: "connecting", attempt: 0 });
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [confirmation, setConfirmation] = useState<Confirmation | null>(null);
  const sessionId = useMemo(() => `side-session-${crypto.randomUUID()}`, []);

  const refresh = useCallback(async (): Promise<void> => {
    const results = await Promise.allSettled([
      client.getSystem(),
      client.getConfigurationDraft(),
      client.getProjection(),
      client.getReadiness(),
      client.getResearchTasks(),
      client.getModels(),
    ]);
    if (results[0].status === "fulfilled") setSystem(results[0].value);
    if (results[1].status === "fulfilled") setDraft(results[1].value);
    if (results[2].status === "fulfilled") setProjection(results[2].value);
    if (results[3].status === "fulfilled") setReadiness(results[3].value);
    setResearch(results[4].status === "fulfilled" ? results[4].value : emptyResearch);
    setModels(results[5].status === "fulfilled" ? results[5].value : emptyModels);
    if (results.slice(0, 4).some((result) => result.status === "rejected")) {
      setError("系统状态读取失败，请检查本地服务后刷新");
    }
  }, [client]);

  useEffect(() => {
    let active = true;
    void refresh();
    const unsubscribe = client.subscribe(
      (value) => active && setProjection(value),
      (state, attempt) => active && setConnection({ state, attempt }),
    );
    return () => {
      active = false;
      unsubscribe();
    };
  }, [client, refresh]);

  const run = async (name: string, operation: () => Promise<unknown>, success: string): Promise<void> => {
    setBusy(name);
    setNotice(null);
    setError(null);
    try {
      await operation();
      setNotice(success);
      await refresh();
    } catch (cause) {
      setError(errorLabel(cause instanceof Error ? cause.message : "BACKEND_OPERATION_FAILED"));
    } finally {
      setBusy(null);
    }
  };

  if (system === null || draft === null || projection === null || readiness === null) {
    return (
      <main className="loading-state">
        <RefreshCw className="spin" size={22} />
        <strong>正在加载操作台</strong>
        {error && <span>{error}</span>}
      </main>
    );
  }

  const serviceAction = (service: ServiceName, action: ServiceAction): void => {
    const contract = findAction(system, `service.${service}.${action}`);
    void run(
      `service:${service}:${action}`,
      () => client.controlService(service, action, contract?.expected_revision ?? system.revision),
      action === "stop" ? `${serviceLabel(service)}已停止` : `${serviceLabel(service)}已就绪`,
    );
  };
  const gatewayAction = (gateway: GatewayName, action: GatewayAction, selected?: boolean): void => {
    const contract = findAction(system, `gateway.${gateway.toLowerCase()}.${action}`);
    void run(
      `gateway:${gateway}:${action}`,
      () => client.controlGateway(gateway, action, contract?.expected_revision ?? system.revision, selected),
      action === "select"
        ? `${gateway}${selected ? " 已选择" : " 已取消选择"}`
        : `${gateway}${action === "stop" ? " 已停止" : " 已连接"}`,
    );
  };
  const startCampaign = (): void => {
    const selected = system.gateways.filter((gateway) => gateway.selected).map((gateway) => gateway.gateway);
    void run("campaign:start", () => client.startCampaign(projection.candidate.candidate_digest, selected), "模拟盘正在启动");
  };
  const pauseCampaign = (): void => {
    setConfirmation(null);
    if (!projection.current.campaign_id) return;
    void run("campaign:pause", () => client.pauseCampaign(projection.current.campaign_id ?? ""), "模拟盘已暂停");
  };
  const emergencyStop = (): void => {
    setConfirmation(null);
    void run("campaign:emergency", () => client.emergencyStop(), "紧急停止已执行");
  };

  const activeView = (() => {
    if (view === "overview") {
      return <OverviewView system={system} projection={projection} models={models} research={research} busy={busy} notice={notice} onService={serviceAction} onNavigate={setView} />;
    }
    if (view === "research") {
      const researchService = system.services.find((service) => service.service === "research");
      return <ResearchView api={client} research={research} available={researchService?.state === "ready" || researchService?.state === "running"} sessionId={sessionId} missionId={`research-${projection.candidate.candidate_digest}`} onResearch={setResearch} />;
    }
    if (view === "models") return <ModelsView models={models} />;
    if (view === "simulation") {
      return <SimulationView system={system} projection={projection} busy={busy} notice={notice} onGateway={gatewayAction} onStartCampaign={startCampaign} onPauseCampaign={() => setConfirmation("pause")} onEmergencyStop={() => setConfirmation("emergency")} />;
    }
    if (view === "evidence") return <EvidenceView projection={projection} />;
    return <SettingsView api={client} draft={draft} onDraft={setDraft} onChanged={refresh} />;
  })();

  return (
    <div className="operations-shell">
      <aside className="sidebar">
        <div className="brand"><div className="brand-icon"><Gauge size={21} /></div><div><strong>Auto Trade 模拟盘控制台</strong><span>vn.py 权威运行边界</span></div></div>
        <nav className="work-nav" role="tablist" aria-label="工作视图">
          {views.map((item) => {
            const Icon = item.icon;
            return <button key={item.id} role="tab" aria-selected={view === item.id} className={view === item.id ? "active" : ""} onClick={() => setView(item.id)} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") setView(item.id); }}><Icon size={17} />{item.label}</button>;
          })}
        </nav>
        <div className="authority-scope"><ShieldCheck size={17} /><div><strong>仅限模拟交易</strong><span>Agent 无网关和交易权限</span></div></div>
      </aside>

      <div className="main-column">
        <header className="topbar">
          <div className="mobile-brand">Auto Trade 模拟盘控制台</div>
          <div className="topbar-status">
            <span className={`connection ${connection.state}`}><Radio size={14} />{connectionLabel(connection.state, connection.attempt)}</span>
            <span className="updated"><Clock3 size={14} />{new Date(projection.updated_at_ms).toLocaleTimeString("zh-CN")}</span>
            <button className="icon-button" title="刷新系统状态" aria-label="刷新系统状态" disabled={busy !== null} onClick={() => void refresh()}><RefreshCw size={16} /></button>
          </div>
        </header>
        <main className="workspace">
          {error && <div className="notice danger" role="alert"><AlertTriangle size={16} />{error}</div>}
          {activeView}
        </main>
      </div>
      {confirmation && (
        <ConfirmationDialog
          action={confirmation}
          onCancel={() => setConfirmation(null)}
          onConfirm={confirmation === "pause" ? pauseCampaign : emergencyStop}
        />
      )}
    </div>
  );
}

function OverviewView({ system, projection, models, research, busy, notice, onService, onNavigate }: {
  system: SystemProjection;
  projection: DemoProjection;
  models: ModelPipelineProjection;
  research: ResearchProjection;
  busy: string | null;
  notice: string | null;
  onService: (service: ServiceName, action: ServiceAction) => void;
  onNavigate: (view: WorkView) => void;
}) {
  const configured = system.configuration.state === "active";
  return (
    <section className="work-view" aria-labelledby="overview-heading">
      <header className="view-heading"><div><span className="view-kicker">本机运行状态</span><h1 id="overview-heading">运行概览</h1></div><div className={`state-chip ${configured ? "ready" : "blocked"}`}>{configured ? `配置版本 ${system.configuration.active_version}` : "尚未完成配置"}</div></header>
      {notice && <div className="notice success" role="status"><CheckCircle2 size={16} />{notice}</div>}
      <section className="summary-strip" aria-label="核心状态">
        <SummaryItem icon={Database} label="配置" value={configured ? "已激活" : "等待配置"} tone={configured ? "ok" : "warning"} />
        <SummaryItem icon={Bot} label="当前候选" value={models.current_candidate ? stateLabel(models.current_candidate.state) : "尚无候选"} />
        <SummaryItem icon={Activity} label="研究任务" value={`${research.tasks.length} 个`} />
        <SummaryItem icon={Gauge} label="模拟盘" value={stateLabel(projection.current.campaign_state)} />
      </section>

      <section className="overview-section" aria-labelledby="services-heading">
        <div className="section-line"><h2 id="services-heading">服务控制</h2><span>固定服务白名单</span></div>
        <div className="service-list">
          {system.services.map((service) => {
            const actions = (["start", "stop", "restart"] as const).map((action) => findAction(system, `service.${service.service}.${action}`));
            const reason = actions.map(actionReason).find((value) => value !== null);
            return <article className="service-row" key={service.service}>
              <div className="service-identity"><span className={`health-dot ${service.state}`} /><div><strong>{serviceLabel(service.service)}</strong><span>{reason ?? "服务状态由本机 Supervisor 核验"}</span></div></div>
              <span className={`state-chip ${service.state}`}>{stateLabel(service.state)}</span>
              <div className="row-actions">
                <button className="button primary" disabled={busy !== null || actions[0]?.state !== "enabled"} onClick={() => onService(service.service, "start")}><Play size={14} />启动{service.service === "research" ? "研究服务" : ""}</button>
                <button className="icon-button" title={`停止${serviceLabel(service.service)}`} aria-label={`停止${serviceLabel(service.service)}`} disabled={busy !== null || actions[1]?.state !== "enabled"} onClick={() => onService(service.service, "stop")}><Square size={15} /></button>
                <button className="icon-button" title={`重启${serviceLabel(service.service)}`} aria-label={`重启${serviceLabel(service.service)}`} disabled={busy !== null || actions[2]?.state !== "enabled"} onClick={() => onService(service.service, "restart")}><RotateCw size={15} /></button>
              </div>
            </article>;
          })}
        </div>
      </section>

      <section className="quick-actions" aria-labelledby="quick-heading">
        <div><h2 id="quick-heading">下一步</h2><p>{configured ? "检查模型和网关后启动模拟盘。" : "先完成设置、分区测试和配置激活。"}</p></div>
        <div><button className="button secondary" onClick={() => onNavigate("settings")}><Settings size={15} />打开设置</button><button className="button secondary" onClick={() => onNavigate("simulation")}><Gauge size={15} />管理模拟盘</button></div>
      </section>
    </section>
  );
}

function SummaryItem({ icon: Icon, label, value, tone }: { icon: typeof Gauge; label: string; value: string; tone?: string }) {
  return <div className={`summary-item ${tone ?? ""}`}><Icon size={18} /><div><span>{label}</span><strong>{value}</strong></div></div>;
}

function EvidenceView({ projection }: { projection: DemoProjection }) {
  const history = projection.history;
  const chartData = history[0]?.gateways.map((gateway) => ({ gateway: gateway.gateway, profit: gateway.net_profit_minor / 100 })) ?? [];
  return (
    <section className="work-view" aria-labelledby="evidence-heading">
      <header className="view-heading"><div><span className="view-kicker">签名且可重建</span><h1 id="evidence-heading">模拟盘证据</h1></div><div className="revision-badge">当前状态与历史证据分开展示</div></header>
      <section className="current-evidence-band"><div><span>当前候选</span><code className="digest" data-testid="candidate-digest" style={{ overflowWrap: "anywhere" }}>{projection.candidate.candidate_digest}</code></div><div><span>当前模拟盘</span><strong>{stateLabel(projection.current.campaign_state)}</strong></div><div><span>风控状态</span><strong>{stateLabel(projection.risk_state)}</strong></div></section>
      {history.length === 0 ? <div className="empty-panel">尚无已封存的五日双网关证据。当前状态不会被显示为历史盈利。</div> : (
        <div className="evidence-layout">
          <div className="evidence-list">{history.map((item) => <EvidenceRow key={item.evidence_digest} evidence={item} />)}</div>
          <aside className="evidence-chart" aria-label="XTP 与 TORA 历史净收益比较">
            <div><strong>双网关净收益</strong><span>扣费后，人民币</span></div>
            <div className="profit-chart">{chartData.length > 0 && <ResponsiveContainer width="100%" height="100%"><BarChart data={chartData} margin={{ top: 12, right: 12, left: 0, bottom: 0 }}><CartesianGrid stroke="#dfe4e2" vertical={false} /><XAxis dataKey="gateway" tickLine={false} axisLine={false} /><YAxis tickLine={false} axisLine={false} /><Tooltip formatter={(value) => cny(Number(value) * 100)} /><Bar dataKey="profit" fill="#176b57" radius={[3, 3, 0, 0]} /></BarChart></ResponsiveContainer>}</div>
          </aside>
        </div>
      )}
    </section>
  );
}

function EvidenceRow({ evidence }: { evidence: HistoricalEvidenceProjection }) {
  return <article className="evidence-row"><div className="evidence-title"><FileCheck2 size={19} /><div><strong>{evidence.ready ? "已签名并通过双网关门禁" : "已保留，未达到演示门禁"}</strong><span>{evidence.sessions[0]} 至 {evidence.sessions.at(-1)}</span></div></div><div className="evidence-profits">{evidence.gateways.map((gateway) => <div key={gateway.gateway}><span>{gateway.gateway}</span><strong className={gateway.net_profit_minor > 0 ? "positive" : "negative"} data-testid={`history-${gateway.gateway}-profit`}>{cny(gateway.net_profit_minor)}</strong><small>{gateway.reconciled ? "对账完成" : "对账未完成"}</small></div>)}</div><code className="digest">{evidence.evidence_digest}</code></article>;
}

function ConfirmationDialog({ action, onCancel, onConfirm }: { action: Confirmation; onCancel: () => void; onConfirm: () => void }) {
  const emergency = action === "emergency";
  return <div className="dialog-backdrop"><section className="confirmation-dialog" role="dialog" aria-modal="true" aria-label={emergency ? "确认紧急停止" : "确认暂停模拟盘"}><div className={emergency ? "dialog-icon danger" : "dialog-icon warning"}>{emergency ? <Octagon size={22} /> : <Pause size={22} />}</div><div><h2>{emergency ? "确认紧急停止" : "确认暂停模拟盘"}</h2><p>{emergency ? "vn.py 将立即阻止新增暴露，并处理符合条件的活动委托。" : "当前证据窗口会结束，系统将进入对账。"}</p></div><div className="dialog-actions"><button className="button secondary" onClick={onCancel}>取消</button><button className={emergency ? "button danger" : "button primary"} onClick={onConfirm}>{emergency ? "确认紧急停止" : "确认暂停"}</button></div></section></div>;
}

import { useEffect, useMemo, useState } from "react";
import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  Clock3,
  FileCheck2,
  Gauge,
  History,
  Octagon,
  Pause,
  Play,
  Radio,
  RefreshCw,
  ShieldCheck,
  WalletCards,
} from "lucide-react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { createDemoApi } from "./api";
import type {
  ConnectionState,
  DemoApi,
  DemoProjection,
  GatewayName,
  GatewayProjection,
  HistoricalEvidenceProjection,
} from "./api";


interface AppProps {
  api?: DemoApi;
}

interface ConnectionView {
  state: ConnectionState;
  attempt: number;
}

const money = (minor: number): string => {
  const sign = minor < 0 ? "-" : "";
  return `${sign}¥${(Math.abs(minor) / 100).toFixed(2)}`;
};

const basisPoints = (value: number): string => `${(value / 100).toFixed(2)}%`;
const milliseconds = (microseconds: number): string => `${(microseconds / 1_000).toFixed(1)} ms`;
const titleCase = (value: string): string => value.replaceAll("_", " ");

const connectionLabel = ({ state, attempt }: ConnectionView): string => {
  if (state === "connected") return "Live connection";
  if (state === "reconnecting") return `Reconnecting · attempt ${attempt}`;
  if (state === "connecting") return "Connecting";
  return "Disconnected";
};

const gatewayColor = (gateway: GatewayName): string => (gateway === "XTP" ? "#176b57" : "#2867a6");

function Metric({ label, value, tone }: { label: string; value: string; tone?: "positive" | "warning" }) {
  return (
    <div className="metric">
      <span>{label}</span>
      <strong className={tone ? `metric-value ${tone}` : "metric-value"}>{value}</strong>
    </div>
  );
}

function GatewayRun({ run }: { run: GatewayProjection }) {
  return (
    <article className="gateway-card" aria-labelledby={`${run.gateway}-heading`}>
      <header className="gateway-header">
        <div className="gateway-identity">
          <span className="gateway-mark" style={{ backgroundColor: gatewayColor(run.gateway) }} />
          <div>
            <h3 id={`${run.gateway}-heading`}>{run.gateway}</h3>
            <span>{titleCase(run.state)}</span>
          </div>
        </div>
        <span className={run.reconciliation_state === "complete" ? "state-tag ok" : "state-tag warning"}>
          {run.reconciliation_state === "complete" ? <CheckCircle2 size={14} /> : <AlertTriangle size={14} />}
          {titleCase(run.reconciliation_state)}
        </span>
      </header>

      <div className="profit-row">
        <div>
          <span>Net profit</span>
          <strong
            className={run.net_profit_minor > 0 ? "profit positive" : "profit negative"}
            data-testid={`current-${run.gateway}-profit`}
          >
            {money(run.net_profit_minor)}
          </strong>
        </div>
        <div className="return-value">
          <span>Return</span>
          <strong>{basisPoints(run.return_bps)}</strong>
        </div>
      </div>

      <div className="metric-grid">
        <Metric label="Realized" value={money(run.realized_profit_minor)} />
        <Metric label="Unrealized" value={money(run.unrealized_profit_minor)} />
        <Metric label="Fees" value={money(run.fees_minor)} />
        <Metric label="Max drawdown" value={basisPoints(run.max_drawdown_bps)} />
        <Metric label="Local p99" value={milliseconds(run.local_latency_us.p99)} />
        <Metric label="Broker p99" value={milliseconds(run.broker_latency_us.p99)} />
        <Metric label="Exposure" value={money(run.gross_exposure_minor)} />
        <Metric label="Risk headroom" value={money(run.risk_headroom_minor)} />
      </div>

      <div className="run-footer">
        <span><Activity size={15} />{run.fill_count} fills</span>
        <span><Radio size={15} />{titleCase(run.connection_state)}</span>
        <span><ShieldCheck size={15} />{run.unresolved_outcomes} unresolved</span>
      </div>

      {run.positions.length > 0 && (
        <div className="positions">
          <h4>Positions</h4>
          <div className="table-scroll">
            <table>
              <thead>
                <tr><th>Symbol</th><th>Quantity</th><th>Available</th><th>Marked value</th><th>P&amp;L</th></tr>
              </thead>
              <tbody>
                {run.positions.map((position) => (
                  <tr key={position.symbol}>
                    <td>{position.symbol}</td>
                    <td>{position.quantity}</td>
                    <td>{position.available_quantity}</td>
                    <td>{money(position.marked_value_minor)}</td>
                    <td className={position.unrealized_profit_minor >= 0 ? "positive" : "negative"}>
                      {money(position.unrealized_profit_minor)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </article>
  );
}

function EvidenceRow({ evidence }: { evidence: HistoricalEvidenceProjection }) {
  return (
    <article className="evidence-row">
      <div className="evidence-status">
        <FileCheck2 size={18} />
        <div>
          <strong>{evidence.ready ? "Signed and verified" : "Retained, not ready"}</strong>
          <span>{evidence.sessions[0]} to {evidence.sessions.at(-1)}</span>
        </div>
      </div>
      <div className="evidence-gateways">
        {evidence.gateways.map((gateway) => (
          <div key={gateway.gateway}>
            <span>{gateway.gateway}</span>
            <strong
              className={gateway.net_profit_minor > 0 ? "positive" : "negative"}
              data-testid={`history-${gateway.gateway}-profit`}
            >
              {money(gateway.net_profit_minor)}
            </strong>
          </div>
        ))}
      </div>
      <code className="digest" title="Evidence digest">{evidence.evidence_digest}</code>
    </article>
  );
}

export function App({ api }: AppProps) {
  const client = useMemo(() => api ?? createDemoApi(), [api]);
  const [projection, setProjection] = useState<DemoProjection | null>(null);
  const [connection, setConnection] = useState<ConnectionView>({ state: "connecting", attempt: 0 });
  const [error, setError] = useState<string | null>(null);
  const [pendingAction, setPendingAction] = useState<string | null>(null);
  const [actionResult, setActionResult] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    client.getProjection()
      .then((value) => active && setProjection(value))
      .catch(() => active && setError("Projection unavailable"));
    const unsubscribe = client.subscribe(
      (value) => active && setProjection(value),
      (state, attempt) => active && setConnection({ state, attempt }),
    );
    return () => {
      active = false;
      unsubscribe();
    };
  }, [client]);

  const runAction = async (name: string, operation: () => Promise<{ state: string }>): Promise<void> => {
    setPendingAction(name);
    setError(null);
    setActionResult(null);
    try {
      const result = await operation();
      setActionResult(titleCase(result.state));
    } catch {
      setError(`${name} failed`);
    } finally {
      setPendingAction(null);
    }
  };

  if (projection === null) {
    return (
      <main className="loading-state">
        <RefreshCw className="spin" size={22} />
        <span>{error ?? "Loading broker simulation state"}</span>
      </main>
    );
  }

  const chartData = projection.current.gateways.map((gateway) => ({
    gateway: gateway.gateway,
    current: gateway.net_profit_minor / 100,
    historical: projection.history[0]?.gateways.find((item) => item.gateway === gateway.gateway)?.net_profit_minor
      ? (projection.history[0].gateways.find((item) => item.gateway === gateway.gateway)?.net_profit_minor ?? 0) / 100
      : 0,
  }));
  const canStart = projection.permitted_actions.includes("start");
  const canPause = projection.permitted_actions.includes("pause") && projection.current.campaign_id !== null;
  const canStop = projection.permitted_actions.includes("emergency_stop");

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand-block">
          <div className="brand-mark"><Gauge size={21} /></div>
          <div>
            <strong>Auto Trade</strong>
            <span>Investor broker simulation</span>
          </div>
        </div>
        <div className="topbar-status">
          <span className="scope-tag">Simulation only</span>
          <span className={`connection ${connection.state}`}>
            {connection.state === "connected" ? <Radio size={14} /> : <RefreshCw size={14} />}
            {connectionLabel(connection)}
          </span>
          <span className="updated"><Clock3 size={14} />{new Date(projection.updated_at_ms).toLocaleTimeString()}</span>
        </div>
      </header>

      <main>
        <section className="identity-band">
          <div>
            <span className="eyebrow">Admitted candidate</span>
            <h1>Broker simulation</h1>
            <code className="digest" data-testid="candidate-digest" style={{ overflowWrap: "anywhere" }}>
              {projection.candidate.candidate_digest}
            </code>
          </div>
          <div className="identity-status">
            <span><ShieldCheck size={16} />{titleCase(projection.candidate.readiness)}</span>
            <span><WalletCards size={16} />{titleCase(projection.current.campaign_state)}</span>
            <span><Gauge size={16} />Risk {titleCase(projection.risk_state)}</span>
          </div>
        </section>

        <section className="control-band" aria-label="Campaign controls">
          <div className="control-copy">
            <strong>Campaign controls</strong>
            <span>{actionResult ? `Last receipt: ${actionResult}` : "vn.py authoritative path"}</span>
          </div>
          <div className="control-actions">
            {canStart && (
              <button
                className="button primary"
                disabled={pendingAction !== null}
                onClick={() => void runAction("Start campaign", () =>
                  client.startCampaign(projection.candidate.candidate_digest, ["XTP", "TORA"]))}
              >
                <Play size={16} />Start campaign
              </button>
            )}
            <button
              className="button secondary"
              disabled={!canPause || pendingAction !== null}
              onClick={() => void runAction("Pause campaign", () =>
                client.pauseCampaign(projection.current.campaign_id ?? ""))}
            >
              <Pause size={16} />Pause campaign
            </button>
            <button
              className="button danger"
              disabled={!canStop || pendingAction !== null}
              onClick={() => void runAction("Emergency stop", () => client.emergencyStop())}
            >
              <Octagon size={16} />Emergency stop
            </button>
          </div>
        </section>

        {error && <div className="error-band" role="alert"><AlertTriangle size={16} />{error}</div>}

        <section className="dashboard-section current-section" aria-labelledby="current-heading">
          <div className="section-heading">
            <div>
              <span className="eyebrow">Live marked state</span>
              <h2 id="current-heading">Current broker simulation</h2>
            </div>
            <span className="revision">Revision {projection.revision}</span>
          </div>
          <div className="current-grid">
            <div className="gateway-grid">
              {projection.current.gateways.map((run) => <GatewayRun key={run.gateway} run={run} />)}
            </div>
            <aside className="chart-panel" aria-label="Current and historical profit comparison">
              <div className="chart-title">
                <strong>Profit comparison</strong>
                <span>Current vs signed history</span>
              </div>
              <div className="chart-legend"><span className="current-key" />Current <span className="history-key" />Historical</div>
              <div className="profit-chart">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={chartData} margin={{ top: 12, right: 8, left: -16, bottom: 0 }}>
                    <CartesianGrid stroke="#e2e6e0" vertical={false} />
                    <XAxis dataKey="gateway" tickLine={false} axisLine={false} />
                    <YAxis tickLine={false} axisLine={false} />
                    <Tooltip formatter={(value: number) => `¥${value.toFixed(2)}`} />
                    <Bar dataKey="current" fill="#176b57" radius={[3, 3, 0, 0]} />
                    <Bar dataKey="historical" fill="#2867a6" radius={[3, 3, 0, 0]} />
                  </BarChart>
                </ResponsiveContainer>
              </div>
            </aside>
          </div>
        </section>

        <section className="dashboard-section history-section" aria-labelledby="history-heading">
          <div className="section-heading">
            <div>
              <span className="eyebrow">Retained five-session records</span>
              <h2 id="history-heading">Historical signed evidence</h2>
            </div>
            <History size={20} />
          </div>
          <div className="evidence-list">
            {projection.history.length > 0
              ? projection.history.map((item) => <EvidenceRow key={item.evidence_digest} evidence={item} />)
              : <div className="empty-state">No sealed campaign evidence</div>}
          </div>
        </section>
      </main>
    </div>
  );
}

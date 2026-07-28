import { AlertTriangle, Octagon, Pause, Play, RefreshCw, Square, Unplug } from "lucide-react";

import type {
  ActionState,
  DemoProjection,
  GatewayAction,
  GatewayName,
  SystemProjection,
} from "../api";
import { blockerLabel, cny, stateLabel } from "../i18n";


interface SimulationViewProps {
  system: SystemProjection;
  projection: DemoProjection;
  busy: string | null;
  notice: string | null;
  onGateway: (gateway: GatewayName, action: GatewayAction, selected?: boolean) => void;
  onStartCampaign: () => void;
  onPauseCampaign: () => void;
  onEmergencyStop: () => void;
}

const findAction = (system: SystemProjection, actionId: string): ActionState | undefined =>
  system.actions.find((action) => action.action_id === actionId);

const actionDisabled = (action: ActionState | undefined, busy: string | null): boolean =>
  busy !== null || action?.state !== "enabled";

const actionBlocker = (action: ActionState | undefined): string | null => {
  const blocker = action?.blockers[0];
  return blocker ? blockerLabel(blocker.code, blocker.parameters) : null;
};

export function SimulationView({
  system,
  projection,
  busy,
  notice,
  onGateway,
  onStartCampaign,
  onPauseCampaign,
  onEmergencyStop,
}: SimulationViewProps) {
  const start = findAction(system, "campaign.start");
  const pause = findAction(system, "campaign.pause");
  const emergency = findAction(system, "campaign.emergency_stop");
  return (
    <section className="work-view" aria-labelledby="simulation-heading">
      <header className="view-heading">
        <div>
          <span className="view-kicker">vn.py 独占交易权限</span>
          <h1 id="simulation-heading">模拟盘运行</h1>
        </div>
        <div className="state-chip">{stateLabel(projection.current.campaign_state)}</div>
      </header>

      {notice && <div className="notice info" role="status">{notice}</div>}

      <div className="gateway-workspace">
        {(["XTP", "TORA"] as const).map((gateway) => {
          const status = system.gateways.find((item) => item.gateway === gateway) ?? {
            gateway,
            state: "unconfigured",
            selected: false,
            error_code: null,
            updated_at_ms: 0,
          };
          const prefix = `gateway.${gateway.toLowerCase()}`;
          const startAction = findAction(system, `${prefix}.start`);
          const stopAction = findAction(system, `${prefix}.stop`);
          const reconnectAction = findAction(system, `${prefix}.reconnect`);
          const selectAction = findAction(system, `${prefix}.select`);
          const blocker = actionBlocker(startAction) ?? actionBlocker(selectAction);
          return (
            <article className="gateway-control" key={gateway}>
              <header>
                <div>
                  <span className={`gateway-dot ${gateway.toLowerCase()}`} />
                  <h2>{gateway}</h2>
                </div>
                <span className={`state-chip ${status.state}`} data-testid={`gateway-${gateway}-state`}>
                  {stateLabel(status.state)}
                </span>
              </header>
              <div className="gateway-selection">
                <span>{status.selected ? "已加入下一次模拟盘" : "未选择"}</span>
                <button
                  className={status.selected ? "button selected" : "button secondary"}
                  disabled={actionDisabled(selectAction, busy)}
                  onClick={() => onGateway(gateway, "select", !status.selected)}
                >
                  {status.selected ? <Unplug size={15} /> : <Play size={15} />}
                  {status.selected ? `取消选择 ${gateway}` : `选择 ${gateway}`}
                </button>
              </div>
              <div className="gateway-actions">
                <button className="button primary" disabled={actionDisabled(startAction, busy)} onClick={() => onGateway(gateway, "start")}><Play size={15} />启动 {gateway}</button>
                <button className="button secondary" disabled={actionDisabled(stopAction, busy)} onClick={() => onGateway(gateway, "stop")}><Square size={15} />停止 {gateway}</button>
                <button className="button secondary" disabled={actionDisabled(reconnectAction, busy)} onClick={() => onGateway(gateway, "reconnect")}><RefreshCw size={15} />重连 {gateway}</button>
              </div>
              {blocker && <div className="blocker-copy"><AlertTriangle size={14} />{blocker}</div>}
            </article>
          );
        })}
      </div>

      <section className="campaign-control" aria-labelledby="campaign-control-heading">
        <div>
          <h2 id="campaign-control-heading">模拟盘控制</h2>
          <p>只会启动已选择且已连接的网关，模型意图仍由 vn.py 风控后执行。</p>
        </div>
        <div className="campaign-actions">
          <button className="button primary" disabled={actionDisabled(start, busy)} onClick={onStartCampaign}><Play size={16} />启动模拟盘</button>
          <button className="button secondary" disabled={actionDisabled(pause, busy)} onClick={onPauseCampaign}><Pause size={16} />暂停模拟盘</button>
          <button className="button danger" disabled={actionDisabled(emergency, busy)} onClick={onEmergencyStop}><Octagon size={16} />紧急停止</button>
        </div>
        {actionBlocker(start) && <div className="blocker-copy"><AlertTriangle size={14} />{actionBlocker(start)}</div>}
      </section>

      <section className="current-runs" aria-labelledby="current-runs-heading">
        <div className="section-line">
          <h2 id="current-runs-heading">当前运行</h2>
          <span>{projection.current.gateways.length} 个网关</span>
        </div>
        {projection.current.gateways.length === 0 ? (
          <div className="empty-panel">当前没有活动运行。网关连接后仍需明确选择并启动模拟盘。</div>
        ) : (
          <div className="run-table-wrap">
            <table>
              <thead><tr><th>网关</th><th>状态</th><th>净收益</th><th>费用</th><th>回撤</th><th>对账</th></tr></thead>
              <tbody>{projection.current.gateways.map((run) => (
                <tr key={run.gateway}>
                  <td>{run.gateway}</td>
                  <td>{stateLabel(run.state)}</td>
                  <td className={run.net_profit_minor >= 0 ? "positive" : "negative"}>{cny(run.net_profit_minor)}</td>
                  <td>{cny(run.fees_minor)}</td>
                  <td>{(run.max_drawdown_bps / 100).toFixed(2)}%</td>
                  <td>{stateLabel(run.reconciliation_state)}</td>
                </tr>
              ))}</tbody>
            </table>
          </div>
        )}
      </section>
    </section>
  );
}

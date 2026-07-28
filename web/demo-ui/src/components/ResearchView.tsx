import { useState } from "react";
import { Clock3, FlaskConical, Plus, X } from "lucide-react";

import type { DemoApi, ResearchProjection, ResearchTaskProjection } from "../api";
import { errorLabel, sourceLabel, stateLabel } from "../i18n";
import { SideMasterPanel } from "./SideMasterPanel";


interface ResearchViewProps {
  api: DemoApi;
  research: ResearchProjection;
  available: boolean;
  sessionId: string;
  missionId: string;
  onResearch: (projection: ResearchProjection) => void;
}

export function ResearchView({ api, research, available, sessionId, missionId, onResearch }: ResearchViewProps) {
  const [objective, setObjective] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const create = async (): Promise<void> => {
    const selected = objective.trim();
    if (!selected || !available) return;
    setBusy("create");
    setError(null);
    try {
      const result = await api.createResearchTask({
        mission_id: `operator-${crypto.randomUUID()}`,
        objective: selected,
        constraints: ["research_only", "no_trading_authority"],
        data_references: [],
        priority: "routine",
        expires_at_ms: Date.now() + 24 * 60 * 60 * 1_000,
      });
      onResearch({ revision: result.revision, tasks: [...research.tasks, result.task] });
      setObjective("");
    } catch (cause) {
      setError(errorLabel(cause instanceof Error ? cause.message : "BACKEND_OPERATION_FAILED"));
    } finally {
      setBusy(null);
    }
  };

  const cancel = async (task: ResearchTaskProjection): Promise<void> => {
    setBusy(task.task_id);
    setError(null);
    try {
      const result = await api.cancelResearchTask(task.task_id, task.task_digest);
      onResearch({
        revision: result.revision,
        tasks: research.tasks.map((item) => item.task_id === task.task_id ? result.task : item),
      });
    } catch (cause) {
      setError(errorLabel(cause instanceof Error ? cause.message : "BACKEND_OPERATION_FAILED"));
    } finally {
      setBusy(null);
    }
  };

  return (
    <section className="work-view" aria-labelledby="research-heading">
      <header className="view-heading">
        <div><span className="view-kicker">只读市场数据与训练能力</span><h1 id="research-heading">量化研究</h1></div>
        <div className={`state-chip ${available ? "ready" : "blocked"}`}>{available ? "研究服务已就绪" : "研究服务尚未启动"}</div>
      </header>

      <section className="research-create" aria-labelledby="research-create-heading">
        <div><FlaskConical size={20} /><div><h2 id="research-create-heading">创建研究任务</h2><p>任务只能研究、训练和编写量化模型，不能接触网关或交易。</p></div></div>
        <label htmlFor="research-objective">研究目标</label>
        <div className="inline-form">
          <input id="research-objective" value={objective} maxLength={2_000} disabled={!available || busy !== null} onChange={(event) => setObjective(event.target.value)} />
          <button className="button primary" disabled={!available || !objective.trim() || busy !== null} onClick={() => void create()}><Plus size={16} />创建研究任务</button>
        </div>
        {!available && <span className="form-hint">请先在概览中启动研究服务。</span>}
        {error && <div className="notice danger" role="alert">{error}</div>}
      </section>

      <section className="research-timeline" aria-labelledby="timeline-heading">
        <div className="section-line"><h2 id="timeline-heading">研究时间线</h2><span>{research.tasks.length} 个任务</span></div>
        {research.tasks.length === 0 ? <div className="empty-panel">尚无研究任务。可以直接创建，或在下方确认 Side Master 提案。</div> : (
          <ol className="timeline-list">
            {research.tasks.map((task) => (
              <li key={task.task_id}>
                <span className={`timeline-dot ${task.state}`} />
                <div className="timeline-main"><div><strong>{task.objective}</strong><span>{sourceLabel(task.source)}</span></div><code className="digest">{task.task_digest}</code></div>
                <div className="timeline-meta"><span className={`state-chip ${task.state}`}>{stateLabel(task.state)}</span><span><Clock3 size={13} />{new Date(task.created_at_ms).toLocaleString("zh-CN")}</span></div>
                {task.state === "queued" && <button className="icon-button" title="取消研究任务" aria-label="取消研究任务" disabled={busy !== null} onClick={() => void cancel(task)}><X size={16} /></button>}
              </li>
            ))}
          </ol>
        )}
      </section>

      <SideMasterPanel api={api} available={available} sessionId={sessionId} missionId={missionId} />
    </section>
  );
}

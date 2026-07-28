import { Box, CheckCircle2, Cpu, FileCheck2, ShieldCheck } from "lucide-react";

import type { ModelPipelineProjection } from "../api";
import { familyLabel, stateLabel } from "../i18n";


export function ModelsView({ models }: { models: ModelPipelineProjection }) {
  return (
    <section className="work-view" aria-labelledby="models-heading">
      <header className="view-heading">
        <div>
          <span className="view-kicker">研究产物</span>
          <h1 id="models-heading">量化模型</h1>
        </div>
        <div className="revision-badge">流水线版本 {models.revision}</div>
      </header>

      <section className="candidate-band" aria-labelledby="candidate-heading">
        <div className="candidate-title">
          <ShieldCheck size={22} />
          <div>
            <span>当前候选</span>
            <h2 id="candidate-heading">
              {models.current_candidate ? familyLabel(models.current_candidate.family) : "尚无已发布候选"}
            </h2>
          </div>
        </div>
        {models.current_candidate ? (
          <div className="candidate-identities">
            <span className="state-chip">{stateLabel(models.current_candidate.state)}</span>
            <div><span>候选摘要</span><code className="digest" data-testid="candidate-digest" style={{ overflowWrap: "anywhere" }}>{models.current_candidate.candidate_digest}</code></div>
            <div><span>模型包摘要</span><code className="digest">{models.current_candidate.package_digest}</code></div>
            <div><span>发布版本</span><strong>{models.current_candidate.publication_revision}</strong></div>
          </div>
        ) : (
          <div className="empty-panel">研究、评估、审计和 vn.py 准入完成后，当前候选会显示在这里。</div>
        )}
      </section>

      <section className="pipeline-section" aria-labelledby="pipeline-heading">
        <div className="section-line">
          <h2 id="pipeline-heading">模型流水线</h2>
          <span>{models.runs.length} 个运行记录</span>
        </div>
        {models.runs.length === 0 ? (
          <div className="empty-panel">尚无模型运行。请在研究视图创建量化研究任务。</div>
        ) : (
          <div className="pipeline-list">
            {models.runs.map((run) => (
              <article className="pipeline-row" key={run.run_id}>
                <div className="pipeline-family">
                  {run.state === "evaluated" ? <CheckCircle2 size={19} /> : <Cpu size={19} />}
                  <div><strong>{familyLabel(run.family)}</strong><span>{run.run_id}</span></div>
                </div>
                <div className="pipeline-progress" aria-label={`${familyLabel(run.family)}进度`}>
                  <div><span style={{ width: `${Math.max(0, Math.min(100, run.progress_percent))}%` }} /></div>
                  <strong>{run.progress_percent}%</strong>
                </div>
                <span className={`state-chip ${run.state}`}>{stateLabel(run.state)}</span>
                <div className="pipeline-artifact">
                  <FileCheck2 size={15} />
                  {run.artifact_digest ? <code className="digest">{run.artifact_digest}</code> : <span>产物尚未生成</span>}
                </div>
              </article>
            ))}
          </div>
        )}
      </section>

      <section className="authority-note">
        <Box size={18} />
        <div><strong>模型只产生有界意图</strong><span>Agent 负责研究与编写模型，网关、交易、风控和生命周期始终由 vn.py 控制。</span></div>
      </section>
    </section>
  );
}

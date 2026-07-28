import { useRef, useState } from "react";
import {
  AlertTriangle,
  Check,
  MessageSquareText,
  RefreshCw,
  Send,
  ShieldCheck,
  X,
} from "lucide-react";

import type {
  GuidanceRevision,
  SideMasterApi,
  SideMasterProposal,
} from "../api";
import { stateLabel } from "../i18n";


interface SideMasterPanelProps {
  api: SideMasterApi;
  available: boolean;
  sessionId: string;
  missionId: string;
}

interface ConversationTurn {
  id: string;
  speaker: "operator" | "side-master";
  content: string;
}

interface ProposalView {
  proposal: SideMasterProposal;
  guidance: GuidanceRevision | null;
  pendingDecision: "confirm" | "reject" | null;
  error: boolean;
}

type ChatState = "ready" | "sending" | "uncertain" | "unavailable";

const operationKey = (prefix: string): string =>
  `${prefix}-${crypto.randomUUID()}-${crypto.randomUUID()}`;

const replyText = (body: unknown): string => {
  if (typeof body === "string") return body;
  return JSON.stringify(body);
};

const chatStateLabel = (state: ChatState): string => {
  if (state === "sending") return "等待 Side Master 回复";
  if (state === "uncertain") return "模型结果待核实";
  if (state === "unavailable") return "Side Master 暂不可用";
  return "可以对话";
};

export function SideMasterPanel({ api, available, sessionId, missionId }: SideMasterPanelProps) {
  const [content, setContent] = useState("");
  const [turns, setTurns] = useState<ConversationTurn[]>([]);
  const [proposals, setProposals] = useState<ProposalView[]>([]);
  const [chatState, setChatState] = useState<ChatState>(available ? "ready" : "unavailable");
  const [retry, setRetry] = useState<{ content: string; key: string } | null>(null);
  const decisionKeys = useRef(new Map<string, string>());

  const sendMessage = async (
    message: string,
    key: string,
    appendOperatorTurn: boolean,
  ): Promise<void> => {
    if (appendOperatorTurn) {
      setTurns((current) => [
        ...current,
        { id: key, speaker: "operator", content: message },
      ]);
    }
    setChatState("sending");
    try {
      const result = await api.sendSideMasterMessage(sessionId, missionId, message, key);
      setRetry(null);
      setContent("");
      if (result.state === "uncertain" || result.provider_outcome === "uncertain") {
        setChatState("uncertain");
        return;
      }
      const reply = result.reply;
      if (reply !== null) {
        setTurns((current) => [
          ...current,
          {
            id: result.result_digest,
            speaker: "side-master",
            content: replyText(reply.body),
          },
        ]);
      }
      const proposal = result.proposal;
      if (proposal !== null) {
        setProposals((current) => [
          ...current.filter((item) => item.proposal.proposal_id !== proposal.proposal_id),
          {
            proposal,
            guidance: null,
            pendingDecision: null,
            error: false,
          },
        ]);
      }
      setChatState("ready");
    } catch {
      setRetry({ content: message, key });
      setChatState("unavailable");
    }
  };

  const submit = (): void => {
    const message = content.trim();
    if (!available || !message || chatState === "sending") return;
    void sendMessage(message, operationKey("chat"), true);
  };

  const decide = async (
    selected: ProposalView,
    decision: "confirm" | "reject",
  ): Promise<void> => {
    const identity = `${selected.proposal.proposal_id}:${decision}`;
    let key = decisionKeys.current.get(identity);
    if (key === undefined) {
      key = operationKey(`proposal-${decision}`);
      decisionKeys.current.set(identity, key);
    }
    setProposals((current) => current.map((item) =>
      item.proposal.proposal_id === selected.proposal.proposal_id
        ? { ...item, pendingDecision: decision, error: false }
        : item));
    try {
      const receipt = await api.decideSideMasterProposal(
        selected.proposal.proposal_id,
        selected.proposal.proposal_digest,
        decision,
        key,
      );
      setProposals((current) => current.map((item) =>
        item.proposal.proposal_id === selected.proposal.proposal_id
          ? {
              proposal: receipt.proposal,
              guidance: receipt.guidance,
              pendingDecision: null,
              error: false,
            }
          : item));
    } catch {
      setProposals((current) => current.map((item) =>
        item.proposal.proposal_id === selected.proposal.proposal_id
          ? { ...item, pendingDecision: null, error: true }
          : item));
    }
  };

  return (
    <section className="side-master-section" aria-labelledby="side-master-heading">
      <div className="side-master-layout">
        <div className="side-master-intro">
          <span className="eyebrow">研究对话</span>
          <h2 id="side-master-heading">Side Master</h2>
          <span className={`side-master-state ${chatState}`}>
            {chatState === "ready" && <ShieldCheck size={14} />}
            {chatState === "sending" && <RefreshCw className="spin" size={14} />}
            {(chatState === "uncertain" || chatState === "unavailable") && <AlertTriangle size={14} />}
            {chatStateLabel(chatState)}
          </span>
        </div>

        <div className="side-master-workspace">
          <div className="conversation" aria-live="polite">
            {turns.length === 0 ? (
              <div className="conversation-empty">
                <MessageSquareText size={18} />
                <span>本次会话还没有消息</span>
              </div>
            ) : turns.map((turn) => (
              <div className={`conversation-turn ${turn.speaker}`} key={turn.id}>
                <span>{turn.speaker === "operator" ? "操作员" : "Side Master"}</span>
                <p>{turn.content}</p>
              </div>
            ))}
          </div>

          {chatState === "uncertain" && (
            <div className="guidance-notice warning" role="status">
              <AlertTriangle size={16} />
              <div>
                <strong>模型结果待核实</strong>
                <span>没有创建研究指引</span>
              </div>
            </div>
          )}
          {chatState === "unavailable" && retry !== null && (
            <div className="guidance-notice error" role="alert">
              <AlertTriangle size={16} />
              <div>
                <strong>Side Master 暂不可用</strong>
                <button
                  className="text-command"
                  onClick={() => void sendMessage(retry.content, retry.key, false)}
                >
                  <RefreshCw size={13} />重试消息
                </button>
              </div>
            </div>
          )}
          {chatState === "unavailable" && retry === null && (
            <div className="guidance-notice error" role="status">
              <AlertTriangle size={16} />
              <div>
                <strong>Side Master 暂不可用</strong>
                <span>请先启动研究服务</span>
              </div>
            </div>
          )}

          <div className="proposal-list">
            {proposals.map((item) => (
              <article className="proposal-item" key={item.proposal.proposal_id}>
                <div className="proposal-heading">
                  <div>
                    <span>研究方向提案</span>
                    <strong>{item.proposal.interpretation}</strong>
                  </div>
                  <span className={`proposal-state ${item.proposal.state}`}>
                    {stateLabel(item.proposal.state)}
                  </span>
                </div>
                <p>{item.proposal.proposed_guidance}</p>
                {item.proposal.state === "pending" && (
                  <div className="proposal-actions">
                    <button
                      className="button secondary"
                      disabled={item.pendingDecision !== null}
                      onClick={() => void decide(item, "reject")}
                    >
                      <X size={15} />拒绝提案
                    </button>
                    <button
                      className="button primary"
                      disabled={item.pendingDecision !== null}
                      onClick={() => void decide(item, "confirm")}
                    >
                      <Check size={15} />确认提案
                    </button>
                  </div>
                )}
                {item.proposal.state === "confirmed" && item.guidance !== null && (
                  <div className="proposal-outcome ok">
                    <Check size={15} />
                    <div>
                      <strong>已确认用于后续研究</strong>
                      {item.guidance.active_campaign_immutable && <span>当前模拟盘保持不变</span>}
                    </div>
                  </div>
                )}
                {item.proposal.state === "rejected" && (
                  <div className="proposal-outcome rejected"><X size={15} />提案已拒绝</div>
                )}
                {item.error && (
                  <div className="proposal-outcome warning"><AlertTriangle size={15} />决策暂未完成</div>
                )}
              </article>
            ))}
          </div>

          <form
            className="message-composer"
            onSubmit={(event) => {
              event.preventDefault();
              submit();
            }}
          >
            <label htmlFor="side-master-message">研究消息</label>
            <textarea
              id="side-master-message"
              rows={3}
              maxLength={8_000}
              value={content}
              disabled={!available || chatState === "sending"}
              onChange={(event) => setContent(event.target.value)}
            />
            <button
              className="button primary"
              type="submit"
              disabled={!available || !content.trim() || chatState === "sending"}
            >
              <Send size={15} />发送消息
            </button>
          </form>
        </div>
      </div>
    </section>
  );
}

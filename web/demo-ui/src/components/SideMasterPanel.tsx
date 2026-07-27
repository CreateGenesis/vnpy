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


interface SideMasterPanelProps {
  api: SideMasterApi;
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

const stateLabel = (state: ChatState): string => {
  if (state === "sending") return "Waiting for Side Master";
  if (state === "uncertain") return "Provider outcome uncertain";
  if (state === "unavailable") return "Side Master unavailable";
  return "Ready";
};

export function SideMasterPanel({ api, sessionId, missionId }: SideMasterPanelProps) {
  const [content, setContent] = useState("");
  const [turns, setTurns] = useState<ConversationTurn[]>([]);
  const [proposals, setProposals] = useState<ProposalView[]>([]);
  const [chatState, setChatState] = useState<ChatState>("ready");
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
    if (!message || chatState === "sending") return;
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
          <span className="eyebrow">Research conversation</span>
          <h2 id="side-master-heading">Side Master</h2>
          <span className={`side-master-state ${chatState}`}>
            {chatState === "ready" && <ShieldCheck size={14} />}
            {chatState === "sending" && <RefreshCw className="spin" size={14} />}
            {(chatState === "uncertain" || chatState === "unavailable") && <AlertTriangle size={14} />}
            {stateLabel(chatState)}
          </span>
        </div>

        <div className="side-master-workspace">
          <div className="conversation" aria-live="polite">
            {turns.length === 0 ? (
              <div className="conversation-empty">
                <MessageSquareText size={18} />
                <span>No conversation in this session</span>
              </div>
            ) : turns.map((turn) => (
              <div className={`conversation-turn ${turn.speaker}`} key={turn.id}>
                <span>{turn.speaker === "operator" ? "Operator" : "Side Master"}</span>
                <p>{turn.content}</p>
              </div>
            ))}
          </div>

          {chatState === "uncertain" && (
            <div className="guidance-notice warning" role="status">
              <AlertTriangle size={16} />
              <div>
                <strong>Provider outcome uncertain</strong>
                <span>No guidance was created</span>
              </div>
            </div>
          )}
          {chatState === "unavailable" && retry !== null && (
            <div className="guidance-notice error" role="alert">
              <AlertTriangle size={16} />
              <div>
                <strong>Side Master unavailable</strong>
                <button
                  className="text-command"
                  onClick={() => void sendMessage(retry.content, retry.key, false)}
                >
                  <RefreshCw size={13} />Retry message
                </button>
              </div>
            </div>
          )}

          <div className="proposal-list">
            {proposals.map((item) => (
              <article className="proposal-item" key={item.proposal.proposal_id}>
                <div className="proposal-heading">
                  <div>
                    <span>Research direction proposal</span>
                    <strong>{item.proposal.interpretation}</strong>
                  </div>
                  <span className={`proposal-state ${item.proposal.state}`}>
                    {item.proposal.state}
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
                      <X size={15} />Reject proposal
                    </button>
                    <button
                      className="button primary"
                      disabled={item.pendingDecision !== null}
                      onClick={() => void decide(item, "confirm")}
                    >
                      <Check size={15} />Confirm proposal
                    </button>
                  </div>
                )}
                {item.proposal.state === "confirmed" && item.guidance !== null && (
                  <div className="proposal-outcome ok">
                    <Check size={15} />
                    <div>
                      <strong>Confirmed for future research</strong>
                      {item.guidance.active_campaign_immutable && <span>Active campaign unchanged</span>}
                    </div>
                  </div>
                )}
                {item.proposal.state === "rejected" && (
                  <div className="proposal-outcome rejected"><X size={15} />Proposal rejected</div>
                )}
                {item.error && (
                  <div className="proposal-outcome warning"><AlertTriangle size={15} />Decision unavailable</div>
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
            <label htmlFor="side-master-message">Research message</label>
            <textarea
              id="side-master-message"
              rows={3}
              maxLength={8_000}
              value={content}
              disabled={chatState === "sending"}
              onChange={(event) => setContent(event.target.value)}
            />
            <button
              className="button primary"
              type="submit"
              disabled={!content.trim() || chatState === "sending"}
            >
              <Send size={15} />Send message
            </button>
          </form>
        </div>
      </div>
    </section>
  );
}

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { vi } from "vitest";

import { SideMasterPanel } from "./SideMasterPanel";
import type {
  ProposalDecisionReceipt,
  SideMasterApi,
  SideMasterChatResult,
  SideMasterProposal,
} from "../api";


const proposal: SideMasterProposal = {
  contract_version: 1,
  entity_type: "side_master_approval_proposal",
  proposal_id: "1a216598-1144-4b21-8714-a711c66f9f31",
  session_id: "side-session-1",
  mission_id: "research-mission-1",
  side_master_identity: "side-master:demo",
  source_turn_digest: `sha256:${"1".repeat(64)}`,
  material_direction_change: true,
  interpretation: "Prefer drawdown stability over turnover",
  proposed_guidance: "Research lower-turnover drawdown controls",
  provider_outcome: "certain",
  state: "pending",
  created_at_ms: 1_000,
  expires_at_ms: 61_000,
  proposal_digest: `sha256:${"2".repeat(64)}`,
};

const dynamicText = (body: string) => ({
  media_type: "text/plain; charset=utf-8" as const,
  body,
  canonical_body_base64: "ignored-by-browser",
  body_digest: `blake3:${"3".repeat(64)}`,
});

const chatResult = (
  reply: string,
  proposalValue: SideMasterProposal | null = null,
): SideMasterChatResult => ({
  contract_version: 1,
  entity_type: "demo_side_master_chat_result",
  session_id: "side-session-1",
  mission_id: "research-mission-1",
  state: "completed",
  reply: dynamicText(reply),
  proposal: proposalValue,
  provider_outcome: "certain",
  result_digest: `sha256:${"4".repeat(64)}`,
});

const decisionReceipt = (
  decision: "confirm" | "reject",
  idempotencyKey: string,
): ProposalDecisionReceipt => {
  const confirmed = decision === "confirm";
  return {
    proposal: {
      ...proposal,
      state: confirmed ? "confirmed" : "rejected",
    },
    guidance: confirmed ? {
      contract_version: 1,
      entity_type: "confirmed_future_research_guidance",
      guidance_id: "2a216598-1144-4b21-8714-a711c66f9f31",
      proposal_id: proposal.proposal_id,
      proposal_digest: proposal.proposal_digest,
      mission_id: proposal.mission_id,
      guidance: proposal.proposed_guidance,
      operator_identity_digest: `sha256:${"5".repeat(64)}`,
      confirmed_at_ms: 2_000,
      scope: "future_research_only",
      not_before_safe_boundary_revision: 12,
      delivery_id: "3a216598-1144-4b21-8714-a711c66f9f31",
      active_campaign_immutable: true,
      signer_id: "demo-guidance-signer",
      verifying_key: "6".repeat(64),
      guidance_digest: `sha256:${"7".repeat(64)}`,
      signature: "8".repeat(128),
    } : null,
    idempotency_key: idempotencyKey,
    decision_digest: `sha256:${"9".repeat(64)}`,
  };
};

const apiMock = (
  result: SideMasterChatResult = chatResult("Continue the current research direction."),
): SideMasterApi => ({
  sendSideMasterMessage: vi.fn().mockResolvedValue(result),
  decideSideMasterProposal: vi.fn().mockImplementation(
    (_proposalId, _proposalDigest, decision, idempotencyKey) =>
      Promise.resolve(decisionReceipt(decision, idempotencyKey)),
  ),
});

const renderPanel = (api: SideMasterApi) => render(
  <SideMasterPanel
    api={api}
    sessionId="side-session-1"
    missionId="research-mission-1"
  />,
);

const send = (content: string): void => {
  fireEvent.change(screen.getByLabelText("Research message"), {
    target: { value: content },
  });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
};


test("renders ordinary conversation without creating a proposal", async () => {
  const api = apiMock();
  renderPanel(api);

  expect(screen.getByText("Side Master")).toBeInTheDocument();
  expect(screen.getByText("Ready")).toBeInTheDocument();
  send("Keep the current objective");

  expect(await screen.findByText("Continue the current research direction.")).toBeInTheDocument();
  expect(screen.getByText("Keep the current objective")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Confirm proposal" })).not.toBeInTheDocument();
  expect(api.sendSideMasterMessage).toHaveBeenCalledWith(
    "side-session-1",
    "research-mission-1",
    "Keep the current objective",
    expect.stringMatching(/^chat-/),
  );
});

test("requires explicit confirmation and shows future-research-only guidance", async () => {
  const api = apiMock(chatResult("I propose a research change.", proposal));
  renderPanel(api);
  send("Prioritize drawdown stability");

  expect(await screen.findByText(proposal.interpretation)).toBeInTheDocument();
  expect(screen.getByText(proposal.proposed_guidance)).toBeInTheDocument();
  expect(api.decideSideMasterProposal).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Confirm proposal" }));

  expect(await screen.findByText("Confirmed for future research")).toBeInTheDocument();
  expect(screen.getByText("Active campaign unchanged")).toBeInTheDocument();
  expect(api.decideSideMasterProposal).toHaveBeenCalledWith(
    proposal.proposal_id,
    proposal.proposal_digest,
    "confirm",
    expect.stringMatching(/^proposal-confirm-/),
  );
});

test("records proposal rejection without creating guidance", async () => {
  const api = apiMock(chatResult("I propose a research change.", proposal));
  renderPanel(api);
  send("Change the next research objective");

  await screen.findByText(proposal.interpretation);
  fireEvent.click(screen.getByRole("button", { name: "Reject proposal" }));

  expect(await screen.findByText("Proposal rejected")).toBeInTheDocument();
  expect(screen.queryByText("Confirmed for future research")).not.toBeInTheDocument();
  expect(api.decideSideMasterProposal).toHaveBeenCalledWith(
    proposal.proposal_id,
    proposal.proposal_digest,
    "reject",
    expect.stringMatching(/^proposal-reject-/),
  );
});

test("shows uncertain provider outcome with no retry or proposal effect", async () => {
  const api = apiMock({
    ...chatResult("unused"),
    state: "uncertain",
    reply: null,
    proposal: null,
    provider_outcome: "uncertain",
  });
  renderPanel(api);
  send("Explore a different factor family");

  expect(await screen.findByText("Provider outcome uncertain")).toBeInTheDocument();
  expect(screen.getByText("No guidance was created")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /retry/i })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Confirm proposal" })).not.toBeInTheDocument();
  expect(api.decideSideMasterProposal).not.toHaveBeenCalled();
});

test("network retry reuses the idempotency key and does not duplicate the user turn", async () => {
  const api = apiMock();
  vi.mocked(api.sendSideMasterMessage)
    .mockRejectedValueOnce(new Error("unavailable"))
    .mockResolvedValueOnce(chatResult("Recovered response"));
  renderPanel(api);
  send("Retry this exact turn");

  expect(await screen.findByText("Side Master unavailable")).toBeInTheDocument();
  const firstKey = vi.mocked(api.sendSideMasterMessage).mock.calls[0][3];
  fireEvent.click(screen.getByRole("button", { name: "Retry message" }));

  expect(await screen.findByText("Recovered response")).toBeInTheDocument();
  expect(vi.mocked(api.sendSideMasterMessage).mock.calls[1][3]).toBe(firstKey);
  await waitFor(() => {
    expect(screen.getAllByText("Retry this exact turn")).toHaveLength(1);
  });
});


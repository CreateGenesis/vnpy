import { beforeEach, describe, expect, it, vi } from "vitest";

import { establishBrowserSession } from "./bootstrap";


const csrf = "csrf-token-0123456789abcdef0123456789abcdef";
const fragment = "fragment-token-0123456789abcdef0123456789abcdef";

const response = (data: unknown, ok = true): Response => ({
  ok,
  status: ok ? 200 : 409,
  json: vi.fn().mockResolvedValue(data),
}) as unknown as Response;

describe("browser bootstrap session", () => {
  beforeEach(() => {
    window.history.replaceState(null, "", "/");
    window.sessionStorage.clear();
    document.querySelector<HTMLMetaElement>('meta[name="auto-trade-csrf"]')?.setAttribute("content", "");
  });

  it("exchanges the fragment before use and removes it from the URL", async () => {
    window.history.replaceState(null, "", `/#bootstrap=${fragment}`);
    const fetcher = vi.fn().mockResolvedValue(response({
      contract_version: 1,
      status: "ok",
      data: { csrf_token: csrf },
    }));

    await establishBrowserSession(fetcher);

    expect(fetcher).toHaveBeenCalledOnce();
    expect(fetcher).toHaveBeenCalledWith("/api/v1/bootstrap/exchange", expect.objectContaining({
      method: "POST",
      credentials: "same-origin",
      body: JSON.stringify({ fragment_token: fragment }),
    }));
    expect(window.location.hash).toBe("");
    expect(document.querySelector<HTMLMetaElement>('meta[name="auto-trade-csrf"]')?.content).toBe(csrf);
    expect(window.sessionStorage.getItem("auto-trade.csrf.v1")).toBe(csrf);
  });

  it("restores the CSRF token on a same-tab refresh without another exchange", async () => {
    window.sessionStorage.setItem("auto-trade.csrf.v1", csrf);
    const fetcher = vi.fn();

    await establishBrowserSession(fetcher);

    expect(fetcher).not.toHaveBeenCalled();
    expect(document.querySelector<HTMLMetaElement>('meta[name="auto-trade-csrf"]')?.content).toBe(csrf);
  });

  it("fails closed and clears malformed bootstrap fragments", async () => {
    window.history.replaceState(null, "", "/#bootstrap=short");

    await expect(establishBrowserSession(vi.fn())).rejects.toThrow("BOOTSTRAP_TOKEN_INVALID");

    expect(window.location.hash).toBe("");
    expect(window.sessionStorage.getItem("auto-trade.csrf.v1")).toBeNull();
  });
});

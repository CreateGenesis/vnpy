const CSRF_STORAGE_KEY = "auto-trade.csrf.v1";
const BOOTSTRAP_PARAMETER = "bootstrap";
const TOKEN_PATTERN = /^[A-Za-z0-9_-]{32,256}$/;

interface BootstrapEnvelope {
  contract_version: number;
  status: string;
  data?: {
    csrf_token?: unknown;
  };
  errors?: Array<{
    code?: unknown;
  }>;
}

type Fetcher = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

const csrfMeta = (): HTMLMetaElement => {
  const existing = document.querySelector<HTMLMetaElement>('meta[name="auto-trade-csrf"]');
  if (existing !== null) return existing;

  const meta = document.createElement("meta");
  meta.name = "auto-trade-csrf";
  document.head.append(meta);
  return meta;
};

const clearFragment = (): void => {
  if (window.location.hash === "") return;
  window.history.replaceState(
    window.history.state,
    "",
    `${window.location.pathname}${window.location.search}`,
  );
};

const clearBrowserSession = (): void => {
  window.sessionStorage.removeItem(CSRF_STORAGE_KEY);
  csrfMeta().content = "";
};

const installCsrfToken = (token: string): void => {
  window.sessionStorage.setItem(CSRF_STORAGE_KEY, token);
  csrfMeta().content = token;
};

const errorCode = (envelope: BootstrapEnvelope, status: number): string => {
  const code = envelope.errors?.[0]?.code;
  return typeof code === "string" && code.length > 0 ? code : `HTTP_${status}`;
};

export async function establishBrowserSession(
  fetcher: Fetcher = window.fetch.bind(window),
): Promise<void> {
  const parameters = new URLSearchParams(window.location.hash.slice(1));
  const fragmentToken = parameters.get(BOOTSTRAP_PARAMETER);

  if (fragmentToken !== null) {
    clearFragment();
    clearBrowserSession();
    if (!TOKEN_PATTERN.test(fragmentToken)) {
      throw new Error("BOOTSTRAP_TOKEN_INVALID");
    }

    try {
      const response = await fetcher("/api/v1/bootstrap/exchange", {
        method: "POST",
        credentials: "same-origin",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ fragment_token: fragmentToken }),
      });
      const envelope = await response.json() as BootstrapEnvelope;
      if (!response.ok || envelope.contract_version !== 1 || envelope.status !== "ok") {
        throw new Error(errorCode(envelope, response.status));
      }

      const csrfToken = envelope.data?.csrf_token;
      if (typeof csrfToken !== "string" || !TOKEN_PATTERN.test(csrfToken)) {
        throw new Error("BOOTSTRAP_RESPONSE_INVALID");
      }
      installCsrfToken(csrfToken);
      return;
    } catch (cause) {
      clearBrowserSession();
      throw cause instanceof Error ? cause : new Error("BOOTSTRAP_EXCHANGE_FAILED");
    }
  }

  const storedToken = window.sessionStorage.getItem(CSRF_STORAGE_KEY);
  if (storedToken !== null && TOKEN_PATTERN.test(storedToken)) {
    installCsrfToken(storedToken);
    return;
  }

  clearBrowserSession();
  throw new Error("BOOTSTRAP_SESSION_REQUIRED");
}

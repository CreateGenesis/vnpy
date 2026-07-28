import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./App";
import { establishBrowserSession } from "./bootstrap";
import "./styles.css";


const root = createRoot(document.getElementById("root")!);

const bootstrapErrorLabel = (cause: unknown): string => {
  const code = cause instanceof Error ? cause.message : "BOOTSTRAP_EXCHANGE_FAILED";
  if (code === "BOOTSTRAP_TOKEN_INVALID") return "启动链接无效或已损坏";
  if (code === "BOOTSTRAP_OPERATOR_MISMATCH") return "当前 Windows 用户与启动用户不一致";
  if (code === "SAME_ORIGIN_REQUIRED") return "启动来源校验失败";
  if (code === "BOOTSTRAP_SESSION_REQUIRED") return "未找到安全启动会话";
  if (code === "BOOTSTRAP_TOKEN_ALREADY_USED") return "启动链接已使用或已过期";
  return "安全会话建立失败";
};

const start = async (): Promise<void> => {
  try {
    await establishBrowserSession();
    root.render(
      <StrictMode>
        <App />
      </StrictMode>,
    );
  } catch (cause) {
    root.render(
      <main className="loading-state startup-error" role="alert">
        <strong>{bootstrapErrorLabel(cause)}</strong>
        <span>请关闭本页，并从 Auto Trade 启动器重新打开控制台。</span>
      </main>,
    );
  }
};

void start();

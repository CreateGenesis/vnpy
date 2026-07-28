import { useEffect, useMemo, useState } from "react";
import { CheckCircle2, ExternalLink, FlaskConical, Save, ShieldCheck } from "lucide-react";

import type {
  ConfigurationDraftProjection,
  ConfigurationDraftUpdate,
  DemoApi,
} from "../api";
import { errorLabel } from "../i18n";


interface SettingsViewProps {
  api: DemoApi;
  draft: ConfigurationDraftProjection;
  onDraft: (draft: ConfigurationDraftProjection) => void;
  onChanged: () => Promise<void>;
}

type Sections = Record<string, Record<string, unknown>>;

const sectionNames: Record<string, string> = {
  operator: "操作员",
  ports: "服务端口",
  rqdata: "RQData",
  master_route: "Master 模型路由",
  worker_route: "Worker 模型路由",
  xtp: "XTP 模拟网关",
  tora: "TORA 模拟网关",
};

const text = (sections: Sections, section: string, key: string, fallback = ""): string => {
  const value = sections[section]?.[key];
  return typeof value === "string" || typeof value === "number" ? String(value) : fallback;
};

export function SettingsView({ api, draft, onDraft, onChanged }: SettingsViewProps) {
  const [sections, setSections] = useState<Sections>(draft.sections);
  const [secrets, setSecrets] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [nextUrl, setNextUrl] = useState<string | null>(null);

  useEffect(() => setSections(draft.sections), [draft]);

  const changed = useMemo(() => new Set(draft.changed_sections), [draft.changed_sections]);

  const setValue = (section: string, key: string, value: string | number | boolean): void => {
    setSections((current) => ({
      ...current,
      [section]: { ...(current[section] ?? {}), [key]: value },
    }));
  };

  const setSecret = (key: string, value: string): void => {
    setSecrets((current) => ({ ...current, [key]: value }));
  };

  const execute = async (name: string, operation: () => Promise<void>): Promise<void> => {
    setBusy(name);
    setNotice(null);
    setError(null);
    try {
      await operation();
    } catch (cause) {
      setError(errorLabel(cause instanceof Error ? cause.message : "BACKEND_OPERATION_FAILED"));
    } finally {
      setBusy(null);
    }
  };

  const save = (): void => {
    void execute("save", async () => {
      const secretUpdates = Object.fromEntries(
        Object.entries(secrets).filter(([, value]) => value.length > 0),
      );
      const command: ConfigurationDraftUpdate = {
        expected_revision: draft.revision,
        sections,
        secret_updates: secretUpdates,
        clear_secrets: [],
      };
      const updated = await api.updateConfigurationDraft(command);
      setSecrets({});
      onDraft(updated);
      setNotice("草稿已保存");
      await onChanged();
    });
  };

  const testSection = (section: string): void => {
    void execute(`test:${section}`, async () => {
      const receipt = await api.testConfigurationSection(section, draft.revision);
      const updated = {
        ...draft,
        test_receipts: { ...draft.test_receipts, [section]: receipt },
      };
      onDraft(updated);
      setNotice(section === "ports" ? "端口测试通过" : `${sectionNames[section]}测试通过`);
    });
  };

  const activate = (): void => {
    void execute("activate", async () => {
      const receipt = await api.activateConfigurationDraft(draft.revision);
      setNextUrl(receipt.next_url ?? null);
      setNotice("配置已激活");
      await onChanged();
    });
  };

  return (
    <section className="work-view" aria-labelledby="settings-heading">
      <header className="view-heading">
        <div>
          <span className="view-kicker">安全配置</span>
          <h1 id="settings-heading">系统设置</h1>
        </div>
        <div className="revision-badge">草稿版本 {draft.revision}</div>
      </header>

      {notice && <div className="notice success" role="status"><CheckCircle2 size={17} />{notice}</div>}
      {error && <div className="notice danger" role="alert">{error}</div>}
      {nextUrl && (
        <div className="notice info">
          <span>新端口已就绪</span>
          <a href={nextUrl}>打开新控制台地址 <ExternalLink size={14} /></a>
        </div>
      )}

      <div className="settings-sections">
        <fieldset className="settings-section">
          <legend>服务端口</legend>
          <div className="form-grid compact">
            {[
              ["web", "Web 端口", 8765],
              ["supervisor", "Supervisor 端口", 8766],
              ["agentd", "研究服务端口", 18801],
              ["model_xtp", "XTP 模型端口", 18811],
              ["model_tora", "TORA 模型端口", 18812],
              ["run_xtp", "XTP 运行端口", 18821],
              ["run_tora", "TORA 运行端口", 18822],
              ["rqdata_fetcher", "RQData 端口", 8786],
            ].map(([key, label, fallback]) => (
              <label key={String(key)}>{label}
                <input
                  type="number"
                  min={1}
                  max={65535}
                  value={text(sections, "ports", String(key), String(fallback))}
                  onChange={(event) => setValue("ports", String(key), Number(event.target.value))}
                />
              </label>
            ))}
          </div>
          <SectionTest
            section="ports"
            label="测试端口"
            changed={changed.has("ports")}
            passed={draft.test_receipts.ports?.passed === true}
            busy={busy}
            onTest={testSection}
          />
        </fieldset>

        <fieldset className="settings-section">
          <legend>RQData Tick 数据</legend>
          <div className="form-grid">
            <label>服务地址
              <input value={text(sections, "rqdata", "endpoint")} onChange={(event) => setValue("rqdata", "endpoint", event.target.value)} />
            </label>
            <label>RQData 用户名
              <input autoComplete="off" value={secrets["rqdata.username"] ?? ""} placeholder={draft.secret_status["rqdata.username"]?.configured ? "已配置，留空则保留" : "请输入"} onChange={(event) => setSecret("rqdata.username", event.target.value)} />
            </label>
            <label>RQData 密码
              <input type="password" autoComplete="new-password" value={secrets["rqdata.password"] ?? ""} placeholder={draft.secret_status["rqdata.password"]?.configured ? "已配置，留空则保留" : "请输入"} onChange={(event) => setSecret("rqdata.password", event.target.value)} />
            </label>
            <label className="checkbox-field"><input type="checkbox" checked={sections.rqdata?.tick_required !== false} onChange={(event) => setValue("rqdata", "tick_required", event.target.checked)} />必须具备 Tick 权限</label>
          </div>
          <SectionTest section="rqdata" label="测试 RQData" changed={changed.has("rqdata")} passed={draft.test_receipts.rqdata?.passed === true} busy={busy} onTest={testSection} />
        </fieldset>

        <fieldset className="settings-section">
          <legend>固定模型路由</legend>
          <div className="route-columns">
            <div className="form-grid">
              <h2>Master / Audit / Side Master</h2>
              <label>接口地址<input value={text(sections, "master_route", "endpoint")} onChange={(event) => setValue("master_route", "endpoint", event.target.value)} /></label>
              <label>模型<input value="gpt-5.6-sol" readOnly /></label>
              <label>API 密钥<input type="password" autoComplete="new-password" value={secrets["master_route.api_key"] ?? ""} placeholder={draft.secret_status["master_route.api_key"]?.configured ? "已配置，留空则保留" : "请输入"} onChange={(event) => setSecret("master_route.api_key", event.target.value)} /></label>
              <SectionTest section="master_route" label="测试 Master 路由" changed={changed.has("master_route")} passed={draft.test_receipts.master_route?.passed === true} busy={busy} onTest={testSection} />
            </div>
            <div className="form-grid">
              <h2>Worker</h2>
              <label>接口地址<input value={text(sections, "worker_route", "endpoint")} onChange={(event) => setValue("worker_route", "endpoint", event.target.value)} /></label>
              <label>模型<input value="deepseek-v4-flash" readOnly /></label>
              <label>API 密钥<input type="password" autoComplete="new-password" value={secrets["worker_route.api_key"] ?? ""} placeholder={draft.secret_status["worker_route.api_key"]?.configured ? "已配置，留空则保留" : "请输入"} onChange={(event) => setSecret("worker_route.api_key", event.target.value)} /></label>
              <SectionTest section="worker_route" label="测试 Worker 路由" changed={changed.has("worker_route")} passed={draft.test_receipts.worker_route?.passed === true} busy={busy} onTest={testSection} />
            </div>
          </div>
        </fieldset>

        <GatewaySettings
          gateway="XTP"
          sections={sections}
          secrets={secrets}
          status={draft.secret_status}
          changed={changed.has("xtp")}
          passed={draft.test_receipts.xtp?.passed === true}
          busy={busy}
          setValue={setValue}
          setSecret={setSecret}
          onTest={testSection}
        />
        <GatewaySettings
          gateway="TORA"
          sections={sections}
          secrets={secrets}
          status={draft.secret_status}
          changed={changed.has("tora")}
          passed={draft.test_receipts.tora?.passed === true}
          busy={busy}
          setValue={setValue}
          setSecret={setSecret}
          onTest={testSection}
        />
      </div>

      <footer className="sticky-actions">
        <button className="button secondary" disabled={busy !== null} onClick={save}><Save size={16} />保存草稿</button>
        <button className="button primary" disabled={busy !== null || draft.changed_sections.some((section) => draft.test_receipts[section]?.passed !== true)} onClick={activate}><ShieldCheck size={16} />激活配置</button>
      </footer>
    </section>
  );
}

function SectionTest({ section, label, changed, passed, busy, onTest }: {
  section: string;
  label: string;
  changed: boolean;
  passed: boolean;
  busy: string | null;
  onTest: (section: string) => void;
}) {
  return (
    <div className="section-test">
      <button className="button ghost" disabled={busy !== null} onClick={() => onTest(section)}><FlaskConical size={15} />{label}</button>
      <span className={passed ? "test-state passed" : "test-state"}>{passed ? "测试已通过" : changed ? "等待测试" : "无待测试变更"}</span>
    </div>
  );
}

function GatewaySettings({ gateway, sections, secrets, status, changed, passed, busy, setValue, setSecret, onTest }: {
  gateway: "XTP" | "TORA";
  sections: Sections;
  secrets: Record<string, string>;
  status: ConfigurationDraftProjection["secret_status"];
  changed: boolean;
  passed: boolean;
  busy: string | null;
  setValue: (section: string, key: string, value: string | number | boolean) => void;
  setSecret: (key: string, value: string) => void;
  onTest: (section: string) => void;
}) {
  const section = gateway.toLowerCase();
  const fields = gateway === "XTP"
    ? [["account", "模拟账号"], ["client_id", "客户端编号"], ["quote_address", "行情地址"], ["quote_port", "行情端口"], ["trading_address", "交易地址"], ["trading_port", "交易端口"], ["quote_protocol", "行情协议"], ["log_level", "日志级别"]]
    : [["account", "模拟账号"], ["product_id", "产品编号"], ["account_type", "账号类型"], ["address_type", "地址类型"], ["quote_server", "行情服务器"], ["trading_server", "交易服务器"]];
  const secretFields = gateway === "XTP"
    ? [["password", "密码"], ["authorization_code", "授权码"]]
    : [["password", "密码"], ["dynamic_key", "动态密钥"]];
  return (
    <fieldset className="settings-section">
      <legend>{gateway} 模拟网关</legend>
      <div className="form-grid compact">
        {fields.map(([key, label]) => (
          <label key={key}>{label}
            <input value={text(sections, section, key)} onChange={(event) => setValue(section, key, /port|client_id/.test(key) ? Number(event.target.value) : event.target.value)} />
          </label>
        ))}
        {secretFields.map(([key, label]) => {
          const path = `${section}.${key}`;
          return <label key={key}>{label}<input type="password" autoComplete="new-password" value={secrets[path] ?? ""} placeholder={status[path]?.configured ? "已配置，留空则保留" : "请输入"} onChange={(event) => setSecret(path, event.target.value)} /></label>;
        })}
      </div>
      <SectionTest section={section} label={`测试 ${gateway}`} changed={changed} passed={passed} busy={busy} onTest={onTest} />
    </fieldset>
  );
}

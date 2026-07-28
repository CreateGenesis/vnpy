export const stateLabel = (value: string): string => {
  const labels: Record<string, string> = {
    active: "运行中",
    admitted: "已准入",
    blocked: "已阻止",
    calibrated: "已校准",
    cancelled: "已取消",
    complete: "已完成",
    completed: "已完成",
    configured: "已配置",
    connected: "已连接",
    connecting: "连接中",
    contained: "已受控",
    degraded: "状态降级",
    disconnected: "连接已断开",
    editing: "编辑中",
    enabled: "可执行",
    evaluated: "已评估",
    expired: "已过期",
    failed: "执行失败",
    inactive: "未启用",
    invalidated: "已失效",
    normal: "正常",
    paused: "已暂停",
    pausing: "暂停中",
    pending: "待确认",
    prepared: "已准备",
    queued: "等待调度",
    ready: "已就绪",
    reconnecting: "正在重连",
    recovering: "恢复中",
    rejected: "已拒绝",
    rollback: "已回滚",
    running: "运行中",
    starting: "启动中",
    stopped: "已停止",
    stopping: "停止中",
    trained: "已训练",
    unavailable: "暂不可用",
    uncertain: "结果待核实",
    unconfigured: "尚未配置",
  };
  return labels[value] ?? "状态待核实";
};

export const blockerLabel = (code: string, parameters: Record<string, unknown> = {}): string => {
  const gateway = typeof parameters.gateway === "string" ? `（${parameters.gateway}）` : "";
  const labels: Record<string, string> = {
    CAMPAIGN_ACTIVE: "模拟盘运行期间不能修改配置",
    CAMPAIGN_NOT_ACTIVE: "当前没有运行中的模拟盘",
    CANDIDATE_NOT_READY: "尚无通过全部门禁的候选模型",
    CONFIGURATION_NOT_ACTIVE: "请先在设置中完成并激活配置",
    GATEWAY_NOT_SELECTED: "请至少选择一个已连接的模拟网关",
    SELECTED_GATEWAY_NOT_READY: `所选网关尚未就绪${gateway}`,
    SERVICE_NOT_CONFIGURED: "请先完成对应服务配置",
    GATEWAY_CONFIGURATION_NOT_ACTIVE: "请先测试并激活该网关配置",
    RESEARCH_SERVICE_UNAVAILABLE: "研究服务尚未启动",
  };
  return labels[code] ?? "当前条件尚未满足，请刷新状态后重试";
};

export const errorLabel = (code: string): string => {
  const labels: Record<string, string> = {
    BACKEND_OPERATION_FAILED: "后台操作失败，请检查服务状态",
    CANDIDATE_NOT_READY: "候选模型尚未就绪",
    CONFIGURATION_REVISION_CONFLICT: "配置已被更新，请刷新后重试",
    CSRF_TOKEN_UNAVAILABLE: "安全会话尚未建立，请刷新页面",
    GATEWAY_NOT_CONNECTED: "网关尚未连接",
    GATEWAY_REVISION_CONFLICT: "网关状态已变化，请刷新后重试",
    RESEARCH_SERVICE_UNAVAILABLE: "研究服务暂不可用",
    SIDE_MASTER_UNAVAILABLE: "Side Master 暂不可用",
  };
  return labels[code] ?? "操作未完成，请刷新状态后重试";
};

export const remediationLabel = (value: string): string => {
  const labels: Record<string, string> = {
    open_models: "查看模型",
    open_settings: "前往设置",
    pause_campaign: "先暂停模拟盘",
    select_gateway: "选择网关",
    start_xtp: "启动 XTP",
    start_tora: "启动 TORA",
  };
  return labels[value] ?? "刷新状态";
};

export const serviceLabel = (value: string): string => {
  const labels: Record<string, string> = {
    model_tora: "TORA 模型服务",
    model_xtp: "XTP 模型服务",
    research: "研究服务",
    rqdata_fetcher: "RQData 数据服务",
  };
  return labels[value] ?? "系统服务";
};

export const familyLabel = (value: string): string => {
  const labels: Record<string, string> = {
    factor: "规则 / 因子",
    lightgbm: "LightGBM",
    lasso: "Lasso",
    mlp: "MLP",
    rule: "规则模型",
    unknown: "待识别",
  };
  return labels[value] ?? "量化模型";
};

export const sourceLabel = (value: string): string =>
  value === "side_master_proposal" ? "Side Master 提案" : "操作员创建";

export const connectionLabel = (state: string, attempt: number): string => {
  if (state === "connected") return "实时连接";
  if (state === "reconnecting") return `正在重连，第 ${attempt} 次`;
  if (state === "connecting") return "正在连接";
  return "连接已断开";
};

export const cny = (minor: number): string =>
  new Intl.NumberFormat("zh-CN", {
    style: "currency",
    currency: "CNY",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(minor / 100);

import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import {
  Activity,
  ArrowDownToLine,
  ArrowRight,
  BookOpen,
  Box,
  Braces,
  CheckCheck,
  ChevronRight,
  CircleHelp,
  ClipboardList,
  Code2,
  Cpu,
  Database,
  Download,
  FileText,
  FlaskConical,
  FolderOpen,
  GitCompareArrows,
  Layers3,
  Play,
  Plus,
  RefreshCw,
  Search,
  Settings2,
  ShieldCheck,
  SlidersHorizontal,
  Sparkles,
  Square,
  Upload,
} from "lucide-react";
import {
  activeStatuses,
  api,
  defaultConfig,
  download,
  getToken,
  number,
  post,
  setToken,
  type Entity,
} from "./api";
import {
  ArrowButton,
  Badge,
  Button,
  Empty,
  Field,
  Json,
  Metric,
  Notice,
  Panel,
  Toast,
} from "./ui";
import {
  ResidualChart,
  RowNnzHistogram,
  SparsityPlot,
  Spectrum,
} from "./charts";
import { ConvergenceHistory } from "./components/ConvergenceHistory";
import { OverviewInsights } from "./components/OverviewInsights";
import { PageOutline } from "./components/PageOutline";
import { WorkbenchActions } from "./components/WorkbenchActions";
import { buildRecentActivity } from "./models/overview";
import { useWorkspaceData } from "./hooks/useWorkspaceData";
import { useJobs } from "./hooks/useJobs";
import { useJobDetail } from "./hooks/useJobDetail";
import { EVENT_WINDOW_LIMIT } from "./models/jobs";
import { ExperimentWorkspace } from "./components/ExperimentWorkspace";
import { ConfigurationDiff } from "./components/ConfigurationDiff";
import {
  projectRole,
  withSolverMethod,
  withComputeDevice,
  cleanupItemCount,
  indexMatricesById,
} from "./models/workspace";
import { JobLifecycleControls } from "./components/JobLifecycleControls";
import { NumericalControls } from "./components/NumericalControls";
import { OperationsCenter } from "./components/OperationsCenter";
import { ModeComparison } from "./components/ModeComparison";
import { ProjectWorkspace } from "./components/ProjectWorkspace";
import { MatrixImportWorkspace } from "./components/MatrixImportWorkspace";
import { MatrixProvenance } from "./components/MatrixProvenance";
import { IndustrialWorkspace } from "./components/IndustrialWorkspace";
import { EngineeringWorkspace } from "./components/EngineeringWorkspace";
import { FieldMapping } from "./components/FieldMapping";
import {
  AssistantGovernance,
  KnowledgeEvidence,
} from "./components/AssistantGovernance";

const navigation = [
  { id: "overview", label: "项目总览", en: "WORKSPACE", icon: FolderOpen },
  {
    id: "matrices",
    label: "矩阵与输入诊断",
    en: "MATRIX LIBRARY",
    icon: Database,
  },
  {
    id: "configure",
    label: "求解配置",
    en: "SOLVER CONFIG",
    icon: SlidersHorizontal,
  },
  { id: "jobs", label: "任务中心", en: "COMPUTE JOBS", icon: Activity },
  { id: "results", label: "结果分析", en: "RESULT ANALYSIS", icon: Layers3 },
  {
    id: "compare",
    label: "运行对比",
    en: "COMPARISON",
    icon: GitCompareArrows,
  },
  { id: "benchmarks", label: "基准实验", en: "BENCHMARKS", icon: FlaskConical },
  { id: "industrial", label: "工业验收", en: "INDUSTRIAL ACCEPTANCE", icon: ShieldCheck },
  { id: "assistant", label: "诊断助手", en: "DIAGNOSTICS", icon: Sparkles },
  {
    id: "reports",
    label: "报告与交付",
    en: "REPORTS & EXPORT",
    icon: FileText,
  },
  { id: "settings", label: "系统设置", en: "SYSTEM", icon: Settings2 },
  { id: "audit", label: "审计日志", en: "AUDIT TRAIL", icon: ClipboardList },
  { id: "coverage", label: "能力与帮助", en: "COVERAGE", icon: CircleHelp },
];
const descriptions: Record<string, string> = {
  overview: "从稀疏矩阵到可信模式，让每一次计算有据可查。",
  matrices: "安全导入，检查非零结构与厄密性，保留原始问题。",
  configure: "冻结目标、算法与资源预算，预检通过后提交计算。",
  jobs: "跟踪真实阶段和数值事件，所有尝试均保留证据。",
  results: "独立复核原问题残差，区分数值有效与目标完整性。",
  compare: "在一致的输入、精度和资源条件下审视运行差异。",
  benchmarks: "冻结输入组合，批量运行并保留全部失败项。",
  industrial: "冻结模型与独立证据，逐项审查工业验收门槛。",
  assistant: "基于可解释规则诊断问题，建议采用后创建新配置。",
  reports: "将运行证据、异常和环境一起交付。",
  settings: "实际探测运行能力，明确当前环境的支持边界。",
  audit: "查看项目和任务操作记录，回溯实验过程。",
  coverage: "查阅功能实现范围、已知限制和工作台使用说明。",
};
const kindLabels: Record<string, string> = {
  indefinite: "不定稀疏矩阵",
  clustered: "聚集谱矩阵",
  repeated: "重复特征值",
  near_singular: "近奇异位移",
  complex: "复数厄密矩阵",
};
const fmtDate = (value: string) =>
  value
    ? new Date(value).toLocaleString("zh-CN", {
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
      })
    : "—";
const short = (value: string) => (value ? value.slice(0, 12) : "—");
const message = (error: unknown) =>
  error instanceof Error ? error.message : String(error);
const isAvailable = (cap: unknown) =>
  typeof cap === "object" && cap !== null
    ? Boolean((cap as Entity).supported ?? (cap as Entity).available)
    : Boolean(cap);
const metricValue = (job: Entity, key: string) =>
  job.result?.metrics?.[key] ?? job.metrics?.[key];

export default function App() {
  const [page, setPage] = useState("overview");
  const [mobile, setMobile] = useState(false);
  const workspace = useWorkspaceData();
  const {
    projects,
    setProjects,
    matrices,
    setMatrices,
    health,
    capabilities,
    identity,
  } = workspace;
  const [projectId, setProjectId] = useState(""),
    [matrixId, setMatrixId] = useState(""),
    [jobId, setJobId] = useState("");
  const [busy, setBusy] = useState(""),
    [toast, setToast] = useState<{ message: string; error: boolean } | null>(
      null,
    );
  const [config, setConfig] = useState<Entity>(() => defaultConfig()),
    [plan, setPlan] = useState<Entity | null>(null),
    [snapshot, setSnapshot] = useState<Entity | null>(null);
  const [search, setSearch] = useState(""),
    [statusFilter, setStatusFilter] = useState("ALL"),
    [projectDialog, setProjectDialog] = useState(false),
    [projectName, setProjectName] = useState(""),
    [projectDescription, setProjectDescription] = useState("");
  const jobList = useJobs(
    projectId,
    page === "jobs" ? statusFilter : "ALL",
    Boolean(identity && (identity.global_admin || projects.length)),
  );
  const { jobs, summary } = jobList;
  const detail = useJobDetail(jobId);
  const { selectedJob, setSelectedJob, events, result } = detail;
  const loading = workspace.loading || jobList.loading;
  const loadError = workspace.error;
  const pollError = [jobList.error, detail.error].filter(Boolean).join("；");
  const [sampleKind, setSampleKind] = useState("indefinite"),
    [sampleN, setSampleN] = useState(500),
    [storage, setStorage] = useState("full");
  const [importFile, setImportFile] = useState<File | null>(null);
  const [numericalError, setNumericalError] = useState("");
  const [comparison, setComparison] = useState<string[]>([]),
    [compareInfo, setCompareInfo] = useState<Entity | null>(null),
    [diagnosisResult, setDiagnosis] = useState<{
      fingerprint: string;
      value: Entity;
    } | null>(null),
    [coverage, setCoverage] = useState<Entity[]>([]),
    [audit, setAudit] = useState<Entity[]>([]);
  const [auth, setAuth] = useState(getToken()),
    [report, setReport] = useState<Entity | null>(null);
  const uploadRef = useRef<HTMLInputElement>(null),
    restoreRef = useRef<HTMLInputElement>(null);
  const [restoreInfo, setRestoreInfo] = useState<Entity | null>(null),
    [modeIndices, setModeIndices] = useState<number[]>([]),
    [vectorPreview, setVectorPreview] = useState<Entity | null>(null),
    [cleanupPreview, setCleanupPreview] = useState<Entity | null>(null);
  const selectedMatrix = matrices.find((m) => m.id === matrixId);
  const currentProject = projects.find((p) => p.id === projectId);
  const scopedJobs = jobs;
  const scopedMatrices = projectId
    ? matrices.filter((m) => m.project_id === projectId)
    : matrices;
  const currentNav = navigation.find((n) => n.id === page)!;
  const diagnosisFingerprint = JSON.stringify(
    jobId
      ? {
          jobId,
          status: selectedJob?.status,
          metrics: selectedJob?.metrics,
          error: selectedJob?.error,
        }
      : { config },
  );
  const diagnosisContext = useRef(diagnosisFingerprint);
  diagnosisContext.current = diagnosisFingerprint;
  const diagnosis =
    diagnosisResult?.fingerprint === diagnosisFingerprint
      ? diagnosisResult.value
      : null;
  const selectProject = (id: string) => {
    setProjectId(id);
    setMatrixId("");
    setJobId("");
    setPlan(null);
    setConfig(defaultConfig());
  };

  const load = async () => {
    jobList.refresh();
    detail.refresh();
    await workspace.reload();
  };
  useEffect(() => {
    if (!toast) return;
    const id = window.setTimeout(() => setToast(null), 6000);
    return () => clearTimeout(id);
  }, [toast]);
  useEffect(() => {
    setModeIndices([]);
    setVectorPreview(null);
  }, [jobId]);
  useEffect(() => {
    if (page !== "coverage" && page !== "audit") return;
    let alive = true;
    api<Entity[]>(page === "coverage" ? "/coverage" : "/audit")
      .then((data) => {
        if (alive) (page === "coverage" ? setCoverage : setAudit)(data);
      })
      .catch((e) => {
        if (alive) setToast({ message: message(e), error: true });
      });
    return () => {
      alive = false;
    };
  }, [page]);

  useEffect(() => {
    setCompareInfo(null);
    if (comparison.length !== 2) {
      return;
    }
    let alive = true;
    post("/compare", { job_ids: comparison })
      .then((data) => {
        if (alive) setCompareInfo(data);
      })
      .catch((e) => {
        if (alive) setToast({ message: message(e), error: true });
      });
    return () => {
      alive = false;
    };
  }, [comparison]);
  useEffect(() => {
    if (!projectDialog && !mobile) return;
    const keydown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setProjectDialog(false);
        setMobile(false);
      }
      if (projectDialog && event.key === "Tab") {
        const controls = Array.from(
          document.querySelectorAll<HTMLElement>(
            ".modal button:not(:disabled),.modal input,.modal textarea",
          ),
        );
        const first = controls[0],
          last = controls[controls.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last?.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first?.focus();
        }
      }
    };
    document.addEventListener("keydown", keydown);
    return () => document.removeEventListener("keydown", keydown);
  }, [projectDialog, mobile]);

  const execute = async (label: string, action: () => Promise<void>) => {
    if (busy) return;
    setBusy(label);
    try {
      await action();
    } catch (e) {
      setToast({ message: message(e), error: true });
    } finally {
      setBusy("");
    }
  };
  const success = (text: string) => setToast({ message: text, error: false });
  const go = (value: string) => {
    setPage(value);
    setMobile(false);
    setSearch("");
    window.scrollTo({ top: 0, behavior: "auto" });
  };
  const selectMatrix = (matrix: Entity) => {
    if (matrix.project_id && matrix.project_id !== projectId) {
      setProjectId(matrix.project_id);
      setJobId("");
      setComparison([]);
    }
    setMatrixId(matrix.id);
    setConfig((c) => ({
      ...c,
      problem: {
        ...c.problem,
        matrix_id: matrix.id,
        storage: matrix.storage || "full",
      },
    }));
    setPlan(null);
    setSnapshot(null);
  };
  const modify = (section: string, key: string, value: unknown) => {
    setConfig((c) => ({ ...c, [section]: { ...c[section], [key]: value } }));
    setPlan(null);
    setSnapshot(null);
  };
  const ensureProject = async () => {
    if (projectId) return projectId;
    if (projects.length) {
      setProjectId(projects[0].id);
      return projects[0].id;
    }
    const project = await post("/projects", {
      name: "电磁模式探索",
      description: "本地稀疏厄密特征值实验",
    });
    setProjects((p) => [project, ...p]);
    setProjectId(project.id);
    return project.id;
  };
  const makeSample = async (run = false) => {
    const pid = await ensureProject();
    const matrix = await post("/samples", {
      project_id: pid,
      kind: sampleKind,
      n: sampleN,
    });
    setMatrices((m) => [matrix, ...m]);
    jobList.refresh();
    selectMatrix(matrix);
    success("合成样例已生成，来源标记为 synthetic");
    if (run) {
      const c = defaultConfig(matrix.id);
      c.target.k = Math.min(12, sampleN - 2);
      const frozen = await post("/configs", { config: c });
      setConfig(frozen.config);
      setSnapshot(frozen);
      const p = await post("/plans", { config_id: frozen.id, project_id: pid });
      setPlan(p);
      go("configure");
      success("样例与预检计划已生成，请检查实际后端和警告后运行。");
    } else go("matrices");
  };
  const createPlan = async () => {
    const pid = await ensureProject();
    const frozen = await post("/configs", { config });
    setSnapshot(frozen);
    setConfig(frozen.config);
    const p = await post("/plans", { config_id: frozen.id, project_id: pid });
    setPlan(p);
    success("配置已冻结，预检计划已生成");
  };
  const submit = async () => {
    if (!plan) return;
    const job = await post("/jobs", {
      plan_id: plan.id,
      idempotency_key: crypto.randomUUID(),
    });
    setSelectedJob(job);
    jobList.refresh();
    setJobId(job.id);
    go("jobs");
    success("任务已提交，正在跟踪实际计算阶段");
  };
  const openJob = (job: Entity, destination = "jobs") => {
    setSelectedJob(job);
    setJobId(job.id);
    go(destination);
  };
  const diagnose = async () => {
    const expected = diagnosisFingerprint;
    const data = await post(
      "/diagnose",
      jobId ? { job_id: jobId } : { config },
    );
    if (diagnosisContext.current !== expected) return;
    setDiagnosis({ fingerprint: expected, value: data });
    success("已生成可解释规则建议");
  };
  const adopt = async (changes: Entity) => {
    const expected = diagnosisFingerprint;
    const frozen = await post("/configs/advice", {
      ...(selectedJob ? { job_id: selectedJob.id } : { config }),
      changes,
    });
    if (diagnosisContext.current !== expected) {
      success("建议配置已创建；当前上下文已变化，未自动切换页面");
      return;
    }
    setConfig(frozen.config);
    setSnapshot(frozen);
    setPlan(null);
    setMatrixId(frozen.config.problem.matrix_id);
    go("configure");
    success("建议已保存为新配置版本；原实验保持可追溯");
  };
  const residuals: number[] = result?.residuals || [];
  const eigenvalues: number[] = result?.eigenvalues || [];
  const validation = result?.validation || {};
  const selectedComparison = jobs.filter((j) => comparison.includes(j.id));
  const comparable = compareInfo?.comparable === true;
  const toggleCompare = (id: string) =>
    setComparison((c) =>
      c.includes(id) ? c.filter((v) => v !== id) : [...c.slice(-1), id],
    );
  const jobPagination = (
    <div className="job-pagination">
      <span className="subtle-copy">
        已加载 {jobs.length} / {jobList.total ?? "—"} 个任务 · 按登记顺序显示
      </span>
      {jobList.hasMore ? (
        <Button
          busy={jobList.loadingMore}
          disabled={jobList.loading || jobList.loadingMore}
          onClick={() => void jobList.loadMore()}
        >
          加载更多任务
        </Button>
      ) : null}
    </div>
  );
  const jobPicker = (
    <>
      <select
        aria-label="选择任务"
        value={jobId}
        onChange={(e) => setJobId(e.target.value)}
      >
        <option value="">选择任务…</option>
        {selectedJob && !scopedJobs.some((j) => j.id === selectedJob.id) ? (
          <option value={selectedJob.id}>
            {short(selectedJob.id)} · {selectedJob.status}
          </option>
        ) : null}
        {scopedJobs.map((j) => (
          <option value={j.id} key={j.id}>
            {short(j.id)} · {j.status}
          </option>
        ))}
      </select>
      {jobList.hasMore ? (
        <Button
          busy={jobList.loadingMore}
          disabled={jobList.loading || jobList.loadingMore}
          onClick={() => void jobList.loadMore()}
        >
          加载更多任务
        </Button>
      ) : null}
    </>
  );
  const configValid =
    !numericalError &&
    !(
      ["block_shift_invert", "jacobi_davidson"].includes(
        config.solver.method,
      ) && config.target.mode !== "nearest_k"
    ) &&
    Boolean(config.problem.matrix_id) &&
    !selectedMatrix?.archived &&
    (config.target.mode === "interval_all"
      ? Number.isFinite(config.target.interval?.lower) &&
        Number.isFinite(config.target.interval?.upper) &&
        config.target.interval.lower < config.target.interval.upper
      : Number.isInteger(config.target.k) &&
        config.target.k > 0 &&
        (!selectedMatrix || config.target.k < selectedMatrix.n)) &&
    config.resources.memory_limit_gib > 0 &&
    config.resources.threads >= 1 &&
    config.resources.walltime_seconds > 0;

  const workbenchActions = <WorkbenchActions page={page} matrix={selectedMatrix}
    job={selectedJob} result={result} config={config} plan={plan} summary={summary}
    onNavigate={go} />;

  return (
    <div className="app-shell">
      {mobile ? (
        <button
          className="sidebar-backdrop"
          aria-label="关闭导航"
          onClick={() => setMobile(false)}
        />
      ) : null}
      <aside className={`sidebar ${mobile ? "open" : ""}`}>
        <button
          className="brand"
          onClick={() => go("overview")}
          aria-label="EMSolver 首页"
        >
          <span>
            EMSolver<small>电磁模式求解工作台</small>
          </span>
        </button>
        <div className="workspace-switch">
          <div>
            <b>工作目录</b>
          </div>
          <button className="paper-nav-close" onClick={() => setMobile(false)}>关闭目录</button>
        </div>
        <div className="nav-section">
          实验与计算
        </div>
        <nav aria-label="主导航">
          {navigation.map((item, index) => (
            <div key={item.id}>
              {index === 4 || index === 9 ? (
                <div className="nav-section secondary">
                  {index === 4 ? "分析与验证" : "系统与交付"}
                </div>
              ) : null}
              <button
                className={`nav-item ${page === item.id ? "active" : ""}`}
                aria-current={page === item.id ? "page" : undefined}
                onClick={() => go(item.id)}
              >
                <span className="paper-nav-number" aria-hidden="true">{index + 1}.</span>
                <span>{item.label}</span>
                {item.id === "jobs" && (summary?.active_jobs ?? 0) > 0 ? (
                  <b>{summary?.active_jobs}</b>
                ) : null}
                {item.id === "matrices" && matrices.length > 0 ? (
                  <em>{summary?.matrices ?? "—"}</em>
                ) : null}
              </button>
            </div>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <div className="local-status">
            <span>{health ? "本地服务已连接" : "等待本地服务"}</span>
            <span className="mono">v{health?.version || "—"}</span>
          </div>
          <div className="privacy">
            离线优先 · 实验数据留在本机
          </div>
        </div>
      </aside>
      <div className="workspace">
        <header className="topbar">
          <div className="breadcrumbs">
            <button
              className="mobile-menu"
              aria-label="打开导航"
              aria-expanded={mobile}
              onClick={() => setMobile(true)}
            >
              目录
            </button>
            <span>工作空间</span>
            <span aria-hidden="true">/</span>
            <b>{currentNav.label}</b>
          </div>
          <div className="topbar-right">
            <span className="environment">
              {health ? "本地服务已连接" : "等待连接"}
            </span>
            <button
              aria-label="刷新工作台"
              className="icon-button"
              onClick={() => void load()}
            >
              {loading ? "同步中" : "刷新"}
            </button>
            <button
              aria-label="查看任务通知"
              className="icon-button"
              onClick={() => go("jobs")}
            >
              任务通知{(summary?.active_jobs ?? 0) > 0 ? ` (${summary?.active_jobs})` : ""}
            </button>
          </div>
        </header>
        <main key={page} className="paper-document" data-workspace={page}>
          <div className="page-heading">
            <div>
              <h1>{currentNav.label}</h1>
              <p className="paper-authors">EMSolver · 稀疏厄密特征值求解与独立验证</p>
              <p className="paper-edition">
                本地工作版 v{health?.version || "—"}
                <span aria-hidden="true"> · </span>
                <time dateTime={new Date().toLocaleDateString("en-CA")}>
                  {new Date().toLocaleDateString("zh-CN", {year: "numeric", month: "long", day: "numeric"})}
                </time>
              </p>
              <div className="paper-abstract" role="note" aria-label="摘要">
                <strong>摘要</strong>
                <p>{descriptions[page]}</p>
              </div>
            </div>
            <div className="heading-actions">
              {page === "overview" ? (
                <>
                  <Button
                    disabled={Boolean(
                      identity?.operations_admin && !identity.global_admin,
                    )}
                    onClick={() => setProjectDialog(true)}
                  >
                    <Plus size={16} />
                    新建项目
                  </Button>
                  <Button
                    primary
                    busy={busy === "demo"}
                    disabled={Boolean(busy)}
                    onClick={() => void execute("demo", () => makeSample(true))}
                  >
                    <Play size={15} />
                    体验完整求解
                  </Button>
                </>
              ) : page === "matrices" ? (
                <Button
                  primary
                  onClick={() => uploadRef.current?.click()}
                  disabled={Boolean(busy)}
                >
                  <Upload size={15} />
                  导入矩阵
                </Button>
              ) : page === "configure" ? (
                <Button
                  primary
                  busy={busy === "plan"}
                  disabled={!configValid || Boolean(busy)}
                  onClick={() => void execute("plan", createPlan)}
                >
                  <ShieldCheck size={16} />
                  校验并生成计划
                </Button>
              ) : page === "jobs" ? (
                <Button onClick={() => void load()}>
                  <RefreshCw size={15} />
                  刷新任务
                </Button>
              ) : null}
            </div>
          </div>
          {loadError ? (
            <Notice kind="error">
              <b>部分数据无法加载</b>
              <p>{loadError}</p>
              <Button onClick={() => void load()}>重试连接</Button>
            </Notice>
          ) : null}
          {pollError ? (
            <Notice kind="warning">
              实时更新中断：{pollError}
              。系统将自动重连；当前显示最后一次成功获取的数据。
            </Notice>
          ) : null}
          {loading ? (
            <div className="loading-strip" role="status">
              <span className="skeleton" />
              正在同步本地实验记录…
            </div>
          ) : null}
          {[
            "overview",
            "matrices",
            "jobs",
            "results",
            "benchmarks",
            "reports",
            "compare",
          ].includes(page) ? (
            <div className="project-bar">
              <div>
                <FolderOpen size={15} />
                <select
                  aria-label="项目范围"
                  value={projectId}
                  onChange={(e) => {
                    selectProject(e.target.value);
                  }}
                >
                  <option value="">全部项目</option>
                  {projects.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.name}
                    </option>
                  ))}
                </select>
              </div>
              <span>
                {currentProject?.description ||
                  "独立输入 · 不可变配置 · 可追溯证据"}
              </span>
            </div>
          ) : null}

          <PageOutline pageKey={page} />
          {["matrices", "configure", "jobs", "results", "compare", "benchmarks", "industrial", "assistant", "reports"].includes(page) ? workbenchActions : null}

          {page === "overview" ? (
            <>
              <div className="metrics-row">
                <Metric
                  label="实验项目"
                  value={number(summary?.projects)}
                  detail="本地项目空间"
                />
                <Metric
                  label="已登记矩阵"
                  value={number(summary?.matrices)}
                  detail="稀疏输入资产"
                />
                <Metric
                  label="计算任务"
                  value={number(summary?.jobs)}
                  detail={
                    <>
                      <span className="green-dot" />
                      {summary?.active_jobs ?? "—"} 项正在执行
                    </>
                  }
                />
                <Metric
                  label="验证通过"
                  value={number(summary?.certified_jobs)}
                  unit={`/ ${summary?.jobs ?? "—"}`}
                  detail="数值与目标认证均通过"
                />
              </div>
              <OverviewInsights summary={summary} jobs={scopedJobs} total={jobList.total}
                hasMore={jobList.hasMore} matrices={scopedMatrices} onOpenJob={openJob}
                onSelectStatus={(status) => { setStatusFilter(status); go("jobs"); }}
                onShowMatrices={() => go("matrices")} />
              {workbenchActions}
              <div className="overview-grid">
                <Panel
                  className="workflow-panel"
                  title="每一次求解，都有完整的证据链"
                  eyebrow="A REPRODUCIBLE COMPUTE WORKFLOW"
                  action={
                    <span className="outlined-label">SPARSE · HERMITIAN</span>
                  }
                >
                  <div className="paper-equation" role="math" aria-label="标准厄密特征值问题，A x 等于 lambda x">
                    <span>Ax = λx</span>
                  </div>
                  <p className="paper-definition">
                    <strong>原问题验证.</strong> 将候选特征对代回原矩阵，分别核对数值误差与目标覆盖，并保留配置、事件和结果。
                  </p>
                  <div className="workflow-steps">
                    {[
                      {
                        n: "01",
                        t: "导入矩阵",
                        d: "结构、精度与厄密性检查",
                        p: "matrices",
                      },
                      {
                        n: "02",
                        t: "规划计算",
                        d: "目标、算法与资源预检",
                        p: "configure",
                      },
                      {
                        n: "03",
                        t: "独立验证",
                        d: "数值残差与完整性分离",
                        p: "results",
                      },
                    ].map((s) => (
                      <button key={s.n} onClick={() => go(s.p)}>
                        <span>{s.n}</span>
                        <div>
                          <b>{s.t}</b>
                          <small>{s.d}</small>
                        </div>
                        <ArrowRight size={15} />
                      </button>
                    ))}
                  </div>
                </Panel>
                <Panel
                  className="environment-panel"
                  title="计算环境"
                  eyebrow="RUNTIME STATUS"
                  action={<Cpu size={19} />}
                >
                  <div className="runtime-head">
                    <span className="runtime-icon">
                      <Cpu size={27} />
                    </span>
                    <div>
                      <b>{health ? "本地计算节点" : "等待服务连接"}</b>
                      <small>CPU · 双精度 · 稀疏计算</small>
                    </div>
                  </div>
                  {["scipy", "slepc", "mpi", "gpu"].map((key) => (
                    <div className="runtime-row" key={key}>
                      <span>
                        {key === "scipy"
                          ? "SciPy / ARPACK"
                          : key === "slepc"
                            ? "SLEPc 扩展"
                            : key === "mpi"
                              ? "MPI 分布式"
                              : "GPU 加速"}
                      </span>
                      <span
                        className={
                          isAvailable(capabilities[key]) ? "supported" : "muted"
                        }
                      >
                        <i />
                        {isAvailable(capabilities[key]) ? "可用" : "未启用"}
                      </span>
                    </div>
                  ))}
                  <ArrowButton onClick={() => go("settings")}>
                    查看能力与支持边界
                  </ArrowButton>
                </Panel>
              </div>
              <Panel
                title={jobList.hasMore ? "已加载任务明细" : "近期任务明细"}
                eyebrow="RECENT COMPUTE JOBS"
                action={
                  <ArrowButton onClick={() => go("jobs")}>全部任务</ArrowButton>
                }
              >
                {scopedJobs.length ? (
                  <JobTable
                    jobs={buildRecentActivity(scopedJobs, jobList.total, jobList.hasMore, 5).items.map((item) => item.job)}
                    matrices={matrices}
                    onOpen={openJob}
                  />
                ) : (
                  <Empty
                    title="准备好第一次可信求解"
                    description="导入 .npz / .npy 稀疏矩阵，或用合成样例体验从预检到结果的完整流程。"
                    action={
                      <Button
                        onClick={() =>
                          void execute("demo", () => makeSample(true))
                        }
                        busy={busy === "demo"}
                      >
                        <FlaskConical size={16} />
                        生成演示样例
                        <ArrowRight size={14} />
                      </Button>
                    }
                  />
                )}
              </Panel>
              <ProjectWorkspace
                projects={projects}
                identity={identity}
                selectedId={projectId}
                onSelect={selectProject}
                onCreate={() => setProjectDialog(true)}
                onUpdated={(project) => {
                  setProjects((items) =>
                    items.map((item) =>
                      item.id === project.id ? project : item,
                    ),
                  );
                  setPlan(null);
                  jobList.refresh();
                }}
                notify={(text, error = false) =>
                  setToast({ message: text, error })
                }
              />
              <div className="footnote">
                <ShieldCheck size={15} />
                <span>
                  计算完成不等于验证通过。只有原问题残差合格且目标认证通过，才标记为成功。
                </span>
                <span className="mono">STANDARD HERMITIAN / CPU</span>
              </div>
            </>
          ) : null}

          {page === "matrices" ? (
            <>
              <MatrixImportWorkspace
                projectId={projectId}
                file={importFile}
                onFileChange={setImportFile}
                storage={storage}
                onStorageChange={setStorage}
                onImported={(matrix) => {
                  setMatrices((items) => [
                    matrix,
                    ...items.filter((item) => item.id !== matrix.id),
                  ]);
                  selectMatrix(matrix);
                  jobList.refresh();
                }}
                notify={(text, error = false) =>
                  setToast({ message: text, error })
                }
              />
              <div className="two-col matrix-layout">
                <div>
                  <Panel
                    title="矩阵资产"
                    eyebrow="INPUT ASSETS"
                    action={
                      <span className="count-label">
                        {scopedMatrices.length} MATRICES
                      </span>
                    }
                  >
                    <div className="search-field">
                      <Search size={16} />
                      <input
                        aria-label="搜索矩阵"
                        value={search}
                        onChange={(e) => setSearch(e.target.value)}
                        placeholder="搜索矩阵名称或编号…"
                      />
                    </div>
                    {scopedMatrices.filter((m) =>
                      (m.name + m.id)
                        .toLowerCase()
                        .includes(search.toLowerCase()),
                    ).length ? (
                      <div className="matrix-list">
                        {scopedMatrices
                          .filter((m) =>
                            (m.name + m.id)
                              .toLowerCase()
                              .includes(search.toLowerCase()),
                          )
                          .map((m) => (
                            <button
                              className={`matrix-item ${matrixId === m.id ? "selected" : ""}`}
                              key={m.id}
                              onClick={() => selectMatrix(m)}
                            >
                              <span className="asset-icon">
                                <Braces size={23} />
                              </span>
                              <div>
                                <b>{m.name}</b>
                                <small>
                                  {number(m.n)} × {number(m.n)} <span>·</span>{" "}
                                  {m.dtype || "—"}
                                </small>
                                <em>
                                  {m.archived
                                    ? "已归档 · 保留历史引用"
                                    : m.source === "synthetic"
                                      ? "合成样例 / SYNTHETIC"
                                      : m.source === "private"
                                        ? "用户导入 / PRIVATE"
                                        : m.source || "用户导入"}
                                </em>
                              </div>
                              <ChevronRight size={17} />
                            </button>
                          ))}
                      </div>
                    ) : (
                      <Empty
                        title="矩阵库为空"
                        description="支持 SciPy 稀疏 .npz 与 NumPy .npy；输入将在服务端安全校验。"
                        action={
                          <Button onClick={() => uploadRef.current?.click()}>
                            <Upload size={15} />
                            选择矩阵文件
                          </Button>
                        }
                      />
                    )}
                  </Panel>
                  <Panel title="生成可复现样例" eyebrow="SYNTHETIC FIXTURES">
                    <p className="panel-copy">
                      用于功能演示与数值验证。样例不包含参考答案，不代表真实电磁工程性能。
                    </p>
                    <div className="form-grid">
                      <Field label="谱分布">
                        <select
                          value={sampleKind}
                          onChange={(e) => setSampleKind(e.target.value)}
                        >
                          {Object.entries(kindLabels).map(([k, v]) => (
                            <option value={k} key={k}>
                              {v}
                            </option>
                          ))}
                        </select>
                      </Field>
                      <Field label="矩阵维度 n">
                        <input
                          type="number"
                          min="20"
                          max="20000"
                          value={sampleN}
                          onChange={(e) => setSampleN(Number(e.target.value))}
                        />
                      </Field>
                    </div>
                    <Button
                      busy={busy === "sample"}
                      disabled={
                        Boolean(busy) || sampleN < 20 || sampleN > 20000
                      }
                      onClick={() => void execute("sample", () => makeSample())}
                    >
                      <Plus size={15} />
                      生成并登记样例
                    </Button>
                  </Panel>
                </div>
                <Panel
                  title="输入诊断"
                  eyebrow="MATRIX INSPECTION"
                  action={
                    selectedMatrix ? (
                      <Badge
                        status={selectedMatrix.archived ? "ARCHIVED" : "READY"}
                      />
                    ) : null
                  }
                >
                  {selectedMatrix ? (
                    <>
                      <SparsityPlot matrix={selectedMatrix} />
                      <RowNnzHistogram
                        histogram={
                          selectedMatrix.diagnostics?.row_nnz_histogram
                        }
                      />
                      <div className="diagnostic-stats">
                        <Metric
                          label="矩阵维度"
                          value={number(selectedMatrix.n)}
                        />
                        <Metric
                          label="非零元素 nnz"
                          value={number(selectedMatrix.nnz)}
                        />
                        <Metric
                          label="稀疏度"
                          value={
                            selectedMatrix.n
                              ? number(
                                  100 *
                                    (selectedMatrix.nnz /
                                      selectedMatrix.n ** 2),
                                  4,
                                )
                              : "—"
                          }
                          unit="%"
                        />
                        <Metric
                          label="厄密相对偏差"
                          value={number(
                            selectedMatrix.diagnostics?.hermitian_error,
                          )}
                        />
                      </div>
                      <dl className="details-list">
                        <dt>数值类型</dt>
                        <dd className="mono">{selectedMatrix.dtype}</dd>
                        <dt>存储形式</dt>
                        <dd>{selectedMatrix.storage || "full"}</dd>
                        <dt>无穷范数</dt>
                        <dd>{number(selectedMatrix.diagnostics?.norm_inf)}</dd>
                        <dt>对角范围</dt>
                        <dd>
                          {number(selectedMatrix.diagnostics?.diagonal_min)} ～{" "}
                          {number(selectedMatrix.diagnostics?.diagonal_max)}
                        </dd>
                        <dt>每行非零数范围</dt>
                        <dd>
                          {number(selectedMatrix.diagnostics?.row_nnz_min)} ～{" "}
                          {number(selectedMatrix.diagnostics?.row_nnz_max)}
                        </dd>
                        <dt>输入指纹</dt>
                        <dd
                          className="hash"
                          title={selectedMatrix.hash || selectedMatrix.sha256}
                        >
                          {selectedMatrix.hash ||
                            selectedMatrix.sha256 ||
                            "由服务端记录"}
                        </dd>
                        <dt>登记时间</dt>
                        <dd>{fmtDate(selectedMatrix.created_at)}</dd>
                      </dl>
                      <Button
                        primary
                        disabled={selectedMatrix.archived}
                        onClick={() => go("configure")}
                      >
                        <SlidersHorizontal size={16} />
                        使用此矩阵配置求解
                        <ArrowRight size={15} />
                      </Button>
                      <Button
                        disabled={selectedMatrix.archived || Boolean(busy)}
                        onClick={() =>
                          void execute("archive", async () => {
                            const archived = await post(
                              `/matrices/${selectedMatrix.id}/archive`,
                            );
                            setMatrices((ms) =>
                              ms.map((m) =>
                                m.id === archived.id ? archived : m,
                              ),
                            );
                            setPlan(null);
                            success("矩阵已归档，文件和既有任务引用均保留");
                          })
                        }
                      >
                        <Box size={14} />
                        归档矩阵
                      </Button>
                      <MatrixProvenance
                        key={`${selectedMatrix.id}:${selectedMatrix.metadata_version ?? 1}`}
                        matrix={selectedMatrix}
                        canEdit={["admin", "analyst"].includes(
                          projectRole(
                            projects.find(
                              (project) =>
                                project.id === selectedMatrix.project_id,
                            ),
                            identity,
                          ) || "",
                        )}
                        canShare={
                          projectRole(
                            projects.find(
                              (project) =>
                                project.id === selectedMatrix.project_id,
                            ),
                            identity,
                          ) === "admin"
                        }
                        onSaved={(matrix) => {
                          setMatrices((items) =>
                            items.map((item) =>
                              item.id === matrix.id ? matrix : item,
                            ),
                          );
                          setPlan(null);
                        }}
                        onDerived={(matrix) => {
                          setMatrices((items) => [
                            matrix,
                            ...items.filter((item) => item.id !== matrix.id),
                          ]);
                          selectMatrix(matrix);
                          jobList.refresh();
                        }}
                        notify={(text, error = false) =>
                          setToast({ message: text, error })
                        }
                      />
                      <EngineeringWorkspace matrix={selectedMatrix}
                        canManage={projectRole(projects.find(project => project.id === selectedMatrix.project_id), identity) === "admin"}
                        onChanged={() => { void workspace.reload(); setPlan(null); }} />
                      <details>
                        <summary>完整诊断数据</summary>
                        <Json
                          value={selectedMatrix.diagnostics || selectedMatrix}
                        />
                      </details>
                    </>
                  ) : (
                    <Empty
                      title="选择一个输入矩阵"
                      description="检查非零结构、厄密性与数据指纹后再进入求解。"
                    />
                  )}
                </Panel>
              </div>
              <Panel title="导入选项">
                <div className="form-grid">
                  <Field
                    label="存储形式"
                    hint="三角存储会按指定方式恢复；不会静默对称化原问题。"
                  >
                    <select
                      value={storage}
                      onChange={(e) => setStorage(e.target.value)}
                    >
                      <option value="full">完整矩阵</option>
                      <option value="upper">上三角</option>
                      <option value="lower">下三角</option>
                    </select>
                  </Field>
                  <div className="import-note">
                    <ShieldCheck size={20} />
                    <span>
                      类型、尺寸、有限值、结构和厄密性由服务端检查。上传失败将显示具体原因。
                    </span>
                  </div>
                </div>
              </Panel>
            </>
          ) : null}

          {page === "configure" ? (
            <div className="config-layout">
              <div>
                <Panel title="输入与求解目标" eyebrow="01 / PROBLEM DEFINITION">
                  <Field label="输入矩阵">
                    <select
                      value={config.problem.matrix_id}
                      onChange={(e) => {
                        const matrix = matrices.find(
                          (m) => m.id === e.target.value,
                        );
                        if (matrix) selectMatrix(matrix);
                      }}
                    >
                      <option value="">选择已登记的矩阵…</option>
                      {scopedMatrices
                        .filter((m) => !m.archived)
                        .map((m) => (
                          <option key={m.id} value={m.id}>
                            {m.name} · n={m.n}
                          </option>
                        ))}
                    </select>
                  </Field>
                  {!matrices.length ? (
                    <Notice>
                      请先在矩阵库导入输入或生成合成样例。
                      <ArrowButton onClick={() => go("matrices")}>
                        进入矩阵库
                      </ArrowButton>
                    </Notice>
                  ) : null}
                  <div className="segmented">
                    <button
                      className={
                        config.target.mode === "nearest_k" ? "selected" : ""
                      }
                      onClick={() => {
                        setConfig((c) => ({
                          ...c,
                          target: {
                            ...c.target,
                            mode: "nearest_k",
                            k: 12,
                            sigma: 0,
                            interval: null,
                          },
                        }));
                        setPlan(null);
                      }}
                    >
                      最近 k 个模式
                    </button>
                    <button
                      className={
                        config.target.mode === "interval_all" ? "selected" : ""
                      }
                      onClick={() => {
                        setConfig((c) => ({
                          ...c,
                          target: {
                            ...c.target,
                            mode: "interval_all",
                            k: null,
                            sigma: null,
                            interval: {
                              lower: -1,
                              upper: 1,
                              closure: "left_closed_right_open",
                              boundary_atol: 1e-10,
                            },
                          },
                        }));
                        setPlan(null);
                      }}
                    >
                      区间内全部模式
                    </button>
                  </div>
                  <div className="form-grid">
                    {config.target.mode === "nearest_k" ? (
                      <>
                        <Field
                          label="目标数量 k"
                          hint="必须小于矩阵维度；最终以原问题独立验证为准。"
                        >
                          <input
                            type="number"
                            min="1"
                            max={
                              selectedMatrix ? selectedMatrix.n - 1 : undefined
                            }
                            value={config.target.k ?? ""}
                            onChange={(e) =>
                              modify("target", "k", Number(e.target.value))
                            }
                          />
                        </Field>
                        <Field
                          label="目标位移 σ"
                          hint="排序中心；工作位移调整不会改变请求目标。"
                        >
                          <input
                            type="number"
                            step="any"
                            value={config.target.sigma ?? ""}
                            onChange={(e) =>
                              modify("target", "sigma", Number(e.target.value))
                            }
                          />
                        </Field>
                      </>
                    ) : (
                      <>
                        <Field label="区间下界 [a">
                          <input
                            type="number"
                            step="any"
                            value={config.target.interval.lower}
                            onChange={(e) =>
                              modify("target", "interval", {
                                ...config.target.interval,
                                lower: Number(e.target.value),
                              })
                            }
                          />
                        </Field>
                        <Field label="区间上界 b)">
                          <input
                            type="number"
                            step="any"
                            value={config.target.interval.upper}
                            onChange={(e) =>
                              modify("target", "interval", {
                                ...config.target.interval,
                                upper: Number(e.target.value),
                              })
                            }
                          />
                        </Field>
                      </>
                    )}
                    <Field label="模式簇策略">
                      <select
                        value={config.target.cluster_policy}
                        onChange={(e) =>
                          modify("target", "cluster_policy", e.target.value)
                        }
                      >
                        <option value="fixed_k">固定数量 · fixed_k</option>
                        <option value="cluster_complete">
                          完整模式簇 · cluster_complete
                        </option>
                      </select>
                    </Field>
                    <Field label="最大候选模式数">
                      <input
                        type="number"
                        min="1"
                        max="10000"
                        value={config.target.max_modes || 200}
                        onChange={(e) =>
                          modify("target", "max_modes", Number(e.target.value))
                        }
                      />
                    </Field>
                  </div>
                </Panel>
                <Panel title="数值算法与精度" eyebrow="02 / NUMERICAL STRATEGY">
                  <div className="form-grid">
                    <Field label="计算后端">
                      <select
                        value={config.solver.backend}
                        onChange={(e) => {
                          const backend = e.target.value;
                          setConfig((previous) =>
                            withSolverMethod(
                              previous,
                              backend === "scipy" ? "arpack" : "krylovschur",
                              backend,
                            ),
                          );
                          setPlan(null);
                          setSnapshot(null);
                        }}
                      >
                        <option value="scipy">SciPy · CPU 稀疏路径</option>
                        <option
                          value="slepc"
                          disabled={!isAvailable(capabilities.slepc)}
                        >
                          SLEPc ·{" "}
                          {isAvailable(capabilities.slepc)
                            ? "可用"
                            : "当前未安装"}
                        </option>
                      </select>
                    </Field>
                    <Field label="实际算法">
                      <select
                        value={config.solver.method}
                        onChange={(e) => {
                          const method = e.target.value;
                          setConfig((previous) =>
                            withSolverMethod(previous, method),
                          );
                          setPlan(null);
                          setSnapshot(null);
                        }}
                      >
                        {config.solver.backend === "scipy" ? <option value="arpack">ARPACK</option> : null}
                        {config.solver.backend === "scipy" ? (
                          <option
                            value="block_shift_invert"
                            disabled={config.target.mode !== "nearest_k"}
                          >
                            块位移反演 · 自研有界迭代
                          </option>
                        ) : null}
                        {config.solver.backend === "scipy" &&
                        capabilities.scipy?.methods?.includes(
                          "jacobi_davidson",
                        ) ? (
                          <option
                            value="jacobi_davidson"
                            disabled={config.target.mode !== "nearest_k"}
                          >
                            Jacobi–Davidson · 投影修正方程
                          </option>
                        ) : null}
                        <option value="auto" disabled>
                          auto（当前未验证）
                        </option>
                        {config.solver.backend === "slepc" ? (
                          <>
                            <option value="krylovschur">Krylov Schur</option>
                          </>
                        ) : null}
                      </select>
                    </Field>
                    <Field
                      label="缩放残差阈值"
                      hint="对每个特征对重算 Ax − λx 的相对误差。"
                    >
                      <input
                        type="number"
                        step="any"
                        min="1e-16"
                        max=".01"
                        value={config.validation.residual_scaled_max}
                        onChange={(e) =>
                          modify(
                            "validation",
                            "residual_scaled_max",
                            Number(e.target.value),
                          )
                        }
                      />
                    </Field>
                    <Field label="正交性阈值">
                      <input
                        type="number"
                        step="any"
                        min="1e-16"
                        value={config.validation.orthogonality_max}
                        onChange={(e) =>
                          modify(
                            "validation",
                            "orthogonality_max",
                            Number(e.target.value),
                          )
                        }
                      />
                    </Field>
                    <Field label="子空间上限">
                      <input
                        type="number"
                        min="3"
                        value={config.solver.subspace_max}
                        onChange={(e) =>
                          modify(
                            "solver",
                            "subspace_max",
                            Number(e.target.value),
                          )
                        }
                      />
                    </Field>
                    <Field label="最大外层迭代">
                      <input
                        type="number"
                        min="1"
                        value={config.solver.max_outer_iterations}
                        onChange={(e) =>
                          modify(
                            "solver",
                            "max_outer_iterations",
                            Number(e.target.value),
                          )
                        }
                      />
                    </Field>
                  </div>
                  <label className="checkbox">
                    <input
                      type="checkbox"
                      checked={config.validation.completeness_required}
                      onChange={(e) =>
                        modify(
                          "validation",
                          "completeness_required",
                          e.target.checked,
                        )
                      }
                    />
                    <span>
                      要求完整目标认证
                      <small>
                        即使存在合格候选，目标未认证时仍标记为部分结果。
                      </small>
                    </span>
                  </label>
                  <label className="checkbox">
                    <input
                      type="checkbox"
                      checked={config.solver.adaptive_shift.enabled}
                      onChange={(e) =>
                        modify("solver", "adaptive_shift", {
                          ...config.solver.adaptive_shift,
                          enabled: e.target.checked,
                        })
                      }
                    />
                    <span>
                      启用有界位移恢复
                      <small>
                        根据数值反馈调整工作位移，完整记录尝试过程。
                      </small>
                    </span>
                  </label>
                </Panel>
                <NumericalControls
                  config={config}
                  capabilities={capabilities}
                  onModify={modify}
                  onValidityChange={setNumericalError}
                />
                <Panel title="硬资源预算" eyebrow="03 / RESOURCE BOUNDARIES">
                  <div className="form-grid">
                    <Field label="计算设备" hint="Metal 执行低精度修正方程稀疏乘积；原问题验收由 CPU 双精度完成。">
                      <select value={config.resources.device} onChange={e => {
                        setConfig(previous => withComputeDevice(previous, e.target.value as "cpu" | "gpu"));
                        setPlan(null); setSnapshot(null);
                      }}><option value="cpu">CPU</option><option value="gpu" disabled={!isAvailable(capabilities.gpu)}>Apple Metal GPU · JD 混合精度修正</option></select>
                    </Field>
                    <Field label="MPI 进程数" hint="当前验证的 SLEPc 配置支持 1 或 2 个进程，每进程 1 线程。">
                      <select value={config.resources.mpi_ranks} disabled={config.solver.backend !== "slepc"}
                        onChange={e => modify("resources", "mpi_ranks", Number(e.target.value))}>
                        <option value="1">1</option><option value="2" disabled={!isAvailable(capabilities.mpi)}>2</option>
                      </select>
                    </Field>
                  </div>
                  <div className="form-grid four">
                    <Field label="内存 / GiB">
                      <input
                        type="number"
                        min=".1"
                        max="1024"
                        step=".5"
                        value={config.resources.memory_limit_gib}
                        onChange={(e) =>
                          modify(
                            "resources",
                            "memory_limit_gib",
                            Number(e.target.value),
                          )
                        }
                      />
                    </Field>
                    <Field label="CPU 线程">
                      <input
                        type="number"
                        min="1"
                        max="256"
                        value={config.resources.threads}
                        onChange={(e) =>
                          modify("resources", "threads", Number(e.target.value))
                        }
                      />
                    </Field>
                    <Field label="时间上限 / s">
                      <input
                        type="number"
                        min="1"
                        value={config.resources.walltime_seconds}
                        onChange={(e) =>
                          modify(
                            "resources",
                            "walltime_seconds",
                            Number(e.target.value),
                          )
                        }
                      />
                    </Field>
                    <Field label="磁盘 / GiB">
                      <input
                        type="number"
                        min=".1"
                        step=".5"
                        value={config.resources.disk_limit_gib}
                        onChange={(e) =>
                          modify(
                            "resources",
                            "disk_limit_gib",
                            Number(e.target.value),
                          )
                        }
                      />
                    </Field>
                  </div>
                  <p className="subtle-copy">
                    CPU 本地计算 · 单进程任务。MPI / GPU
                    仅在能力探测及对应后端验证通过后启用。
                  </p>
                </Panel>
              </div>
              <div className="config-aside">
                <Panel
                  title="执行计划"
                  eyebrow="PREFLIGHT CHECK"
                  action={<ShieldCheck size={19} />}
                >
                  {plan ? (
                    <>
                      <div className="plan-status">
                        <CheckCheck size={23} />
                        <div>
                          <b>计划已生成</b>
                          <small>配置已冻结，提交前将重新检查</small>
                        </div>
                      </div>
                      <dl className="details-list">
                        <dt>实际后端</dt>
                        <dd>
                          {plan.config?.solver?.backend ||
                            config.solver.backend}
                        </dd>
                        <dt>求解算法</dt>
                        <dd>
                          {plan.config?.solver?.method || config.solver.method}
                        </dd>
                        <dt>计划版本</dt>
                        <dd className="mono">{short(plan.id)}</dd>
                        <dt>配置哈希</dt>
                        <dd className="hash">{plan.hash || snapshot?.hash}</dd>
                      </dl>
                      {(plan.warnings || []).map((w: any, i: number) => (
                        <Notice key={i} kind="warning">
                          {typeof w === "string" ? w : JSON.stringify(w)}
                        </Notice>
                      ))}
                      <details open>
                        <summary>资源预估与支持边界</summary>
                        <Json value={plan.estimate} />
                      </details>
                      <Button
                        primary
                        busy={busy === "run"}
                        disabled={Boolean(busy)}
                        onClick={() => void execute("run", submit)}
                      >
                        <Play size={15} />
                        确认计划并运行
                      </Button>
                    </>
                  ) : (
                    <>
                      <div className="plan-illustration">
                        <SlidersHorizontal size={36} strokeWidth={1} />
                      </div>
                      <h3>先规划，再计算</h3>
                      <p className="panel-copy">
                        检查输入、目标、真实后端与资源预算。生成计划后，完整配置将保存为不可变快照。
                      </p>
                      {!configValid ? (
                        <Notice kind="warning">
                          请选择输入，并检查目标数量、区间和资源上限。
                        </Notice>
                      ) : null}
                      <Button
                        primary
                        disabled={!configValid || Boolean(busy)}
                        busy={busy === "plan"}
                        onClick={() => void execute("plan", createPlan)}
                      >
                        <ShieldCheck size={15} />
                        生成预检计划
                      </Button>
                    </>
                  )}
                </Panel>
                <details className="panel config-json">
                  <summary>
                    <Code2 size={16} />
                    完整配置 / JSON
                  </summary>
                  <Json value={config} />
                  <Button
                    onClick={() => {
                      const blob = new Blob([JSON.stringify(config, null, 2)], {
                        type: "application/json",
                      });
                      const url = URL.createObjectURL(blob);
                      const a = document.createElement("a");
                      a.href = url;
                      a.download = "config.json";
                      a.click();
                      URL.revokeObjectURL(url);
                    }}
                  >
                    <Download size={14} />
                    导出配置
                  </Button>
                </details>
                {snapshot?.configuration_diff ? (
                  <Panel
                    title="已采用建议的配置差异"
                    eyebrow="SERVER VALIDATED CHANGES"
                  >
                    <ConfigurationDiff
                      changes={snapshot.configuration_diff}
                      empty="建议未改变配置字段；新版本仍保留原目标和资源约束。"
                    />
                  </Panel>
                ) : null}
                {plan?.strategy || plan?.estimate ? (
                  <details className="panel">
                    <summary>冻结执行链与内存分项</summary>
                    <p className="subtle-copy">
                      执行链列出主算法与允许的完整回退配置。检查点只用于新任务热启动，不代表精确续算。
                    </p>
                    <Json
                      value={{
                        strategy: plan.strategy,
                        estimate: plan.estimate,
                      }}
                    />
                  </details>
                ) : null}
                <div className="aside-note">
                  <BookOpen size={17} />
                  <p>
                    原始矩阵始终保留。所有候选均回到原问题验证，不以变换后的残差替代验收。
                  </p>
                </div>
              </div>
            </div>
          ) : null}

          {page === "jobs" ? (
            <>
              <Panel
                title="计算任务"
                eyebrow="JOB QUEUE"
                action={
                  <div className="queue-actions">
                    <span className="count-label">
                      {summary?.active_jobs ?? "—"} ACTIVE /{" "}
                      {summary?.jobs ?? "—"} TOTAL
                    </span>
                    <select
                      aria-label="筛选任务状态"
                      value={statusFilter}
                      onChange={(e) => setStatusFilter(e.target.value)}
                    >
                      <option value="ALL">全部状态</option>
                      <option value="QUEUED">排队中</option>
                      <option value="RUNNING">运行中</option>
                      <option value="VERIFYING">验证中</option>
                      <option value="CANCELLING">取消中</option>
                      <option value="SUCCEEDED">验证通过</option>
                      <option value="PARTIAL">部分结果</option>
                      <option value="FAILED">失败</option>
                      <option value="CANCELLED">已取消</option>
                      <option value="TIMED_OUT">已超时</option>
                    </select>
                  </div>
                }
              >
                {scopedJobs.length ? (
                  <JobTable
                    jobs={scopedJobs}
                    matrices={matrices}
                    onOpen={openJob}
                    selected={jobId}
                  />
                ) : (
                  <Empty
                    title={
                      statusFilter === "ALL"
                        ? "尚未提交任务"
                        : "当前状态下暂无任务"
                    }
                    description={
                      statusFilter === "ALL"
                        ? "选择矩阵、设定目标并生成预检计划后，即可启动独立计算进程。"
                        : "请切换任务状态筛选，或等待任务阶段更新。"
                    }
                    action={
                      <Button onClick={() => go("configure")}>
                        <Plus size={15} />
                        配置新任务
                      </Button>
                    }
                  />
                )}
                {jobPagination}
              </Panel>
              {jobId ? (
                <>
                  <div className="section-title">
                    <div>
                      <span className="eyebrow">LIVE COMPUTE DETAIL</span>
                      <h2>
                        运行详情 <span className="mono">{short(jobId)}</span>
                      </h2>
                    </div>
                    <div className="heading-actions">
                      {selectedJob ? (
                        <Badge status={selectedJob.status} />
                      ) : null}
                      {selectedJob && activeStatuses.has(selectedJob.status) ? (
                        <Button
                          busy={busy === "cancel"}
                          disabled={Boolean(busy)}
                          onClick={() =>
                            void execute("cancel", async () => {
                              const job = await post(`/jobs/${jobId}/cancel`);
                              detail.updateSelectedJob(job);
                              success("已发送取消请求，等待工作进程停止");
                            })
                          }
                        >
                          <Square size={13} />
                          取消任务
                        </Button>
                      ) : (
                        <Button
                          busy={busy === "restart"}
                          disabled={Boolean(busy)}
                          onClick={() =>
                            void execute("restart", async () => {
                              const job = await post(`/jobs/${jobId}/restart`);
                              setSelectedJob(job);
                              jobList.refresh();
                              setJobId(job.id);
                              success("已创建关联的新任务，原运行记录保留");
                            })
                          }
                        >
                          <RefreshCw size={14} />
                          重试为新任务
                        </Button>
                      )}
                      <Button onClick={() => go("results")}>
                        <Layers3 size={15} />
                        查看结果
                      </Button>
                    </div>
                  </div>
                  {selectedJob ? (
                    <>
                      <JobLifecycleControls
                        job={selectedJob}
                        canRun={
                          ["analyst", "admin"].includes(
                            projectRole(
                              projects.find(
                                (project) =>
                                  project.id === selectedJob.project_id,
                              ),
                              identity,
                            ) || "",
                          ) &&
                          !projects.find(
                            (project) => project.id === selectedJob.project_id,
                          )?.archived
                        }
                        canManageRetention={
                          projectRole(
                            projects.find(
                              (project) =>
                                project.id === selectedJob.project_id,
                            ),
                            identity,
                          ) === "admin"
                        }
                        onUpdated={detail.updateSelectedJob}
                        onRestarted={(job, select) => {
                          jobList.refresh();
                          if (select) {
                            setSelectedJob(job);
                            setJobId(job.id);
                          }
                        }}
                        notify={(text, error = false) =>
                          setToast({ message: text, error })
                        }
                      />
                      <div className="metrics-row">
                        <Metric
                          label="当前阶段"
                          value={
                            <span className="stage-text">
                              {selectedJob.stage || "等待调度"}
                            </span>
                          }
                          detail={
                            activeStatuses.has(selectedJob.status)
                              ? "阶段来自服务端实时事件"
                              : "任务已进入终态"
                          }
                        />
                        <Metric
                          label="实际计算耗时"
                          value={number(
                            metricValue(selectedJob, "walltime_seconds") ??
                              metricValue(selectedJob, "elapsed_seconds"),
                          )}
                          unit="s"
                          detail="后端测量值"
                        />
                        <Metric
                          label="峰值内存"
                          value={number(
                            metricValue(selectedJob, "peak_rss_tree_bytes") !==
                              undefined
                              ? metricValue(
                                  selectedJob,
                                  "peak_rss_tree_bytes",
                                ) / 1048576
                              : metricValue(selectedJob, "peak_rss_mib"),
                          )}
                          unit="MiB"
                          detail="进程监测值"
                        />
                        <Metric
                          label="合格候选"
                          value={number(result?.n_accepted)}
                          unit={
                            selectedJob.config?.target?.k
                              ? `/ ${selectedJob.config.target.k}`
                              : ""
                          }
                          detail="独立验证合格数量"
                        />
                      </div>
                      <details className="panel">
                        <summary>真实迭代、准备与事件开销</summary>
                        <p className="subtle-copy">
                          未提供的计数显示“—”。ARPACK
                          不公开真实外层迭代数；块算法计数来自实际执行。
                        </p>
                        <dl className="details-list">
                          {[
                            ["准备耗时 / s", "prepare_seconds"],
                            ["外层迭代", "outer_iterations"],
                            ["残差检查", "residual_checks"],
                            ["残差检查耗时 / s", "residual_check_seconds"],
                            ["已软锁定模式", "soft_locked_modes"],
                            ["重启次数", "restarts"],
                            ["已执行回退", "fallbacks_executed"],
                            ["已发出事件", "events_emitted"],
                            ["保留事件", "events_retained"],
                            ["丢弃事件", "events_dropped"],
                            ["事件发出耗时 / s", "event_emission_seconds"],
                          ].map(([label, key]) => (
                            <div className="metric-detail-row" key={key}>
                              <dt>{label}</dt>
                              <dd>{number(metricValue(selectedJob, key))}</dd>
                            </div>
                          ))}
                        </dl>
                      </details>
                      {selectedJob.error ? (
                        <Notice kind="error">
                          <b>{selectedJob.error_code || "任务异常"}</b>
                          <p>
                            {typeof selectedJob.error === "string"
                              ? selectedJob.error
                              : JSON.stringify(selectedJob.error)}
                          </p>
                        </Notice>
                      ) : null}
                      <Panel
                        title="收敛与工作位移"
                        eyebrow="MEASURED CONVERGENCE HISTORY"
                      >
                        <ConvergenceHistory
                          events={events}
                          method={selectedJob.config?.solver?.method}
                          threshold={
                            selectedJob.config?.validation?.residual_scaled_max
                          }
                          truncated={detail.truncated}
                        />
                      </Panel>
                      <div className="two-col">
                        <Panel
                          title="执行事件"
                          eyebrow="STRUCTURED EVENT STREAM"
                          action={
                            <Button
                              disabled={Boolean(busy)}
                              onClick={() =>
                                void execute("events", () =>
                                  download(
                                    `/jobs/${jobId}/download?file=events.jsonl`,
                                    `events-${short(jobId)}.jsonl`,
                                  ),
                                )
                              }
                            >
                              <Download size={14} />
                              下载完整事件
                            </Button>
                          }
                        >
                          <p className="subtle-copy" role="status">
                            {detail.truncated
                              ? `仅显示最近 ${EVENT_WINDOW_LIMIT} 条事件；较早记录请下载完整事件。`
                              : `已显示 ${events.length} 条事件，最多保留最近 ${EVENT_WINDOW_LIMIT} 条。`}
                          </p>
                          {events.length ? (
                            <div className="event-log">
                              {events.map((e, i) => (
                                <div className="event-row" key={e.seq ?? i}>
                                  <span className="event-seq">
                                    {String(e.seq ?? i + 1).padStart(3, "0")}
                                  </span>
                                  <time>
                                    {e.elapsed_seconds !== undefined
                                      ? `${number(e.elapsed_seconds, 2)} s`
                                      : fmtDate(e.time)}
                                  </time>
                                  <span className="event-stage">
                                    {e.stage || "SYSTEM"}
                                  </span>
                                  <div>
                                    <b>{e.event || e.message}</b>
                                    <small>
                                      {Object.entries(e)
                                        .filter(
                                          ([k]) =>
                                            ![
                                              "seq",
                                              "time",
                                              "stage",
                                              "event",
                                              "elapsed_seconds",
                                              "job_id",
                                            ].includes(k),
                                        )
                                        .map(
                                          ([k, v]) =>
                                            `${k}=${typeof v === "object" ? JSON.stringify(v) : v}`,
                                        )
                                        .join(" · ")}
                                    </small>
                                  </div>
                                </div>
                              ))}
                            </div>
                          ) : (
                            <Empty
                              title="等待运行事件"
                              description="任务开始后会显示真实阶段与数值反馈；未提供迭代遥测时不显示虚构进度。"
                            />
                          )}
                        </Panel>
                        <Panel
                          title="冻结的运行上下文"
                          eyebrow="IMMUTABLE SNAPSHOT"
                        >
                          <dl className="details-list">
                            <dt>矩阵</dt>
                            <dd>
                              {matrices.find(
                                (m) => m.id === selectedJob.matrix_id,
                              )?.name || short(selectedJob.matrix_id)}
                            </dd>
                            <dt>后端 / 算法</dt>
                            <dd>
                              {selectedJob.config?.solver?.backend} /{" "}
                              {selectedJob.config?.solver?.method}
                            </dd>
                            <dt>原始目标 σ</dt>
                            <dd>{number(selectedJob.config?.target?.sigma)}</dd>
                            <dt>任务创建</dt>
                            <dd>{fmtDate(selectedJob.created_at)}</dd>
                            <dt>父任务</dt>
                            <dd>{short(selectedJob.parent_job_id)}</dd>
                          </dl>
                          <details>
                            <summary>位移尝试与恢复记录</summary>
                            <Json
                              value={
                                result?.attempts ||
                                events.filter(
                                  (e) =>
                                    String(e.event).includes("shift") ||
                                    String(e.event).includes("attempt"),
                                )
                              }
                            />
                          </details>
                          <details>
                            <summary>完整配置</summary>
                            <Json value={selectedJob.config} />
                          </details>
                          <Button
                            onClick={() => {
                              setConfig(structuredClone(selectedJob.config));
                              setMatrixId(selectedJob.matrix_id);
                              setPlan(null);
                              setSnapshot(null);
                              go("configure");
                              success("已复制配置，可修改后创建新版本");
                            }}
                          >
                            <Plus size={15} />
                            复制为新实验
                          </Button>
                        </Panel>
                      </div>
                    </>
                  ) : (
                    <div className="loading-strip">
                      <span className="skeleton" />
                      正在读取运行详情…
                    </div>
                  )}
                </>
              ) : null}
            </>
          ) : null}

          {page === "results" ? (
            <>
              <div className="selection-toolbar">
                <span>
                  <Layers3 size={17} />
                  分析运行
                </span>
                {jobPicker}
                {selectedJob ? <Badge status={selectedJob.status} /> : null}
              </div>
              {jobId && result ? (
                <>
                  <div className="result-banner">
                    <div
                      className={`result-symbol ${selectedJob?.status === "SUCCEEDED" ? "passed" : "partial"}`}
                    >
                      <ShieldCheck size={25} />
                    </div>
                    <div>
                      <h2>
                        {selectedJob?.status === "SUCCEEDED"
                          ? "数值验证与目标认证通过"
                          : selectedJob?.status === "PARTIAL"
                            ? "已获得候选，目标完整性尚未认证"
                            : "运行结果与验证证据"}
                      </h2>
                      <p>
                        {selectedJob?.status === "PARTIAL"
                          ? "部分结果可以用于探索；不能据此声明已找到全部目标或最近的 k 个模式。"
                          : "以独立重算的原问题残差和目标覆盖判定为准。"}
                      </p>
                    </div>
                    <div className="validation-status">
                      <span>
                        数值验证 <Badge status={validation.numerical_status} />
                      </span>
                      <span>
                        目标认证{" "}
                        <Badge status={validation.target_certification} />
                      </span>
                    </div>
                  </div>
                  <div className="metrics-row">
                    <Metric
                      label="返回特征值"
                      value={number(eigenvalues.length)}
                      unit="modes"
                    />
                    <Metric
                      label="合格特征对"
                      value={number(result.n_accepted)}
                      unit="pairs"
                    />
                    <Metric
                      label="最大缩放残差"
                      value={
                        residuals.length ? number(Math.max(...residuals)) : "—"
                      }
                    />
                    <Metric
                      label="正交性误差"
                      value={number(
                        validation.orthogonality_error ??
                          validation.orthogonality,
                      )}
                    />
                  </div>
                  <div className="two-col result-charts">
                    <Panel title="原问题残差" eyebrow="INDEPENDENT VALIDATION">
                      <ResidualChart
                        residuals={residuals}
                        threshold={
                          selectedJob?.config?.validation?.residual_scaled_max
                        }
                      />
                    </Panel>
                    <Panel
                      title="特征值谱分布"
                      eyebrow="EIGENVALUE DISTRIBUTION"
                    >
                      <Spectrum
                        values={eigenvalues}
                        sigma={selectedJob?.config?.target?.sigma ?? 0}
                      />
                      <div className="spectrum-copy">
                        <span className="green-dot" />
                        实际返回模式 <span className="threshold-line" />
                        原始目标位移
                      </div>
                      <p className="panel-copy">
                        相近模式的位置可能重叠。模式间距可用于识别聚集谱；完整模式簇判定以验证记录为准。
                      </p>
                    </Panel>
                  </div>
                  <Panel
                    title="特征对与验证残差"
                    eyebrow="MODE TABLE"
                    action={
                      <Button
                        onClick={() =>
                          void execute("eigenvalues", () =>
                            download(
                              `/jobs/${jobId}/download?file=eigenvalues.npy`,
                              "eigenvalues.npy",
                            ),
                          )
                        }
                      >
                        <Download size={14} />
                        下载特征值
                      </Button>
                    }
                  >
                    <div className="table-wrap">
                      <table>
                        <caption className="paper-table-caption">特征对与独立验证残差</caption>
                        <thead>
                          <tr>
                            <th>
                              <input
                                type="checkbox"
                                aria-label="选择全部模式"
                                checked={
                                  eigenvalues.length > 0 &&
                                  modeIndices.length === eigenvalues.length
                                }
                                onChange={(e) =>
                                  setModeIndices(
                                    e.target.checked
                                      ? eigenvalues.map((_, i) => i)
                                      : [],
                                  )
                                }
                              />
                            </th>
                            <th>模式</th>
                            <th>特征值 λ</th>
                            {Array.isArray(result.frequencies_hz) ? (
                              <th>授权频率 / Hz</th>
                            ) : null}
                            <th>与 σ 的距离</th>
                            <th>独立缩放残差</th>
                            <th>数值判定</th>
                            <th>模式关系</th>
                          </tr>
                        </thead>
                        <tbody>
                          {eigenvalues.map((v, i) => (
                            <tr key={i}>
                              <td>
                                <input
                                  type="checkbox"
                                  aria-label={`选择模式 ${i + 1}`}
                                  checked={modeIndices.includes(i)}
                                  onChange={() =>
                                    setModeIndices((ids) =>
                                      ids.includes(i)
                                        ? ids.filter((id) => id !== i)
                                        : [...ids, i],
                                    )
                                  }
                                />
                              </td>
                              <td className="mono">
                                {String(i + 1).padStart(3, "0")}
                              </td>
                              <td className="mono">{number(v, 10)}</td>
                              {Array.isArray(result.frequencies_hz) ? (
                                <td
                                  className="mono"
                                  title={
                                    result.frequencies_hz[i] === null
                                      ? "负特征值或物理单位未确认，不提供Hz数值"
                                      : "依据本任务冻结的工程频率映射与单位换算"
                                  }
                                >
                                  {number(result.frequencies_hz[i], 6)}
                                </td>
                              ) : null}
                              <td className="mono">
                                {number(
                                  Math.abs(
                                    v -
                                      (selectedJob?.config?.target?.sigma ?? 0),
                                  ),
                                  8,
                                )}
                              </td>
                              <td className="mono">{number(residuals[i])}</td>
                              <td>
                                <Badge
                                  status={
                                    typeof residuals[i] === "number" &&
                                    residuals[i] <=
                                      (selectedJob?.config?.validation
                                        ?.residual_scaled_max ?? 1e-8)
                                      ? "PASSED"
                                      : "UNVERIFIED"
                                  }
                                />
                              </td>
                              <td className="muted">
                                {eigenvalues.some(
                                  (other, j) =>
                                    i !== j &&
                                    Math.abs(v - other) <=
                                      1e-8 * Math.max(1, Math.abs(v)),
                                )
                                  ? "邻近 / 重根候选"
                                  : "独立位置"}{" "}
                                <button
                                  className="link-button"
                                  onClick={() =>
                                    void execute("vector", async () => {
                                      setVectorPreview(
                                        await api(
                                          `/jobs/${jobId}/vectors/${i}`,
                                        ),
                                      );
                                    })
                                  }
                                >
                                  查看向量
                                </button>
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                    <div className="table-footer">
                      模式关系采用相对间距 1e−8
                      的展示阈值，不替代子空间与完整性认证。
                    </div>
                  </Panel>
                  {vectorPreview ? (
                    <Panel
                      title={`模式 ${vectorPreview.mode + 1} · 代数向量分量`}
                      eyebrow="ALGEBRAIC MODE PREVIEW"
                      action={
                        <button
                          className="icon-button"
                          aria-label="关闭向量预览"
                          onClick={() => setVectorPreview(null)}
                        >
                          关闭预览
                        </button>
                      }
                    >
                      <VectorChart samples={vectorPreview.samples} />
                      <p className="panel-copy">
                        从 {vectorPreview.total_components} 个代数分量中采样{" "}
                        {vectorPreview.samples.length}{" "}
                        个点。没有网格几何信息，不将分量曲线解释为物理场云图。
                      </p>
                    </Panel>
                  ) : null}
                  {selectedJob ? <FieldMapping job={selectedJob}/> : null}
                  <div className="download-strip">
                    <span>
                      <Download size={18} />
                      运行产物
                    </span>
                    {[
                      "eigenvalues.npy",
                      "eigenvectors.npy",
                      "residuals.npy",
                      "validation.json",
                      "metrics.json",
                    ].map((file) => (
                      <Button
                        key={file}
                        disabled={Boolean(busy)}
                        onClick={() =>
                          void execute(file, () =>
                            download(
                              `/jobs/${jobId}/download?file=${file}`,
                              file,
                            ),
                          )
                        }
                      >
                        {file}
                        <ArrowDownToLine size={13} />
                      </Button>
                    ))}
                  </div>
                  <div className="selection-toolbar">
                    <span>已选择 {modeIndices.length} 个模式</span>
                    {["numpy", "scipy"].map((format) => (
                      <Button
                        key={format}
                        disabled={!modeIndices.length || Boolean(busy)}
                        onClick={() =>
                          void execute("selection", async () => {
                            await download(
                              `/jobs/${jobId}/export`,
                              `selected-modes.${format === "numpy" ? "npz" : "zip"}`,
                              {
                                method: "POST",
                                body: JSON.stringify({
                                  indices: modeIndices,
                                  format,
                                }),
                              },
                            );
                            success("所选模式与原始列索引已导出");
                          })
                        }
                      >
                        <Download size={14} />
                        所选模式 / {format}
                      </Button>
                    ))}
                  </div>
                  <details className="panel">
                    <summary>完整验证与指标</summary>
                    <Json value={{ validation, metrics: result.metrics }} />
                  </details>
                </>
              ) : (
                <Empty
                  title={
                    jobId ? "尚无可分析的数值结果" : "选择一个运行查看结果"
                  }
                  description={
                    jobId
                      ? "任务可能仍在运行、已取消或未产出合格候选。请查看任务阶段与错误记录。"
                      : "结果同时展示每个特征对的数值误差与整个目标的认证状态。"
                  }
                  action={
                    <Button onClick={() => go("jobs")}>
                      <Activity size={15} />
                      进入任务中心
                    </Button>
                  }
                />
              )}
            </>
          ) : null}

          {page === "compare" ? (
            <>
              <Panel title="选择两个运行" eyebrow="CONTROLLED COMPARISON">
                <p className="panel-copy">
                  请选择基线与候选。只有输入、目标、误差门槛、硬件环境和预算一致时，才能得出可比的性能结论。
                </p>
                {scopedJobs.length ? (
                  <div className="compare-picks">
                    {scopedJobs.map((j) => (
                      <label
                        key={j.id}
                        className={comparison.includes(j.id) ? "chosen" : ""}
                      >
                        <input
                          type="checkbox"
                          checked={comparison.includes(j.id)}
                          onChange={() => toggleCompare(j.id)}
                        />
                        <div>
                          <b className="mono">{short(j.id)}</b>
                          <small>
                            {matrices.find((m) => m.id === j.matrix_id)?.name ||
                              short(j.matrix_id)}{" "}
                            · {fmtDate(j.created_at)}
                          </small>
                        </div>
                        <Badge status={j.status} />
                      </label>
                    ))}
                  </div>
                ) : (
                  <Empty
                    title="暂无可对比运行"
                    description="至少保留两个运行记录后进行比较。"
                  />
                )}
                {jobPagination}
              </Panel>
              {selectedComparison.length === 2 ? (
                <>
                  <Notice kind={comparable ? "info" : "warning"}>
                    {!compareInfo
                      ? "正在校验输入、精度、预算与环境可比性…"
                      : comparable
                        ? "已核验输入、目标、精度、资源与运行环境一致，且运行通过认证。"
                        : "运行未通过完整认证，或输入、目标、资源、精度与环境存在差异。以下数据仅供并列查看，不能直接比较加速比。"}
                    {comparable && typeof compareInfo?.speedup === "number" ? (
                      <p>
                        <b>
                          基线 / 候选耗时比：{number(compareInfo.speedup, 2)} ×
                        </b>{" "}
                        · 仅采用独立工作进程实测耗时。
                      </p>
                    ) : null}
                  </Notice>
                  <ModeComparison ids={comparison} jobs={selectedComparison} />
                  <Panel title="运行指标对照">
                    <div className="table-wrap">
                      <table>
                        <caption className="paper-table-caption">运行指标与冻结条件对照</caption>
                        <thead>
                          <tr>
                            <th>指标 / 条件</th>
                            {selectedComparison.map((j) => (
                              <th key={j.id} className="mono">
                                {short(j.id)}
                              </th>
                            ))}
                          </tr>
                        </thead>
                        <tbody>
                          {[
                            [
                              "状态",
                              (j: Entity) => <Badge status={j.status} />,
                            ],
                            ["输入矩阵", (j: Entity) => short(j.matrix_id)],
                            [
                              "目标 k / σ",
                              (j: Entity) =>
                                `${j.config?.target?.k ?? "interval"} / ${j.config?.target?.sigma ?? "—"}`,
                            ],
                            [
                              "后端 / 算法",
                              (j: Entity) =>
                                `${j.config?.solver?.backend} / ${j.config?.solver?.method}`,
                            ],
                            [
                              "残差门槛",
                              (j: Entity) =>
                                number(
                                  j.config?.validation?.residual_scaled_max,
                                ),
                            ],
                            [
                              "内存上限 / GiB",
                              (j: Entity) =>
                                number(j.config?.resources?.memory_limit_gib),
                            ],
                            [
                              "CPU 线程",
                              (j: Entity) =>
                                number(j.config?.resources?.threads),
                            ],
                            [
                              "总耗时 / s",
                              (j: Entity) =>
                                number(
                                  metricValue(j, "walltime_seconds") ??
                                    metricValue(j, "elapsed_seconds"),
                                ),
                            ],
                            [
                              "峰值内存 / MiB",
                              (j: Entity) =>
                                number(
                                  metricValue(j, "peak_rss_tree_bytes") !==
                                    undefined
                                    ? metricValue(j, "peak_rss_tree_bytes") /
                                        1048576
                                    : metricValue(j, "peak_rss_mib"),
                                ),
                            ],
                            [
                              "合格数量",
                              (j: Entity) => number(j.result?.n_accepted),
                            ],
                            [
                              "目标认证",
                              (j: Entity) =>
                                j.result?.validation?.target_certification ||
                                "—",
                            ],
                          ].map(([label, fn]) => (
                            <tr key={String(label)}>
                              <td>{label as string}</td>
                              {selectedComparison.map((j) => (
                                <td key={j.id}>
                                  {(fn as (j: Entity) => React.ReactNode)(j)}
                                </td>
                              ))}
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </Panel>
                  <div className="two-col">
                    {selectedComparison.map((j) => (
                      <Panel key={j.id} title={`配置 ${short(j.id)}`}>
                        <Json value={j.config} />
                      </Panel>
                    ))}
                  </div>
                </>
              ) : (
                <div className="selection-hint">
                  已选择 {selectedComparison.length} / 2 个运行
                </div>
              )}
            </>
          ) : null}

          {page === "benchmarks" ? (
            <ExperimentWorkspace
              projectId={projectId}
              matrices={matrices}
              baseConfig={config}
              onOpenJob={openJob}
              onChanged={load}
              notify={(text, error = false) =>
                setToast({ message: text, error })
              }
            />
          ) : null}

          {page === "industrial" ? (
            <>
            <Field label="工业验收项目"><select value={projectId} aria-label="工业验收项目" onChange={event => setProjectId(event.target.value)}><option value="">选择项目</option>{projects.map(project => <option key={project.id} value={project.id}>{project.name}</option>)}</select></Field>
            <IndustrialWorkspace key={projectId} projectId={projectId} jobs={jobs}
              canManage={projectRole(currentProject, identity) === "admin"}
              canCheck={["admin", "analyst"].includes(projectRole(currentProject, identity) || "")} />
            </>
          ) : null}

          {page === "assistant" ? (
            <>
              <AssistantGovernance
                selectedJob={selectedJob}
                config={config}
                jobs={jobs}
                projects={projects}
                identity={identity}
                notify={(text, error = false) =>
                  setToast({ message: text, error })
                }
              />
              <div className="selection-toolbar">
                <span>
                  <Sparkles size={17} />
                  诊断上下文
                </span>
                {jobPicker}
                <Button
                  primary
                  busy={busy === "diagnose"}
                  disabled={
                    Boolean(busy) || (!jobId && !config.problem.matrix_id)
                  }
                  onClick={() => void execute("diagnose", diagnose)}
                >
                  运行规则诊断
                  <ArrowRight size={14} />
                </Button>
              </div>
              <Notice>
                <b>离线规则引擎</b> ·
                当前不依赖语言模型。建议仅修改参数白名单，不读取参考答案、不改变任务目标、不放宽硬资源上限。
              </Notice>
              {diagnosis ? (
                <>
                  <div className="section-title">
                    <div>
                      <span className="eyebrow">
                        EXPLAINABLE RECOMMENDATIONS
                      </span>
                      <h2>诊断建议</h2>
                    </div>
                    <span className="outlined-label">
                      建议来源：{diagnosis.source || "rules"}
                    </span>
                  </div>
                  {diagnosis.suggestions?.length ? (
                    <div className="suggestion-list">
                      {diagnosis.suggestions.map((s: Entity, i: number) => (
                        <Panel
                          title={s.title}
                          eyebrow={`RULE ${String(i + 1).padStart(2, "0")}`}
                          key={i}
                        >
                          <p className="panel-copy">{s.reason}</p>
                          <div className="suggestion-changes">
                            <b>建议的配置变化</b>
                            <Json value={s.changes} />
                          </div>
                          <Button
                            disabled={Boolean(busy)}
                            busy={busy === `adopt${i}`}
                            onClick={() =>
                              void execute(`adopt${i}`, () => adopt(s.changes))
                            }
                          >
                            <Plus size={14} />
                            采用为新配置版本
                            <ArrowRight size={14} />
                          </Button>
                        </Panel>
                      ))}
                    </div>
                  ) : (
                    <Panel>
                      <Empty
                        title="当前没有额外规则建议"
                        description="可查看原始运行错误、资源指标与独立验证结果，继续分析。"
                      />
                    </Panel>
                  )}
                  {diagnosis.knowledge?.length ? (
                    <Panel title="依据与知识出处">
                      <KnowledgeEvidence entries={diagnosis.knowledge} />
                    </Panel>
                  ) : null}
                  <details className="panel">
                    <summary>完整诊断记录</summary>
                    <Json value={diagnosis} />
                  </details>
                </>
              ) : (
                <Panel>
                  <Empty
                    title="从运行证据中定位问题"
                    description="选择一个任务运行诊断，或以当前求解配置作为上下文。规则会解释建议原因，采用后创建新实验配置。"
                    action={
                      <div className="assistant-symbol">
                        <Braces size={25} />
                        <ArrowRight size={20} />
                        <SlidersHorizontal size={25} />
                      </div>
                    }
                  />
                </Panel>
              )}
            </>
          ) : null}

          {page === "reports" ? (
            <>
              <div className="selection-toolbar">
                <span>
                  <FileText size={17} />
                  交付运行
                </span>
                {jobPicker}
                {selectedJob ? <Badge status={selectedJob.status} /> : null}
              </div>
              <div className="two-col">
                <Panel title="单次运行报告" eyebrow="EVIDENCE REPORT">
                  <div className="report-preview">
                    <span className="eyebrow">
                      EMSOLVER / EXPERIMENT REPORT
                    </span>
                    <FileText size={43} strokeWidth={1} />
                    <h3>证据完整，结果可解释</h3>
                    <p>
                      目标与输入指纹 · 实际算法与环境
                      <br />
                      独立验证 · 资源开销 · 异常与限制
                    </p>
                  </div>
                  <Button
                    primary
                    disabled={!jobId || Boolean(busy)}
                    busy={busy === "report"}
                    onClick={() =>
                      void execute("report", async () => {
                        const r = await post("/reports", { job_id: jobId });
                        setReport(r);
                        await download(
                          r.url,
                          `emsolver-report-${short(jobId)}.html`,
                        );
                        success("HTML 运行报告已生成并下载");
                      })
                    }
                  >
                    <FileText size={15} />
                    生成并下载报告
                  </Button>
                  {report ? (
                    <p className="subtle-copy">
                      报告编号 <span className="mono">{short(report.id)}</span>{" "}
                      · 可离线打开
                    </p>
                  ) : null}
                </Panel>
                <Panel title="脱敏复现实验包" eyebrow="REPRODUCIBILITY BUNDLE">
                  <div className="bundle-icon">
                    <Box size={43} strokeWidth={1} />
                  </div>
                  <p className="panel-copy">
                    公开包导出脱敏配置、运行摘要、源码与清单。默认排除私有输入和受限特征值、向量及诊断；只有任务冻结的
                    matrix_metadata.public 为 true 时才包含可公开结果。
                  </p>
                  <ul className="check-list">
                    <li>
                      <ShieldCheck size={16} />
                      明确的导出策略与排除项
                    </li>
                    <li>
                      <ShieldCheck size={16} />
                      脱敏配置与公开环境摘要
                    </li>
                    <li>
                      <ShieldCheck size={16} />
                      经公开声明允许的数值结果
                    </li>
                    <li>
                      <ShieldCheck size={16} />
                      清单与可复现说明
                    </li>
                  </ul>
                  <Button
                    disabled={!jobId || Boolean(busy)}
                    busy={busy === "bundle"}
                    onClick={() =>
                      void execute("bundle", async () => {
                        await download(
                          `/jobs/${jobId}/bundle`,
                          `emsolver-bundle-${short(jobId)}.zip`,
                        );
                        success("脱敏复现包已下载");
                      })
                    }
                  >
                    <Download size={15} />
                    下载复现包
                  </Button>
                  <p className="subtle-copy">
                    不打包
                    .env、凭据或私有输入；受限结果也默认排除。完整复算需合法原始输入。内部完整留存请使用设置页的受保护备份，其包含私有数据。
                  </p>
                </Panel>
              </div>
              <Notice>
                此处导出运行证据，不自动声明参赛材料完整。源码、许可证、公开数据说明与正式性能验收仍需分别核对。
              </Notice>
            </>
          ) : null}

          {page === "settings" ? (
            <>
              <Panel
                title="当前身份与授权模式"
                eyebrow="IDENTITY & PROJECT ROLES"
              >
                <dl className="details-list">
                  <dt>会话身份</dt>
                  <dd className="mono">{identity?.subject || "尚未确认"}</dd>
                  <dt>部署模式</dt>
                  <dd>
                    {identity?.mode === "local"
                      ? "离线本地用户"
                      : identity?.mode === "shared-token"
                        ? "共享令牌"
                        : identity?.mode === "token_users"
                          ? "独立令牌身份"
                          : "尚未确认"}
                  </dd>
                  <dt>工作台管理员</dt>
                  <dd>
                    {identity
                      ? identity.global_admin
                        ? "是"
                        : "否"
                      : "尚未确认"}
                  </dd>
                  <dt>运维管理员</dt>
                  <dd>
                    {identity?.operations_admin
                      ? "是 · 默认无业务读取与下载权"
                      : "否"}
                  </dd>
                </dl>
                <p className="panel-copy">
                  查看者读取历史，分析者导入与运行，项目管理员管理成员与数据公开声明。运维角色只管理脱敏资源与配额；工作台所有者为另行显式授予的业务与完整备份权限。账号由服务端配置。
                </p>
                {identity?.global_admin ? (
                  <details>
                    <summary>服务端已配置的身份编号</summary>
                    <Json value={identity.configured_subjects || []} />
                  </details>
                ) : null}
              </Panel>
              {identity?.operations_admin || identity?.global_admin ? (
                <OperationsCenter
                  key={identity.subject}
                  identity={identity}
                  onSaved={() => void workspace.reload()}
                />
              ) : null}
              <div className="two-col">
                <Panel
                  title="实际运行能力"
                  eyebrow="CAPABILITY PROBE"
                  action={
                    <Button onClick={() => void load()}>
                      <RefreshCw size={14} />
                      重新探测
                    </Button>
                  }
                >
                  {["scipy", "slepc", "mpi", "gpu"].map((key) => (
                    <div className="capability-item" key={key}>
                      <span className="capability-icon">
                        <Cpu size={21} />
                      </span>
                      <div>
                        <h3>
                          {key === "scipy"
                            ? "SciPy / ARPACK"
                            : key.toUpperCase()}
                        </h3>
                        <p>
                          {capabilities[key]?.reason ||
                            capabilities[key]?.limitation ||
                            (isAvailable(capabilities[key])
                              ? "当前环境已验证可用"
                              : "当前构建未启用此扩展路径")}
                        </p>
                        {capabilities[key]?.version ? (
                          <small className="mono">
                            v{capabilities[key].version}
                          </small>
                        ) : null}
                      </div>
                      <span
                        className={`availability ${isAvailable(capabilities[key]) ? "yes" : ""}`}
                      >
                        {isAvailable(capabilities[key]) ? "可用" : "未支持"}
                      </span>
                    </div>
                  ))}
                  <details>
                    <summary>完整能力清单</summary>
                    <Json value={capabilities} />
                  </details>
                </Panel>
                <div>
                  <Panel title="连接与访问" eyebrow="LOCAL SERVICE">
                    <Field
                      label="服务地址"
                      hint="开发时由 Vite 转发到 127.0.0.1:8000；发布版本同源访问。"
                    >
                      <input readOnly value="/api/v1" className="mono" />
                    </Field>
                    <Field
                      label="可选访问令牌"
                      hint="令牌由服务端解析为会话身份，并按项目成员角色授权；只保存在当前浏览器会话。"
                    >
                      <input
                        type="password"
                        autoComplete="off"
                        value={auth}
                        onChange={(e) => setAuth(e.target.value)}
                        placeholder="仅在服务启用 EMSOLVER_API_TOKEN 时填写"
                      />
                    </Field>
                    <Button
                      onClick={() => {
                        setToken(auth.trim());
                        // A new identity starts with empty in-memory project and result caches.
                        window.location.reload();
                      }}
                    >
                      保存并重新连接
                    </Button>
                    <details>
                      <summary>服务健康状态</summary>
                      <Json value={health} />
                    </details>
                  </Panel>
                  <Panel title="本地数据备份" eyebrow="DATA OPERATIONS">
                    <p className="panel-copy">
                      将应用数据备份为 ZIP
                      文件。备份可能含输入和输出，请在可信设备中妥善保管。
                    </p>
                    <Button
                      disabled={Boolean(busy) || !identity?.global_admin}
                      busy={busy === "backup"}
                      onClick={() =>
                        void execute("backup", async () => {
                          const response = await fetch("/api/v1/backup", {
                            method: "POST",
                            headers: getToken()
                              ? { Authorization: `Bearer ${getToken()}` }
                              : {},
                          });
                          if (!response.ok)
                            throw new Error(`备份失败：${response.status}`);
                          const url = URL.createObjectURL(
                            await response.blob(),
                          );
                          const a = document.createElement("a");
                          a.href = url;
                          a.download = "emsolver-backup.zip";
                          a.click();
                          setTimeout(() => URL.revokeObjectURL(url), 1000);
                          success("本地备份已下载");
                        })
                      }
                    >
                      <Download size={15} />
                      创建并下载备份
                    </Button>
                    <Button
                      disabled={Boolean(busy) || !identity?.global_admin}
                      busy={busy === "restore"}
                      onClick={() => restoreRef.current?.click()}
                    >
                      <Upload size={15} />
                      校验备份并恢复到新实例
                    </Button>
                    {restoreInfo ? (
                      <Notice>
                        已恢复为独立实例{" "}
                        <b className="mono">{restoreInfo.instance_id}</b>
                        。当前实例未切换；请通过运维入口启动新实例。
                      </Notice>
                    ) : null}
                    <p className="subtle-copy">
                      恢复操作会校验 ZIP
                      的哈希和引用关系，创建独立新实例，不覆盖当前实验数据。
                    </p>
                  </Panel>
                  <Panel title="存储维护" eyebrow="RETENTION POLICY">
                    <p className="panel-copy">
                      预览超过 24
                      小时且未被实验引用的临时文件。已登记的输入和保留任务始终受保护。
                    </p>
                    <Button
                      disabled={Boolean(busy) || !identity?.global_admin}
                      onClick={() =>
                        void execute("cleanup", async () => {
                          setCleanupPreview(
                            await post("/cleanup", { dry_run: true }),
                          );
                          success("已完成只读清理预览，未删除任何文件");
                        })
                      }
                    >
                      <Search size={15} />
                      预览可清理文件
                    </Button>
                    {cleanupPreview ? (
                      <>
                        <Json value={cleanupPreview} />
                        {cleanupPreview.dry_run &&
                        cleanupItemCount(cleanupPreview) > 0 ? (
                          <Button
                            disabled={Boolean(busy) || !identity?.global_admin}
                            busy={busy === "cleanup-execute"}
                            onClick={() =>
                              void execute("cleanup-execute", async () => {
                                const outcome = await post("/cleanup", {
                                  dry_run: false,
                                });
                                setCleanupPreview(outcome);
                                success(
                                  `已回收 ${outcome.removed?.length ?? 0} 个临时项，${outcome.retention?.expired?.length ?? 0} 个到期任务结果`,
                                );
                              })
                            }
                          >
                            执行无引用临时文件回收
                          </Button>
                        ) : null}
                      </>
                    ) : null}
                  </Panel>
                </div>
              </div>
              <Notice>
                本工作台的能力标识来自服务端实际探测。未安装的后端、GPU 与 MPI
                不会作为可运行选项提供。
              </Notice>
            </>
          ) : null}

          {page === "audit" ? (
            <Panel
              title="操作轨迹"
              eyebrow="APPEND-ONLY AUDIT"
              action={
                <Button
                  disabled={Boolean(busy)}
                  onClick={() =>
                    void execute("audit", async () => {
                      setAudit(await api<Entity[]>("/audit"));
                      success("审计记录已刷新");
                    })
                  }
                >
                  <RefreshCw size={14} />
                  刷新
                </Button>
              }
            >
              {audit.length ? (
                <div className="table-wrap">
                  <table>
                        <caption className="paper-table-caption">项目与任务操作审计</caption>
                    <thead>
                      <tr>
                        <th>时间</th>
                        <th>操作</th>
                        <th>对象</th>
                        <th>详情</th>
                      </tr>
                    </thead>
                    <tbody>
                      {audit.map((row, i) => (
                        <tr key={row.id || i}>
                          <td>{fmtDate(row.created_at || row.time)}</td>
                          <td className="mono">{row.action || row.event}</td>
                          <td className="mono">
                            {short(
                              row.entity_id || row.object_id || row.resource_id,
                            )}
                          </td>
                          <td>
                            <details>
                              <summary>查看记录</summary>
                              <Json value={row} />
                            </details>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <Empty
                  title="暂无审计记录"
                  description="创建项目、导入数据与提交运行后，服务端记录会显示在此。"
                />
              )}
            </Panel>
          ) : null}

          {page === "coverage" ? (
            <>
              <div className="help-intro">
                <div>
                  <span className="eyebrow">
                    A TRUSTWORTHY SCIENTIFIC WORKBENCH
                  </span>
                  <h2>明确能力边界，让结论经得起复核。</h2>
                  <p>
                    EMSolver
                    面向稀疏厄密特征值问题。界面统一显示真实数据、独立验证和可追溯的实验记录。
                  </p>
                </div>
                <ShieldCheck size={70} strokeWidth={0.8} />
              </div>
              <div className="two-col">
                <Panel title="工作台使用路径">
                  <div className="protocol-list">
                    {[
                      [
                        "创建项目与输入",
                        "导入矩阵或生成合成样例，确认输入诊断。",
                      ],
                      [
                        "目标与资源预检",
                        "选择最近 k 个或区间目标，冻结配置并检查计划。",
                      ],
                      [
                        "运行与独立验证",
                        "跟踪真实阶段，分别检查数值误差和目标认证。",
                      ],
                      [
                        "比较与复现交付",
                        "条件一致时比较指标，下载报告与脱敏复现包。",
                      ],
                    ].map(([t, d], i) => (
                      <div key={t}>
                        <span>0{i + 1}</span>
                        <p>
                          <b>{t}</b>
                          {d}
                        </p>
                      </div>
                    ))}
                  </div>
                </Panel>
                <Panel title="理解结果状态">
                  <div className="status-explanation">
                    <Badge status="SUCCEEDED" />
                    <p>数值与请求目标均认证通过；可按报告范围使用。</p>
                    <Badge status="PARTIAL" />
                    <p>存在候选，但目标完整性或最近性尚未认证；适合探索。</p>
                    <Badge status="FAILED" />
                    <p>
                      未满足验收或发生计算故障；查看错误与事件，不将其计作通过。
                    </p>
                  </div>
                </Panel>
              </div>
              <Notice>
                已实现{" "}
                {
                  coverage.filter((item) => item.status === "implemented")
                    .length
                }{" "}
                项所列范围，部分实现{" "}
                {coverage.filter((item) => item.status === "partial").length}{" "}
                项，有条件或尚未实现{" "}
                {
                  coverage.filter((item) => item.status === "conditional")
                    .length
                }{" "}
                项。登记不是完整 PRD 或官方验收通过证明；请逐项查看范围与限制。
              </Notice>
              <Panel
                title="功能覆盖登记"
                eyebrow="IMPLEMENTATION & LIMITATIONS"
                action={
                  <span className="count-label">
                    {coverage.length} REQUIREMENTS
                  </span>
                }
              >
                {coverage.length ? (
                  <div className="table-wrap">
                    <table>
                        <caption className="paper-table-caption">功能范围、边界与验证证据</caption>
                      <thead>
                        <tr>
                          <th>编号</th>
                          <th>功能</th>
                          <th>实现状态</th>
                          <th>边界 / 限制</th>
                          <th>证据</th>
                        </tr>
                      </thead>
                      <tbody>
                        {coverage.map((item, i) => (
                          <tr key={item.id || i}>
                            <td className="mono">{item.id}</td>
                            <td className="coverage-name">
                              <b>{item.title}</b>
                              <small>{item.implemented_scope}</small>
                            </td>
                            <td>
                              <span className="outlined-label">
                                {{
                                  implemented: "已实现（所列范围）",
                                  partial: "部分实现",
                                  conditional: "有条件 / 未实现",
                                }[item.status as string] || item.status}
                              </span>
                            </td>
                            <td className="coverage-limitation">
                              {item.limitation || "—"}
                            </td>
                            <td className="mono">
                              {Array.isArray(item.evidence)
                                ? item.evidence.join(", ")
                                : typeof item.evidence === "object"
                                  ? JSON.stringify(item.evidence)
                                  : item.evidence || "—"}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                ) : (
                  <Empty
                    title="覆盖登记尚未加载"
                    description="请检查服务连接，功能声明以服务端登记为准。"
                  />
                )}
              </Panel>
            </>
          ) : null}

          <footer className="workspace-footer">
            <span>
              EMSolver <b>科学计算工作台</b>
            </span>
            <span>
              可追溯输入 <i /> 可复核结果 <i /> 可复现实验
            </span>
            <span className="mono">
              LOCAL-FIRST / v{health?.version || "—"}
            </span>
          </footer>
        </main>
      </div>
      <input
        ref={uploadRef}
        hidden
        type="file"
        accept=".npz,.npy"
        onChange={(e) => {
          const f = e.target.files?.[0];
          if (f) {
            setImportFile(f);
            go("matrices");
          }
          e.target.value = "";
        }}
      />
      <input
        ref={restoreRef}
        hidden
        type="file"
        accept=".zip"
        onChange={(e) => {
          const file = e.target.files?.[0];
          if (file)
            void execute("restore", async () => {
              const form = new FormData();
              form.append("file", file);
              const outcome = await api("/backup/restore", {
                method: "POST",
                body: form,
              });
              setRestoreInfo(outcome);
              success("备份校验通过，已恢复到独立新实例");
            });
          e.target.value = "";
        }}
      />
      {projectDialog ? (
        <div className="modal-backdrop" onClick={() => setProjectDialog(false)}>
          <section
            className="modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="project-title"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="modal-heading">
              <div>
                <span className="eyebrow">NEW EXPERIMENT SPACE</span>
                <h2 id="project-title">创建实验项目</h2>
              </div>
              <button
                className="icon-button"
                aria-label="关闭新建项目"
                onClick={() => setProjectDialog(false)}
              >
                关闭
              </button>
            </div>
            <form
              onSubmit={(e: FormEvent) => {
                e.preventDefault();
                void execute("project", async () => {
                  const p = await post("/projects", {
                    name: projectName.trim(),
                    description: projectDescription,
                  });
                  setProjects((v) => [p, ...v]);
                  setProjectId(p.id);
                  setProjectDialog(false);
                  setProjectName("");
                  setProjectDescription("");
                  success("实验项目已创建");
                });
              }}
            >
              <Field label="项目名称">
                <input
                  autoFocus
                  required
                  maxLength={120}
                  value={projectName}
                  onChange={(e) => setProjectName(e.target.value)}
                  placeholder="例如：腔体谐振模式探索"
                />
              </Field>
              <Field label="实验说明（可选）">
                <textarea
                  rows={3}
                  maxLength={2000}
                  value={projectDescription}
                  onChange={(e) => setProjectDescription(e.target.value)}
                  placeholder="记录计算目标、数据来源与实验背景…"
                />
              </Field>
              <div className="modal-actions">
                <Button type="button" onClick={() => setProjectDialog(false)}>
                  取消
                </Button>
                <Button
                  type="submit"
                  primary
                  disabled={!projectName.trim()}
                  busy={busy === "project"}
                >
                  <Plus size={15} />
                  创建项目
                </Button>
              </div>
            </form>
          </section>
        </div>
      ) : null}
      {toast ? (
        <Toast
          message={toast.message}
          error={toast.error}
          onClose={() => setToast(null)}
        />
      ) : null}
    </div>
  );
}

function JobTable({
  jobs,
  matrices,
  onOpen,
  selected,
}: {
  jobs: Entity[];
  matrices: Entity[];
  onOpen: (j: Entity, d?: string) => void;
  selected?: string;
}) {
  const matrixById = useMemo(() => indexMatricesById(matrices), [matrices]);
  return (
    <div className="table-wrap">
      <table className="jobs-table">
                        <caption className="paper-table-caption">已登记计算任务</caption>
        <thead>
          <tr>
            <th>实验 / 输入矩阵</th>
            <th>求解目标</th>
            <th>状态</th>
            <th>实际后端</th>
            <th>创建时间</th>
            <th aria-label="操作" />
          </tr>
        </thead>
        <tbody>
          {jobs.map((job) => (
            <tr className={selected === job.id ? "selected" : ""} key={job.id}>
              <td>
                <button className="table-job-link" onClick={() => onOpen(job)}>
                  <span className="job-icon">
                    <Activity size={17} />
                  </span>
                  <div>
                    <b>
                      {matrixById.get(job.matrix_id)?.name ||
                        "稀疏特征值实验"}
                    </b>
                    <small className="mono">{short(job.id)}</small>
                  </div>
                </button>
              </td>
              <td className="mono">
                {job.config?.target?.mode === "interval_all"
                  ? `[${job.config.target.interval?.lower}, ${job.config.target.interval?.upper})`
                  : `k=${job.config?.target?.k ?? "—"} · σ=${job.config?.target?.sigma ?? "—"}`}
              </td>
              <td>
                <Badge status={job.status} />
              </td>
              <td>
                <span className="backend-pill">
                  {job.config?.solver?.backend || "—"}
                </span>
              </td>
              <td className="date-cell">{fmtDate(job.created_at)}</td>
              <td>
                <button
                  aria-label={`查看任务 ${short(job.id)}`}
                  className="icon-button"
                  onClick={() => onOpen(job)}
                >
                  查看
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function VectorChart({ samples }: { samples: Entity[] }) {
  if (!samples?.length) return null;
  const max = Math.max(...samples.map((v) => v.abs), 1e-20);
  const x = (i: number) => 35 + (i / Math.max(1, samples.length - 1)) * 650;
  const y = (v: number) => 165 - (v / max) * 130;
  return (
    <figure className="vector-chart paper-figure">
      <svg
        viewBox="0 0 720 200"
        role="img"
        aria-label="特征向量绝对值代数分量采样"
      >
        <line x1="35" x2="685" y1="165" y2="165" stroke="#D4D4D0" />
        <path
          d={samples
            .map((v, i) => `${i ? "L" : "M"}${x(i)},${y(v.abs)}`)
            .join(" ")}
          stroke="#111111"
          strokeWidth="1.5"
          fill="none"
        />
        <text x="35" y="190">
          分量 {samples[0].index}
        </text>
        <text x="620" y="190">
          {samples[samples.length - 1].index}
        </text>
        <text x="35" y="20">
          |x| / max = 1
        </text>
      </svg>
      <figcaption className="paper-caption">特征向量绝对值的代数分量采样，按原始自由度索引显示。</figcaption>
    </figure>
  );
}

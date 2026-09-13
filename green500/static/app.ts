type CategoryKey = "financial_report" | "environment_report" | "social_employee" | "financial_targets" | "climate_targets";
type Category = { key: CategoryKey; label: string };
type CoverageCounts = { available: number; pending: number; failed: number; latest_verified?: number; newer_available?: number; unknown?: number };
type SourceFreshness = {
  status: "unknown" | "latest_verified" | "newer_available";
  reason?: string; label?: string; checked_at?: string | null; valid_until?: string | null;
  publication_date?: string | null; report_period?: { start?: string | null; end?: string | null; label?: string | null } | null;
  report_family?: string | null; scope?: string | null;
  newer_candidate?: { url?: string; title?: string; download_status?: string } | null;
  category_assessments?: Partial<Record<CategoryKey, { status?: string; target_status?: string; label?: string; scope?: string; organizational_boundary?: string; target_type?: string; notes?: string[] }>>;
};
type Classification = {
  match_status: "matched" | "ambiguous" | "unmatched";
  source_tickers: string[];
  sector: string | null;
  sub_industry: string | null;
  source_commit: string;
};
type OfficialScore = {
  status: "available" | "premium_only" | "unmatched" | "not_checked" | "access_blocked" | "ambiguous" | "invalid";
  esg_score: number | null; csa_score: number | null;
  last_updated: string | null; fetched_at: string | null; assessment_year: number | null;
  provider_company_name: string | null; provider_industry: string | null;
  provider_cid: string | null;
  is_csa_survey_respondent: boolean | null;
  provider_url: string | null; score_under_review: string | null; source_sha256: string | null;
};
type Company = {
  cik: string; name: string; symbols: string[]; sector: string;
  sub_industry?: string | null; classification?: Classification | null;
  coverage: Partial<Record<CategoryKey, CoverageCounts>>;
  financial_data_count: number; sbti_match_status: string | null;
  last_checked_at: string | null; collection_status: string; source_count: number;
  spglobal_score?: OfficialScore;
  financial_processing?: FinancialProcessing;
  report_processing?: Partial<Record<CategoryKey, ReportProcessing>>;
};
type ReportProcessing = { status: "not_processed" | "processing" | "completed" | "needs_review"; has_result: boolean; model?: string; fiscal_year?: number; updated_at?: string; populated_fields?: number };
type FinancialProcessing = ReportProcessing;
type FinancialMetric = { value: number | null; unit: string | null; status: string; fiscal_year: number; source_document: string; source_url: string | null; source_page: number | null; source_section: string | null; confidence: number; notes: string | null };
type FinancialData = { company_id: string; company_name: string; fiscal_year: number; currency: string } & Record<string, FinancialMetric | string | number>;
type FieldDefinition = { description: string; unit: string | null; type: "number" | "integer" | "boolean" };
type FeatureEvidence = { block_id: number; quote: string; raw_value: string | null; source_unit: string | null; scale_factor: number | null };
type FeatureMetadata = { status: string; reporting_year: number | null; reason: string | null; qualification: string | null; confidence: number | null; evidence: FeatureEvidence | null };
type FixedReportData = {
  company: { name: string; ticker: string }; reporting_year: number | null; boundary: string | null; limitations: string[];
  values: Record<string, number | boolean | null>; metadata: Record<string, FeatureMetadata | null>;
};
type ProcessedReportResult = { processing: ReportProcessing; data: Record<string, unknown> | null; evidence: unknown; field_definitions?: Record<string, FieldDefinition>; source_document_id: number | null; model?: string; processed_at?: string };
type FinancialResult = ProcessedReportResult & { data: FinancialData | null; evidence: Record<string, { quote?: string; operands?: { quote?: string; component_value?: number }[] }> };
type Catalog = {
  companies: Company[];
  summary: { company_count: number; available_reports: number; pending_tasks: number; running_tasks: number; score_count?: number; ai_completed_companies?: number };
  updated_at: string; categories: Category[];
};
type Source = {
  id: number; title: string; url: string; categories: CategoryKey[];
  reporting_period: string | null; discovered_via: string; acquisition_method?: string;
  collection_status: string; document_id: number | null; sha256: string | null;
  byte_count: number | null; content_type: string | null; last_checked_at: string | null;
  parse_status: string | null; evidence: unknown; freshness?: SourceFreshness;
};
type Dataset = {
  id: number; title: string; url: string; source_key: string; sha256: string;
  byte_count: number; content_type: string; last_checked_at: string | null;
  acquisition_method?: string;
};
type CompanyDetail = { sources: Source[]; datasets: Dataset[] };
type ClassificationRecord = { cik: string; sector: string; sub_industry: string; classification: Classification };
type ModalSource = {
  key: string; kind: "report" | "sec" | "sbti"; title: string; url: string;
  reportingPeriod: string | null; status: string; documentId: number | null;
  sha256: string | null; byteCount: number | null; contentType: string | null;
  checkedAt: string | null; acquisitionMethod: string; evidence: unknown; freshness?: SourceFreshness;
};
type DialogState = { company: Company; category: Category; sources: ModalSource[]; selectedKey: string };
type TextPreview = {
  text: string;
  format: string;
  is_complete: boolean;
  warnings: string[];
  line_count: number;
  source_sha256: string;
  source_url: string;
};

const REQUIRED_CATEGORIES: Category[] = [
  { key: "financial_report", label: "Financial" },
  { key: "environment_report", label: "Environment" },
  { key: "social_employee", label: "People" },
  { key: "financial_targets", label: "Financial targets" },
  { key: "climate_targets", label: "Climate targets" },
];
const element = <T extends HTMLElement>(id: string): T => document.getElementById(id) as T;
const make = <K extends keyof HTMLElementTagNameMap>(tag: K, text = "", className = ""): HTMLElementTagNameMap[K] => {
  const node = document.createElement(tag);
  node.textContent = text;
  node.className = className;
  return node;
};
class HttpError extends Error {
  constructor(public readonly status: number, message: string) { super(message); }
}

let catalog: Catalog | null = null;
let categories = REQUIRED_CATEGORIES;
let classificationsByCik = new Map<string, ClassificationRecord>();
let isAuthenticated = false;
let catalogRequestNumber = 0;
let detailRequestNumber = 0;
let previewRequestNumber = 0;
let sectorSignature = "";
let dialogState: DialogState | null = null;
let scoreCompanyCik: string | null = null;
let reportResultView = false;
let reportResultRequestNumber = 0;
let reportResultSignature = "";
let previewSignature = "";
let previewMode: "document" | "text" = "document";
const priorCoverage = new Map<string, string>();
const PROCESSABLE_CATEGORIES = new Set<CategoryKey>(["financial_report", "environment_report", "social_employee", "financial_targets", "climate_targets"]);

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    ...init, credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...(init.headers || {}) },
  });
  if (!response.ok) {
    let message = "HTTP " + response.status;
    try {
      const value = await response.json();
      if (typeof value.detail === "string") message = value.detail;
    } catch { /* Keep the HTTP status when the response has no JSON body. */ }
    throw new HttpError(response.status, message);
  }
  return response.json() as Promise<T>;
}

function countsFor(company: Company, key: CategoryKey): CoverageCounts {
  const value = company.coverage?.[key];
  return { ...value, available: Number(value?.available || 0), pending: Number(value?.pending || 0), failed: Number(value?.failed || 0) };
}
function formatDate(value: string | null | undefined): string {
  if (!value) return "—";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}
function formatTime(value: string | null | undefined): string {
  if (!value) return "—";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}
function formatBytes(value: number | null): string {
  if (value === null) return "";
  if (value < 1024 * 1024) return Math.max(1, Math.round(value / 1024)) + " KB";
  return (value / (1024 * 1024)).toFixed(1) + " MB";
}
function formatType(value: string | null): string {
  if (!value) return "File";
  const type = value.split(";", 1)[0].toLowerCase();
  if (type === "application/pdf") return "PDF";
  if (type === "application/json") return "JSON";
  if (type === "text/html") return "HTML";
  if (type === "text/csv") return "CSV";
  if (type.includes("spreadsheet") || type.includes("excel")) return "Spreadsheet";
  if (type.startsWith("text/")) return "Text";
  return "File";
}
function canonicalUrl(value: string): string {
  try {
    const parsed = new URL(value);
    parsed.hash = "";
    parsed.hostname = parsed.hostname.toLowerCase();
    return parsed.toString();
  } catch { return value; }
}
function showLogin(message = ""): void {
  isAuthenticated = false;
  element<HTMLDialogElement>("score-dialog").close();
  element("app-view").hidden = true;
  element("login-view").hidden = false;
  element("login-error").textContent = message;
  element<HTMLInputElement>("viewer-password").focus();
}
function showCatalog(): void {
  isAuthenticated = true;
  element("login-view").hidden = true;
  element("app-view").hidden = false;
}
function mergeClassifications(value: Catalog): Catalog {
  return {
    ...value,
    companies: value.companies.map(company => {
      if (company.classification) return company;
      const reference = classificationsByCik.get(company.cik);
      return reference ? { ...company, sector: reference.sector, sub_industry: reference.sub_industry, classification: reference.classification } : company;
    }),
  };
}
function updateSectorFilter(companies: Company[]): void {
  const sectors = [...new Set(companies.map(company => company.sector).filter(Boolean))].sort();
  const signature = sectors.join("\n");
  if (signature === sectorSignature) return;
  sectorSignature = signature;
  const select = element<HTMLSelectElement>("sector");
  const selected = select.value;
  select.replaceChildren(new Option("All sectors", ""), ...sectors.map(value => new Option(value, value)));
  select.value = sectors.includes(selected) ? selected : "";
}
function renderHeader(): void {
  const row = make("tr");
  for (const label of ["Company", "Industry", "S&P scores", ...categories.map(category => category.label)]) {
    const heading = make("th", label);
    heading.scope = "col";
    row.append(heading);
  }
  element("company-head").replaceChildren(row);
}
function visibleCompanies(): Company[] {
  if (!catalog) return [];
  const query = element<HTMLInputElement>("search").value.trim().toLowerCase();
  const sector = element<HTMLSelectElement>("sector").value;
  const coverageFilter = element<HTMLSelectElement>("coverage-filter").value;
  const completeOnly = coverageFilter === "complete";
  return catalog.companies.filter(company => {
    const identity = (company.name + " " + company.symbols.join(" ") + " " + company.cik).toLowerCase();
    return (!query || identity.includes(query)) && (!sector || company.sector === sector)
      && (!completeOnly || REQUIRED_CATEGORIES.every(category => countsFor(company, category.key).available > 0))
      && (coverageFilter !== "latest" || REQUIRED_CATEGORIES.every(category => (countsFor(company, category.key).latest_verified || 0) > 0))
      && (coverageFilter !== "ai_done" || [...PROCESSABLE_CATEGORIES].some(key => processingFor(company, key).status === "completed"))
      && (coverageFilter !== "ai_review" || [...PROCESSABLE_CATEGORIES].some(key => processingFor(company, key).status === "needs_review"));
  });
}
function processingFor(company: Company, category: CategoryKey): ReportProcessing {
  const legacy = category === "financial_report" ? company.financial_processing : undefined;
  return company.report_processing?.[category] || legacy || { status: "not_processed", has_result: false };
}
function coverageLabel(counts: CoverageCounts): { text: string; className: string } {
  if ((counts.latest_verified || 0) > 0) return { text: "Latest " + counts.latest_verified, className: "ready" };
  if (counts.available > 0) return { text: "Files " + counts.available, className: "downloaded" };
  if (counts.pending > 0) return { text: "Pending", className: "pending" };
  if (counts.failed > 0) return { text: "Issue", className: "issue" };
  return { text: "—", className: "missing" };
}
function categoryCell(company: Company, category: Category): HTMLTableCellElement {
  const counts = countsFor(company, category.key);
  const status = coverageLabel(counts);
  const cell = make("td", "", "category-cell");
  cell.dataset.label = category.label;
  const button = make("button", status.text, "coverage-button " + status.className);
  button.type = "button";
  button.title = counts.available + " downloaded files; " + (counts.latest_verified || 0) + " latest-source checks verified";
  button.setAttribute("aria-label", company.name + " · " + category.label + " sources · " + status.text);
  button.onclick = () => void openSourceDialog(company, category);
  const key = company.cik + ":" + category.key;
  const signature = counts.available + ":" + counts.pending + ":" + counts.failed;
  const previous = priorCoverage.get(key);
  if (previous !== undefined && previous !== signature) button.classList.add("changed");
  priorCoverage.set(key, signature);
  cell.append(button);
  if (PROCESSABLE_CATEGORIES.has(category.key)) {
    const processing = processingFor(company, category.key);
    const state = processing.status;
    const labels = { not_processed: "Not started", processing: "Processing", completed: "Extracted", needs_review: "Retry" };
    const resultLabel = state === "completed" && processing.populated_fields === 0 ? "No values" : labels[state];
    const ai = make("button", resultLabel, "processing-button " + state);
    ai.type = "button";
    ai.setAttribute("aria-label", company.name + " · " + category.label + " AI results · " + resultLabel);
    ai.onclick = () => void openSourceDialog(company, category, true);
    cell.append(ai);
  }
  return cell;
}
function scoreStatusLabel(score: OfficialScore | undefined): string {
  const labels = {
    available: "Available", premium_only: "Premium access", unmatched: "Company match unresolved",
    not_checked: "Pending", access_blocked: "Source access issue", ambiguous: "Company match needs review",
    invalid: "Source needs review",
  };
  return labels[score?.status || "not_checked"];
}
function hasOfficialScore(score: OfficialScore | undefined): boolean {
  return score?.status === "available" && typeof score.esg_score === "number";
}
function scoreCell(company: Company): HTMLTableCellElement {
  const score = company.spglobal_score;
  const cell = make("td", "", "score-cell");
  cell.dataset.label = "S&P scores";
  const label = hasOfficialScore(score) ? "CSA " + (score!.csa_score ?? "—") + " · ESG " + score!.esg_score : score?.status === "premium_only" ? "Premium" : "—";
  const button = make("button", hasOfficialScore(score) ? "" : label, "score-button" + (hasOfficialScore(score) ? " available" : ""));
  if (hasOfficialScore(score)) {
    for (const [name, value] of [["CSA", score!.csa_score], ["ESG", score!.esg_score]] as const) {
      const metric = make("span", "", "score-value");
      metric.append(make("small", name), make("strong", value === null ? "—" : String(value), "score-" + name.toLowerCase()));
      button.append(metric);
    }
  }
  button.type = "button";
  button.title = hasOfficialScore(score) ? "S&P Global · " + label + " · Out of 100 · Updated " + formatDate(score?.last_updated) : scoreStatusLabel(score);
  button.setAttribute("aria-label", company.name + " · S&P Global · " + (hasOfficialScore(score) ? label + " out of 100" : scoreStatusLabel(score)));
  button.onclick = () => {
    scoreCompanyCik = company.cik;
    renderScoreDialog();
    element<HTMLDialogElement>("score-dialog").showModal();
  };
  cell.append(button);
  return cell;
}
function renderScoreDialog(): void {
  const company = catalog?.companies.find(item => item.cik === scoreCompanyCik);
  if (!company) return;
  const score = company.spglobal_score;
  element("score-company").textContent = company.name;
  const body = element("score-body");
  const metrics = make("div", "", "score-metrics");
  for (const [label, value] of [["CSA score", score?.csa_score], ["ESG score", score?.esg_score]] as const) {
    const metric = make("div");
    metric.append(make("span", label), make("strong", hasOfficialScore(score) && value !== null && value !== undefined ? String(value) : "—"), make("small", "out of 100"));
    metrics.append(metric);
  }
  const details = make("dl", "", "score-details");
  const entries: [string, string][] = [
    ["Status", scoreStatusLabel(score)],
    ["Updated by S&P", formatDate(score?.last_updated)],
    ["Collected", formatDate(score?.fetched_at)],
  ];
  if (score?.provider_company_name && score.provider_company_name !== company.name) entries.push(["Listed company", score.provider_company_name]);
  if (score?.provider_cid) entries.push(["S&P company ID", score.provider_cid]);
  if (score?.provider_industry) entries.push(["S&P industry", score.provider_industry]);
  entries.push(["CSA survey", score?.is_csa_survey_respondent === true ? "Participant" : score?.is_csa_survey_respondent === false ? "Non-participant" : "Not provided"]);
  if (score?.assessment_year != null) entries.push(["Assessment year", String(score.assessment_year)]);
  if (score?.score_under_review) entries.push(["Under review", score.score_under_review]);
  for (const [label, value] of entries) details.append(make("dt", label), make("dd", value));
  const methodology = make("details", "", "score-methodology");
  methodology.append(make("summary", "How to read these scores"));
  methodology.append(make("p", "CSA (Corporate Sustainability Assessment) excludes modeling. ESG can include modeled estimates. Compare scores within the same S&P industry.", "score-note"));
  if (score?.is_csa_survey_respondent != null) {
    methodology.append(make("p", score.is_csa_survey_respondent
      ? "This company’s ESG score uses its CSA survey responses, public information and modeling."
      : "This company’s ESG score uses public information and modeling without active CSA survey participation.", "score-note"));
  }
  body.replaceChildren(metrics, details, methodology);
  if (score?.provider_url) {
    try {
      const url = new URL(score.provider_url);
      if (url.protocol === "https:" && url.hostname === "www.spglobal.com") {
        const link = make("a", "S&P Global source ↗", "button-link");
        link.href = url.href;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        body.append(link);
      }
    } catch { /* A missing valid provider link remains absent. */ }
  }
}
function renderCompanies(): void {
  const scrollY = window.scrollY;
  const body = element<HTMLTableSectionElement>("companies");
  body.replaceChildren();
  const visible = visibleCompanies();
  for (const company of visible) {
    const row = make("tr", "", "company-row");
    const identity = make("td", "", "company-cell");
    identity.dataset.label = "Company";
    identity.append(make("strong", company.name), make("span", company.symbols.join(" · ") || company.cik, "company-meta"));
    const industry = make("td", "", "industry-cell");
    industry.dataset.label = "Industry";
    industry.append(
      make("strong", company.sector || "—"),
      make("span", company.sub_industry || company.classification?.sub_industry || "", "company-meta"),
    );
    row.append(identity, industry, scoreCell(company));
    for (const category of categories) row.append(categoryCell(company, category));
    body.append(row);
  }
  if (!visible.length) {
    const row = make("tr");
    const cell = make("td", "No matching companies.", "empty-message");
    cell.colSpan = 8;
    row.append(cell);
    body.append(row);
  }
  element("row-count").textContent = visible.length + " of " + (catalog?.companies.length || 0);
  window.scrollTo({ top: scrollY });
}
function renderCatalog(value: Catalog): void {
  catalog = mergeClassifications(value);
  const serverLabels = new Map((value.categories || []).map(item => [item.key, item.label]));
  categories = REQUIRED_CATEGORIES.map(category => ({ ...category, label: serverLabels.get(category.key)?.replace(" reports", "") || category.label }));
  element("company-count").textContent = String(value.summary.company_count);
  element("available-reports").textContent = String(value.summary.available_reports);
  element("score-count").textContent = String(value.summary.score_count ?? value.companies.filter(company => hasOfficialScore(company.spglobal_score)).length);
  const processingCount = (category: CategoryKey): { completed: number; eligible: number; active: number; retry: number } => ({
    completed: value.companies.filter(company => processingFor(company, category).status === "completed").length,
    eligible: value.companies.filter(company => countsFor(company, category).available > 0).length,
    active: value.companies.filter(company => processingFor(company, category).status === "processing").length,
    retry: value.companies.filter(company => processingFor(company, category).status === "needs_review").length,
  });
  const setProcessingCount = (id: string, category: CategoryKey): number => {
    const counts = processingCount(category);
    const node = element(id);
    node.textContent = counts.completed + "/" + counts.eligible + (counts.active ? " +" + counts.active : "");
    node.title = counts.completed + " completed · " + counts.active + " processing · " + counts.retry + " retry";
    return counts.active;
  };
  const aiActive =
    setProcessingCount("ai-count", "financial_report")
    + setProcessingCount("environment-ai-count", "environment_report")
    + setProcessingCount("social-ai-count", "social_employee")
    + setProcessingCount("financial-target-ai-count", "financial_targets")
    + setProcessingCount("climate-ai-count", "climate_targets");
  const active = value.summary.pending_tasks + value.summary.running_tasks + aiActive;
  element("active-tasks").textContent = String(active);
  element("live-dot").classList.toggle("active", active > 0);
  element("last-refresh").textContent = "Updated " + formatTime(value.updated_at);
  element("catalog-updated").textContent = formatDate(value.updated_at);
  updateSectorFilter(catalog.companies);
  renderHeader();
  renderCompanies();
  if (element<HTMLDialogElement>("score-dialog").open) renderScoreDialog();
}

function datasetAllowed(dataset: Dataset, category: CategoryKey): boolean {
  return (category === "financial_report" && dataset.source_key === "sec_companyfacts")
    || (category === "climate_targets" && dataset.source_key === "sbti");
}
function modalSources(company: Company, category: Category, detail: CompanyDetail): ModalSource[] {
  const values: ModalSource[] = detail.sources.filter(source => source.categories.includes(category.key)).map(source => ({
    key: "report:" + canonicalUrl(source.url), kind: "report", title: source.title, url: source.url,
    reportingPeriod: source.freshness?.report_period?.label || source.reporting_period, status: source.collection_status, documentId: source.document_id,
    sha256: source.sha256, byteCount: source.byte_count, contentType: source.content_type,
    checkedAt: source.last_checked_at, acquisitionMethod: source.acquisition_method || source.discovered_via,
    evidence: source.evidence, freshness: source.freshness,
  }));
  for (const dataset of detail.datasets.filter(item => datasetAllowed(item, category.key))) {
    const isSbti = dataset.source_key === "sbti";
    values.push({
      key: dataset.source_key + ":" + canonicalUrl(dataset.url), kind: isSbti ? "sbti" : "sec",
      title: isSbti ? "SBTi target dataset" : "SEC Company Facts", url: dataset.url,
      reportingPeriod: null, status: isSbti && company.sbti_match_status === "unique_name" ? "provisional" : "available",
      documentId: dataset.id, sha256: dataset.sha256, byteCount: dataset.byte_count,
      contentType: dataset.content_type, checkedAt: dataset.last_checked_at,
      acquisitionMethod: dataset.acquisition_method || "provider dataset", evidence: null,
    });
  }
  const latestByUrl = new Map<string, ModalSource>();
  for (const value of values) {
    const current = latestByUrl.get(value.key);
    if (!current || (value.documentId || 0) > (current.documentId || 0)) latestByUrl.set(value.key, value);
  }
  return [...latestByUrl.values()].sort((left, right) => {
    if (Boolean(left.documentId) !== Boolean(right.documentId)) return left.documentId ? -1 : 1;
    const rank = (source: ModalSource) => source.freshness?.status === "latest_verified" ? 2 : source.freshness?.status === "newer_available" ? 0 : 1;
    if (rank(left) !== rank(right)) return rank(right) - rank(left);
    const leftDate = left.freshness?.report_period?.end || left.freshness?.publication_date || "";
    const rightDate = right.freshness?.report_period?.end || right.freshness?.publication_date || "";
    return rightDate.localeCompare(leftDate) || left.title.localeCompare(right.title);
  });
}
function sourceStatus(source: ModalSource): { label: string; className: string } {
  if (source.status === "provisional") return { label: "Provisional", className: "provisional" };
  if (source.documentId) {
    if (["failed", "partial", "unknown", "cancelled"].includes(source.status)) return { label: "Downloaded · refresh issue", className: "issue" };
    return { label: "Downloaded", className: "downloaded" };
  }
  if (["pending", "running", "not_downloaded"].includes(source.status)) return { label: "Pending", className: "pending" };
  if (["failed", "partial", "unknown", "cancelled"].includes(source.status)) return { label: "Issue", className: "issue" };
  return { label: "—", className: "missing" };
}
function freshnessLabel(source: ModalSource): string {
  if (source.freshness?.status === "latest_verified") return "Latest source verified";
  if (source.freshness?.status === "newer_available") return "Newer source available";
  if (source.freshness?.reason === "verification_expired") return "Latest-source check expired";
  if (source.freshness?.reason === "content_changed") return "Source changed · recheck needed";
  return "Latest source unchecked";
}
function sourceKind(source: ModalSource): string {
  if (source.kind === "sec") return "SEC financial data";
  if (source.kind === "sbti") return "SBTi data";
  const title = source.title.toLowerCase();
  if (title.includes("proxy") || title.includes("def 14a")) return "Proxy statement";
  if (title.includes("10-q") || title.includes("quarterly")) return "Quarterly filing";
  if (title.includes("10-k") || title.includes("20-f") || title.includes("40-f")) return "Annual filing";
  if (title.includes("annual report")) return "Annual report";
  return "Report";
}
function renderSourceList(): void {
  if (!dialogState) return;
  const list = element("source-list");
  const scrollTop = list.scrollTop;
  list.replaceChildren();
  for (const source of dialogState.sources) {
    const status = sourceStatus(source);
    const button = make("button", "", "source-item");
    button.type = "button";
    button.classList.toggle("selected", source.key === dialogState.selectedKey);
    const top = make("span", "", "source-item-top");
    top.append(make("small", sourceKind(source)), make("i", status.label, status.className));
    button.append(
      top, make("strong", source.title),
      make("span", [source.reportingPeriod, formatType(source.contentType)].filter(Boolean).join(" · "), "source-item-meta"),
    );
    if (source.kind === "report") {
      const freshness = make("span", freshnessLabel(source), "source-freshness " + (source.freshness?.status || "unknown"));
      button.append(freshness);
    }
    button.onclick = () => selectSource(source.key);
    list.append(button);
  }
  if (!dialogState.sources.length) list.append(make("p", "No sources yet.", "empty-message"));
  list.scrollTop = scrollTop;
}
function appendEvidence(container: HTMLElement, evidence: unknown): void {
  if (!evidence) return;
  const entries = Array.isArray(evidence) ? evidence : [evidence];
  const list = make("ul");
  for (const entry of entries) {
    if (entry && typeof entry === "object") {
      const value = entry as Record<string, unknown>;
      const location = [value.page ? "p. " + value.page : "", value.section || ""].filter(Boolean).join(" · ");
      const text = String(value.brief_paraphrase || value.summary || value.text || "Evidence recorded");
      list.append(make("li", (location ? location + ": " : "") + text));
    } else list.append(make("li", String(entry)));
  }
  container.append(list);
}
function renderPreviewHeader(source: ModalSource): void {
  const header = element("preview-header");
  header.replaceChildren();
  const title = make("div");
  title.append(
    make("span", sourceKind(source), "overline"), make("h3", source.title),
    make("p", [source.reportingPeriod, formatType(source.contentType), formatBytes(source.byteCount)].filter(Boolean).join(" · "), "preview-meta"),
  );
  if (source.kind === "report") {
    const review = source.freshness;
    title.append(make("p", [freshnessLabel(source), review?.checked_at ? "Checked " + formatDate(review.checked_at) : ""].filter(Boolean).join(" · "), "source-freshness " + (review?.status || "unknown")));
    if (review?.scope) title.append(make("p", "Scope: " + review.scope.replaceAll("_", " "), "preview-meta"));
    const assessment = dialogState ? review?.category_assessments?.[dialogState.category.key] : undefined;
    if (assessment) {
      const details = [assessment.label || (dialogState?.category.key.endsWith("_targets") && assessment.target_status ? "Target: " + assessment.target_status : assessment.status?.replaceAll("_", " ")), assessment.target_type?.replaceAll("_", " "), (assessment.organizational_boundary || assessment.scope)?.replaceAll("_", " "), assessment.notes?.join(" ")].filter(Boolean).join(" · ");
      if (details) title.append(make("p", details, "source-assessment"));
    }
  }
  const actions = make("div", "", "preview-actions");
  if (source.contentType?.toLowerCase().startsWith("application/pdf")) {
    const documentTab = make("button", "Document", "preview-tab");
    documentTab.type = "button";
    documentTab.classList.toggle("active", previewMode === "document");
    documentTab.onclick = () => {
      if (previewMode === "document") return;
      previewMode = "document";
      previewSignature = "";
      void renderPreview(source);
    };
    const textTab = make("button", "Text", "preview-tab");
    textTab.type = "button";
    textTab.classList.toggle("active", previewMode === "text");
    textTab.onclick = () => {
      if (previewMode === "text") return;
      previewMode = "text";
      previewSignature = "";
      void renderPreview(source);
    };
    actions.append(documentTab, textTab);
  }
  if (source.documentId) {
    const download = make("a", "Download", "button-link");
    download.href = "/api/catalog/files/" + source.documentId + "/download";
    actions.append(download);
  }
  if (source.url) {
    const original = make("a", "Original ↗", "button-link quiet-link");
    original.href = source.url;
    original.target = "_blank";
    original.rel = "noopener noreferrer";
    actions.append(original);
  }
  const details = make("details", "", "file-details");
  details.append(make("summary", "File details"));
  const detailBody = make("div");
  detailBody.append(
    make("p", "Status: " + sourceStatus(source).label),
    make("p", "Checked: " + formatDate(source.checkedAt)),
    make("p", "Collected via: " + (source.acquisitionMethod || "—")),
  );
  if (source.sha256) detailBody.append(make("p", "SHA-256 " + source.sha256, "digest"));
  appendEvidence(detailBody, source.evidence);
  details.append(detailBody);
  header.append(title, actions, details);
}
async function renderPreview(source: ModalSource): Promise<void> {
  renderPreviewHeader(source);
  const body = element("preview-body");
  if (!source.documentId) {
    previewSignature = "";
    body.replaceChildren(make("p", "This source has not been downloaded.", "empty-message"));
    return;
  }
  const signature = source.documentId + ":" + (source.sha256 || "") + ":" + previewMode;
  if (signature === previewSignature) return;
  previewSignature = signature;
  const viewUrl = "/api/catalog/files/" + source.documentId + "/view";
  const type = (source.contentType || "").split(";", 1)[0].toLowerCase();
  if (type === "application/pdf" && previewMode === "document") {
    const frame = document.createElement("iframe");
    frame.src = viewUrl;
    frame.title = source.title;
    frame.className = "pdf-preview";
    body.replaceChildren(frame);
    return;
  }
  const requestNumber = ++previewRequestNumber;
  body.replaceChildren(make("p", "Loading…", "empty-message"));
  try {
    const useDecodedText = previewMode === "text"
      || type === "text/csv"
      || (!type.startsWith("text/") && type !== "application/json" && type !== "application/xml");
    const response = await fetch(
      useDecodedText ? "/api/catalog/files/" + source.documentId + "/text" : viewUrl,
      { credentials: "same-origin" },
    );
    if (!response.ok) throw new HttpError(response.status, "HTTP " + response.status);
    let text: string;
    let previewNotice: HTMLElement | null = null;
    if (useDecodedText) {
      const decoded = await response.json() as TextPreview;
      text = decoded.text;
      if (!decoded.is_complete || decoded.warnings.length) {
        const details = make("details", "", "preview-warning");
        details.append(make("summary", decoded.is_complete ? "Preview note" : "Preview shortened"));
        const note = make("p", decoded.warnings.join(" · ") || "Download the original for the complete file.");
        details.append(note);
        previewNotice = details;
      }
      if (decoded.format.toLowerCase() === "json") {
        try { text = JSON.stringify(JSON.parse(text), null, 2); } catch { /* Keep decoded text. */ }
      }
    } else {
      text = await response.text();
    }
    if (!useDecodedText && type === "application/json") {
      try { text = JSON.stringify(JSON.parse(text), null, 2); } catch { /* Show malformed provider JSON as inert text. */ }
    }
    if (requestNumber !== previewRequestNumber || previewSignature !== signature) return;
    const preview = make("pre", text, "text-preview");
    body.replaceChildren(...(previewNotice ? [previewNotice, preview] : [preview]));
  } catch (error) {
    if (requestNumber === previewRequestNumber) body.replaceChildren(make("p", "Preview unavailable · " + String(error), "empty-message"));
  }
}
function selectSource(key: string): void {
  if (!dialogState) return;
  if (dialogState.selectedKey !== key) {
    previewSignature = "";
    previewMode = "document";
  }
  dialogState.selectedKey = key;
  renderSourceList();
  const source = dialogState.sources.find(item => item.key === key);
  if (source) void renderPreview(source);
}
async function refreshDialogDetail(selectFirst: boolean): Promise<void> {
  if (!dialogState) return;
  const state = dialogState;
  const requestNumber = ++detailRequestNumber;
  try {
    const detail = await request<CompanyDetail>("/api/catalog/" + state.company.cik);
    if (requestNumber !== detailRequestNumber || dialogState !== state) return;
    const sources = modalSources(state.company, state.category, detail);
    state.sources = sources;
    const selected = sources.find(item => item.key === state.selectedKey);
    if (!selected && (selectFirst || state.selectedKey)) {
      state.selectedKey = (sources.find(item => item.documentId) || sources[0])?.key || "";
      previewMode = "document";
      previewSignature = "";
    }
    renderSourceList();
    const current = sources.find(item => item.key === state.selectedKey);
    if (current) void renderPreview(current);
    else {
      previewSignature = "";
      element("preview-header").replaceChildren();
      element("preview-body").replaceChildren(make("p", "No sources yet.", "empty-message"));
    }
  } catch (error) {
    if (error instanceof HttpError && error.status === 401) {
      element<HTMLDialogElement>("source-dialog").close();
      showLogin("Session expired.");
      return;
    }
    element("source-list").replaceChildren(make("p", String(error), "error"));
  }
}
const FINANCIAL_METRICS = [
  ["revenue", "Revenue"], ["employees", "Employees"], ["total_capex", "Total capital expenditure"],
  ["green_transition_capex", "Green transition capex"], ["operating_income", "Operating income"],
  ["total_assets", "Total assets"], ["cash_and_equivalents", "Cash and equivalents"],
  ["total_debt", "Total debt"], ["operating_cash_flow", "Operating cash flow"],
] as const;

function setReportResultView(show: boolean): void {
  reportResultView = show;
  element("source-content").hidden = show;
  element("report-result-panel").hidden = !show;
  element("report-source-tab").classList.toggle("active", !show);
  element("report-result-tab").classList.toggle("active", show);
}

function addOriginalResultAction(actions: HTMLElement, value: ProcessedReportResult): void {
  if (!value.source_document_id) return;
  const original = make("button", "Original", "button-link");
  original.type = "button";
  original.onclick = async () => {
    setReportResultView(false);
    await refreshDialogDetail(true);
    const source = dialogState?.sources.find(item => item.documentId === value.source_document_id);
    if (source) selectSource(source.key);
  };
  actions.append(original);
}

function renderFinancialResult(panel: HTMLElement, value: FinancialResult, state: DialogState): void {
  const data = value.data;
  if (!data) return;
  const header = make("div", "", "financial-result-header");
  const identity = make("div");
  identity.append(make("h3", data.company_name + " · FY " + data.fiscal_year), make("p", [value.model, "Processed " + formatDate(value.processed_at)].filter(Boolean).join(" · "), "preview-meta"));
  if (value.processing.status !== "completed") identity.append(make("p", "Showing the previous result. The latest attempt needs a retry.", "source-assessment"));
  const actions = make("div", "", "preview-actions");
  const download = make("a", "Download JSON", "button-link");
  download.href = "/api/catalog/" + state.company.cik + "/financial?download=true";
  actions.append(download);
  addOriginalResultAction(actions, value);
  header.append(identity, actions);
  panel.append(header);
  const sourceTitle = (data.revenue as FinancialMetric)?.source_document;
  if (sourceTitle) panel.append(make("p", sourceTitle, "financial-source-title"));
  const metrics = make("div", "", "financial-metrics");
  for (const [key, label] of FINANCIAL_METRICS) {
    const metric = data[key] as FinancialMetric | null;
    const row = make("article", "", "financial-metric");
    const title = make("div");
    title.append(make("strong", label), make("small", metric?.unit || ""));
    const number = metric?.value == null ? "—" : new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 }).format(metric.value);
    row.append(title, make("span", number, "financial-value"), make("span", (metric?.status || "not_disclosed").replaceAll("_", " "), "metric-status"));
    const details = make("details", "", "metric-evidence");
    details.append(make("summary", "Source & notes"));
    if (metric?.notes) details.append(make("p", metric.notes));
    if (metric?.source_section) details.append(make("p", metric.source_section));
    if (metric?.source_page) details.append(make("p", "Page " + metric.source_page));
    const evidence = value.evidence[key];
    if (evidence?.quote) details.append(make("blockquote", evidence.quote));
    for (const operand of evidence?.operands || []) if (operand.quote) details.append(make("blockquote", operand.quote));
    if (metric) details.append(make("p", "Extraction confidence: " + Math.round(metric.confidence * 100) + "%"));
    row.append(details);
    metrics.append(row);
  }
  panel.append(metrics);
  const raw = make("details", "", "financial-json");
  raw.append(make("summary", "JSON"), make("pre", JSON.stringify(data, null, 2), "text-preview"));
  panel.append(raw);
}
function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
function fieldLabel(value: string): string {
  const text = value.replaceAll("_", " ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}
function displayValue(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "number") return new Intl.NumberFormat(undefined, { maximumFractionDigits: 4 }).format(value);
  const text = String(value);
  return /^[a-z0-9]+(?:_[a-z0-9]+)+$/i.test(text) ? text.replaceAll("_", " ") : text;
}
function addProperty(list: HTMLElement, label: string, value: unknown): void {
  list.append(make("dt", fieldLabel(label)), make("dd", displayValue(value)));
}
const SOURCE_DETAIL_KEYS = new Set([
  "source_document", "source_url", "source_page", "source_section", "notes", "confidence",
  "organizational_boundary", "geographic_scope", "externally_assured",
]);
function appendSourceDetails(container: HTMLElement, value: Record<string, unknown>): void {
  const present = [...SOURCE_DETAIL_KEYS].filter(key => value[key] !== null && value[key] !== undefined && value[key] !== "");
  if (!present.length) return;
  const details = make("details", "", "metric-evidence");
  details.append(make("summary", "Source & notes"));
  const list = make("dl", "", "report-properties");
  for (const key of present) addProperty(list, key, value[key]);
  details.append(list);
  container.append(details);
}
function isMetric(value: Record<string, unknown>): boolean {
  return "value" in value && ("unit" in value || "year" in value || "status" in value);
}
function renderMetric(label: string, value: Record<string, unknown>): HTMLElement {
  const row = make("article", "", "financial-metric report-metric");
  const title = make("div");
  title.append(make("strong", fieldLabel(label)), make("small", [value.unit, value.year].filter(item => item !== null && item !== undefined && item !== "").map(String).join(" · ")));
  row.append(title, make("span", displayValue(value.value), "financial-value"), make("span", displayValue(value.status || "reported"), "metric-status"));
  appendSourceDetails(row, value);
  return row;
}
function renderRecordCard(label: string, value: Record<string, unknown>, index?: number): HTMLElement {
  const card = make("article", "", "report-record");
  const title = typeof value.target_name === "string" && value.target_name
    ? value.target_name : typeof value.metric_name === "string" && value.metric_name
      ? value.metric_name : fieldLabel(label) + (index === undefined ? "" : " " + (index + 1));
  card.append(make("h5", title));
  const properties = make("dl", "", "report-properties");
  const nested: [string, unknown][] = [];
  for (const [key, child] of Object.entries(value)) {
    if (SOURCE_DETAIL_KEYS.has(key) || key === "target_name" || key === "metric_name") continue;
    if (isRecord(child) || Array.isArray(child)) nested.push([key, child]);
    else if (child !== null && child !== undefined && child !== "") addProperty(properties, key, child);
  }
  if (properties.childElementCount) card.append(properties);
  for (const [key, child] of nested) appendStructuredValue(card, key, child);
  appendSourceDetails(card, value);
  return card;
}
function appendStructuredValue(container: HTMLElement, label: string, value: unknown): void {
  if (value === null || value === undefined || (Array.isArray(value) && !value.length)) return;
  if (isRecord(value) && isMetric(value)) {
    container.append(renderMetric(label, value));
    return;
  }
  if (isRecord(value)) {
    const group = make("section", "", "report-group");
    group.append(make("h4", fieldLabel(label)));
    const primitives = make("dl", "", "report-properties");
    for (const [key, child] of Object.entries(value)) {
      if (SOURCE_DETAIL_KEYS.has(key)) continue;
      if (isRecord(child) || Array.isArray(child)) appendStructuredValue(group, key, child);
      else if (child !== null && child !== undefined && child !== "") addProperty(primitives, key, child);
    }
    if (primitives.childElementCount) group.insertBefore(primitives, group.children[1] || null);
    appendSourceDetails(group, value);
    if (group.childElementCount > 1) container.append(group);
    return;
  }
  if (Array.isArray(value)) {
    const group = make("section", "", "report-group");
    group.append(make("h4", fieldLabel(label)));
    if (value.every(child => !isRecord(child) && !Array.isArray(child))) {
      group.append(make("p", value.map(displayValue).join(" · "), "report-list-value"));
    } else {
      value.forEach((child, index) => {
        if (isRecord(child)) group.append(isMetric(child) ? renderMetric(label + " " + (index + 1), child) : renderRecordCard(label, child, index));
        else appendStructuredValue(group, label + " " + (index + 1), child);
      });
    }
    container.append(group);
    return;
  }
  const properties = make("dl", "", "report-properties report-single-property");
  addProperty(properties, label, value);
  container.append(properties);
}
function firstNestedString(value: unknown, key: string): string | undefined {
  if (Array.isArray(value)) {
    for (const child of value) {
      const found = firstNestedString(child, key);
      if (found) return found;
    }
  } else if (isRecord(value)) {
    if (typeof value[key] === "string" && value[key]) return value[key] as string;
    for (const child of Object.values(value)) {
      const found = firstNestedString(child, key);
      if (found) return found;
    }
  }
  return undefined;
}
function renderGenericResult(panel: HTMLElement, value: ProcessedReportResult, state: DialogState): void {
  const data = value.data;
  if (!data) return;
  const header = make("div", "", "report-result-header");
  const identity = make("div");
  const year = data.reporting_year ?? data.fiscal_year;
  identity.append(make("h3", String(data.company_name || state.company.name) + (year ? " · " + year : "")), make("p", [value.model, "Processed " + formatDate(value.processed_at)].filter(Boolean).join(" · "), "preview-meta"));
  if (value.processing.status !== "completed") identity.append(make("p", "Showing the previous result. The latest attempt needs a retry.", "source-assessment"));
  const actions = make("div", "", "preview-actions");
  const download = make("a", "Download JSON", "button-link");
  download.href = "/api/catalog/" + state.company.cik + "/processed/" + state.category.key + "?download=true";
  actions.append(download);
  addOriginalResultAction(actions, value);
  header.append(identity, actions);
  panel.append(header);
  const sourceTitle = firstNestedString(data, "source_document");
  if (sourceTitle) panel.append(make("p", sourceTitle, "financial-source-title"));
  const groups = make("div", "", "report-groups");
  for (const [key, child] of Object.entries(data)) {
    if (["company", "company_id", "company_name", "reporting_year", "fiscal_year"].includes(key)) continue;
    appendStructuredValue(groups, key, child);
  }
  if (groups.childElementCount) panel.append(groups);
  else panel.append(make("p", "No extracted fields were returned.", "empty-message"));
  if (value.evidence && (!isRecord(value.evidence) || Object.keys(value.evidence).length)) {
    const evidence = make("details", "", "financial-json report-json");
    evidence.append(make("summary", "Extraction evidence"), make("pre", JSON.stringify(value.evidence, null, 2), "text-preview"));
    panel.append(evidence);
  }
  const raw = make("details", "", "financial-json report-json");
  raw.append(make("summary", "JSON"), make("pre", JSON.stringify(data, null, 2), "text-preview"));
  panel.append(raw);
}

function fixedFieldGroup(field: string, category: CategoryKey): string {
  if (category === "financial_targets") return field.includes("_long_term_") ? "Long-term targets" : "Annual targets";
  if (category === "climate_targets") return "Climate targets";
  if (category === "social_employee") return "People and social measures";
  return "Environmental measures";
}

function renderFixedResult(panel: HTMLElement, value: ProcessedReportResult, state: DialogState): void {
  const data = value.data as unknown as FixedReportData;
  if (!data?.company || !data.values || !data.metadata) return;
  const definitions = value.field_definitions || {};
  const populated = Object.entries(data.values).filter(([, fieldValue]) => fieldValue !== null);
  const header = make("div", "", "report-result-header");
  const identity = make("div");
  identity.append(
    make("h3", data.company.name + (data.reporting_year ? " · " + data.reporting_year : "")),
    make("p", [Object.keys(data.values).length + " fixed fields · " + populated.length + " available", value.model, "Processed " + formatDate(value.processed_at)].filter(Boolean).join(" · "), "preview-meta"),
  );
  if (value.processing.status !== "completed") identity.append(make("p", "Showing the previous result. The latest attempt needs a retry.", "source-assessment"));
  const actions = make("div", "", "preview-actions");
  const download = make("a", "Download JSON", "button-link");
  download.href = "/api/catalog/" + state.company.cik + "/processed/" + state.category.key + "?download=true";
  actions.append(download);
  addOriginalResultAction(actions, value);
  header.append(identity, actions);
  panel.append(header);
  if (data.boundary) panel.append(make("p", data.boundary, "fixed-boundary"));
  if (data.limitations?.length) {
    const limitations = make("details", "", "fixed-limitations");
    limitations.append(make("summary", "Limitations"));
    const list = make("ul");
    for (const item of data.limitations) list.append(make("li", item));
    limitations.append(list);
    panel.append(limitations);
  }
  const groups = new Map<string, [string, number | boolean | null, FeatureMetadata | null][]>();
  for (const [field, fieldValue] of Object.entries(data.values)) {
    const group = fixedFieldGroup(field, state.category.key);
    if (!groups.has(group)) groups.set(group, []);
    groups.get(group)!.push([field, fieldValue, data.metadata[field] || null]);
  }
  const container = make("div", "", "report-groups fixed-feature-groups");
  for (const [groupName, fields] of groups) {
    const group = make("section", "", "report-group");
    group.append(make("h4", groupName));
    const metrics = make("div", "", "financial-metrics");
    for (const [field, fieldValue, metadata] of fields) {
      const definition = definitions[field];
      const row = make("article", "", "financial-metric fixed-feature");
      const title = make("div");
      title.append(
        make("strong", definition?.description || fieldLabel(field)),
        make("small", [definition?.unit, metadata?.reporting_year].filter(item => item !== null && item !== undefined && item !== "").map(String).join(" · ")),
      );
      row.dataset.field = field;
      row.append(title, make("span", fieldValue === null ? "null" : displayValue(fieldValue), "financial-value"), make("span", displayValue(metadata?.status || (fieldValue === null ? "not extracted" : "reported")), "metric-status"));
      if (metadata) {
        const detail = make("details", "", "metric-evidence");
        detail.append(make("summary", "Source & notes"));
        if (metadata.qualification) detail.append(make("p", metadata.qualification));
        if (metadata.reason) detail.append(make("p", metadata.reason));
        if (metadata.evidence?.quote) detail.append(make("blockquote", metadata.evidence.quote));
        const source = metadata.evidence;
        if (source) detail.append(make("p", "Source block " + source.block_id + (source.raw_value ? " · copied value " + source.raw_value : "")));
        row.append(detail);
      }
      metrics.append(row);
    }
    group.append(metrics);
    container.append(group);
  }
  if (container.childElementCount) panel.append(container);
  else panel.append(make("p", "No source-supported values were extracted.", "empty-message"));
  const raw = make("details", "", "financial-json report-json");
  raw.append(make("summary", "JSON"), make("pre", JSON.stringify(data, null, 2), "text-preview"));
  panel.append(raw);
}

async function refreshReportResult(): Promise<void> {
  const state = dialogState;
  if (!state || !reportResultView) return;
  const number = ++reportResultRequestNumber;
  const panel = element("report-result-panel");
  try {
    const path = state.category.key === "financial_report"
      ? "/api/catalog/" + state.company.cik + "/financial"
      : "/api/catalog/" + state.company.cik + "/processed/" + state.category.key;
    const value = await request<ProcessedReportResult>(path);
    if (number !== reportResultRequestNumber || dialogState !== state || !reportResultView) return;
    const signature = state.category.key + ":" + JSON.stringify(value);
    if (signature === reportResultSignature) return;
    reportResultSignature = signature;
    panel.replaceChildren();
    if (!value.data) {
      const actions = { not_processed: "Not processed yet.", processing: "Extracting report data…", completed: "Result unavailable.", needs_review: "Extraction failed. Needs a retry." };
      panel.append(make("p", actions[value.processing.status], "empty-message"));
      return;
    }
    if (state.category.key === "financial_report") renderFinancialResult(panel, value as FinancialResult, state);
    else renderFixedResult(panel, value, state);
  } catch (error) {
    if (error instanceof HttpError && error.status === 401) {
      element<HTMLDialogElement>("source-dialog").close();
      showLogin("Session expired.");
    } else if (number === reportResultRequestNumber) panel.replaceChildren(make("p", "AI results unavailable.", "empty-message"));
  }
}

async function openSourceDialog(company: Company, category: Category, showAi = false): Promise<void> {
  dialogState = { company, category, sources: [], selectedKey: "" };
  previewSignature = "";
  previewMode = "document";
  element("dialog-company").textContent = company.name;
  element("dialog-title").textContent = category.label;
  element("report-tabs").hidden = !PROCESSABLE_CATEGORIES.has(category.key);
  reportResultSignature = "";
  setReportResultView(showAi && PROCESSABLE_CATEGORIES.has(category.key));
  element("report-result-panel").replaceChildren(make("p", "Loading…", "empty-message"));
  element("source-list").replaceChildren(make("p", "Loading…", "empty-message"));
  element("preview-header").replaceChildren();
  element("preview-body").replaceChildren(make("p", "Loading…", "empty-message"));
  const dialog = element<HTMLDialogElement>("source-dialog");
  if (!dialog.open) dialog.showModal();
  if (reportResultView) await refreshReportResult();
  else await refreshDialogDetail(true);
}
async function loadClassifications(): Promise<void> {
  try {
    const response = await fetch("/static/company-classifications.json?v=a8794f6", { credentials: "same-origin" });
    if (!response.ok) return;
    const value = await response.json() as { companies: ClassificationRecord[] };
    classificationsByCik = new Map(value.companies.map(company => [company.cik, company]));
    if (catalog) renderCatalog(catalog);
  } catch { /* The API classification remains authoritative. */ }
}
async function refreshCatalog(): Promise<void> {
  if (!isAuthenticated) return;
  const requestNumber = ++catalogRequestNumber;
  try {
    const value = await request<Catalog>("/api/catalog");
    if (requestNumber !== catalogRequestNumber) return;
    element("notice").textContent = "";
    renderCatalog(value);
  } catch (error) {
    if (error instanceof HttpError && error.status === 401) { showLogin("Session expired."); return; }
    element("notice").textContent = String(error);
  }
}
async function refreshAll(): Promise<void> {
  await refreshCatalog();
  if (element<HTMLDialogElement>("source-dialog").open) {
    if (reportResultView) await refreshReportResult();
    else await refreshDialogDetail(false);
  }
}

element<HTMLFormElement>("login-form").onsubmit = async event => {
  event.preventDefault();
  const input = element<HTMLInputElement>("viewer-password");
  const submit = element<HTMLFormElement>("login-form").querySelector<HTMLButtonElement>("button[type=submit]");
  if (submit) submit.disabled = true;
  element("login-error").textContent = "";
  try {
    await request<{ authenticated: boolean }>("/api/login", { method: "POST", body: JSON.stringify({ password: input.value }) });
    input.value = "";
    showCatalog();
    await Promise.all([loadClassifications(), refreshCatalog()]);
  } catch (error) {
    element("login-error").textContent = error instanceof Error ? error.message : String(error);
    input.select();
  } finally { if (submit) submit.disabled = false; }
};
element("logout").onclick = async () => {
  try { await request<{ authenticated: boolean }>("/api/logout", { method: "POST", body: "{}" }); }
  finally {
    catalog = null;
    const dialog = element<HTMLDialogElement>("source-dialog");
    if (dialog.open) dialog.close();
    showLogin();
  }
};
element("dialog-close").onclick = () => element<HTMLDialogElement>("source-dialog").close();
element("report-source-tab").onclick = async () => { setReportResultView(false); await refreshDialogDetail(true); };
element("report-result-tab").onclick = async () => { setReportResultView(true); await refreshReportResult(); };
element("score-close").onclick = () => element<HTMLDialogElement>("score-dialog").close();
element<HTMLDialogElement>("score-dialog").addEventListener("close", () => { scoreCompanyCik = null; });
element<HTMLDialogElement>("source-dialog").addEventListener("close", () => {
  dialogState = null;
  previewSignature = "";
  previewRequestNumber += 1;
  reportResultRequestNumber += 1;
  reportResultView = false;
});
element("search").oninput = renderCompanies;
element("sector").onchange = renderCompanies;
element("coverage-filter").onchange = renderCompanies;
async function initialize(): Promise<void> {
  try {
    const session = await request<{ authenticated: boolean }>("/api/session");
    if (!session.authenticated) { showLogin(); return; }
    showCatalog();
    await Promise.all([loadClassifications(), refreshCatalog()]);
  } catch (error) { showLogin(error instanceof HttpError && error.status === 401 ? "" : String(error)); }
}
void initialize();
setInterval(() => void refreshAll(), 3000);

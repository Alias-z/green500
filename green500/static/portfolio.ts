type TargetName = "esg" | "csa";
type ModelFamily = "ebm" | "catboost";
type Operation = "add" | "percent" | "set";
type CategoryKey = "financial" | "social" | "environmental" | "climate_target" | "financial_target";

type ModelField = {
  feature_name: string;
  label: string;
  canonical_unit: string | null;
  category_or_context: string;
  editable: boolean;
};
type TargetDefinition = { active_features: string[]; fields: ModelField[]; metrics: unknown };
type ComparisonRun = {
  run_id: string;
  snapshot_id: string;
  prediction_as_of: string;
  targets: Record<TargetName, TargetDefinition>;
  limitations: string[];
  company_count: number;
};
type CompanyIdentity = { company_cik: string; name: string; ticker: string | null; industry: string | null };
type CompanyEvaluation = {
  company: CompanyIdentity;
  features: Record<string, unknown>;
  metadata: Record<string, unknown>;
  predictions: Record<ModelFamily, number | null>;
  personalized_index: number | null;
  category_contributions: Record<string, number>;
  coverage: { active_feature_count: number; available_active_feature_count: number };
  warnings: string[];
};
type CompanyResponse = { companies: CompanyEvaluation[]; total: number };
type Adjustment = { feature_name: string; operation: Operation; value: number };
type ScenarioChange = {
  company_cik: string;
  feature_name: string;
  original_value: unknown;
  scenario_value: unknown;
  operation: Operation;
  amount: number;
  status: string;
};
type ComparedCompany = {
  company: CompanyIdentity;
  original: CompanyEvaluation;
  scenario: CompanyEvaluation;
  differences: Record<ModelFamily, number | null>;
  original_rank: number;
  scenario_rank: number;
  personalized_rank: number;
  interpretation: string[];
};
type ComparisonResponse = {
  snapshot_id: string;
  companies: ComparedCompany[];
  changes: ScenarioChange[];
  warnings: string[];
  interpretation: string[];
};
type PairPreset = {
  id: string;
  label: string;
  description: string;
  target: TargetName;
  company_ids: [string, string];
  default_feature: string;
  actions: Record<Direction, { label: string; operation: Operation; value: number }>;
};
type RankingPreset = { company_ids: string[]; target: TargetName; weights: Record<string, number> };
type ReachPreset = { company_id: string; target: TargetName; model_family: ModelFamily; desired_score: number };
type FundConstraints = {
  fund_usd: number;
  max_companies: number;
  max_company_weight: number;
  max_industry_weight: number;
  min_coverage: number;
};
type FundPreset = {
  company_ids: string[];
  target: TargetName;
  adjustments: Adjustment[];
  weights: Record<string, number>;
  constraints: FundConstraints;
  objective: "personalized_index" | "financial_resilience";
  sustainability_eligible_fraction: number;
};
type DemoPresets = { snapshot_id: string; pair_presets: PairPreset[]; reach_preset?: ReachPreset; ranking_preset: RankingPreset; fund_preset: FundPreset };
type TargetSuggestion = {
  feature_name: string;
  label: string;
  category: string;
  unit: string | null;
  current_value: number | null;
  suggested_value: number;
  operation: "set";
  projected_predictions: Record<ModelFamily, number | null>;
  selected_model_change: number;
  absolute_change?: number;
  relative_change_pct?: number | null;
  percentage_point_change?: number | null;
  remaining_gap: number;
  reaches_target: boolean;
  range_position: number | string;
  warning: string | null;
};
type TargetSuggestionResponse = {
  company: CompanyIdentity;
  target: TargetName;
  model_family: ModelFamily;
  original_predictions: Record<ModelFamily, number | null>;
  desired_score: number;
  desired_direction: string;
  suggestions: TargetSuggestion[];
  warnings: string[];
  disclosure: string;
};
type RankedCompany = {
  company: CompanyIdentity;
  raw_ebm_prediction: number;
  personalized_index: number;
  original_rank: number;
  personalized_rank: number;
  rank_change: number;
  category_contributions: Record<string, number>;
  weighted_category_effect: number;
};
type RankingResponse = {
  snapshot_id?: string;
  company_count: number;
  weights: Record<string, number>;
  companies: RankedCompany[];
};
type Allocation = {
  company_cik: string;
  name: string;
  ticker: string | null;
  industry: string | null;
  weight: number;
  amount_usd: number;
  rank: number;
  reason: string;
};
type AllocationResult = {
  allocations: Allocation[];
  allocated_usd: number;
  unallocated_usd: number;
  warnings: string[];
  objective_name?: string;
  objective_method?: string;
  portfolio_objective_score?: number;
};
type PortfolioResponse = { snapshot_id: string; baseline: AllocationResult; scenario: AllocationResult; interpretation: string[] };
type RenewableScenarioCompany = {
  company: CompanyIdentity;
  original_renewable_pct: number | null;
  scenario_renewable_pct: number | null;
  original_score: number;
  scenario_score: number;
  score_change: number;
  original_rank: number;
  scenario_rank: number;
  rank_change: number;
};
type RenewableScenarioSector = {
  sector: string;
  company_count: number;
  original_score_sum: number;
  scenario_score_sum: number;
  original_share_pct: number;
  scenario_share_pct: number;
  share_change_percentage_points: number;
  fund_amount_usd: number;
};
type RenewableScenarioResponse = {
  target: "csa";
  model_family: "ebm";
  snapshot_id: string;
  renewable_multiplier: number;
  step_summary: { observed_count: number; missing_count: number; saturated_count: number; increaseable_count: number };
  companies: RenewableScenarioCompany[];
  sectors: RenewableScenarioSector[];
  total_score_sum: { original: number; scenario: number };
  disclosure: string;
};
type Category = { key: CategoryKey; label: string };
type Direction = "increase" | "decrease";
type DemoName = "compare" | "reach" | "ranking" | "fund";

const CATEGORIES: Category[] = [
  { key: "financial", label: "Financial" },
  { key: "social", label: "People & society" },
  { key: "environmental", label: "Environment" },
  { key: "climate_target", label: "Climate targets" },
  { key: "financial_target", label: "Financial targets" },
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

let run: ComparisonRun | null = null;
let demos: DemoPresets | null = null;
const companiesByTarget = new Map<TargetName, CompanyEvaluation[]>();
let pairScenarioByCik = new Map<string, ComparedCompany>();
let pairParameterTransitions = new Map<string, { featureName: string; before: unknown; after: unknown }>();
let pairChanges: ScenarioChange[] = [];
let pairRequestNumber = 0;
let reachRequestNumber = 0;
let rankingRequestNumber = 0;
let fundRequestNumber = 0;
let rankingWeight = 1;
let rankingTimer: number | undefined;
let renewableMultiplier = 1;
let renewableRequestNumber = 0;
let latestRenewableScenario: RenewableScenarioResponse | null = null;

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    ...init,
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...(init.headers || {}) },
  });
  if (!response.ok) {
    let message = "Request failed with HTTP " + response.status + ".";
    try {
      const value = await response.json() as { detail?: unknown };
      message = typeof value.detail === "string" ? value.detail : value.detail ? JSON.stringify(value.detail) : message;
    } catch { /* Keep the HTTP status if the response is not JSON. */ }
    throw new HttpError(response.status, message);
  }
  return response.json() as Promise<T>;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function numeric(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function formatScore(value: number | null | undefined): string {
  return typeof value === "number" && Number.isFinite(value) ? value.toFixed(2) : "—";
}

function formatDelta(value: number | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "No result";
  if (Math.abs(value) < 1e-12) return "No model change";
  if (Math.abs(value) < 0.005) return value > 0 ? "+<0.01" : "−<0.01";
  return (value > 0 ? "+" : "") + value.toFixed(2);
}

function formatValue(value: unknown, unit: string | null = null): string {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value !== "number") return String(value);
  const formatted = value.toLocaleString(undefined, { maximumFractionDigits: Math.abs(value) >= 100 ? 1 : 2 });
  if (!unit) return formatted;
  if (unit === "%" || unit.toLowerCase() === "percent") return formatted + "%";
  return formatted + " " + unit;
}

function formatMoney(value: number): string {
  return new Intl.NumberFormat(undefined, { style: "currency", currency: "USD", notation: "compact", maximumFractionDigits: 1 }).format(value);
}

function formatWeight(value: number): string {
  return (value * 100).toFixed(1) + "%";
}

function showLogin(message = ""): void {
  element("app-view").hidden = true;
  element("login-view").hidden = false;
  element("login-error").textContent = message;
  element<HTMLInputElement>("viewer-password").focus();
}

function showApp(): void {
  element("login-view").hidden = true;
  element("app-view").hidden = false;
}

function showError(message = ""): void {
  element("page-error").textContent = message;
}

function showStatus(message = ""): void {
  element("page-status").textContent = message;
}

function handleRequestError(error: unknown): void {
  if (error instanceof HttpError && error.status === 401) showLogin("Session expired.");
  else showError(error instanceof Error ? error.message : String(error));
}

function defaultWeights(): Record<CategoryKey, number> {
  return { financial: 1, social: 1, environmental: 1, climate_target: 1, financial_target: 1 };
}

function definitionFor(target: TargetName): TargetDefinition {
  if (!run) throw new Error("The published model is not loaded.");
  return run.targets[target];
}

function fieldsFor(target: TargetName, editableOnly = false): ModelField[] {
  const definition = definitionFor(target);
  const active = new Set(definition.active_features);
  return definition.fields.filter(field => active.has(field.feature_name) && field.category_or_context !== "fixed_context" && (!editableOnly || field.editable));
}

function fieldFor(target: TargetName, featureName: string): ModelField | undefined {
  return fieldsFor(target).find(field => field.feature_name === featureName);
}

function fieldLabel(field: ModelField): string {
  return field.label || field.feature_name.replace(/_/g, " ").replace(/\b\w/g, character => character.toUpperCase());
}

function appendCompanyOptions(select: HTMLSelectElement, companies: CompanyEvaluation[], selected = ""): void {
  select.replaceChildren(...companies.map(item => new Option(item.company.name + (item.company.ticker ? " · " + item.company.ticker : ""), item.company.company_cik)));
  if (companies.some(item => item.company.company_cik === selected)) select.value = selected;
}

function keepPairDistinct(changed: "a" | "b"): void {
  const companyA = element<HTMLSelectElement>("compare-company-a");
  const companyB = element<HTMLSelectElement>("compare-company-b");
  if (companyA.value !== companyB.value) return;
  const target = currentCompareTarget();
  const replacement = (companiesByTarget.get(target) || []).find(company => company.company.company_cik !== companyA.value);
  if (!replacement) return;
  if (changed === "a") companyB.value = replacement.company.company_cik;
  else companyA.value = replacement.company.company_cik;
}

function appendParameterOptions(select: HTMLSelectElement, target: TargetName, selected = ""): void {
  select.replaceChildren();
  for (const category of CATEGORIES) {
    const group = document.createElement("optgroup");
    const pairIds = selectedPairIds();
    const categoryFields = fieldsFor(target, true)
      .filter(field => field.category_or_context === category.key)
      .sort((left, right) => pairObservedCount(target, pairIds, right.feature_name) - pairObservedCount(target, pairIds, left.feature_name) || fieldLabel(left).localeCompare(fieldLabel(right)));
    group.label = category.label;
    for (const field of categoryFields) group.append(new Option(fieldLabel(field), field.feature_name));
    select.append(group);
  }
  if ([...select.options].some(option => option.value === selected)) select.value = selected;
}

function pairObservedCount(target: TargetName, pairIds: [string, string], featureName: string): number {
  return pairIds.filter(cik => numeric(companyEvaluation(target, cik)?.features[featureName]) !== null).length;
}

async function loadCompanies(target: TargetName): Promise<CompanyEvaluation[]> {
  const existing = companiesByTarget.get(target);
  if (existing) return existing;
  const response = await request<CompanyResponse>("/api/model-comparison/companies", {
    method: "POST",
    body: JSON.stringify({ target, search: "", industry: null, filters: [], sort_by: "company_name", direction: "asc", limit: 500 }),
  });
  companiesByTarget.set(target, response.companies);
  return response.companies;
}

function companyEvaluation(target: TargetName, companyCik: string): CompanyEvaluation | undefined {
  return companiesByTarget.get(target)?.find(item => item.company.company_cik === companyCik);
}

function currentPairPreset(): PairPreset {
  if (!demos) throw new Error("Demo presets are not loaded.");
  const id = element<HTMLSelectElement>("pair-preset").value;
  return demos.pair_presets.find(preset => preset.id === id) || demos.pair_presets[0];
}

function currentCompareTarget(): TargetName {
  return element<HTMLSelectElement>("compare-target").value as TargetName;
}

function selectedPairIds(): [string, string] {
  return [element<HTMLSelectElement>("compare-company-a").value, element<HTMLSelectElement>("compare-company-b").value];
}

async function applyPairPreset(): Promise<void> {
  const preset = currentPairPreset();
  const companies = await loadCompanies(preset.target);
  element<HTMLSelectElement>("compare-target").value = preset.target;
  appendCompanyOptions(element<HTMLSelectElement>("compare-company-a"), companies, preset.company_ids[0]);
  appendCompanyOptions(element<HTMLSelectElement>("compare-company-b"), companies, preset.company_ids[1]);
  appendParameterOptions(element<HTMLSelectElement>("compare-parameter"), preset.target, preset.default_feature);
  pairScenarioByCik.clear();
  pairParameterTransitions.clear();
  pairChanges = [];
  renderCompare();
}

async function changeCompareTarget(): Promise<void> {
  const target = currentCompareTarget();
  const previousIds = selectedPairIds();
  const companies = await loadCompanies(target);
  appendCompanyOptions(element<HTMLSelectElement>("compare-company-a"), companies, previousIds[0]);
  appendCompanyOptions(element<HTMLSelectElement>("compare-company-b"), companies, previousIds[1]);
  const preset = currentPairPreset();
  const preferred = fieldFor(target, preset.default_feature)?.editable ? preset.default_feature : "";
  appendParameterOptions(element<HTMLSelectElement>("compare-parameter"), target, preferred);
  pairScenarioByCik.clear();
  pairParameterTransitions.clear();
  pairChanges = [];
  renderCompare();
}

function renderCompare(): void {
  renderPairScores();
  renderCompareButtons();
  renderCompareDetails();
}

function renderPairScores(): void {
  const target = currentCompareTarget();
  const container = element("compare-results");
  container.replaceChildren();
  for (const [position, cik] of selectedPairIds().entries()) {
    const baseline = companyEvaluation(target, cik);
    if (!baseline) continue;
    const compared = pairScenarioByCik.get(cik);
    const card = make("article", "", "score-card");
    const heading = make("header");
    heading.append(make("span", position === 0 ? "Company A" : "Company B"), make("h2", baseline.company.name), make("small", [baseline.company.ticker, baseline.company.industry].filter(Boolean).join(" · ")));
    const scores = make("div", "", "model-scores");
    for (const family of ["ebm", "catboost"] as const) {
      const score = make("div", "", "model-score");
      const modelLabel = family === "ebm" ? "EBM" : "CatBoost";
      if (!compared) score.append(make("span", modelLabel, "model-current-label"), make("strong", formatScore(baseline.predictions[family]), "model-current-value"));
      else {
        const delta = compared.differences[family];
        score.append(
          make("span", modelLabel + " change", "model-change-label"),
          make("strong", formatDelta(delta), "model-change-value " + ((delta || 0) > 0 ? "positive" : (delta || 0) < 0 ? "negative" : "neutral")),
          make("small", formatScore(compared.original.predictions[family]) + " → " + formatScore(compared.scenario.predictions[family]), "model-score-transition"),
        );
      }
      scores.append(score);
    }
    if (!compared && pairScenarioByCik.size) card.append(make("p", "Unchanged in this scenario", "unchanged"));
    card.prepend(heading);
    card.append(scores);
    const parameter = fieldFor(target, element<HTMLSelectElement>("compare-parameter").value);
    if (parameter) {
      const transition = pairParameterTransitions.get(cik);
      const currentValue = compared?.scenario.features[parameter.feature_name] ?? baseline.features[parameter.feature_name];
      const text = transition && transition.featureName === parameter.feature_name
        ? fieldLabel(parameter) + ": " + formatValue(transition.before, parameter.canonical_unit) + " → " + formatValue(transition.after, parameter.canonical_unit)
        : fieldLabel(parameter) + ": " + formatValue(currentValue, parameter.canonical_unit);
      card.append(make("p", text, "current-parameter"));
    }
    container.append(card);
  }
}

function currentComparisonRule(direction: Direction): Adjustment {
  const target = currentCompareTarget();
  const featureName = element<HTMLSelectElement>("compare-parameter").value;
  const field = fieldFor(target, featureName);
  if (!field) throw new Error("Choose an active model parameter.");
  const preset = currentPairPreset();
  if (featureName === preset.default_feature && target === preset.target) {
    const action = preset.actions[direction];
    return { feature_name: featureName, operation: action.operation, value: action.value };
  }
  const values = selectedPairIds().map(cik => numeric(companyEvaluation(target, cik)?.features[featureName])).filter((value): value is number => value !== null);
  if (!values.length) {
    const value = direction === "increase" ? (field.canonical_unit === "%" ? 100 : 1) : 0;
    return { feature_name: featureName, operation: "set", value };
  }
  const minimum = Math.min(...values);
  const maximum = Math.max(...values);
  if (field.canonical_unit === "%" || field.canonical_unit?.toLowerCase() === "percent") {
    return { feature_name: featureName, operation: "set", value: direction === "increase" ? Math.min(100, maximum + 20) : Math.max(0, minimum - 20) };
  }
  const value = direction === "increase" ? maximum + Math.max(Math.abs(maximum) * .5, 1) : minimum - Math.max(Math.abs(minimum) * .5, 1);
  return { feature_name: featureName, operation: "set", value };
}

function ruleLabel(rule: Adjustment): string {
  const field = fieldFor(currentCompareTarget(), rule.feature_name);
  const value = formatValue(rule.value, field?.canonical_unit || null);
  if (rule.operation === "set") return "Set to " + value;
  if (rule.operation === "percent") return (rule.value >= 0 ? "+" : "") + value;
  return (rule.value >= 0 ? "+" : "") + value;
}

function renderCompareButtons(): void {
  const field = fieldFor(currentCompareTarget(), element<HTMLSelectElement>("compare-parameter").value);
  if (!field) return;
  element("compare-decrease").querySelector("small")!.textContent = stepLabel(field, "decrease");
  element("compare-increase").querySelector("small")!.textContent = stepLabel(field, "increase");
  element<HTMLButtonElement>("compare-decrease").disabled = !canStepPair("decrease");
  element<HTMLButtonElement>("compare-increase").disabled = !canStepPair("increase");
  element<HTMLButtonElement>("compare-reset").disabled = pairScenarioByCik.size === 0;
}

function isPercentField(field: ModelField): boolean {
  return field.canonical_unit === "%" || field.canonical_unit?.toLowerCase() === "percent";
}

function isYearField(field: ModelField): boolean {
  return field.feature_name.endsWith("_year") || field.canonical_unit?.toLowerCase() === "year";
}

function stepLabel(field: ModelField, direction: Direction): string {
  if (isPercentField(field)) return direction === "increase" ? "+25%" : "−25%";
  if (isYearField(field)) return direction === "increase" ? "+10 years" : "−10 years";
  return direction === "increase" ? "+50% of current value" : "−50% of current value";
}

function currentPairEvaluation(target: TargetName, cik: string): CompanyEvaluation | undefined {
  return pairScenarioByCik.get(cik)?.scenario || companyEvaluation(target, cik);
}

function nextStepValue(field: ModelField, current: number | null, direction: Direction, observed: number[]): number | null {
  if (current === null) {
    if (!observed.length) return null;
    current = direction === "increase" ? Math.max(...observed) : Math.min(...observed);
  }
  if (isPercentField(field)) return Math.max(0, Math.min(100, current + (direction === "increase" ? 25 : -25)));
  if (isYearField(field)) return Math.max(1900, Math.min(2200, current + (direction === "increase" ? 10 : -10)));
  if (current > 0) return direction === "increase" ? current * 1.5 : current * .5;
  if (current < 0) return direction === "increase" ? current * .5 : current * 1.5;
  const scale = Math.max(1, ...observed.map(value => Math.abs(value)));
  return direction === "increase" ? scale * .5 : -scale * .5;
}

function stepPlan(cik: string, direction: Direction): { before: number | null; after: number | null } {
  const target = currentCompareTarget();
  const field = fieldFor(target, element<HTMLSelectElement>("compare-parameter").value);
  if (!field) return { before: null, after: null };
  const observed = selectedPairIds().map(id => numeric(currentPairEvaluation(target, id)?.features[field.feature_name])).filter((value): value is number => value !== null);
  const before = numeric(currentPairEvaluation(target, cik)?.features[field.feature_name]);
  return { before, after: nextStepValue(field, before, direction, observed) };
}

function canStepPair(direction: Direction): boolean {
  return affectedPairIds().some(cik => {
    const plan = stepPlan(cik, direction);
    return plan.after !== null && !Object.is(plan.before, plan.after);
  });
}

function hasVisibleStepResponse(company: ComparedCompany, previous: CompanyEvaluation): boolean {
  return (["ebm", "catboost"] as const).some(family => {
    const before = previous.predictions[family];
    const after = company.scenario.predictions[family];
    return typeof before === "number" && typeof after === "number" && Math.abs(after - before) >= .005;
  });
}

async function compareOneCompanyAtValue(cik: string, field: ModelField, value: number): Promise<{ company: ComparedCompany; changes: ScenarioChange[] }> {
  if (!run) throw new Error("The published model is not loaded.");
  const response = await request<ComparisonResponse>("/api/model-comparison/compare", {
    method: "POST",
    body: JSON.stringify({ target: currentCompareTarget(), company_ids: [cik], adjustments: [{ feature_name: field.feature_name, operation: "set", value }], weights: defaultWeights(), snapshot_id: run.snapshot_id, preview_only: false }),
  });
  if (response.snapshot_id !== run.snapshot_id || !response.companies[0]) throw new Error("The comparison response does not match the current model snapshot.");
  return { company: response.companies[0], changes: response.changes };
}

async function runCompanyStep(cik: string, field: ModelField, direction: Direction): Promise<{ company: ComparedCompany; changes: ScenarioChange[]; before: unknown; after: unknown } | null> {
  const target = currentCompareTarget();
  const initial = currentPairEvaluation(target, cik);
  if (!initial) return null;
  const initialValue = numeric(initial.features[field.feature_name]);
  let currentValue = initialValue;
  let response: { company: ComparedCompany; changes: ScenarioChange[] } | null = null;
  for (let attempt = 0; attempt < 8; attempt += 1) {
    const observed = selectedPairIds().map(id => numeric(currentPairEvaluation(target, id)?.features[field.feature_name])).filter((value): value is number => value !== null);
    const next = nextStepValue(field, currentValue, direction, observed);
    if (next === null || Object.is(next, currentValue)) break;
    response = await compareOneCompanyAtValue(cik, field, next);
    currentValue = numeric(response.company.scenario.features[field.feature_name]);
    if (hasVisibleStepResponse(response.company, initial) || currentValue === null) break;
  }
  return response ? { ...response, before: initialValue, after: response.company.scenario.features[field.feature_name] } : null;
}

async function runCumulativePairStep(direction: Direction): Promise<void> {
  const field = fieldFor(currentCompareTarget(), element<HTMLSelectElement>("compare-parameter").value);
  if (!field || !run) return;
  const ids = affectedPairIds().filter(cik => {
    const plan = stepPlan(cik, direction);
    return plan.after !== null && !Object.is(plan.before, plan.after);
  });
  if (!ids.length) { showStatus("The selected companies are already at this parameter’s semantic bound."); return; }
  const requestNumber = ++pairRequestNumber;
  element<HTMLButtonElement>("compare-decrease").disabled = true;
  element<HTMLButtonElement>("compare-increase").disabled = true;
  showStatus((direction === "increase" ? "Increasing " : "Decreasing ") + fieldLabel(field) + "…");
  try {
    const results = await Promise.all(ids.map(cik => runCompanyStep(cik, field, direction)));
    if (requestNumber !== pairRequestNumber) return;
    pairChanges = [];
    const transitions: string[] = [];
    for (const result of results) {
      if (!result) continue;
      const cik = result.company.company.company_cik;
      pairScenarioByCik.set(cik, result.company);
      pairParameterTransitions.set(cik, { featureName: field.feature_name, before: result.before, after: result.after });
      pairChanges.push(...result.changes);
      transitions.push(result.company.company.name + " " + formatValue(result.before, field.canonical_unit) + " → " + formatValue(result.after, field.canonical_unit));
    }
    renderCompare();
    showError();
    showStatus(fieldLabel(field) + " updated: " + transitions.join("; ") + ".");
  } catch (error) {
    if (requestNumber === pairRequestNumber) handleRequestError(error);
  } finally { renderCompareButtons(); }
}

function resetPairScenario(message = true): void {
  pairRequestNumber += 1;
  pairScenarioByCik.clear();
  pairParameterTransitions.clear();
  pairChanges = [];
  renderCompare();
  if (message) showStatus("Comparison reset to published values.");
}

function affectedPairIds(): string[] {
  const [companyA, companyB] = selectedPairIds();
  const selected = element<HTMLSelectElement>("compare-apply-to").value;
  return selected === "a" ? [companyA] : selected === "b" ? [companyB] : [companyA, companyB];
}

async function runPairScenario(direction: Direction, suppliedRule?: Adjustment, suppliedIds?: string[]): Promise<void> {
  if (!run) return;
  const target = currentCompareTarget();
  const adjustment = suppliedRule || currentComparisonRule(direction);
  const ids = suppliedIds || affectedPairIds();
  const signature = JSON.stringify({ target, pair: selectedPairIds(), ids, adjustment });
  const requestNumber = ++pairRequestNumber;
  showStatus("Running the two-company scenario…");
  try {
    const response = await request<ComparisonResponse>("/api/model-comparison/compare", {
      method: "POST",
      body: JSON.stringify({ target, company_ids: ids, adjustments: [adjustment], weights: defaultWeights(), snapshot_id: run.snapshot_id, preview_only: false }),
    });
    const currentSignature = JSON.stringify({ target: currentCompareTarget(), pair: selectedPairIds(), ids: suppliedIds || affectedPairIds(), adjustment: suppliedRule || currentComparisonRule(direction) });
    if (requestNumber !== pairRequestNumber || signature !== currentSignature || response.snapshot_id !== run.snapshot_id) return;
    pairScenarioByCik = new Map(response.companies.map(company => [company.company.company_cik, company]));
    pairChanges = response.changes;
    pairParameterTransitions.clear();
    for (const change of response.changes) {
      pairParameterTransitions.set(change.company_cik, { featureName: change.feature_name, before: change.original_value, after: change.scenario_value });
    }
    renderCompare();
    const skipped = response.changes.filter(change => change.status !== "applied").length;
    showError();
    showStatus("Scenario scores updated" + (skipped ? "; " + skipped + " changes were skipped because values are missing." : "."));
  } catch (error) {
    if (requestNumber === pairRequestNumber) handleRequestError(error);
    showStatus();
  }
}

function renderCompareDetails(): void {
  const target = currentCompareTarget();
  const container = element("compare-detail-content");
  container.replaceChildren();
  for (const cik of selectedPairIds()) {
    const evaluation = pairScenarioByCik.get(cik)?.scenario || companyEvaluation(target, cik);
    const original = pairScenarioByCik.get(cik)?.original || evaluation;
    if (!evaluation || !original) continue;
    const section = make("section");
    section.append(make("h3", evaluation.company.name));
    for (const category of CATEGORIES) {
      const categoryFields = fieldsFor(target).filter(field => field.category_or_context === category.key);
      if (!categoryFields.length) continue;
      section.append(make("h4", category.label));
      const list = make("dl", "", "field-list");
      for (const field of categoryFields) {
        const before = original.features[field.feature_name];
        const after = evaluation.features[field.feature_name];
        const value = Object.is(before, after) ? formatValue(after, field.canonical_unit) : formatValue(before, field.canonical_unit) + " → " + formatValue(after, field.canonical_unit);
        const description = make("dd");
        description.append(make("span", value));
        const evidence = make("button", "Evidence", "evidence-button");
        evidence.type = "button";
        evidence.onclick = () => openEvidence(original, field);
        description.append(evidence);
        list.append(make("dt", fieldLabel(field)), description);
      }
      section.append(list);
    }
    container.append(section);
  }
}

async function prepareReachDemo(target: TargetName, companyCik = ""): Promise<void> {
  const companies = await loadCompanies(target);
  element<HTMLSelectElement>("reach-target").value = target;
  const fallback = currentPairPreset().company_ids[0];
  appendCompanyOptions(element<HTMLSelectElement>("reach-company"), companies, companyCik || fallback);
  setReachDesiredScore();
  element("reach-suggestions").replaceChildren();
}

function setReachDesiredScore(): void {
  const target = element<HTMLSelectElement>("reach-target").value as TargetName;
  const cik = element<HTMLSelectElement>("reach-company").value;
  const family = element<HTMLSelectElement>("reach-model").value as ModelFamily;
  const current = companyEvaluation(target, cik)?.predictions[family];
  if (typeof current === "number") element<HTMLInputElement>("reach-score").value = String(Math.min(100, Math.ceil(current + 5)));
}

async function findTargetSuggestions(): Promise<void> {
  if (!run) return;
  const target = element<HTMLSelectElement>("reach-target").value as TargetName;
  const companyId = element<HTMLSelectElement>("reach-company").value;
  const modelFamily = element<HTMLSelectElement>("reach-model").value as ModelFamily;
  const desiredScore = Number(element<HTMLInputElement>("reach-score").value);
  if (!Number.isFinite(desiredScore)) { showError("Enter a desired score."); return; }
  const signature = JSON.stringify({ target, companyId, modelFamily, desiredScore });
  const requestNumber = ++reachRequestNumber;
  element("reach-suggestions").replaceChildren(make("p", "Searching saved-model sensitivity…", "empty-result"));
  try {
    const response = await request<TargetSuggestionResponse>("/api/model-comparison/target-suggestions", {
      method: "POST",
      body: JSON.stringify({ target, company_id: companyId, desired_score: desiredScore, model_family: modelFamily, snapshot_id: run.snapshot_id, max_suggestions: 3 }),
    });
    const currentSignature = JSON.stringify({ target: element<HTMLSelectElement>("reach-target").value, companyId: element<HTMLSelectElement>("reach-company").value, modelFamily: element<HTMLSelectElement>("reach-model").value, desiredScore: Number(element<HTMLInputElement>("reach-score").value) });
    if (requestNumber !== reachRequestNumber || signature !== currentSignature || response.target !== target) return;
    renderTargetSuggestions(response);
    showError();
  } catch (error) {
    if (requestNumber === reachRequestNumber) handleRequestError(error);
  }
}

function renderTargetSuggestions(response: TargetSuggestionResponse): void {
  const container = element("reach-suggestions");
  container.replaceChildren();
  element("reach-disclosure").textContent = response.suggestions.length + " options";
  if (!response.suggestions.length) container.append(make("p", "No one-parameter suggestion was found for this desired score.", "empty-result"));
  for (const suggestion of response.suggestions) {
    const card = make("article", "", "suggestion-card");
    const heading = make("header");
    heading.append(make("h2", suggestion.label));
    const otherFamily: ModelFamily = response.model_family === "ebm" ? "catboost" : "ebm";
    const otherChange = (suggestion.projected_predictions[otherFamily] || 0) - (response.original_predictions[otherFamily] || 0);
    const selectedDirection = Math.sign(suggestion.selected_model_change);
    const otherDirection = Math.sign(otherChange);
    const directionLabel = !otherDirection ? "no change" : otherDirection === selectedDirection ? "same direction" : "opposite direction";
    const metrics = make("div", "", "suggestion-metrics");
    metrics.append(
      suggestionMetric("Required change", requiredParameterChange(suggestion), "required-change"),
      suggestionMetric(response.model_family.toUpperCase() + " change", formatDelta(suggestion.selected_model_change), "suggested-score-change"),
      suggestionMetric("Projected score", formatScore(suggestion.projected_predictions[response.model_family]) + (suggestion.reaches_target ? " · reaches target" : " · gap " + formatScore(Math.abs(suggestion.remaining_gap))), "projected-score"),
    );
    const apply = make("button", "Apply to comparison");
    apply.type = "button";
    apply.onclick = () => void applySuggestionToComparison(response, suggestion);
    const details = make("details", "", "suggestion-details");
    const detailContent = make("div");
    detailContent.append(
      make("p", "Parameter value: " + formatValue(suggestion.current_value, suggestion.unit) + " → " + formatValue(suggestion.suggested_value, suggestion.unit) + "."),
      make("p", (otherFamily === "ebm" ? "EBM" : "CatBoost") + ": " + formatScore(response.original_predictions[otherFamily]) + " → " + formatScore(suggestion.projected_predictions[otherFamily]) + " (" + directionLabel + ")."),
      make("p", "Training range position: " + formatRangePosition(suggestion.range_position) + ". Direction: " + response.desired_direction + "."),
    );
    if (suggestion.warning) detailContent.append(make("p", suggestion.warning, "warning"));
    detailContent.append(make("p", response.disclosure));
    details.append(make("summary", "Details"), detailContent);
    card.append(heading, metrics, apply, details);
    container.append(card);
  }
  if (response.warnings.length) {
    const details = make("details", "", "reach-details");
    const warnings = make("ul", "", "warning-list");
    for (const warning of response.warnings) warnings.append(make("li", warning));
    details.append(make("summary", "More details"), warnings);
    container.append(details);
  }
}

function suggestionMetric(label: string, value: string, className: string): HTMLElement {
  const metric = make("div", "", "suggestion-metric " + className);
  metric.append(make("span", label), make("strong", value));
  return metric;
}

function requiredParameterChange(suggestion: TargetSuggestion): string {
  if (typeof suggestion.percentage_point_change === "number" && Number.isFinite(suggestion.percentage_point_change)) {
    return signedAmount(suggestion.percentage_point_change, "%", 1);
  }
  if (typeof suggestion.relative_change_pct === "number" && Number.isFinite(suggestion.relative_change_pct)) {
    return signedAmount(suggestion.relative_change_pct, "%", 0);
  }
  const absolute = typeof suggestion.absolute_change === "number"
    ? suggestion.absolute_change
    : typeof suggestion.current_value === "number"
      ? suggestion.suggested_value - suggestion.current_value
      : suggestion.suggested_value;
  return signedAmount(absolute, suggestion.unit ? " " + suggestion.unit : "", Math.abs(absolute) >= 100 ? 0 : 1);
}

function signedAmount(value: number, suffix: string, maximumFractionDigits: number): string {
  return (value > 0 ? "+" : value < 0 ? "−" : "") + Math.abs(value).toLocaleString(undefined, { maximumFractionDigits }) + suffix;
}

function formatRangePosition(value: number | string): string {
  return typeof value === "number" && Number.isFinite(value)
    ? (value * 100).toFixed(1) + "% of training range"
    : String(value);
}

async function applySuggestionToComparison(response: TargetSuggestionResponse, suggestion: TargetSuggestion): Promise<void> {
  showDemo("compare");
  element<HTMLSelectElement>("compare-target").value = response.target;
  await changeCompareTarget();
  const companies = companiesByTarget.get(response.target) || [];
  appendCompanyOptions(element<HTMLSelectElement>("compare-company-a"), companies, response.company.company_cik);
  const companyB = element<HTMLSelectElement>("compare-company-b");
  if (companyB.value === response.company.company_cik) {
    const alternative = companies.find(company => company.company.company_cik !== response.company.company_cik);
    if (alternative) companyB.value = alternative.company.company_cik;
  }
  appendParameterOptions(element<HTMLSelectElement>("compare-parameter"), response.target, suggestion.feature_name);
  element<HTMLSelectElement>("compare-apply-to").value = "a";
  pairScenarioByCik.clear();
  pairParameterTransitions.clear();
  renderCompare();
  await runPairScenario("increase", { feature_name: suggestion.feature_name, operation: "set", value: suggestion.suggested_value }, [response.company.company_cik]);
}

async function runRanking(weight: number): Promise<void> {
  if (!run) return;
  if (!Number.isFinite(weight) || weight < 0 || weight > 3) {
    showError("Environmental weight must be a number from 0 to 3.");
    return;
  }
  rankingWeight = weight;
  const signature = JSON.stringify({ weight, snapshot: run.snapshot_id });
  const requestNumber = ++rankingRequestNumber;
  element("ranking-results").replaceChildren(make("p", "Calculating 500 company ranks…", "empty-result"));
  try {
    const response = await request<RankingResponse>("/api/model-comparison/rank", {
      method: "POST",
      body: JSON.stringify({ target: "esg", weights: { environmental: weight }, snapshot_id: run.snapshot_id }),
    });
    if (requestNumber !== rankingRequestNumber || signature !== JSON.stringify({ weight: rankingWeight, snapshot: run.snapshot_id })) return;
    if (response.snapshot_id && response.snapshot_id !== run.snapshot_id) return;
    renderRanking(response);
    showError();
  } catch (error) {
    if (requestNumber === rankingRequestNumber) handleRequestError(error);
  }
}

function renderRanking(response: RankingResponse): void {
  element("ranking-count").textContent = response.company_count.toLocaleString() + " companies";
  const aflac = response.companies.find(item => item.company.ticker === "AFL");
  const focus = element("ranking-focus");
  if (aflac) {
    const movement = !aflac.rank_change ? "—" : aflac.rank_change > 0 ? "↑" + aflac.rank_change : "↓" + Math.abs(aflac.rank_change);
    const indexChange = aflac.personalized_index - aflac.raw_ebm_prediction;
    focus.replaceChildren(
      make("strong", "Aflac (AFL)"),
      rankingFocusMetric("Original rank", "#" + aflac.original_rank),
      rankingFocusMetric("New rank", "#" + aflac.personalized_rank),
      rankingFocusMetric("Move", movement),
      rankingFocusMetric("Index change", compactSigned(indexChange) + "%"),
    );
  } else {
    focus.replaceChildren();
  }
  const container = element("ranking-results");
  container.replaceChildren();
  const table = make("table");
  const head = make("thead");
  const headingRow = make("tr");
  for (const label of ["New rank", "Company", "Before", "Move", "Index change"]) headingRow.append(make("th", label));
  head.append(headingRow);
  const body = make("tbody");
  for (const item of [...response.companies].sort((left, right) => left.personalized_rank - right.personalized_rank)) {
    const movement = item.rank_change;
    const company = make("td");
    company.append(make("strong", item.company.name), make("small", item.company.ticker || ""));
    const move = !movement ? "—" : movement > 0 ? "↑" + movement : "↓" + Math.abs(movement);
    const effect = numeric(item.weighted_category_effect);
    const row = make("tr");
    row.dataset.companyCik = item.company.company_cik;
    if (item.company.ticker === "AFL") row.classList.add("ranking-example");
    row.append(make("td", "#" + item.personalized_rank), company, make("td", "#" + item.original_rank), make("td", move), make("td", effect === null ? "—" : (effect >= 0 ? "+" : "") + effect.toFixed(2) + "%"));
    body.append(row);
  }
  table.append(head, body);
  container.append(table);
}

function rankingFocusMetric(label: string, value: string): HTMLSpanElement {
  const metric = make("span", label);
  metric.append(make("b", value));
  return metric;
}

function scheduleRanking(): void {
  window.clearTimeout(rankingTimer);
  const input = element<HTMLInputElement>("ranking-weight");
  const value = Number(input.value);
  if (!Number.isFinite(value) || value < 0 || value > 3) {
    showError("Environmental weight must be a number from 0 to 3.");
    return;
  }
  rankingRequestNumber += 1;
  rankingTimer = window.setTimeout(() => void runRanking(value), 200);
}

function renderFundPreset(): void {
  if (!demos) return;
  const container = element("fund-preset-summary");
  container.replaceChildren();
  const assumptions = make("div", "", "assumptions");
  for (const text of [
    "Objective: profitability + cash-flow strength",
    "Net zero: renewable 100% · Scope 1 −50%",
    "Constraints: Environment + Climate 3× · diversified",
  ]) assumptions.append(make("strong", text));
  container.append(assumptions);
}

async function runFundCase(): Promise<void> {
  if (!run || !demos) return;
  const preset = demos.fund_preset;
  const signature = JSON.stringify({ preset, snapshot: run.snapshot_id });
  const requestNumber = ++fundRequestNumber;
  const button = element<HTMLButtonElement>("fund-run");
  button.disabled = true;
  element("fund-results").replaceChildren(make("p", "Running saved models and constraints…", "empty-result"));
  try {
    const response = await request<PortfolioResponse>("/api/model-comparison/portfolio", {
      method: "POST",
      body: JSON.stringify({ target: preset.target, company_ids: preset.company_ids, adjustments: preset.adjustments, weights: { ...defaultWeights(), ...preset.weights }, constraints: preset.constraints, objective: preset.objective, sustainability_eligible_fraction: preset.sustainability_eligible_fraction, snapshot_id: run.snapshot_id, preview_only: false }),
    });
    if (requestNumber !== fundRequestNumber || signature !== JSON.stringify({ preset: demos.fund_preset, snapshot: run.snapshot_id }) || response.snapshot_id !== run.snapshot_id) return;
    renderFundResults(response);
    showError();
  } catch (error) {
    if (requestNumber === fundRequestNumber) handleRequestError(error);
  } finally { button.disabled = false; }
}

function renderFundResults(response: PortfolioResponse): void {
  const container = element("fund-results");
  container.replaceChildren();
  const baselineScore = response.baseline.portfolio_objective_score;
  const scenarioScore = response.scenario.portfolio_objective_score;
  const scoreChange = typeof baselineScore === "number" && typeof scenarioScore === "number"
    ? " · financial resilience " + baselineScore.toFixed(2) + " → " + scenarioScore.toFixed(2)
    : "";
  const total = make("p", formatMoney(response.scenario.allocated_usd) + " allocated · " + formatMoney(response.scenario.unallocated_usd) + " cash" + scoreChange, "fund-total");
  const baselineIds = new Set(response.baseline.allocations.map(item => item.company_cik));
  const scenarioIds = new Set(response.scenario.allocations.map(item => item.company_cik));
  const changes = make("div", "", "holding-changes");
  changes.append(changeList("Leaves portfolio", response.baseline.allocations.filter(item => !scenarioIds.has(item.company_cik))), changeList("Enters portfolio", response.scenario.allocations.filter(item => !baselineIds.has(item.company_cik))));
  const scenarioSection = make("section", "", "scenario-holdings");
  scenarioSection.append(make("h2", "Scenario portfolio"));
  const scenarioList = make("ol");
  for (const allocation of response.scenario.allocations) {
    const item = make("li");
    item.append(make("strong", allocation.name), make("span", formatMoney(allocation.amount_usd)));
    scenarioList.append(item);
  }
  scenarioSection.append(scenarioList);
  const details = make("details", "", "fund-details");
  const detailBody = make("div");
  detailBody.append(make("h3", "Baseline holdings"), detailedHoldingList(response.baseline.allocations), make("h3", "Scenario allocation reasons"), detailedHoldingList(response.scenario.allocations));
  const preset = demos!.fund_preset;
  detailBody.append(make("h3", "Constraints"), make("p", preset.constraints.max_companies + " holdings maximum; " + formatWeight(preset.constraints.max_company_weight) + " company cap; " + formatWeight(preset.constraints.max_industry_weight) + " industry cap; " + formatWeight(preset.constraints.min_coverage) + " minimum disclosure coverage."));
  if (response.scenario.objective_method) detailBody.append(make("h3", response.scenario.objective_name || "Financial objective"), make("p", response.scenario.objective_method));
  const notes = [...response.interpretation, ...response.baseline.warnings.map(value => "Baseline: " + value), ...response.scenario.warnings.map(value => "Scenario: " + value)];
  if (notes.length) {
    const list = make("ul", "", "warning-list");
    for (const note of notes) list.append(make("li", note));
    detailBody.append(make("h3", "Notes"), list);
  }
  details.append(make("summary", "Details"), detailBody);
  container.append(total, changes, scenarioSection, details);
}

function changeList(label: string, allocations: Allocation[]): HTMLElement {
  const section = make("section");
  section.append(make("h2", label));
  const list = make("div", "", "change-chips");
  if (!allocations.length) list.append(make("span", "None", "empty-chip"));
  else for (const allocation of allocations) list.append(make("span", allocation.name));
  section.append(list);
  return section;
}

function detailedHoldingList(allocations: Allocation[]): HTMLOListElement {
  const list = make("ol", "", "detailed-holdings");
  for (const allocation of allocations) {
    const item = make("li");
    item.append(make("strong", allocation.name + " · " + formatMoney(allocation.amount_usd)), make("p", allocation.reason));
    list.append(item);
  }
  return list;
}

async function runRenewableScenario(multiplier: number): Promise<void> {
  if (!run) return;
  const requestNumber = ++renewableRequestNumber;
  const increase = element<HTMLButtonElement>("renewable-increase");
  const reset = element<HTMLButtonElement>("renewable-reset");
  const computePortfolio = element<HTMLButtonElement>("compute-portfolio");
  increase.disabled = true;
  reset.disabled = true;
  computePortfolio.disabled = true;
  element("renewable-company-count").textContent = "Loading…";
  try {
    const response = await request<RenewableScenarioResponse>("/api/model-comparison/renewable-scenario", {
      method: "POST",
      body: JSON.stringify({ target: "csa", model_family: "ebm", renewable_multiplier: multiplier, snapshot_id: run.snapshot_id }),
    });
    if (requestNumber !== renewableRequestNumber || response.snapshot_id !== run.snapshot_id || Math.abs(response.renewable_multiplier - multiplier) > 1e-9) return;
    renewableMultiplier = response.renewable_multiplier;
    renderRenewableScenario(response);
    showError();
  } catch (error) {
    if (requestNumber === renewableRequestNumber) handleRequestError(error);
  } finally {
    if (requestNumber === renewableRequestNumber) {
      increase.disabled = false;
      reset.disabled = Math.abs(renewableMultiplier - 1) < 1e-9;
      computePortfolio.disabled = latestRenewableScenario === null;
    }
  }
}

function renderRenewableScenario(response: RenewableScenarioResponse): void {
  latestRenewableScenario = response;
  const cumulative = (response.renewable_multiplier - 1) * 100;
  const cumulativeElement = element("renewable-cumulative");
  cumulativeElement.textContent = formatCumulativePercent(cumulative);
  cumulativeElement.title = cumulative.toString() + "%";
  element("renewable-company-count").textContent = response.companies.length.toLocaleString() + " companies";
  const body = element("renewable-ranking").querySelector<HTMLTableSectionElement>("tbody")!;
  body.replaceChildren();
  for (const item of response.companies) {
    const row = make("tr");
    row.dataset.companyCik = item.company.company_cik;
    if (item.score_change < 0) row.classList.add("negative-score");
    const company = make("td");
    company.append(make("strong", item.company.name), make("small", item.company.ticker || ""));
    const renewable = formatValue(item.original_renewable_pct, "%") + " → " + formatValue(item.scenario_renewable_pct, "%");
    const scoreChange = make("td", compactSigned(item.score_change), item.score_change > 0 ? "positive" : item.score_change < 0 ? "negative" : "neutral");
    const move = !item.rank_change ? "—" : item.rank_change > 0 ? "↑" + item.rank_change : "↓" + Math.abs(item.rank_change);
    row.append(make("td", "#" + item.scenario_rank), company, make("td", renewable), scoreChange, make("td", move));
    body.append(row);
  }
  const portfolioDialog = element<HTMLDialogElement>("portfolio-dialog");
  if (portfolioDialog.open) renderSectorShifts(response.sectors);
  const detail = element("renewable-details").querySelector("div")!;
  const summary = make("dl", "", "summary-list");
  summary.append(
    make("dt", "Observed values"), make("dd", response.step_summary.observed_count.toLocaleString()),
    make("dt", "Missing values"), make("dd", response.step_summary.missing_count.toLocaleString()),
    make("dt", "At 100%"), make("dd", response.step_summary.saturated_count.toLocaleString()),
    make("dt", "CSA EBM score sum"), make("dd", response.total_score_sum.original.toFixed(2) + " → " + response.total_score_sum.scenario.toFixed(2)),
  );
  detail.replaceChildren(summary, make("p", response.disclosure));
}

function formatCumulativePercent(value: number): string {
  const sign = value > 0 ? "+" : value < 0 ? "−" : "";
  const absolute = Math.abs(value);
  if (absolute < 1_000_000) return sign + absolute.toLocaleString(undefined, { maximumFractionDigits: 1 }) + "%";
  if (absolute < 1_000_000_000_000) {
    return sign + new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 }).format(absolute) + "%";
  }
  return sign + absolute.toExponential(1).replace("e+", "e") + "%";
}

function compactSigned(value: number): string {
  if (Math.abs(value) < .005) return "0.00";
  return (value > 0 ? "+" : "") + value.toFixed(2);
}

function renderSectorShifts(sectors: RenewableScenarioSector[]): void {
  const chart = element("sector-shifts");
  chart.replaceChildren();
  const colors = ["#1f5d3a", "#447f58", "#6b9b66", "#96ad6b", "#c1b764", "#d39a58", "#c87451", "#a65350", "#76506e", "#58658a", "#427f89"];
  let start = 0;
  const slices = sectors.map((sector, index) => {
    const end = index === sectors.length - 1 ? 100 : start + sector.scenario_share_pct;
    const slice = colors[index % colors.length] + " " + start.toFixed(6) + "% " + end.toFixed(6) + "%";
    start = end;
    return slice;
  });
  const pie = make("div", "", "sector-pie");
  pie.setAttribute("role", "img");
  pie.setAttribute("aria-label", "Sector portfolio: " + sectors.map(sector => sector.sector + " " + sector.scenario_share_pct.toFixed(1) + "%").join(", "));
  pie.style.background = "conic-gradient(from -90deg, " + slices.join(", ") + ")";
  const legend = make("div", "", "sector-legend");
  sectors.forEach((sector, index) => {
    const row = make("div", "", "sector-legend-row");
    const swatch = make("span", "", "sector-swatch");
    swatch.style.backgroundColor = colors[index % colors.length];
    const label = make("span", "", "sector-legend-label");
    label.append(make("strong", sector.sector), make("small", sector.company_count + " companies"));
    const values = make("span", "", "sector-legend-values");
    const shift = sector.share_change_percentage_points;
    const details = make("div");
    details.append(make("span", compactPercentShift(shift), shift > 0 ? "positive" : shift < 0 ? "negative" : "neutral"), make("span", formatMoney(sector.fund_amount_usd)));
    values.append(make("strong", sector.scenario_share_pct.toFixed(1) + "%"), details);
    row.append(swatch, label, values);
    legend.append(row);
  });
  chart.append(pie, legend);
}

function compactPercentShift(value: number): string {
  if (value > 0 && value < .005) return "+<0.01%";
  if (value < 0 && value > -.005) return "−<0.01%";
  return (value > 0 ? "+" : "") + value.toFixed(2) + "%";
}

function openRenewablePortfolio(): void {
  if (!latestRenewableScenario) return;
  renderSectorShifts(latestRenewableScenario.sectors);
  const dialog = element<HTMLDialogElement>("portfolio-dialog");
  if (!dialog.open) dialog.showModal();
}

function increaseRenewableScenario(): void {
  const step = Number(element<HTMLInputElement>("renewable-step").value);
  if (!Number.isFinite(step) || step <= 0) {
    showError("Enter a positive percentage.");
    return;
  }
  const nextMultiplier = renewableMultiplier * (1 + step / 100);
  void runRenewableScenario(Number.isFinite(nextMultiplier) ? nextMultiplier : Number.MAX_VALUE);
}

function resetRenewableScenario(): void {
  void runRenewableScenario(1);
}

function showDemo(name: DemoName): void {
  for (const candidate of ["compare", "reach", "ranking", "fund"] as const) {
    const active = candidate === name;
    element("demo-" + candidate).hidden = !active;
    const tab = element("demo-tab-" + candidate);
    tab.setAttribute("aria-selected", String(active));
  }
  showError();
  showStatus();
  if (name === "ranking" && !element("ranking-results").children.length) void runRanking(Number(element<HTMLInputElement>("ranking-weight").value));
  if (name === "fund" && !element("renewable-ranking").querySelector("tbody")!.children.length) {
    void runRenewableScenario(1);
  }
}

function openEvidence(evaluation: CompanyEvaluation, field: ModelField): void {
  element("evidence-company").textContent = evaluation.company.name;
  element("evidence-title").textContent = fieldLabel(field);
  const body = element("evidence-body");
  body.replaceChildren(make("p", formatValue(evaluation.features[field.feature_name], field.canonical_unit), "evidence-value"));
  const metadata = evaluation.metadata[field.feature_name];
  if (!isRecord(metadata)) body.append(make("p", "No source evidence is recorded for this parameter.", "note"));
  else {
    const list = make("dl", "", "evidence-list");
    for (const [key, value] of Object.entries(metadata)) {
      if (key === "source_view_url" || value === null || value === undefined || value === "") continue;
      list.append(make("dt", key.replace(/_/g, " ")), make("dd", typeof value === "object" ? JSON.stringify(value) : String(value)));
    }
    body.append(list);
    if (typeof metadata.source_view_url === "string" && metadata.source_view_url.startsWith("/")) {
      const link = make("a", "Open source document", "document-link");
      link.href = metadata.source_view_url;
      link.target = "_blank";
      link.rel = "noopener";
      body.append(link);
    }
  }
  const dialog = element<HTMLDialogElement>("evidence-dialog");
  if (!dialog.open) dialog.showModal();
}

function installEvents(): void {
  element<HTMLFormElement>("login-form").onsubmit = async event => {
    event.preventDefault();
    const input = element<HTMLInputElement>("viewer-password");
    try {
      await request<{ authenticated: boolean }>("/api/login", { method: "POST", body: JSON.stringify({ password: input.value }) });
      input.value = "";
      await loadWorkspace();
      if (run && demos) showApp();
    } catch (error) { element("login-error").textContent = error instanceof Error ? error.message : String(error); }
  };
  element("logout").onclick = async () => {
    try { await request<{ authenticated: boolean }>("/api/logout", { method: "POST", body: "{}" }); }
    finally { showLogin(); }
  };
  for (const name of ["compare", "reach", "ranking", "fund"] as const) element("demo-tab-" + name).onclick = () => showDemo(name);
  element<HTMLSelectElement>("pair-preset").onchange = () => void applyPairPreset();
  element<HTMLSelectElement>("compare-target").onchange = () => void changeCompareTarget();
  element<HTMLSelectElement>("compare-company-a").onchange = () => { keepPairDistinct("a"); resetPairScenario(false); };
  element<HTMLSelectElement>("compare-company-b").onchange = () => { keepPairDistinct("b"); resetPairScenario(false); };
  element<HTMLSelectElement>("compare-parameter").onchange = () => resetPairScenario(false);
  element<HTMLSelectElement>("compare-apply-to").onchange = renderCompareButtons;
  element("compare-decrease").onclick = () => void runCumulativePairStep("decrease");
  element("compare-increase").onclick = () => void runCumulativePairStep("increase");
  element("compare-reset").onclick = () => resetPairScenario();
  element<HTMLSelectElement>("reach-target").onchange = event => void prepareReachDemo((event.currentTarget as HTMLSelectElement).value as TargetName);
  element<HTMLSelectElement>("reach-company").onchange = setReachDesiredScore;
  element<HTMLSelectElement>("reach-model").onchange = setReachDesiredScore;
  element("reach-find").onclick = () => void findTargetSuggestions();
  element<HTMLInputElement>("ranking-weight").oninput = scheduleRanking;
  element("renewable-increase").onclick = increaseRenewableScenario;
  element("renewable-reset").onclick = resetRenewableScenario;
  element("compute-portfolio").onclick = openRenewablePortfolio;
  element("portfolio-close").onclick = () => element<HTMLDialogElement>("portfolio-dialog").close();
  element("evidence-close").onclick = () => element<HTMLDialogElement>("evidence-dialog").close();
}

async function loadWorkspace(): Promise<void> {
  try {
    const [loadedRun, loadedDemos] = await Promise.all([
      request<ComparisonRun>("/api/model-comparison/run"),
      request<DemoPresets>("/api/model-comparison/demos"),
    ]);
    run = loadedRun;
    demos = loadedDemos;
    if (!demos.pair_presets.length) throw new Error("No validated two-company demo preset is published.");
    if (demos.snapshot_id && demos.snapshot_id !== run.snapshot_id) throw new Error("The published demo presets and model snapshot differ. Reload the page.");
    element("run-summary").textContent = run.prediction_as_of + " · saved models";
    element<HTMLSelectElement>("pair-preset").replaceChildren(...demos.pair_presets.map(preset => new Option(preset.label, preset.id)));
    await applyPairPreset();
    const reachPreset = demos.reach_preset;
    await prepareReachDemo(reachPreset?.target || currentPairPreset().target, reachPreset?.company_id || currentPairPreset().company_ids[0]);
    if (reachPreset) {
      element<HTMLSelectElement>("reach-model").value = reachPreset.model_family;
      element<HTMLInputElement>("reach-score").value = String(reachPreset.desired_score);
    }
    await loadCompanies(demos.ranking_preset.target);
    showDemo("compare");
  } catch (error) { handleRequestError(error); }
}

async function initialize(): Promise<void> {
  installEvents();
  try {
    const session = await request<{ authenticated: boolean }>("/api/session");
    if (!session.authenticated) { showLogin(); return; }
    await loadWorkspace();
    if (run && demos) showApp();
  } catch (error) { showLogin(error instanceof HttpError && error.status === 401 ? "" : error instanceof Error ? error.message : String(error)); }
}

void initialize();
export {};

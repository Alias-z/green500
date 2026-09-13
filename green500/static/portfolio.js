const CATEGORIES = [
    { key: "financial", label: "Financial" },
    { key: "social", label: "People & society" },
    { key: "environmental", label: "Environment" },
    { key: "climate_target", label: "Climate targets" },
    { key: "financial_target", label: "Financial targets" },
];
const element = (id) => document.getElementById(id);
const make = (tag, text = "", className = "") => {
    const node = document.createElement(tag);
    node.textContent = text;
    node.className = className;
    return node;
};
class HttpError extends Error {
    constructor(status, message) {
        super(message);
        this.status = status;
    }
}
let run = null;
let demos = null;
const companiesByTarget = new Map();
let pairScenarioByCik = new Map();
let pairParameterTransitions = new Map();
let pairChanges = [];
let pairRequestNumber = 0;
let reachRequestNumber = 0;
let rankingRequestNumber = 0;
let fundRequestNumber = 0;
let rankingWeight = 1;
let rankingTimer;
let renewableMultiplier = 1;
let renewableRequestNumber = 0;
let latestRenewableScenario = null;
async function request(path, init = {}) {
    const response = await fetch(path, {
        ...init,
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", ...(init.headers || {}) },
    });
    if (!response.ok) {
        let message = "Request failed with HTTP " + response.status + ".";
        try {
            const value = await response.json();
            message = typeof value.detail === "string" ? value.detail : value.detail ? JSON.stringify(value.detail) : message;
        }
        catch { /* Keep the HTTP status if the response is not JSON. */ }
        throw new HttpError(response.status, message);
    }
    return response.json();
}
function isRecord(value) {
    return typeof value === "object" && value !== null && !Array.isArray(value);
}
function numeric(value) {
    return typeof value === "number" && Number.isFinite(value) ? value : null;
}
function formatScore(value) {
    return typeof value === "number" && Number.isFinite(value) ? value.toFixed(2) : "—";
}
function formatDelta(value) {
    if (typeof value !== "number" || !Number.isFinite(value))
        return "No result";
    if (Math.abs(value) < 1e-12)
        return "No model change";
    if (Math.abs(value) < 0.005)
        return value > 0 ? "+<0.01" : "−<0.01";
    return (value > 0 ? "+" : "") + value.toFixed(2);
}
function formatValue(value, unit = null) {
    if (value === null || value === undefined || value === "")
        return "—";
    if (typeof value === "boolean")
        return value ? "Yes" : "No";
    if (typeof value !== "number")
        return String(value);
    const formatted = value.toLocaleString(undefined, { maximumFractionDigits: Math.abs(value) >= 100 ? 1 : 2 });
    if (!unit)
        return formatted;
    if (unit === "%" || unit.toLowerCase() === "percent")
        return formatted + "%";
    return formatted + " " + unit;
}
function formatMoney(value) {
    return new Intl.NumberFormat(undefined, { style: "currency", currency: "USD", notation: "compact", maximumFractionDigits: 1 }).format(value);
}
function formatWeight(value) {
    return (value * 100).toFixed(1) + "%";
}
function showLogin(message = "") {
    element("app-view").hidden = true;
    element("login-view").hidden = false;
    element("login-error").textContent = message;
    element("viewer-password").focus();
}
function showApp() {
    element("login-view").hidden = true;
    element("app-view").hidden = false;
}
function showError(message = "") {
    element("page-error").textContent = message;
}
function showStatus(message = "") {
    element("page-status").textContent = message;
}
function handleRequestError(error) {
    if (error instanceof HttpError && error.status === 401)
        showLogin("Session expired.");
    else
        showError(error instanceof Error ? error.message : String(error));
}
function defaultWeights() {
    return { financial: 1, social: 1, environmental: 1, climate_target: 1, financial_target: 1 };
}
function definitionFor(target) {
    if (!run)
        throw new Error("The published model is not loaded.");
    return run.targets[target];
}
function fieldsFor(target, editableOnly = false) {
    const definition = definitionFor(target);
    const active = new Set(definition.active_features);
    return definition.fields.filter(field => active.has(field.feature_name) && field.category_or_context !== "fixed_context" && (!editableOnly || field.editable));
}
function fieldFor(target, featureName) {
    return fieldsFor(target).find(field => field.feature_name === featureName);
}
function fieldLabel(field) {
    return field.label || field.feature_name.replace(/_/g, " ").replace(/\b\w/g, character => character.toUpperCase());
}
function appendCompanyOptions(select, companies, selected = "") {
    select.replaceChildren(...companies.map(item => new Option(item.company.name + (item.company.ticker ? " · " + item.company.ticker : ""), item.company.company_cik)));
    if (companies.some(item => item.company.company_cik === selected))
        select.value = selected;
}
function keepPairDistinct(changed) {
    const companyA = element("compare-company-a");
    const companyB = element("compare-company-b");
    if (companyA.value !== companyB.value)
        return;
    const target = currentCompareTarget();
    const replacement = (companiesByTarget.get(target) || []).find(company => company.company.company_cik !== companyA.value);
    if (!replacement)
        return;
    if (changed === "a")
        companyB.value = replacement.company.company_cik;
    else
        companyA.value = replacement.company.company_cik;
}
function appendParameterOptions(select, target, selected = "") {
    select.replaceChildren();
    for (const category of CATEGORIES) {
        const group = document.createElement("optgroup");
        const pairIds = selectedPairIds();
        const categoryFields = fieldsFor(target, true)
            .filter(field => field.category_or_context === category.key)
            .sort((left, right) => pairObservedCount(target, pairIds, right.feature_name) - pairObservedCount(target, pairIds, left.feature_name) || fieldLabel(left).localeCompare(fieldLabel(right)));
        group.label = category.label;
        for (const field of categoryFields)
            group.append(new Option(fieldLabel(field), field.feature_name));
        select.append(group);
    }
    if ([...select.options].some(option => option.value === selected))
        select.value = selected;
}
function pairObservedCount(target, pairIds, featureName) {
    return pairIds.filter(cik => numeric(companyEvaluation(target, cik)?.features[featureName]) !== null).length;
}
async function loadCompanies(target) {
    const existing = companiesByTarget.get(target);
    if (existing)
        return existing;
    const response = await request("/api/model-comparison/companies", {
        method: "POST",
        body: JSON.stringify({ target, search: "", industry: null, filters: [], sort_by: "company_name", direction: "asc", limit: 500 }),
    });
    companiesByTarget.set(target, response.companies);
    return response.companies;
}
function companyEvaluation(target, companyCik) {
    return companiesByTarget.get(target)?.find(item => item.company.company_cik === companyCik);
}
function currentPairPreset() {
    if (!demos)
        throw new Error("Demo presets are not loaded.");
    const id = element("pair-preset").value;
    return demos.pair_presets.find(preset => preset.id === id) || demos.pair_presets[0];
}
function currentCompareTarget() {
    return element("compare-target").value;
}
function selectedPairIds() {
    return [element("compare-company-a").value, element("compare-company-b").value];
}
async function applyPairPreset() {
    const preset = currentPairPreset();
    const companies = await loadCompanies(preset.target);
    element("compare-target").value = preset.target;
    appendCompanyOptions(element("compare-company-a"), companies, preset.company_ids[0]);
    appendCompanyOptions(element("compare-company-b"), companies, preset.company_ids[1]);
    appendParameterOptions(element("compare-parameter"), preset.target, preset.default_feature);
    element("compare-apply-to").value = "both";
    pairScenarioByCik.clear();
    pairParameterTransitions.clear();
    pairChanges = [];
    renderCompare();
}
async function changeCompareTarget() {
    const target = currentCompareTarget();
    const previousIds = selectedPairIds();
    const companies = await loadCompanies(target);
    appendCompanyOptions(element("compare-company-a"), companies, previousIds[0]);
    appendCompanyOptions(element("compare-company-b"), companies, previousIds[1]);
    const preset = currentPairPreset();
    const preferred = fieldFor(target, preset.default_feature)?.editable ? preset.default_feature : "";
    appendParameterOptions(element("compare-parameter"), target, preferred);
    pairScenarioByCik.clear();
    pairParameterTransitions.clear();
    pairChanges = [];
    renderCompare();
}
function renderCompare() {
    renderPairDescription();
    renderPairScores();
    renderCompareButtons();
    renderCompareDetails();
}
function renderPairDescription() {
    const preset = currentPairPreset();
    const pair = selectedPairIds();
    const matchesPreset = currentCompareTarget() === preset.target
        && pair[0] === preset.company_ids[0]
        && pair[1] === preset.company_ids[1]
        && element("compare-parameter").value === preset.default_feature
        && element("compare-apply-to").value === "both";
    element("pair-description").textContent = matchesPreset
        ? preset.description
        : "Apply one measurable change to either company or both companies and compare the saved models' responses.";
}
function renderPairScores() {
    const target = currentCompareTarget();
    const container = element("compare-results");
    container.replaceChildren();
    for (const [position, cik] of selectedPairIds().entries()) {
        const baseline = companyEvaluation(target, cik);
        if (!baseline)
            continue;
        const compared = pairScenarioByCik.get(cik);
        const card = make("article", "", "score-card");
        const heading = make("header");
        heading.append(make("span", position === 0 ? "Company A" : "Company B"), make("h2", baseline.company.name), make("small", [baseline.company.ticker, baseline.company.industry].filter(Boolean).join(" · ")));
        const scores = make("div", "", "model-scores");
        for (const family of ["ebm", "catboost"]) {
            const score = make("div", "", "model-score");
            const modelLabel = family === "ebm" ? "EBM" : "CatBoost";
            if (!compared)
                score.append(make("span", modelLabel, "model-current-label"), make("strong", formatScore(baseline.predictions[family]), "model-current-value"));
            else {
                const delta = compared.differences[family];
                score.append(make("span", modelLabel + " change", "model-change-label"), make("strong", formatDelta(delta), "model-change-value " + ((delta || 0) > 0 ? "positive" : (delta || 0) < 0 ? "negative" : "neutral")), make("small", formatScore(compared.original.predictions[family]) + " → " + formatScore(compared.scenario.predictions[family]), "model-score-transition"));
            }
            scores.append(score);
        }
        if (!compared && pairScenarioByCik.size)
            card.append(make("p", "Unchanged in this scenario", "unchanged"));
        card.prepend(heading);
        card.append(scores);
        const parameter = fieldFor(target, element("compare-parameter").value);
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
    renderPairContrastSummary();
}
function scoreDirectionClass(value) {
    if (typeof value !== "number" || !Number.isFinite(value) || Math.abs(value) < 1e-12)
        return "neutral";
    return value > 0 ? "positive" : "negative";
}
function renderPairContrastSummary() {
    const summary = element("compare-summary");
    summary.replaceChildren();
    summary.hidden = true;
    const compared = selectedPairIds().map(cik => pairScenarioByCik.get(cik));
    if (compared.some(value => !value))
        return;
    const companies = compared;
    const field = fieldFor(currentCompareTarget(), element("compare-parameter").value);
    if (!field)
        return;
    const movements = companies.map(company => {
        const before = numeric(company.original.features[field.feature_name]);
        const after = numeric(company.scenario.features[field.feature_name]);
        return before === null || after === null ? 0 : after - before;
    });
    const sameInputDirection = movements.every(value => value > 0) || movements.every(value => value < 0);
    const oppositeForBothModels = ["ebm", "catboost"].every(family => {
        const left = companies[0].differences[family];
        const right = companies[1].differences[family];
        return typeof left === "number" && typeof right === "number" && left * right < 0;
    });
    const target = currentCompareTarget().toUpperCase();
    const inputDirection = movements[0] > 0 ? "increase" : "decrease";
    summary.append(make("strong", sameInputDirection && oppositeForBothModels
        ? `Same ${fieldLabel(field)} ${inputDirection} · opposite predicted ${target} response`
        : `Predicted ${target} sensitivity`));
    for (const company of companies) {
        const result = make("div", "", "contrast-result");
        result.append(make("span", company.company.name));
        for (const family of ["ebm", "catboost"]) {
            const delta = company.differences[family];
            result.append(make("b", `${family === "ebm" ? "EBM" : "CatBoost"} ${formatDelta(delta)}`, scoreDirectionClass(delta)));
        }
        summary.append(result);
    }
    summary.hidden = false;
}
function currentComparisonRule(direction) {
    const target = currentCompareTarget();
    const featureName = element("compare-parameter").value;
    const field = fieldFor(target, featureName);
    if (!field)
        throw new Error("Choose an active model parameter.");
    const preset = currentPairPreset();
    if (featureName === preset.default_feature && target === preset.target) {
        const action = preset.actions[direction];
        return { feature_name: featureName, operation: action.operation, value: action.value };
    }
    const values = selectedPairIds().map(cik => numeric(companyEvaluation(target, cik)?.features[featureName])).filter((value) => value !== null);
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
function ruleLabel(rule) {
    const field = fieldFor(currentCompareTarget(), rule.feature_name);
    const value = formatValue(rule.value, field?.canonical_unit || null);
    if (rule.operation === "set")
        return "Set to " + value;
    if (rule.operation === "percent")
        return (rule.value >= 0 ? "+" : "") + value;
    return (rule.value >= 0 ? "+" : "") + value;
}
function renderCompareButtons() {
    const field = fieldFor(currentCompareTarget(), element("compare-parameter").value);
    if (!field)
        return;
    const isEmissions = field.feature_name.includes("emissions");
    element("compare-decrease").querySelector("strong").textContent = isEmissions ? "Decrease emissions" : "Decrease";
    element("compare-increase").querySelector("strong").textContent = isEmissions ? "Increase emissions" : "Increase";
    element("compare-decrease").querySelector("small").textContent = stepLabel(field, "decrease");
    element("compare-increase").querySelector("small").textContent = stepLabel(field, "increase");
    element("compare-decrease").disabled = !canStepPair("decrease");
    element("compare-increase").disabled = !canStepPair("increase");
    element("compare-reset").disabled = pairScenarioByCik.size === 0;
}
function isPercentField(field) {
    return field.canonical_unit === "%" || field.canonical_unit?.toLowerCase() === "percent";
}
function isYearField(field) {
    return field.feature_name.endsWith("_year") || field.canonical_unit?.toLowerCase() === "year";
}
function stepLabel(field, direction) {
    if (isPercentField(field))
        return direction === "increase" ? "+25%" : "−25%";
    if (isYearField(field))
        return direction === "increase" ? "+10 years" : "−10 years";
    return direction === "increase" ? "+50% of current value" : "−50% of current value";
}
function currentPairEvaluation(target, cik) {
    return pairScenarioByCik.get(cik)?.scenario || companyEvaluation(target, cik);
}
function nextStepValue(field, current, direction, observed) {
    if (current === null) {
        if (!observed.length)
            return null;
        current = direction === "increase" ? Math.max(...observed) : Math.min(...observed);
    }
    if (isPercentField(field))
        return Math.max(0, Math.min(100, current + (direction === "increase" ? 25 : -25)));
    if (isYearField(field))
        return Math.max(1900, Math.min(2200, current + (direction === "increase" ? 10 : -10)));
    if (current > 0)
        return direction === "increase" ? current * 1.5 : current * .5;
    if (current < 0)
        return direction === "increase" ? current * .5 : current * 1.5;
    const scale = Math.max(1, ...observed.map(value => Math.abs(value)));
    return direction === "increase" ? scale * .5 : -scale * .5;
}
function stepPlan(cik, direction) {
    const target = currentCompareTarget();
    const field = fieldFor(target, element("compare-parameter").value);
    if (!field)
        return { before: null, after: null };
    const observed = selectedPairIds().map(id => numeric(currentPairEvaluation(target, id)?.features[field.feature_name])).filter((value) => value !== null);
    const before = numeric(currentPairEvaluation(target, cik)?.features[field.feature_name]);
    return { before, after: nextStepValue(field, before, direction, observed) };
}
function canStepPair(direction) {
    return affectedPairIds().some(cik => {
        const plan = stepPlan(cik, direction);
        return plan.after !== null && !Object.is(plan.before, plan.after);
    });
}
function hasVisibleStepResponse(company, previous) {
    return ["ebm", "catboost"].some(family => {
        const before = previous.predictions[family];
        const after = company.scenario.predictions[family];
        return typeof before === "number" && typeof after === "number" && Math.abs(after - before) >= .005;
    });
}
async function compareOneCompanyAtValue(cik, field, value) {
    if (!run)
        throw new Error("The published model is not loaded.");
    const response = await request("/api/model-comparison/compare", {
        method: "POST",
        body: JSON.stringify({ target: currentCompareTarget(), company_ids: [cik], adjustments: [{ feature_name: field.feature_name, operation: "set", value }], weights: defaultWeights(), snapshot_id: run.snapshot_id, preview_only: false }),
    });
    if (response.snapshot_id !== run.snapshot_id || !response.companies[0])
        throw new Error("The comparison response does not match the current model snapshot.");
    return { company: response.companies[0], changes: response.changes };
}
async function runCompanyStep(cik, field, direction) {
    const target = currentCompareTarget();
    const initial = currentPairEvaluation(target, cik);
    if (!initial)
        return null;
    const initialValue = numeric(initial.features[field.feature_name]);
    let currentValue = initialValue;
    let response = null;
    for (let attempt = 0; attempt < 8; attempt += 1) {
        const observed = selectedPairIds().map(id => numeric(currentPairEvaluation(target, id)?.features[field.feature_name])).filter((value) => value !== null);
        const next = nextStepValue(field, currentValue, direction, observed);
        if (next === null || Object.is(next, currentValue))
            break;
        response = await compareOneCompanyAtValue(cik, field, next);
        currentValue = numeric(response.company.scenario.features[field.feature_name]);
        if (hasVisibleStepResponse(response.company, initial) || currentValue === null)
            break;
    }
    return response ? { ...response, before: initialValue, after: response.company.scenario.features[field.feature_name] } : null;
}
async function runCumulativePairStep(direction) {
    const field = fieldFor(currentCompareTarget(), element("compare-parameter").value);
    if (!field || !run)
        return;
    const ids = affectedPairIds().filter(cik => {
        const plan = stepPlan(cik, direction);
        return plan.after !== null && !Object.is(plan.before, plan.after);
    });
    if (!ids.length) {
        showStatus("The selected companies are already at this parameter’s semantic bound.");
        return;
    }
    const requestNumber = ++pairRequestNumber;
    element("compare-decrease").disabled = true;
    element("compare-increase").disabled = true;
    showStatus((direction === "increase" ? "Increasing " : "Decreasing ") + fieldLabel(field) + "…");
    try {
        const results = await Promise.all(ids.map(cik => runCompanyStep(cik, field, direction)));
        if (requestNumber !== pairRequestNumber)
            return;
        pairChanges = [];
        const transitions = [];
        for (const result of results) {
            if (!result)
                continue;
            const cik = result.company.company.company_cik;
            pairScenarioByCik.set(cik, result.company);
            pairParameterTransitions.set(cik, { featureName: field.feature_name, before: result.before, after: result.after });
            pairChanges.push(...result.changes);
            transitions.push(result.company.company.name + " " + formatValue(result.before, field.canonical_unit) + " → " + formatValue(result.after, field.canonical_unit));
        }
        renderCompare();
        showError();
        showStatus(fieldLabel(field) + " updated: " + transitions.join("; ") + ".");
    }
    catch (error) {
        if (requestNumber === pairRequestNumber)
            handleRequestError(error);
    }
    finally {
        renderCompareButtons();
    }
}
function resetPairScenario(message = true) {
    pairRequestNumber += 1;
    pairScenarioByCik.clear();
    pairParameterTransitions.clear();
    pairChanges = [];
    renderCompare();
    if (message)
        showStatus("Comparison reset to published values.");
}
function affectedPairIds() {
    const [companyA, companyB] = selectedPairIds();
    const selected = element("compare-apply-to").value;
    return selected === "a" ? [companyA] : selected === "b" ? [companyB] : [companyA, companyB];
}
async function runPairScenario(direction, suppliedRule, suppliedIds) {
    if (!run)
        return;
    const target = currentCompareTarget();
    const adjustment = suppliedRule || currentComparisonRule(direction);
    const ids = suppliedIds || affectedPairIds();
    const signature = JSON.stringify({ target, pair: selectedPairIds(), ids, adjustment });
    const requestNumber = ++pairRequestNumber;
    showStatus("Running the two-company scenario…");
    try {
        const response = await request("/api/model-comparison/compare", {
            method: "POST",
            body: JSON.stringify({ target, company_ids: ids, adjustments: [adjustment], weights: defaultWeights(), snapshot_id: run.snapshot_id, preview_only: false }),
        });
        const currentSignature = JSON.stringify({ target: currentCompareTarget(), pair: selectedPairIds(), ids: suppliedIds || affectedPairIds(), adjustment: suppliedRule || currentComparisonRule(direction) });
        if (requestNumber !== pairRequestNumber || signature !== currentSignature || response.snapshot_id !== run.snapshot_id)
            return;
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
    }
    catch (error) {
        if (requestNumber === pairRequestNumber)
            handleRequestError(error);
        showStatus();
    }
}
function renderCompareDetails() {
    const target = currentCompareTarget();
    const container = element("compare-detail-content");
    container.replaceChildren();
    for (const cik of selectedPairIds()) {
        const evaluation = pairScenarioByCik.get(cik)?.scenario || companyEvaluation(target, cik);
        const original = pairScenarioByCik.get(cik)?.original || evaluation;
        if (!evaluation || !original)
            continue;
        const section = make("section");
        section.append(make("h3", evaluation.company.name));
        for (const category of CATEGORIES) {
            const categoryFields = fieldsFor(target).filter(field => field.category_or_context === category.key);
            if (!categoryFields.length)
                continue;
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
async function prepareReachDemo(target, companyCik = "") {
    const companies = await loadCompanies(target);
    element("reach-target").value = target;
    const fallback = currentPairPreset().company_ids[0];
    appendCompanyOptions(element("reach-company"), companies, companyCik || fallback);
    setReachDesiredScore();
    element("reach-suggestions").replaceChildren();
}
function updateReachSolutionsButton() {
    const target = element("reach-target").value.toUpperCase();
    const score = Number(element("reach-score").value);
    const displayedScore = Number.isFinite(score)
        ? score.toLocaleString(undefined, { maximumFractionDigits: 1 })
        : "—";
    element("reach-find").textContent = `Show 3 parameter solutions for ${target} target = ${displayedScore}`;
}
function setReachDesiredScore() {
    const target = element("reach-target").value;
    const cik = element("reach-company").value;
    const family = element("reach-model").value;
    const current = companyEvaluation(target, cik)?.predictions[family];
    if (typeof current === "number")
        element("reach-score").value = String(Math.min(100, Math.ceil(current + 5)));
    updateReachSolutionsButton();
}
async function findTargetSuggestions() {
    if (!run)
        return;
    const target = element("reach-target").value;
    const companyId = element("reach-company").value;
    const modelFamily = element("reach-model").value;
    const desiredScore = Number(element("reach-score").value);
    if (!Number.isFinite(desiredScore)) {
        showError("Enter a desired score.");
        return;
    }
    const findButton = element("reach-find");
    findButton.disabled = true;
    findButton.textContent = "Calculating 3 parameter solutions…";
    const signature = JSON.stringify({ target, companyId, modelFamily, desiredScore });
    const requestNumber = ++reachRequestNumber;
    element("reach-suggestions").replaceChildren(make("p", "Searching saved-model sensitivity…", "empty-result"));
    try {
        const response = await request("/api/model-comparison/target-suggestions", {
            method: "POST",
            body: JSON.stringify({ target, company_id: companyId, desired_score: desiredScore, model_family: modelFamily, snapshot_id: run.snapshot_id, max_suggestions: 3 }),
        });
        const currentSignature = JSON.stringify({ target: element("reach-target").value, companyId: element("reach-company").value, modelFamily: element("reach-model").value, desiredScore: Number(element("reach-score").value) });
        if (requestNumber !== reachRequestNumber || signature !== currentSignature || response.target !== target)
            return;
        renderTargetSuggestions(response);
        showError();
    }
    catch (error) {
        if (requestNumber === reachRequestNumber)
            handleRequestError(error);
    }
    finally {
        if (requestNumber === reachRequestNumber) {
            findButton.disabled = false;
            updateReachSolutionsButton();
        }
    }
}
function renderTargetSuggestions(response) {
    const container = element("reach-suggestions");
    container.replaceChildren();
    element("reach-disclosure").textContent = response.suggestions.length + " options";
    if (!response.suggestions.length)
        container.append(make("p", "No one-parameter suggestion was found for this desired score.", "empty-result"));
    for (const suggestion of response.suggestions) {
        const card = make("article", "", "suggestion-card");
        const heading = make("header");
        heading.append(make("h2", suggestion.label));
        const otherFamily = response.model_family === "ebm" ? "catboost" : "ebm";
        const otherChange = (suggestion.projected_predictions[otherFamily] || 0) - (response.original_predictions[otherFamily] || 0);
        const selectedDirection = Math.sign(suggestion.selected_model_change);
        const otherDirection = Math.sign(otherChange);
        const directionLabel = !otherDirection ? "no change" : otherDirection === selectedDirection ? "same direction" : "opposite direction";
        const metrics = make("div", "", "suggestion-metrics");
        metrics.append(suggestionMetric("Required change", requiredParameterChange(suggestion), "required-change"), suggestionMetric(response.model_family.toUpperCase() + " change", formatDelta(suggestion.selected_model_change), "suggested-score-change"), suggestionMetric("Projected score", formatScore(suggestion.projected_predictions[response.model_family]) + (suggestion.reaches_target ? " · reaches target" : " · gap " + formatScore(Math.abs(suggestion.remaining_gap))), "projected-score"));
        const apply = make("button", "Apply to comparison");
        apply.type = "button";
        apply.onclick = () => void applySuggestionToComparison(response, suggestion);
        const details = make("details", "", "suggestion-details");
        const detailContent = make("div");
        detailContent.append(make("p", "Parameter value: " + formatValue(suggestion.current_value, suggestion.unit) + " → " + formatValue(suggestion.suggested_value, suggestion.unit) + "."), make("p", (otherFamily === "ebm" ? "EBM" : "CatBoost") + ": " + formatScore(response.original_predictions[otherFamily]) + " → " + formatScore(suggestion.projected_predictions[otherFamily]) + " (" + directionLabel + ")."), make("p", "Training range position: " + formatRangePosition(suggestion.range_position) + ". Direction: " + response.desired_direction + "."));
        if (suggestion.warning)
            detailContent.append(make("p", suggestion.warning, "warning"));
        detailContent.append(make("p", response.disclosure));
        details.append(make("summary", "Details"), detailContent);
        card.append(heading, metrics, apply, details);
        container.append(card);
    }
    if (response.warnings.length) {
        const details = make("details", "", "reach-details");
        const warnings = make("ul", "", "warning-list");
        for (const warning of response.warnings)
            warnings.append(make("li", warning));
        details.append(make("summary", "More details"), warnings);
        container.append(details);
    }
}
function suggestionMetric(label, value, className) {
    const metric = make("div", "", "suggestion-metric " + className);
    metric.append(make("span", label), make("strong", value));
    return metric;
}
function requiredParameterChange(suggestion) {
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
function signedAmount(value, suffix, maximumFractionDigits) {
    return (value > 0 ? "+" : value < 0 ? "−" : "") + Math.abs(value).toLocaleString(undefined, { maximumFractionDigits }) + suffix;
}
function formatRangePosition(value) {
    return typeof value === "number" && Number.isFinite(value)
        ? (value * 100).toFixed(1) + "% of training range"
        : String(value);
}
async function applySuggestionToComparison(response, suggestion) {
    showDemo("compare");
    element("compare-target").value = response.target;
    await changeCompareTarget();
    const companies = companiesByTarget.get(response.target) || [];
    appendCompanyOptions(element("compare-company-a"), companies, response.company.company_cik);
    const companyB = element("compare-company-b");
    if (companyB.value === response.company.company_cik) {
        const alternative = companies.find(company => company.company.company_cik !== response.company.company_cik);
        if (alternative)
            companyB.value = alternative.company.company_cik;
    }
    appendParameterOptions(element("compare-parameter"), response.target, suggestion.feature_name);
    element("compare-apply-to").value = "a";
    pairScenarioByCik.clear();
    pairParameterTransitions.clear();
    renderCompare();
    await runPairScenario("increase", { feature_name: suggestion.feature_name, operation: "set", value: suggestion.suggested_value }, [response.company.company_cik]);
}
async function runRanking(weight) {
    if (!run)
        return;
    if (!Number.isFinite(weight) || weight < 0 || weight > 3) {
        showError("Environmental weight must be a number from 0 to 3.");
        return;
    }
    rankingWeight = weight;
    const signature = JSON.stringify({ weight, snapshot: run.snapshot_id });
    const requestNumber = ++rankingRequestNumber;
    element("ranking-results").replaceChildren(make("p", "Calculating 500 company ranks…", "empty-result"));
    try {
        const response = await request("/api/model-comparison/rank", {
            method: "POST",
            body: JSON.stringify({ target: "esg", weights: { environmental: weight }, snapshot_id: run.snapshot_id }),
        });
        if (requestNumber !== rankingRequestNumber || signature !== JSON.stringify({ weight: rankingWeight, snapshot: run.snapshot_id }))
            return;
        if (response.snapshot_id && response.snapshot_id !== run.snapshot_id)
            return;
        renderRanking(response);
        showError();
    }
    catch (error) {
        if (requestNumber === rankingRequestNumber)
            handleRequestError(error);
    }
}
function renderRanking(response) {
    element("ranking-count").textContent = response.company_count.toLocaleString() + " companies";
    const aflac = response.companies.find(item => item.company.ticker === "AFL");
    const focus = element("ranking-focus");
    if (aflac) {
        const movement = !aflac.rank_change ? "—" : aflac.rank_change > 0 ? "↑" + aflac.rank_change : "↓" + Math.abs(aflac.rank_change);
        const indexChange = aflac.personalized_index - aflac.raw_ebm_prediction;
        focus.replaceChildren(make("strong", "Aflac (AFL)"), rankingFocusMetric("Original rank", "#" + aflac.original_rank), rankingFocusMetric("New rank", "#" + aflac.personalized_rank), rankingFocusMetric("Move", movement), rankingFocusMetric("Index change", compactSigned(indexChange) + "%"));
    }
    else {
        focus.replaceChildren();
    }
    const container = element("ranking-results");
    container.replaceChildren();
    const table = make("table");
    const head = make("thead");
    const headingRow = make("tr");
    for (const label of ["New rank", "Company", "Before", "Move", "Index change"])
        headingRow.append(make("th", label));
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
        if (item.company.ticker === "AFL")
            row.classList.add("ranking-example");
        row.append(make("td", "#" + item.personalized_rank), company, make("td", "#" + item.original_rank), make("td", move), make("td", effect === null ? "—" : (effect >= 0 ? "+" : "") + effect.toFixed(2) + "%"));
        body.append(row);
    }
    table.append(head, body);
    container.append(table);
}
function rankingFocusMetric(label, value) {
    const metric = make("span", label);
    metric.append(make("b", value));
    return metric;
}
function scheduleRanking() {
    window.clearTimeout(rankingTimer);
    const input = element("ranking-weight");
    const value = Number(input.value);
    if (!Number.isFinite(value) || value < 0 || value > 3) {
        showError("Environmental weight must be a number from 0 to 3.");
        return;
    }
    rankingRequestNumber += 1;
    rankingTimer = window.setTimeout(() => void runRanking(value), 200);
}
function renderFundPreset() {
    if (!demos)
        return;
    const container = element("fund-preset-summary");
    container.replaceChildren();
    const assumptions = make("div", "", "assumptions");
    for (const text of [
        "Objective: profitability + cash-flow strength",
        "Net zero: renewable 100% · Scope 1 −50%",
        "Constraints: Environment + Climate 3× · diversified",
    ])
        assumptions.append(make("strong", text));
    container.append(assumptions);
}
async function runFundCase() {
    if (!run || !demos)
        return;
    const preset = demos.fund_preset;
    const signature = JSON.stringify({ preset, snapshot: run.snapshot_id });
    const requestNumber = ++fundRequestNumber;
    const button = element("fund-run");
    button.disabled = true;
    element("fund-results").replaceChildren(make("p", "Running saved models and constraints…", "empty-result"));
    try {
        const response = await request("/api/model-comparison/portfolio", {
            method: "POST",
            body: JSON.stringify({ target: preset.target, company_ids: preset.company_ids, adjustments: preset.adjustments, weights: { ...defaultWeights(), ...preset.weights }, constraints: preset.constraints, objective: preset.objective, sustainability_eligible_fraction: preset.sustainability_eligible_fraction, snapshot_id: run.snapshot_id, preview_only: false }),
        });
        if (requestNumber !== fundRequestNumber || signature !== JSON.stringify({ preset: demos.fund_preset, snapshot: run.snapshot_id }) || response.snapshot_id !== run.snapshot_id)
            return;
        renderFundResults(response);
        showError();
    }
    catch (error) {
        if (requestNumber === fundRequestNumber)
            handleRequestError(error);
    }
    finally {
        button.disabled = false;
    }
}
function renderFundResults(response) {
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
    const preset = demos.fund_preset;
    detailBody.append(make("h3", "Constraints"), make("p", preset.constraints.max_companies + " holdings maximum; " + formatWeight(preset.constraints.max_company_weight) + " company cap; " + formatWeight(preset.constraints.max_industry_weight) + " industry cap; " + formatWeight(preset.constraints.min_coverage) + " minimum disclosure coverage."));
    if (response.scenario.objective_method)
        detailBody.append(make("h3", response.scenario.objective_name || "Financial objective"), make("p", response.scenario.objective_method));
    const notes = [...response.interpretation, ...response.baseline.warnings.map(value => "Baseline: " + value), ...response.scenario.warnings.map(value => "Scenario: " + value)];
    if (notes.length) {
        const list = make("ul", "", "warning-list");
        for (const note of notes)
            list.append(make("li", note));
        detailBody.append(make("h3", "Notes"), list);
    }
    details.append(make("summary", "Details"), detailBody);
    container.append(total, changes, scenarioSection, details);
}
function changeList(label, allocations) {
    const section = make("section");
    section.append(make("h2", label));
    const list = make("div", "", "change-chips");
    if (!allocations.length)
        list.append(make("span", "None", "empty-chip"));
    else
        for (const allocation of allocations)
            list.append(make("span", allocation.name));
    section.append(list);
    return section;
}
function detailedHoldingList(allocations) {
    const list = make("ol", "", "detailed-holdings");
    for (const allocation of allocations) {
        const item = make("li");
        item.append(make("strong", allocation.name + " · " + formatMoney(allocation.amount_usd)), make("p", allocation.reason));
        list.append(item);
    }
    return list;
}
async function runRenewableScenario(multiplier) {
    if (!run)
        return;
    const requestNumber = ++renewableRequestNumber;
    const increase = element("renewable-increase");
    const reset = element("renewable-reset");
    const computePortfolio = element("compute-portfolio");
    increase.disabled = true;
    reset.disabled = true;
    computePortfolio.disabled = true;
    element("renewable-company-count").textContent = "Loading…";
    try {
        const response = await request("/api/model-comparison/renewable-scenario", {
            method: "POST",
            body: JSON.stringify({ target: "csa", model_family: "ebm", renewable_multiplier: multiplier, snapshot_id: run.snapshot_id }),
        });
        if (requestNumber !== renewableRequestNumber || response.snapshot_id !== run.snapshot_id || Math.abs(response.renewable_multiplier - multiplier) > 1e-9)
            return;
        renewableMultiplier = response.renewable_multiplier;
        renderRenewableScenario(response);
        showError();
    }
    catch (error) {
        if (requestNumber === renewableRequestNumber)
            handleRequestError(error);
    }
    finally {
        if (requestNumber === renewableRequestNumber) {
            increase.disabled = false;
            reset.disabled = Math.abs(renewableMultiplier - 1) < 1e-9;
            computePortfolio.disabled = latestRenewableScenario === null;
        }
    }
}
function renderRenewableScenario(response) {
    latestRenewableScenario = response;
    const cumulative = (response.renewable_multiplier - 1) * 100;
    const cumulativeElement = element("renewable-cumulative");
    cumulativeElement.textContent = formatCumulativePercent(cumulative);
    cumulativeElement.title = cumulative.toString() + "%";
    element("renewable-company-count").textContent = response.companies.length.toLocaleString() + " companies";
    const body = element("renewable-ranking").querySelector("tbody");
    body.replaceChildren();
    for (const item of response.companies) {
        const row = make("tr");
        row.dataset.companyCik = item.company.company_cik;
        if (item.score_change < 0)
            row.classList.add("negative-score");
        const company = make("td");
        company.append(make("strong", item.company.name), make("small", item.company.ticker || ""));
        const renewable = formatValue(item.original_renewable_pct, "%") + " → " + formatValue(item.scenario_renewable_pct, "%");
        const scoreChange = make("td", compactSigned(item.score_change), item.score_change > 0 ? "positive" : item.score_change < 0 ? "negative" : "neutral");
        const move = !item.rank_change ? "—" : item.rank_change > 0 ? "↑" + item.rank_change : "↓" + Math.abs(item.rank_change);
        row.append(make("td", "#" + item.scenario_rank), company, make("td", renewable), scoreChange, make("td", move));
        body.append(row);
    }
    const portfolioDialog = element("portfolio-dialog");
    if (portfolioDialog.open)
        renderSectorShifts(response.sectors);
    const detail = element("renewable-details").querySelector("div");
    const summary = make("dl", "", "summary-list");
    summary.append(make("dt", "Observed values"), make("dd", response.step_summary.observed_count.toLocaleString()), make("dt", "Missing values"), make("dd", response.step_summary.missing_count.toLocaleString()), make("dt", "At 100%"), make("dd", response.step_summary.saturated_count.toLocaleString()), make("dt", "CSA EBM score sum"), make("dd", response.total_score_sum.original.toFixed(2) + " → " + response.total_score_sum.scenario.toFixed(2)));
    detail.replaceChildren(summary, make("p", response.disclosure));
}
function formatCumulativePercent(value) {
    const sign = value > 0 ? "+" : value < 0 ? "−" : "";
    const absolute = Math.abs(value);
    if (absolute < 1_000_000)
        return sign + absolute.toLocaleString(undefined, { maximumFractionDigits: 1 }) + "%";
    if (absolute < 1_000_000_000_000) {
        return sign + new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 }).format(absolute) + "%";
    }
    return sign + absolute.toExponential(1).replace("e+", "e") + "%";
}
function compactSigned(value) {
    if (Math.abs(value) < .005)
        return "0.00";
    return (value > 0 ? "+" : "") + value.toFixed(2);
}
function renderSectorShifts(sectors) {
    const chart = element("sector-shifts");
    chart.replaceChildren();
    const sectorColors = {
        "Communication Services": "#5856d6",
        "Consumer Discretionary": "#ff9500",
        "Consumer Staples": "#a2845e",
        Energy: "#ff3b30",
        Financials: "#007aff",
        "Health Care": "#79ab52",
        Industrials: "#af52de",
        "Information Technology": "#5ac8fa",
        Materials: "#8e8e93",
        "Real Estate": "#ff2d55",
        Utilities: "#30b0c7",
    };
    const colorFor = (sector) => sectorColors[sector] || "#636366";
    let start = 0;
    const slices = sectors.map((sector, index) => {
        const end = index === sectors.length - 1 ? 100 : start + sector.scenario_share_pct;
        const slice = colorFor(sector.sector) + " " + start.toFixed(6) + "% " + end.toFixed(6) + "%";
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
        swatch.style.backgroundColor = colorFor(sector.sector);
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
function compactPercentShift(value) {
    if (value > 0 && value < .005)
        return "+<0.01%";
    if (value < 0 && value > -.005)
        return "−<0.01%";
    return (value > 0 ? "+" : "") + value.toFixed(2) + "%";
}
function openRenewablePortfolio() {
    if (!latestRenewableScenario)
        return;
    renderSectorShifts(latestRenewableScenario.sectors);
    const dialog = element("portfolio-dialog");
    if (!dialog.open)
        dialog.showModal();
}
function increaseRenewableScenario() {
    const step = Number(element("renewable-step").value);
    if (!Number.isFinite(step) || step <= 0) {
        showError("Enter a positive percentage.");
        return;
    }
    const nextMultiplier = renewableMultiplier * (1 + step / 100);
    void runRenewableScenario(Number.isFinite(nextMultiplier) ? nextMultiplier : Number.MAX_VALUE);
}
function resetRenewableScenario() {
    void runRenewableScenario(1);
}
function showDemo(name) {
    for (const candidate of ["compare", "reach", "ranking", "fund"]) {
        const active = candidate === name;
        element("demo-" + candidate).hidden = !active;
        const tab = element("demo-tab-" + candidate);
        tab.setAttribute("aria-selected", String(active));
    }
    showError();
    showStatus();
    if (name === "ranking" && !element("ranking-results").children.length)
        void runRanking(Number(element("ranking-weight").value));
    if (name === "fund" && !element("renewable-ranking").querySelector("tbody").children.length) {
        void runRenewableScenario(1);
    }
}
function openEvidence(evaluation, field) {
    element("evidence-company").textContent = evaluation.company.name;
    element("evidence-title").textContent = fieldLabel(field);
    const body = element("evidence-body");
    body.replaceChildren(make("p", formatValue(evaluation.features[field.feature_name], field.canonical_unit), "evidence-value"));
    const metadata = evaluation.metadata[field.feature_name];
    if (!isRecord(metadata))
        body.append(make("p", "No source evidence is recorded for this parameter.", "note"));
    else {
        const list = make("dl", "", "evidence-list");
        for (const [key, value] of Object.entries(metadata)) {
            if (key === "source_view_url" || value === null || value === undefined || value === "")
                continue;
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
    const dialog = element("evidence-dialog");
    if (!dialog.open)
        dialog.showModal();
}
function installEvents() {
    element("login-form").onsubmit = async (event) => {
        event.preventDefault();
        const input = element("viewer-password");
        try {
            await request("/api/login", { method: "POST", body: JSON.stringify({ password: input.value }) });
            input.value = "";
            await loadWorkspace();
            if (run && demos)
                showApp();
        }
        catch (error) {
            element("login-error").textContent = error instanceof Error ? error.message : String(error);
        }
    };
    element("logout").onclick = async () => {
        try {
            await request("/api/logout", { method: "POST", body: "{}" });
        }
        finally {
            showLogin();
        }
    };
    for (const name of ["compare", "reach", "ranking", "fund"])
        element("demo-tab-" + name).onclick = () => showDemo(name);
    element("pair-preset").onchange = () => void applyPairPreset();
    element("compare-target").onchange = () => void changeCompareTarget();
    element("compare-company-a").onchange = () => { keepPairDistinct("a"); resetPairScenario(false); };
    element("compare-company-b").onchange = () => { keepPairDistinct("b"); resetPairScenario(false); };
    element("compare-parameter").onchange = () => resetPairScenario(false);
    element("compare-apply-to").onchange = () => { renderPairDescription(); renderCompareButtons(); };
    element("compare-decrease").onclick = () => void runCumulativePairStep("decrease");
    element("compare-increase").onclick = () => void runCumulativePairStep("increase");
    element("compare-reset").onclick = () => resetPairScenario();
    element("reach-target").onchange = event => void prepareReachDemo(event.currentTarget.value);
    element("reach-company").onchange = setReachDesiredScore;
    element("reach-model").onchange = setReachDesiredScore;
    element("reach-score").oninput = updateReachSolutionsButton;
    element("reach-find").onclick = () => void findTargetSuggestions();
    element("ranking-weight").oninput = scheduleRanking;
    element("renewable-increase").onclick = increaseRenewableScenario;
    element("renewable-reset").onclick = resetRenewableScenario;
    element("compute-portfolio").onclick = openRenewablePortfolio;
    element("portfolio-close").onclick = () => element("portfolio-dialog").close();
    element("evidence-close").onclick = () => element("evidence-dialog").close();
}
async function loadWorkspace() {
    try {
        const [loadedRun, loadedDemos] = await Promise.all([
            request("/api/model-comparison/run"),
            request("/api/model-comparison/demos"),
        ]);
        run = loadedRun;
        demos = loadedDemos;
        if (!demos.pair_presets.length)
            throw new Error("No validated two-company demo preset is published.");
        if (demos.snapshot_id && demos.snapshot_id !== run.snapshot_id)
            throw new Error("The published demo presets and model snapshot differ. Reload the page.");
        element("run-summary").textContent = run.prediction_as_of + " · saved models";
        element("pair-preset").replaceChildren(...demos.pair_presets.map(preset => new Option(preset.label, preset.id)));
        await applyPairPreset();
        const reachPreset = demos.reach_preset;
        await prepareReachDemo(reachPreset?.target || currentPairPreset().target, reachPreset?.company_id || currentPairPreset().company_ids[0]);
        if (reachPreset) {
            element("reach-model").value = reachPreset.model_family;
            element("reach-score").value = String(reachPreset.desired_score);
            updateReachSolutionsButton();
        }
        await loadCompanies(demos.ranking_preset.target);
        showDemo("compare");
    }
    catch (error) {
        handleRequestError(error);
    }
}
async function initialize() {
    installEvents();
    try {
        const session = await request("/api/session");
        if (!session.authenticated) {
            showLogin();
            return;
        }
        await loadWorkspace();
        if (run && demos)
            showApp();
    }
    catch (error) {
        showLogin(error instanceof HttpError && error.status === 401 ? "" : error instanceof Error ? error.message : String(error));
    }
}
void initialize();
export {};

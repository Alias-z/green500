import { METRIC_LABELS, SECTORS, type Company, type Sector } from "./sp500";

export function parseResults(input: unknown): Company[] {
  if (!input || typeof input !== "object" || !("companies" in input) || !Array.isArray(input.companies)) {
    throw new Error("Results must contain a companies array.");
  }
  const seen = new Set<string>();
  return input.companies.map((record: any, id: number) => {
    const company = record?.company;
    if (!company || typeof company.ticker !== "string" || typeof company.name !== "string" ||
        !SECTORS.includes(company.sector as Sector) || seen.has(company.ticker)) {
      throw new Error(`Invalid or duplicate company at row ${id + 1}.`);
    }
    seen.add(company.ticker);
    const metrics = METRIC_LABELS.map(label => record.materiality?.topics?.[label]);
    if (metrics.some(value => typeof value !== "number" || !Number.isInteger(value) || value < 0 || value > 3)) {
      throw new Error(`Invalid materiality ratings for ${company.ticker}.`);
    }
    return {
      id, ticker: company.ticker, name: company.name, sector: company.sector,
      metrics, avgMateriality: metrics.reduce((a, b) => a + b, 0) / metrics.length,
      highPriorityTopics: record.materiality.high_priority_topics ?? "",
      profileMethod: record.materiality.method ?? "",
      environment: record.environment,
    };
  });
}

export async function loadResults(signal?: AbortSignal): Promise<Company[]> {
  const response = await fetch(`${import.meta.env.BASE_URL}results.json`, { signal, cache: "no-cache" });
  if (!response.ok) throw new Error(`Unable to load company results (HTTP ${response.status}).`);
  return parseResults(await response.json());
}

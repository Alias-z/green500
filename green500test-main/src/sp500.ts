export const SECTORS = ["Health Care","Information Technology","Consumer Discretionary","Financials","Consumer Staples","Industrials","Utilities","Materials","Real Estate","Energy","Communication Services"] as const;

export type Sector = (typeof SECTORS)[number];

export const METRIC_LABELS = ["Climate & GHG","Energy","Water","Waste & Circularity","Biodiversity & Land","Pollution & Toxic Releases","Workers & Labor","Health & Safety","Human Rights & Supply Chain","Product & Customer Responsibility","Data Privacy & Cybersecurity","Community & Social Impact","Transition & Green Investment","Physical Climate Risk","Resource & Supply Resilience"] as const;

export type MetricLabel = (typeof METRIC_LABELS)[number];

export interface Company {
  id: number;
  ticker: string;
  name: string;
  sector: Sector;
  metrics: number[];
  avgMateriality: number;
  highPriorityTopics: string;
  profileMethod: string;
  environment?: Record<string, unknown>;
}

import type { Company } from './sp500';

export type SortKey = 'rank' | 'name' | 'sector' | 'score' | 'environmental' | 'social' | 'financial';
export interface PerformanceCompany extends Company {
  rank: number;
  score: number | null;
  environmental: number | null;
  social: number | null;
  financial: number | null;
}
export function rankPerformance(companies: Company[]): PerformanceCompany[] {
  const rows = companies.map(company => {
    const environmental = company.scores?.environmental?.value ?? null;
    const social = company.scores?.social?.value ?? null;
    const financial = company.scores?.financial?.value ?? null;
    const available = [[environmental, 50], [social, 25], [financial, 25]]
      .filter((pair): pair is [number, number] => pair[0] !== null);
    const score = available.length ? Math.round(available.reduce((sum, [value, weight]) => sum + value * weight, 0)
      / available.reduce((sum, [, weight]) => sum + weight, 0) * 10) / 10 : null;
    return { ...company, environmental, social, financial, score, rank: 0 };
  });
  rows.sort((a, b) => comparePerformance(a, b, 'score', 'desc'));
  return rows.map((company, index) => ({ ...company, rank: index + 1 }));
}
export function comparePerformance(a: PerformanceCompany, b: PerformanceCompany, key: SortKey, dir: 'asc' | 'desc') {
  const left = a[key], right = b[key];
  // Missing is always last, including descending order; zero is a real score.
  if (left === null && right !== null) return 1;
  if (right === null && left !== null) return -1;
  const cmp = left === null || right === null ? 0 : typeof left === 'string' && typeof right === 'string'
    ? left.localeCompare(right) : Number(left) - Number(right);
  return cmp * (dir === 'asc' ? 1 : -1) || a.name.localeCompare(b.name) || a.ticker.localeCompare(b.ticker);
}

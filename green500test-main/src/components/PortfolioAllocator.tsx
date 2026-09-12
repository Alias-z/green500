import { useMemo, useState } from "react";
import { SECTORS, type Company, type Sector } from "../sp500";
import { scoreAndRank, type SustainabilityWeights } from "../scoring";

interface Props { companies: Company[]; weights: SustainabilityWeights; }
const COLORS = ["#79ab52","#0071e3","#af52de","#ff9500","#5ac8fa","#ff3b30","#a2845e","#5856d6","#30b0c7","#8e8e93","#ff2d55"];

function compactCHF(value: number): string {
  const compact = (divisor: number, suffix: string) => {
    const number = value / divisor;
    return `CHF ${number >= 100 ? number.toFixed(0) : number >= 10 ? number.toFixed(1) : number.toFixed(2)}${suffix}`
      .replace(/\.0(?=[A-Z]$)/, "")
      .replace(/(\.\d)0(?=[A-Z]$)/, "$1");
  };
  if (value >= 1_000_000_000) return compact(1_000_000_000, "B");
  if (value >= 1_000_000) return compact(1_000_000, "M");
  if (value >= 1_000) return compact(1_000, "K");
  return `CHF ${Math.round(value)}`;
}

export default function PortfolioAllocator({ companies, weights }: Props) {
  const [amount, setAmount] = useState(10000);
  const [holdings, setHoldings] = useState(15);

  const rows = useMemo(() => {
    const ranked = scoreAndRank(companies, weights).sort((a, b) => b.score - a.score || a.name.localeCompare(b.name));
    const sectorLeaders = SECTORS.map(sector => ranked.find(company => company.sector === sector)).filter((company): company is (typeof ranked)[number] => Boolean(company));
    const selectedIds = new Set(sectorLeaders.map(company => company.id));
    const remaining = ranked.filter(company => !selectedIds.has(company.id));
    const selected = [...sectorLeaders, ...remaining.slice(0, Math.max(0, holdings - sectorLeaders.length))]
      .sort((a, b) => b.score - a.score || a.name.localeCompare(b.name));
    const scoreTotal = selected.reduce((sum, company) => sum + Math.max(company.score, 1), 0);
    return selected.map(company => ({
      ...company,
      allocation: amount * Math.max(company.score, 1) / scoreTotal,
      share: Math.max(company.score, 1) / scoreTotal * 100,
    }));
  }, [companies, weights, amount, holdings]);

  const sectors = useMemo(() => {
    const totals = new Map<Sector, number>(SECTORS.map(sector => [sector, 0]));
    rows.forEach(row => totals.set(row.sector, (totals.get(row.sector) ?? 0) + row.share));
    return SECTORS.map(sector => [sector, totals.get(sector) ?? 0] as const);
  }, [rows]);

  let cursor = 0;
  const gradient = sectors.map(([sector, share]) => {
    const index = SECTORS.indexOf(sector);
    const start = cursor;
    cursor += share;
    return `${COLORS[index]} ${start}% ${cursor}%`;
  }).join(",");
  const money = new Intl.NumberFormat("en-CH", { style: "currency", currency: "CHF", maximumFractionDigits: 0 });

  return (
    <section className="apple-shell portfolio-card">
      <div className="portfolio-heading">
        <div><p className="eyebrow">PERSONAL PORTFOLIO</p><h2>Portfolio allocator</h2><p>Includes every GICS sector, then adds the highest-scoring companies using your sustainability weights.</p></div>
        <div className="portfolio-inputs">
          <label>Portfolio value<input type="number" min="100" step="100" value={amount} onChange={event => setAmount(Math.max(0, Number(event.target.value)))} /></label>
          <label>Holdings<select value={holdings} onChange={event => setHoldings(Number(event.target.value))}>{[11,15,20,25,30].map(number => <option key={number}>{number}</option>)}</select></label>
        </div>
      </div>
      <div className="allocation-summary">
        <div className="donut" style={{ background: `conic-gradient(${gradient})` }}><div><strong>{compactCHF(amount)}</strong><span>allocated</span></div></div>
        <div className="sector-legend">{sectors.map(([sector, share]) => { const index=SECTORS.indexOf(sector); return <div key={sector}><i style={{ background: COLORS[index] }} /><span>{sector}</span><b>{share.toFixed(1)}%</b></div>; })}</div>
      </div>
      <div className="allocation-table">
        <div className="allocation-row allocation-header"><span>Company</span><span>Score</span><span>Weight</span><span>Allocation</span></div>
        {rows.map((row, index) => <div className="allocation-row" key={row.id}><span><em>{index + 1}</em><span><b>{row.name}</b><small>{row.ticker} · {row.sector}</small></span></span><strong>{row.score}</strong><span>{row.share.toFixed(1)}%</span><b>{money.format(row.allocation)}</b></div>)}
      </div>
      <p className="portfolio-note">Disclaimer: this allocation is for educational and informational purposes only. Consult a financial professional if you are unsure.</p>
    </section>
  );
}

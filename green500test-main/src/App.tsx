import { useState, useMemo, useCallback } from "react";
import { ALL_COMPANIES, METRIC_LABELS, type Sector } from "./sp500";
import { DEFAULT_WEIGHTS, scoreAndRank, type ScoredCompany, type SustainabilityWeights } from "./scoring";
import WeightControls from "./components/WeightControls";
import PortfolioAllocator from "./components/PortfolioAllocator";
import NetZeroFund from "./components/NetZeroFund";
import AppToolbar from "./components/AppToolbar";

type Tab = "All" | Sector;

const SECTOR_COLORS: Record<Sector, string> = {
  "Communication Services": "#5856d6", "Consumer Discretionary": "#ff9500",
  "Consumer Staples": "#a2845e", Energy: "#ff3b30", Financials: "#007aff",
  "Health Care": "#79ab52", Industrials: "#af52de", "Information Technology": "#5ac8fa",
  Materials: "#8e8e93", "Real Estate": "#ff2d55", Utilities: "#30b0c7",
};

const SCORE_COLOR = (s: number) =>
  s >= 75 ? "#79ab52" : s >= 50 ? "#79ab52" : s >= 30 ? "#ff9f0a" : "#ff453a";

const INDICATORS = METRIC_LABELS.map((label, index) => ({ label, index }));

function ExpandChevron({ open }: { open: boolean }) {
  return (
    <svg className="expand-chevron" style={{ transform: open ? "rotate(180deg)" : "rotate(0deg)" }} width="12" height="8" viewBox="0 0 12 8" fill="none" aria-hidden="true">
      <path d="M1.5 1.5L6 6L10.5 1.5" stroke="#6e6e73" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

// Company logos use two independent market-data sources before a guaranteed monogram fallback.
function CompanyLogo({ name, ticker, size = 32 }: { name: string; ticker: string; size?: number }) {
  const initial = name.charAt(0).toUpperCase();
  const [sourceIndex, setSourceIndex] = useState(0);
  const normalizedTicker = ticker.replaceAll(".", "-");
  const sources = [
    `https://assets.parqet.com/logos/symbol/${encodeURIComponent(ticker)}?format=png`,
    `https://images.financialmodelingprep.com/symbol/${normalizedTicker}.png`,
  ];

  if (sourceIndex >= sources.length) {
    return (
      <div
        className="rounded-[10px] flex items-center justify-center shrink-0 font-semibold"
        style={{ width: size, height: size, background: "#e8e8ed", color: "#636366", fontSize: size * 0.4 }}
        aria-label={`${name} logo fallback`}
      >
        {initial}
      </div>
    );
  }

  return (
    <div
      className="rounded-[10px] shrink-0 overflow-hidden flex items-center justify-center"
      style={{ width: size, height: size, background: "#f2f2f7", boxShadow: "0 0 0 0.5px rgba(0,0,0,0.12), 0 1px 3px rgba(0,0,0,0.06)" }}
    >
      <img
        src={sources[sourceIndex]}
        alt={`${name} logo`}
        className="w-full h-full object-cover"
        loading="lazy"
        onError={() => setSourceIndex(index => index + 1)}
      />
    </div>
  );
}

function MiniBar({ value, color }: { value: number; color: string }) {
  return (
    <div className="flex items-center gap-1.5">
      <div className="flex-1 rounded-full overflow-hidden" style={{ height: 3, background: "#e8e8ed" }}>
        <div className="h-full rounded-full transition-all duration-500" style={{ width: `${value}%`, background: color }} />
      </div>
      <span className="text-[11px] tabular-nums" style={{ color: "#86868b", minWidth: 22, textAlign: "right", fontFamily: "var(--font-sans)" }}>
        {value}
      </span>
    </div>
  );
}

function IndicatorBar({ label, raw }: { label: string; raw: number }) {
  const value = Math.round((raw / 3) * 100);
  return (
    <div className="space-y-1">
      <div className="flex justify-between items-center">
        <span className="text-[12px]" style={{ color: "#636366" }}>{label}</span>
        <span className="text-[11px] px-1.5 py-px rounded-full" style={{ background: "rgba(0,113,227,0.08)", color: "#0071e3", fontFamily: "var(--font-sans)" }}>{raw}/3</span>
      </div>
      <MiniBar value={value} color="#0071e3" />
    </div>
  );
}

function ExpandedRow({ company }: { company: ScoredCompany }) {
  return (
    <tr className="row-expand">
      <td colSpan={7} className="px-4 pb-4 pt-0">
        <div
          className="rounded-[14px] p-4 grid gap-3 mx-10"
          style={{
            background: "#ffffff",
            border: "0.5px solid rgba(0,0,0,0.08)",
            gridTemplateColumns: "1fr 1fr",
            columnGap: 24,
          }}
        >
          {INDICATORS.map((indicator) => (
            <IndicatorBar key={indicator.label} label={indicator.label} raw={company.metrics[indicator.index]} />
          ))}
        </div>
      </td>
    </tr>
  );
}

const PAGE_SIZE = 20;

export default function App() {
  const [activeTab, setActiveTab] = useState<Tab>("All");
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [page, setPage] = useState(0);
  const [sortKey, setSortKey] = useState<"rank" | "score" | "name">("rank");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("asc");
  const [searchQuery, setSearchQuery] = useState("");
  const [weights, setWeights] = useState<SustainabilityWeights>(DEFAULT_WEIGHTS);
  const [view, setView] = useState<"dashboard" | "portfolio" | "netzero">("dashboard");

  const scored = useMemo(() => {
    const sectorCompanies = activeTab === "All" ? ALL_COMPANIES : ALL_COMPANIES.filter(c => c.sector === activeTab);
    const query = searchQuery.trim().toLocaleLowerCase();
    const filtered = query ? sectorCompanies.filter(c => c.name.toLocaleLowerCase().includes(query) || c.ticker.toLocaleLowerCase().includes(query)) : sectorCompanies;
    const ranked = scoreAndRank(filtered, weights);
    if (activeTab !== "All") return ranked;
    return [...ranked]
      .sort((a, b) => b.score - a.score || a.name.localeCompare(b.name))
      .map((company, index, companies) => ({
        ...company,
        sectorRank: index + 1,
        sectorCount: companies.length,
      }));
  }, [activeTab, searchQuery, weights]);

  const sorted = useMemo(() => {
    const arr = [...scored];
    arr.sort((a, b) => {
      const cmp =
        sortKey === "rank" ? a.sectorRank - b.sectorRank :
        sortKey === "score" ? b.score - a.score :
        a.name.localeCompare(b.name);
      return sortDir === "asc" ? cmp : -cmp;
    });
    return arr;
  }, [scored, sortKey, sortDir]);

  const pageData = useMemo(() => sorted.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE), [sorted, page]);
  const totalPages = Math.ceil(sorted.length / PAGE_SIZE);

  const toggleExpanded = useCallback((id: number) => {
    setExpanded(prev => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  }, []);

  const handleSort = (key: typeof sortKey) => {
    if (sortKey === key) setSortDir(d => d === "asc" ? "desc" : "asc");
    else { setSortKey(key); setSortDir("asc"); }
    setPage(0);
  };

  const SortArrow = ({ col }: { col: typeof sortKey }) =>
    sortKey === col ? (
      <span className="ml-1" style={{ color: "#0071e3", fontSize: 10 }}>{sortDir === "asc" ? "↑" : "↓"}</span>
    ) : (
      <span className="ml-1" style={{ color: "#d1d1d6", fontSize: 10 }}>⇅</span>
    );

  return (
    <div className="min-h-screen" style={{ background: "#f5f5f7", fontFamily: "-apple-system, BlinkMacSystemFont, 'SF Pro Text', sans-serif" }}>

      {/* Toolbar from import (adapted) */}
      <div style={{ position: "sticky", top: 0, zIndex: 40, background: "rgba(245,245,247,0.88)", backdropFilter: "blur(20px)", WebkitBackdropFilter: "blur(20px)", borderBottom: "0.5px solid rgba(0,0,0,0.1)" }}>
        <AppToolbar
          activeTab={activeTab}
          onTabChange={(t) => { setActiveTab(t); setPage(0); setExpanded(new Set()); }}
          count={sorted.length}
          searchQuery={searchQuery}
          onSearchQueryChange={(query) => { setSearchQuery(query); setPage(0); setExpanded(new Set()); }}
          view={view}
          onViewChange={setView}
        />

      </div>

      {view !== "netzero" && <WeightControls weights={weights} onChange={setWeights} />}

      {/* Table */}
      <div className={`${view === "dashboard" ? "block" : "hidden"} apple-shell mt-6 mb-10 rounded-[22px] overflow-hidden`} style={{ background: "#ffffff", boxShadow: "0 1px 3px rgba(0,0,0,0.08), 0 0 0 0.5px rgba(0,0,0,0.06)" }}>
        <table className="w-full table-fixed border-collapse text-sm"><colgroup><col style={{width:"7%"}}/><col style={{width:"24%"}}/><col style={{width:"19%"}}/><col style={{width:"20%"}}/><col style={{width:"14%"}}/><col style={{width:"14%"}}/><col style={{width:"2%"}}/></colgroup>
          <thead>
            <tr style={{ borderBottom: "0.5px solid rgba(0,0,0,0.08)", background: "#fafafa" }}>
              <th className="text-left px-4 py-3 cursor-pointer select-none" style={{ color: "#86868b", fontSize: 11, fontWeight: 600, letterSpacing: "0.04em", width: 70 }} onClick={() => handleSort("rank")}>
                RANK <SortArrow col="rank" />
              </th>
              <th className="text-left px-4 py-3 cursor-pointer select-none" style={{ color: "#86868b", fontSize: 11, fontWeight: 600, letterSpacing: "0.04em" }} onClick={() => handleSort("name")}>
                COMPANY <SortArrow col="name" />
              </th>
              <th className="text-left px-4 py-3" style={{ color: "#86868b", fontSize: 11, fontWeight: 600, letterSpacing: "0.04em" }}>
                SECTOR
              </th>
              <th className="text-left px-4 py-3 cursor-pointer select-none" style={{ color: "#86868b", fontSize: 11, fontWeight: 600, letterSpacing: "0.04em", minWidth: 180 }} onClick={() => handleSort("score")}>
                SUSTAINABILITY SCORE <SortArrow col="score" />
              </th>
              <th className="text-left px-4 py-3" style={{ color: "#86868b", fontSize: 11, fontWeight: 600, letterSpacing: "0.04em", minWidth: 100 }}>
                ENVIRONMENTAL
              </th>
              <th className="text-left px-4 py-3" style={{ color: "#86868b", fontSize: 11, fontWeight: 600, letterSpacing: "0.04em", minWidth: 100 }}>
                SOCIAL & RESILIENCE
              </th>
              <th style={{ width: 20 }} />
            </tr>
          </thead>
          <tbody>
            {pageData.map((company) => {
              const isOpen = expanded.has(company.id);
              const sColor = "#0071e3";
              const scoreColor = "#79ab52";
              const emAvg = Math.round((company.metrics.slice(0, 6).reduce((sum, value) => sum + value, 0) / 18) * 100);
              const resAvg = Math.round((company.metrics.slice(6).reduce((sum, value) => sum + value, 0) / 27) * 100);

              return [
                <tr
                  key={company.id}
                  onClick={() => toggleExpanded(company.id)}
                  className="interactive-row cursor-pointer"
                  style={{ borderBottom: isOpen ? "none" : "0.5px solid rgba(0,0,0,0.06)" }}
                  onMouseEnter={e => { if (!isOpen) (e.currentTarget as HTMLElement).style.background = "#f9f9fb"; }}
                  onMouseLeave={e => { if (!isOpen) (e.currentTarget as HTMLElement).style.background = "transparent"; }}
                >
                  {/* Rank */}
                  <td className="px-4 py-3">
                    <div className="flex flex-col">
                      <span className="font-semibold text-[12px]" style={{ color: "#1d1d1f", fontFamily: "var(--font-sans)" }}>
                        {company.sectorRank}
                      </span>
                      <span className="text-[11px]" style={{ color: "#c7c7cc", fontFamily: "var(--font-sans)" }}>
                        /{company.sectorCount}
                      </span>
                    </div>
                  </td>

                  {/* Company */}
                  <td className="px-4 py-3">
                    <div className="flex items-center gap-3">
                      <CompanyLogo name={company.name} ticker={company.ticker} size={32} />
                      <div>
                        <div className="font-medium text-[14px]" style={{ color: "#1d1d1f" }}>{company.name}</div>
                        <div className="text-[11px]" style={{ color: "#86868b", fontFamily: "var(--font-sans)" }}>{company.ticker}</div>
                      </div>
                    </div>
                  </td>

                  {/* Sector */}
                  <td className="px-4 py-3">
                    <span
                      className="text-[12px] px-2 py-0.5 rounded-full font-medium"
                      style={{ background: `${sColor}12`, color: sColor }}
                    >
                      {company.sector}
                    </span>
                  </td>

                  {/* Score */}
                  <td className="px-4 py-3">
                    <div className="flex items-center gap-3">
                      <span className="text-[16px] font-semibold tabular-nums" style={{ color: scoreColor, minWidth: 36, fontFamily: "-apple-system, BlinkMacSystemFont, 'SF Pro Display', 'SF Pro Text', sans-serif", letterSpacing: "-0.02em" }}>
                        {company.avgMateriality.toFixed(2)}
                      </span>
                      <div className="flex-1" style={{ minWidth: 80 }}>
                        <div className="rounded-full" style={{ height: 4, background: "#f2f2f7", minWidth: 80 }}>
                          <div
                            className="rounded-full transition-all duration-500"
                            style={{ height: 4, width: `${company.score}%`, background: scoreColor }}
                          />
                        </div>
                      </div>
                    </div>
                  </td>

                  {/* Emissions avg */}
                  <td className="px-4 py-3" style={{ minWidth: 100 }}>
                    <MiniBar value={emAvg} color={SCORE_COLOR(emAvg)} />
                  </td>

                  {/* Resilience avg */}
                  <td className="px-4 py-3" style={{ minWidth: 100 }}>
                    <MiniBar value={resAvg} color={SCORE_COLOR(resAvg)} />
                  </td>

                  {/* Expand chevron */}
                  <td className="pr-4 py-3 text-center">
                    <ExpandChevron open={isOpen} />
                  </td>
                </tr>,

                isOpen && (
                  <ExpandedRow key={`${company.id}-exp`} company={company} />
                ),
              ];
            })}
            {pageData.length === 0 && (
              <tr><td colSpan={7} className="px-5 py-14 text-center text-[14px]" style={{ color: "#86868b" }}>No companies match “{searchQuery}”.</td></tr>
            )}
          </tbody>
        </table>

        {/* Pagination — Apple-style */}
        <div className="flex items-center justify-between px-5 py-3" style={{ borderTop: "0.5px solid rgba(0,0,0,0.08)" }}>
          <span className="text-[13px]" style={{ color: "#86868b" }}>
            {sorted.length === 0 ? 0 : page * PAGE_SIZE + 1}–{Math.min((page + 1) * PAGE_SIZE, sorted.length)} of {sorted.length}
          </span>
          <div className="flex items-center gap-1">
            <PaginationBtn onClick={() => setPage(0)} disabled={page === 0} label="«" />
            <PaginationBtn onClick={() => setPage(p => p - 1)} disabled={page === 0} label="‹" />
            {Array.from({ length: Math.min(7, totalPages) }, (_, i) => {
              let p = page < 4 ? i : page > totalPages - 5 ? totalPages - 7 + i : page - 3 + i;
              if (p < 0) p = i;
              return (
                <button
                  key={p}
                  onClick={() => setPage(p)}
                  className="transition-all text-[13px] rounded-lg"
                  style={{
                    minWidth: 30, height: 30,
                    background: p === page ? "#0071e3" : "transparent",
                    color: p === page ? "#ffffff" : "#1d1d1f",
                    fontFamily: "var(--font-sans)",
                    border: "none", cursor: "pointer",
                  }}
                >
                  {p + 1}
                </button>
              );
            })}
            <PaginationBtn onClick={() => setPage(p => p + 1)} disabled={totalPages === 0 || page === totalPages - 1} label="›" />
            <PaginationBtn onClick={() => setPage(totalPages - 1)} disabled={totalPages === 0 || page === totalPages - 1} label="»" />
          </div>
          <span className="text-[13px]" style={{ color: "#c7c7cc", fontFamily: "var(--font-sans)" }}>
            {totalPages === 0 ? 0 : page + 1} / {totalPages}
          </span>
        </div>
      </div>
      {view === "portfolio" && <PortfolioAllocator companies={ALL_COMPANIES} weights={weights} />}
      {view === "netzero" && <NetZeroFund companies={ALL_COMPANIES} />}
    </div>
  );
}

function PaginationBtn({ onClick, disabled, label }: { onClick: () => void; disabled: boolean; label: string }) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className="transition-all text-[14px] rounded-lg"
      style={{
        minWidth: 30, height: 30,
        background: "transparent",
        color: disabled ? "#d1d1d6" : "#1d1d1f",
        border: "none",
        cursor: disabled ? "not-allowed" : "pointer",
      }}
    >
      {label}
    </button>
  );
}

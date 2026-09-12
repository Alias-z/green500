import { useMemo, useState } from "react";
import { SECTORS, type Company, type Sector } from "../sp500";

interface Props { companies: Company[]; }
interface ClimateCompany extends Company {
  climateScore: number; intensity: number; momentum: number; credibility: number;
  transition: number; risk: number; capacity: number;
}
const SECTOR_COLORS=["#79ab52","#0071e3","#af52de","#ff9500","#5ac8fa","#ff3b30","#a2845e","#5856d6","#30b0c7","#8e8e93","#ff2d55"];
const BASELINE:Record<Sector,number>={
  "Communication Services":8,"Consumer Discretionary":10,"Consumer Staples":7,Energy:4,Financials:13,
  "Health Care":12,Industrials:9,"Information Technology":29,Materials:3,"Real Estate":3,Utilities:2
};
const hash=(value:string)=>[...value].reduce((sum,char)=>((sum*31+char.charCodeAt(0))>>>0),17);
const clamp=(value:number,min=0,max=100)=>Math.min(max,Math.max(min,value));
const compactCHF=(value:number)=>new Intl.NumberFormat("en-CH",{style:"currency",currency:"CHF",notation:"compact",maximumFractionDigits:1}).format(value);

function modelCompany(company:Company,fund:number):ClimateCompany{
  const seed=hash(company.ticker);
  const climate=(company.metrics[0]+company.metrics[1]+company.metrics[4])/9*100;
  const transition=(company.metrics[12]+company.metrics[14])/6*100;
  const resilience=(company.metrics[13]/3)*100;
  const intensity=clamp(112-climate*.82+(seed%31),12,145);
  const momentum=clamp(22+transition*.58+(seed%23),10,96);
  const credibility=clamp(18+resilience*.56+((seed>>3)%29),8,98);
  const risk=clamp(45-company.metrics[9]*7+((seed>>5)%24),5,70);
  const climateScore=Math.round(clamp((145-intensity)/1.33*.35+momentum*.25+credibility*.20+transition*.15+(100-risk)*.05));
  const liquidityFactor=.55+((seed%46)/100);
  const capacity=Math.max(fund*.005,Math.min(fund*.05,50_000_000*liquidityFactor));
  return {...company,climateScore,intensity:Math.round(intensity),momentum:Math.round(momentum),credibility:Math.round(credibility),transition:Math.round(transition),risk:Math.round(risk),capacity};
}

export default function NetZeroFund({companies}:Props){
  const [fund,setFund]=useState(1_000_000_000);
  const [maxPosition,setMaxPosition]=useState(5);
  const [sectorBand,setSectorBand]=useState(5);
  const result=useMemo(()=>{
    const modeled=companies.map(company=>modelCompany(company,fund)).sort((a,b)=>b.climateScore-a.climateScore||a.name.localeCompare(b.name));
    const weights=new Map<number,number>();
    // Start every sector at its benchmark less the selected band, preventing accidental sector exclusion.
    for(const sector of SECTORS){
      const candidates=modeled.filter(company=>company.sector===sector).slice(0,8);
      let target=Math.max(.5,BASELINE[sector]-sectorBand);
      for(const company of candidates){
        if(target<=.001) break;
        const cap=Math.min(maxPosition,company.capacity/fund*100);
        const weight=Math.min(cap,target);
        if(weight>0){weights.set(company.id,weight);target-=weight;}
      }
    }
    let allocated=[...weights.values()].reduce((sum,value)=>sum+value,0);
    for(let passes=0;passes<8&&allocated<99.999;passes++){
      for(const company of modeled){
        if(allocated>=99.999) break;
        const current=weights.get(company.id)??0;
        const sectorTotal=[...weights].reduce((sum,[id,w])=>sum+(modeled.find(c=>c.id===id)?.sector===company.sector?w:0),0);
        const sectorCap=BASELINE[company.sector]+sectorBand;
        const companyCap=Math.min(maxPosition,company.capacity/fund*100);
        const add=Math.min(companyCap-current,sectorCap-sectorTotal,100-allocated,.75);
        if(add>.001){weights.set(company.id,current+add);allocated+=add;}
      }
    }
    const rows=modeled.filter(company=>weights.has(company.id)).map(company=>({...company,weight:weights.get(company.id)??0})).sort((a,b)=>b.weight-a.weight||b.climateScore-a.climateScore);
    const invested=rows.reduce((sum,row)=>sum+row.weight,0);
    const weighted=(key:"intensity"|"momentum"|"credibility"|"climateScore")=>rows.reduce((sum,row)=>sum+row[key]*row.weight,0)/Math.max(invested,1);
    const sectors=SECTORS.map(sector=>({sector,weight:rows.filter(row=>row.sector===sector).reduce((sum,row)=>sum+row.weight,0)}));
    return {rows,sectors,invested,intensity:weighted("intensity"),momentum:weighted("momentum"),credibility:weighted("credibility"),score:weighted("climateScore")};
  },[companies,fund,maxPosition,sectorBand]);

  let cursor=0;
  const gradient=result.sectors.map(({sector,weight})=>{const start=cursor;cursor+=weight;return `${SECTOR_COLORS[SECTORS.indexOf(sector)]} ${start}% ${cursor}%`}).join(",");
  return <section className="apple-shell netzero-card">
    <div className="netzero-hero">
      <div><p className="eyebrow">NET-ZERO FUND · HACKATHON MVP</p><h2>Transition optimizer</h2><p>Maximizes a forward-looking climate score while enforcing diversification, position limits, and modeled CHF 1B liquidity capacity.</p></div>
      <div className="netzero-inputs">
        <label>Fund size<input value={fund} min={1_000_000} step={10_000_000} type="number" onChange={e=>setFund(Math.max(1_000_000,Number(e.target.value)||0))}/></label>
        <label>Max position <output>{maxPosition}%</output><input type="range" min="1" max="10" value={maxPosition} onChange={e=>setMaxPosition(Number(e.target.value))}/></label>
        <label>Sector band <output>±{sectorBand}%</output><input type="range" min="0" max="10" value={sectorBand} onChange={e=>setSectorBand(Number(e.target.value))}/></label>
      </div>
    </div>
    <div className="model-banner"><strong>Modeled proxy</strong><span>Climate inputs are inferred from the current materiality sheet for product testing. Replace with licensed or verified issuer data before investment use.</span></div>
    <div className="netzero-kpis">
      <div><span>Climate score</span><strong>{result.score.toFixed(0)}</strong><small>/100 proxy</small></div>
      <div><span>Carbon intensity</span><strong>{result.intensity.toFixed(0)}</strong><small>index = 100 baseline</small></div>
      <div><span>Reduction momentum</span><strong>{result.momentum.toFixed(0)}</strong><small>/100 proxy</small></div>
      <div><span>Target credibility</span><strong>{result.credibility.toFixed(0)}</strong><small>/100 proxy</small></div>
    </div>
    <div className="netzero-summary">
      <div className="donut" style={{background:`conic-gradient(${gradient})`}}><div><strong>{compactCHF(fund)}</strong><span>{result.invested.toFixed(1)}% invested</span></div></div>
      <div className="sector-legend">{result.sectors.map(({sector,weight})=><div key={sector}><i style={{background:SECTOR_COLORS[SECTORS.indexOf(sector)]}}/><span>{sector}</span><b>{weight.toFixed(1)}%</b></div>)}</div>
    </div>
    <div className="allocation-table netzero-table">
      <div className="allocation-row allocation-header"><span>Company</span><span>Climate</span><span>Intensity</span><span>Weight / allocation</span></div>
      {result.rows.map((row,index)=><div className="allocation-row" key={row.id}><span><em>{index+1}</em><span><b>{row.name}</b><small>{row.ticker} · {row.sector}</small></span></span><strong>{row.climateScore}</strong><span>{row.intensity}</span><b>{row.weight.toFixed(2)}% · {compactCHF(fund*row.weight/100)}</b></div>)}
    </div>
    <div className="method-card"><h3>Objective and guardrails</h3><p><b>Objective:</b> 35% lower emissions intensity, 25% reduction momentum, 20% target credibility, 15% transition investment, and 5% controversy risk. <b>Constraints:</b> every sector represented, sector exposure within the selected S&P 500 benchmark band, per-company cap, and modeled liquidity capacity.</p><p>This is a decision-support prototype—not investment advice, a forecast, or a guarantee of net zero. A production optimizer must ingest dated Scope 1, 2 and material Scope 3 emissions, enterprise value, revenue, targets, controversies, prices and average daily trading volume, with audit trails and periodic rebalancing.</p></div>
  </section>;
}

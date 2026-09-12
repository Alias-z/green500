import type { Company } from "./sp500";
export interface SustainabilityWeights { environmental:number; financial:number; social:number; }
export const DEFAULT_WEIGHTS:SustainabilityWeights={environmental:50,financial:25,social:25};
export interface ScoredCompany extends Company { score:number; sectorRank:number; sectorCount:number; }
const avg=(values:number[])=>values.reduce((sum,value)=>sum+value,0)/values.length;
export function sustainabilityScore(company:Company,weights:SustainabilityWeights):number{
 const total=weights.environmental+weights.financial+weights.social;
 const normalized=total===0?DEFAULT_WEIGHTS:weights;
 const divisor=total===0?100:total;
 const environmental=avg(company.metrics.slice(0,6))/3*100;
 const social=avg(company.metrics.slice(6,12))/3*100;
 const financial=avg(company.metrics.slice(12,15))/3*100;
 return environmental*(normalized.environmental/divisor)+financial*(normalized.financial/divisor)+social*(normalized.social/divisor);
}
export function scoreAndRank(companies:Company[],weights:SustainabilityWeights):ScoredCompany[]{
 const groups=new Map<string,Company[]>();
 for(const company of companies){const group=groups.get(company.sector)??[];group.push(company);groups.set(company.sector,group)}
 const result:ScoredCompany[]=[];
 for(const group of groups.values()){const ranked=[...group].sort((a,b)=>sustainabilityScore(b,weights)-sustainabilityScore(a,weights)||a.name.localeCompare(b.name));ranked.forEach((company,index)=>result.push({...company,score:Math.round(sustainabilityScore(company,weights)),sectorRank:index+1,sectorCount:ranked.length}))}
 return result;
}
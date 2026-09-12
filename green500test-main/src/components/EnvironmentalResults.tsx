import type { Company } from "../sp500";

const isObject = (value: unknown): value is Record<string, any> => !!value && typeof value === "object" && !Array.isArray(value);
const label = (key: string) => key.replaceAll("_", " ");

export default function EnvironmentalResults({ company }: { company: Company }) {
  const environment = company.environment;
  if (!environment || environment.status === "not_extracted") {
    return <section className="mx-10 mb-3 p-4 rounded-xl bg-white"><h3>Environmental results</h3><p>Missing — no environmental data extracted yet.</p></section>;
  }
  const source = isObject(environment.source) ? environment.source : {};
  const observations: { name: string; metric: Record<string, any> }[] = [];
  function collect(value: unknown, name: string) {
    if (!isObject(value)) return;
    if ("value" in value) { observations.push({ name, metric: value }); return; }
    for (const [key, child] of Object.entries(value)) collect(child, `${name} / ${label(key)}`);
  }
  for (const group of ["greenhouse_gas", "energy", "water", "waste_circularity", "biodiversity_land", "pollution", "targets"]) {
    collect(environment[group], label(group));
  }
  const safeUrl = (value: unknown) => typeof value === "string" && /^https?:\/\//i.test(value) ? value : undefined;
  return <section className="mx-10 mb-3 p-4 rounded-xl bg-white border border-gray-100">
    <h3 className="font-semibold">Environmental results · {String(environment.reporting_year ?? "Period varies")}</h3>
    {source.url && <a className="text-blue-700 underline" href={safeUrl(source.url)} target="_blank" rel="noreferrer">{String(source.title ?? "Source report")}</a>}
    {isObject(environment.boundary) && <p className="text-xs mt-2 text-gray-600">{environment.boundary.description}</p>}
    {company.scores?.environmental.value != null && <div className="mt-4 p-3 rounded-lg bg-gray-50">
      <h4 className="font-semibold">Environmental score: {company.scores.environmental.value.toFixed(1)}/100 · Incomplete data</h4>
      <p className="text-xs mt-1">Equal average of {company.scores.environmental.coverage?.scored_topics} available topics out of 6. Missing topics are excluded.</p>
      {company.scores.environmental.components?.map(component => <div key={component.topic} className="mt-3 text-xs">
        <p className="font-medium">{component.label}: {component.score.toFixed(2)}/100</p>
        <p>{component.formula}</p>
        {component.annualized_change_percent != null && <p>Annualized reduction: {component.annualized_change_percent.toFixed(2)}% ({component.baseline_year}–{component.reporting_year})</p>}
        {component.inputs.map(input => <p key={input.path}>{label(input.path)}: {Number(input.value).toLocaleString()} {input.unit}
          {input.evidence?.pdf_page && safeUrl(source.url) && <> · <a className="underline text-blue-700" href={`${source.url.split("#")[0]}#page=${input.evidence.pdf_page}`} target="_blank" rel="noreferrer">PDF page {input.evidence.pdf_page}</a></>}
          {input.evidence?.section && <> · {input.evidence.section}</>}
        </p>)}
      </div>)}
      {company.scores.environmental.limitations?.map(note => <p key={note} className="text-xs text-gray-600 mt-2">{note}</p>)}
    </div>}
    <dl className="grid gap-3 mt-4 sm:grid-cols-2">
      {observations.map(({ name, metric }) => <div key={name} className="border-b border-gray-100 pb-2">
        <dt className="text-xs text-gray-600 capitalize">{name}</dt>
        <dd className="font-medium">{metric.value == null ? "Missing" : `${typeof metric.value === "number" ? metric.value.toLocaleString() : String(metric.value)} ${metric.unit ?? ""}`}</dd>
        {metric.evidence?.pdf_page && <a className="text-xs text-blue-700 underline" href={safeUrl(source.url) ? `${source.url.split("#")[0]}#page=${metric.evidence.pdf_page}` : undefined} target="_blank" rel="noreferrer">PDF page {metric.evidence.pdf_page}</a>}
        {metric.evidence?.section && <p className="text-xs text-gray-600">Section: {metric.evidence.section}</p>}
        {metric.qualification && <p className="text-xs text-gray-600 mt-1">{metric.qualification}</p>}
      </div>)}
    </dl>
    {Array.isArray(environment.comparability_notes) && <ul className="text-xs text-gray-600 mt-3 list-disc pl-4">{environment.comparability_notes.map((note: string) => <li key={note}>{note}</li>)}</ul>}
  </section>;
}

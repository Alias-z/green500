import type { SustainabilityWeights } from "../scoring";

interface Props {
  weights: SustainabilityWeights;
  onChange: (weights: SustainabilityWeights) => void;
}

const ITEMS = [
  ["environmental", "Environmental weight"],
  ["financial", "Financial/economic weight"],
  ["social", "Social weight"],
] as const;

export default function WeightControls({ weights, onChange }: Props) {
  const total = weights.environmental + weights.financial + weights.social;

  return (
    <section className="apple-shell weight-card" aria-labelledby="weight-title">
      <div>
        <h2 id="weight-title">Define what sustainability means to you</h2>
        <p>
          Scores automatically normalize your relative priorities
          {total === 0 ? " using equal default weights" : ""}.
        </p>
      </div>

      <div className="weight-grid">
        {ITEMS.map(([key, label]) => (
          <label key={key} className="weight-control">
            <span>
              <b>{label}</b>
              <output>{weights[key]}%</output>
            </span>
            <input
              type="range"
              min="0"
              max="100"
              step="5"
              value={weights[key]}
              onChange={(event) =>
                onChange({ ...weights, [key]: Number(event.target.value) })
              }
            />
          </label>
        ))}
      </div>
    </section>
  );
}

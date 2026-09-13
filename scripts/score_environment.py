"""Provisional, evidence-based indicator index; never consumes materiality or demos."""
import math

TOPICS = ['greenhouse_gas', 'energy', 'water', 'waste_circularity', 'biodiversity_land', 'pollution']
METHOD = 'environment-indicators-v1'


def score_environment(environment):
    components = []
    year = environment.get('reporting_year')

    def metric(path):
        group, key = path.split('.')
        item = environment.get(group, {}).get(key, {})
        value = item.get('value')
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            return None
        if not item.get('evidence') or item.get('status') in ['company_target', 'not_extracted', 'company_estimate']:
            return None
        return item

    def add(topic, label, score, formula, paths, **extra):
        if not math.isfinite(score):
            raise ValueError('Non-finite score')
        components.append({'topic': topic, 'label': label, 'score': round(max(0, min(100, score)), 2),
                           'formula': formula, 'inputs': [{'path': p, **metric(p)} for p in paths], **extra})

    # Compare a reported inventory with its own same-source baseline, never another sector's absolute tonnes.
    g = environment.get('greenhouse_gas', {})
    current_paths = None
    for key in ['scope_1_2_market_based', 'scope_1_2_total_market_based', 'scope_1_2_total']:
        if metric('greenhouse_gas.' + key):
            current_paths = ['greenhouse_gas.' + key]
            break
    if current_paths is None and all(metric('greenhouse_gas.' + k) for k in ['scope_1', 'scope_2_market_based']):
        current_paths = ['greenhouse_gas.scope_1', 'greenhouse_gas.scope_2_market_based']
    baseline_paths = None
    baseline_year = None
    for prior in range(year - 1, 2018, -1) if isinstance(year, int) else []:
        combined = f'greenhouse_gas.scope_1_2_market_based_{prior}'
        separate = [f'greenhouse_gas.scope_1_{prior}', f'greenhouse_gas.scope_2_market_based_{prior}']
        if metric(combined):
            baseline_paths = [combined]
        elif all(metric(p) for p in separate):
            baseline_paths = separate
        if baseline_paths:
            baseline_year = prior
            break
    if current_paths and baseline_paths:
        current = sum(metric(p)['value'] for p in current_paths)
        baseline = sum(metric(p)['value'] for p in baseline_paths)
        if baseline > 0 and all(metric(p)['unit'] == 'tCO2e' for p in current_paths + baseline_paths):
            rate = 100 * (1 - (current / baseline) ** (1 / (year - baseline_year)))
            add('greenhouse_gas', 'Scope 1 + 2 emissions trend (market-based)', 50 + 5 * rate,
                'clamp(50 + 5 × annualized reduction %, 0, 100)', current_paths + baseline_paths,
                annualized_change_percent=round(rate, 4), baseline_year=baseline_year, reporting_year=year)
    else:
        path = 'greenhouse_gas.scope_1_2_reduction_since_2019'
        item = metric(path)
        if item and isinstance(year, int) and year > 2019 and item['value'] <= 100:
            rate = 100 * (1 - (1 - item['value'] / 100) ** (1 / (year - 2019)))
            add('greenhouse_gas', 'Scope 1 + 2 emissions trend (market-based)', 50 + 5 * rate,
                'clamp(50 + 5 × annualized reduction %, 0, 100)', [path],
                annualized_change_percent=round(rate, 4), baseline_year=2019, reporting_year=year)

    for path, label in [('energy.renewable_electricity_share', 'Renewable electricity share'),
                        ('energy.office_renewable_electricity_matching_percent', 'Office electricity certificate matching')]:
        item = metric(path)
        if item and item['unit'] == 'percent' and item['value'] <= 100:
            add('energy', label, item['value'], 'Reported percentage, on a 0–100 scale', [path])
            break
    else:
        for numerator, denominator, label in [('renewable_energy', 'total_energy', 'Renewable share of total energy'),
                                               ('renewable_electricity', 'office_energy', 'Renewable electricity as share of office energy')]:
            paths = ['energy.' + numerator, 'energy.' + denominator]
            n, d = (metric(p) for p in paths)
            if n and d and d['value'] > 0 and n['value'] <= d['value'] and n['unit'] == d['unit']:
                add('energy', label, n['value'] / d['value'] * 100, '100 × renewable energy / total energy', paths)
                break

    path = 'water.withdrawal_reduction_since_2024'
    item = metric(path)
    if item and year == 2025 and item['unit'] == 'percent':
        add('water', 'Water withdrawal trend', 50 + 5 * item['value'],
            'clamp(50 + 5 × annual reduction %, 0, 100)', [path], reporting_year=2025, baseline_year=2024)

    topics = {topic: None for topic in TOPICS}
    for topic in TOPICS:
        values = [c['score'] for c in components if c['topic'] == topic]
        if values:
            topics[topic] = round(sum(values) / len(values), 2)
    available = [v for v in topics.values() if v is not None]
    return {'value': round(sum(available) / len(available), 1) if available else None,
            'scale': 100, 'status': 'provisional' if available else 'insufficient_data',
            'method': METHOD, 'reporting_year': year, 'topics': topics,
            'coverage': {'scored_topics': len(available), 'total_topics': len(TOPICS)},
            'components': components,
            'limitations': ['Project-defined indicator index, not an external ESG rating or science-based target assessment.',
                           'Trend scale: unchanged = 50; annual reduction of 10% or more = 100; increase of 10% or more = 0.',
                           'Equal weights across available topics only; missing topics are excluded, not treated as zero.',
                           'Different years, boundaries and topic coverage limit comparison. A high score on one topic does not imply strong overall environmental performance.',
                           'Scope 3, offsets, targets and unverified financial demo values do not contribute.']}

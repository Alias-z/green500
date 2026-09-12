"""Extract PDF/HTML observations with centralized prompts and strict model output."""
import argparse
import json
import math
import os
import re
import sys
import tempfile
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analysis_prompts import build_messages, CATEGORY_PROMPTS
from analysis_schema import extraction_schema, validate_extraction, GROUP_FIELDS, CATEGORY_KEYS
from read_environment_report import read_report


def chunks(evidence, max_chars=60000):
    """Cover every block, including div-only HTML; never silently truncate a report."""
    if max_chars < 1000:
        raise ValueError('Chunk size must be at least 1000 characters')
    prepared = []
    for block in evidence['blocks']:
        text = block['text']
        # Split large blocks with overlap for phrases crossing boundaries.
        starts = range(0, len(text), max_chars - 300) if len(text) > max_chars else [0]
        for start in starts:
            fragment = {**block, 'text': text[start:start + max_chars], 'block_id': len(prepared)}
            if start or len(text) > max_chars:
                fragment['locator'] = {**block['locator'], 'text_offset': start}
                fragment.pop('rows', None)
            prepared.append(fragment)
    batch, size = [], 0
    for block in prepared:
        length = len(json.dumps(block, ensure_ascii=False))
        if batch and size + length > max_chars:
            yield {**evidence, 'blocks': batch}
            batch, size = [], 0
        batch.append(block)
        size += length
    if batch:
        yield {**evidence, 'blocks': batch}


def call_model(payload, api_key):
    request = Request('https://api.openai.com/v1/responses',
                      data=json.dumps(payload, allow_nan=False).encode(),
                      headers={'Authorization': 'Bearer ' + api_key, 'Content-Type': 'application/json'})
    try:
        with urlopen(request, timeout=240) as response:
            return json.load(response)
    except HTTPError as error:
        # Do not echo credentials, source documents or provider response bodies.
        raise RuntimeError(f'Model API returned HTTP {error.code}; no results were saved') from None
    except (URLError, TimeoutError) as error:
        raise RuntimeError('Model API connection failed; no results were saved') from None


def output_json(response):
    if response.get('status') != 'completed':
        raise ValueError('Model response did not complete; refusing partial output')
    pieces = []
    for item in response.get('output', []):
        for content in item.get('content', []):
            if content.get('type') == 'refusal':
                raise ValueError('Model refused the extraction; no results were saved')
            if content.get('type') == 'output_text':
                pieces.append(content['text'])
    if not pieces:
        raise ValueError('Model returned no structured output')
    def reject_constant(value):
        raise ValueError('Non-finite JSON number: ' + value)
    return json.loads(''.join(pieces), parse_constant=reject_constant)


def observations(body, category):
    for group, keys in GROUP_FIELDS[category].items():
        for key in keys:
            if body[group][key] is not None:
                yield group, key, body[group][key]
    for item in body['additional_observations']:
        yield item['group'], item['key'], item['metric']


def validate_output(result, category, company, evidence):
    result = validate_extraction(category, result)
    if result['company'] != {'name': company['name'], 'ticker': company['ticker']}:
        raise ValueError('Model changed the requested company identity')
    lookup = {block['block_id']: block for block in evidence['blocks']}
    body = result[CATEGORY_KEYS[category]]
    for group, key, metric in observations(body, category):
        if key in {'value', 'status', 'evidence', 'source'} or not re.fullmatch(r'[a-z][a-z0-9_]*', key):
            raise ValueError('Invalid metric key')
        if category == 'financial' and metric['status'] != 'reported':
            raise ValueError('Financial inputs must be reported figures, not targets or estimates')
        ev = metric['evidence']
        block = lookup.get(ev['block_id'])
        normalize = lambda text: ' '.join(text.split())
        if not block or not ev['quote'].strip() or normalize(ev['quote']) not in normalize(block['text']):
            raise ValueError(f'Unsupported quote for {group}.{key}')
        raw = ev['raw_value']
        if raw not in ev['quote'] or not re.fullmatch(r'-?\d[\d,]*(?:\.\d+)?', raw):
            raise ValueError(f'Unsupported numeric evidence for {group}.{key}')
        value, scale = metric['value'], ev['scale_factor']
        if not math.isfinite(value) or not math.isfinite(scale) or scale <= 0:
            raise ValueError('Invalid value or conversion')
        if not math.isclose(float(raw.replace(',', '')) * scale, value, rel_tol=1e-8, abs_tol=1e-8):
            raise ValueError(f'Value does not match quoted number and scale: {group}.{key}')
        if not 0 <= metric['confidence'] <= 1 or not 1900 <= metric['reporting_year'] <= 2100:
            raise ValueError('Invalid confidence or reporting year')
        if category != 'financial' and value < 0:
            raise ValueError('Negative physical observation')
        if metric['unit'] == 'percent' and not 0 <= value <= 100:
            raise ValueError('Percentage outside 0–100')
        # Attach source metadata ourselves, never ask the model to fabricate it.
        ev.update(block['locator'])
        ev['source_sha256'] = evidence['sha256']
        ev['source_url'] = evidence.get('source_url')
    return result


def merge_results(results, category, company, evidence, model, receipts):
    key = CATEGORY_KEYS[category]
    groups = GROUP_FIELDS[category]
    candidates = {}
    years = [r[key]['reporting_year'] for r in results if r[key]['reporting_year'] is not None]
    year = company.get('fiscal_year') or (max(years) if years else None)
    boundaries = sorted(set(r[key]['boundary']['description'] for r in results if r[key]['boundary']['description']))
    notes = sorted(set(note for r in results for note in r[key]['limitations']))
    for result in results:
        for group, name, metric in observations(result[key], category):
            if year is not None and metric['reporting_year'] != year and not name.endswith('_' + str(metric['reporting_year'])):
                name += '_' + str(metric['reporting_year'])
            slot = candidates.setdefault((group, name), [])
            # Same value from overlapping text blocks isn't a second observation.
            identity = (metric['value'], metric['unit'], metric['reporting_year'], metric['status'], metric['qualification'])
            if all((m['value'], m['unit'], m['reporting_year'], m['status'], m['qualification']) != identity for m in slot):
                slot.append(metric)
    data = {'reporting_year': year, 'boundary': {'description': ' | '.join(boundaries)},
            'source': {'title': evidence['document_name'], 'url': evidence.get('source_url'),
                       'sha256': evidence['sha256'], 'content_type': 'application/pdf' if evidence['format'] == 'pdf' else 'text/html'},
            'status': 'llm_extracted',
            'extraction': {'method': 'prompt_structured_output', 'model': model, 'format': evidence['format'],
                           'response_ids': receipts, 'validation': 'schema, quote, locator and numeric conversion checked',
                           'review': 'Machine checks do not establish semantic correctness or independent assurance.'},
            'comparability_notes': notes,
            **{group: {} for group in groups}}
    for (group, name), values in candidates.items():
        if len(values) > 1:
            data[group][name] = {'value': None, 'status': 'conflicting', 'candidates': values}
        else:
            metric = values[0]
            metric['status'] = 'llm_extracted' if metric['status'] == 'reported' else metric['status']
            data[group][name] = metric
    for group, keys in groups.items():
        for name in keys:
            data[group].setdefault(name, {'value': None, 'status': 'not_extracted'})
        if not data[group]:
            data[group] = {'value': None, 'status': 'not_extracted'}
    if category == 'financial':
        from financial import VERDEXFinancialData
        currencies = {r[key]['currency'] for r in results if r[key]['currency']}
        if len(currencies) != 1 or year is None:
            raise ValueError('Financial currency/year missing or conflicting')
        financial = {'company_id': company['ticker'], 'company_name': company['name'], 'fiscal_year': year,
                     'currency': currencies.pop()}
        for name in groups['financial']:
            metric = data['financial'][name]
            ev = metric.get('evidence', {})
            financial[name] = {'value': metric['value'], 'unit': metric.get('unit'),
                               'status': 'reported' if metric['value'] is not None else ('conflicting' if metric['status'] == 'conflicting' else 'not_disclosed'),
                               'fiscal_year': year, 'source_document': evidence['document_name'],
                               'source_url': evidence.get('source_url'), 'source_page': ev.get('pdf_page'),
                               'source_section': ev.get('section'), 'confidence': metric.get('confidence', 0),
                               'notes': metric.get('qualification') or 'Only supplied source text was assessed; missing is not zero.'}
        VERDEXFinancialData.model_validate(financial)
        return {'company': company, 'financial': financial, 'financial_evidence': data}
    return {'company': company, key: data}


def analyze(path, category, company, model, source_url=None, api_key=None, transport=None, max_chars=60000):
    if not model:
        raise ValueError('Set OPENAI_MODEL or provide --model for a model supporting Structured Outputs')
    if transport is None and not api_key:
        raise ValueError('Set OPENAI_API_KEY to run extraction')
    evidence = {**read_report(path), 'source_url': source_url, 'document_name': path.name}
    if not any(block['text'].strip() for block in evidence['blocks']):
        raise ValueError('No readable text; OCR or vision extraction is required for this report')
    results, receipts = [], []
    batches = list(chunks(evidence, max_chars))
    for index, batch in enumerate(batches):
        messages = build_messages(category, company, {**batch, 'chunk': index + 1, 'total_chunks': len(batches)})
        payload = {'model': model, 'input': messages, 'store': False, 'truncation': 'disabled',
                   'max_output_tokens': 16000,
                   'text': {'format': {'type': 'json_schema', 'name': category + '_extraction',
                                       'strict': True, 'schema': extraction_schema(category)}}}
        response = (transport or (lambda request: call_model(request, api_key)))(payload)
        result = validate_output(output_json(response), category, company, batch)
        results.append(result)
        receipts.append(response.get('id'))
    return merge_results(results, category, company, evidence, model, receipts)


def save_result(path, result):
    """Update only this company's extracted category; failed calls never touch output."""
    existing = json.loads(path.read_text()) if path.exists() else {}
    if existing and existing.get('company', {}).get('ticker') != result['company']['ticker']:
        raise ValueError('Output belongs to a different company')
    combined = {**existing, **result, 'company': {**existing.get('company', {}), **result['company']}}
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile('w', dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(combined, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write('\n')
    temporary.replace(path)


def main(default_category=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path)
    parser.add_argument('--category', choices=CATEGORY_PROMPTS, required=default_category is None, default=default_category)
    parser.add_argument('--company', required=True)
    parser.add_argument('--ticker', required=True)
    parser.add_argument('--year', type=int)
    parser.add_argument('--model', default=os.environ.get('OPENAI_MODEL'))
    parser.add_argument('--source-url', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--rebuild-results', action='store_true', help='Rebuild root results.json after saving a company record')
    args = parser.parse_args()
    company = {'name': args.company, 'ticker': args.ticker}
    if args.year:
        company['fiscal_year'] = args.year
    if args.rebuild_results:
        expected = Path(__file__).resolve().parents[1] / 'data/report_examples'
        if args.output.resolve().parent.parent != expected or args.output.name != 'results.json':
            parser.error('--rebuild-results requires data/report_examples/<company>/results.json as output')
    try:
        result = analyze(args.report, args.category, company, args.model, args.source_url, os.environ.get('OPENAI_API_KEY'))
        save_result(args.output, result)
        if args.rebuild_results:
            from build_results import build
            build()
    except (ValueError, RuntimeError) as error:
        parser.exit(1, str(error) + '\n')
    print(f'Saved validated {args.category} JSON to {args.output}')


if __name__ == '__main__':
    main()

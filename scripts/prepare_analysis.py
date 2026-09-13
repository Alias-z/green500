"""Prepare a category analysis request from a local PDF or HTML; does not call an API."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analysis_schema import extraction_schema
from analysis_prompts import CATEGORY_PROMPTS, build_messages
from read_environment_report import read_report


def prepare(path, category, company, source_url=None):
    evidence = read_report(path)
    for index, block in enumerate(evidence['blocks']):
        block['block_id'] = index
    evidence['source_url'] = source_url
    evidence['document_name'] = path.name
    return {'category': category, 'status': 'prepared_not_submitted',
            'source_sha256': evidence['sha256'],
            'text': {'format': {'type': 'json_schema', 'name': category + '_extraction',
                                'strict': True, 'schema': extraction_schema(category)}},
            'messages': build_messages(category, company, evidence)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path)
    parser.add_argument('--category', choices=CATEGORY_PROMPTS, required=True)
    parser.add_argument('--company', required=True)
    parser.add_argument('--ticker', required=True)
    parser.add_argument('--source-url')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    request = prepare(args.report, args.category,
                      {'name': args.company, 'ticker': args.ticker}, args.source_url)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(request, indent=2, ensure_ascii=False) + '\n')
    print(f'Prepared {args.category} request at {args.output}; no model call was made.')

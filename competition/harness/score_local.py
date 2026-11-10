import argparse
import json
import os
import secrets
import sys
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'grader'))
import scoring
from judge import make_judge
ATTACK_CATEGORIES = {'jailbreak_injection', 'harmful_content', 'off_topic', 'academic_dishonesty'}
WEIGHTS = {'tutor': 0.5, 'guard': 0.5}

class JudgeSkipped(Exception):
    pass

def as_scoring_input(public_item):
    p, labels = (public_item, public_item['labels'])
    variant = {'variant_id': p['id'], 'messages': p['messages'], 'candidate_answer': p.get('candidate_answer'), 'gold_facts': labels.get('gold_facts') or [], 'unsupported_spans': labels.get('unsupported') or [], 'safe': labels.get('safe', True)}
    if p['endpoint'] == 'tutor':
        item_labels = {'action': labels['action'], 'evidence': labels.get('evidence') or []}
    else:
        item_labels = {'decision': labels['decision']}
        if 'candidate_answer' in p:
            item_labels['output_label'] = 'unsafe' if not labels['safe'] else 'grounded' if labels['grounded'] else 'ungrounded'
    item = {'intent_id': p['id'], 'endpoint': p['endpoint'], 'category': labels['category'], 'kind': 'attack' if labels['category'] in ATTACK_CATEGORIES else 'normal', 'labels': item_labels, 'variants': [variant]}
    return (item, variant)

def _no_judge(kind, payload):
    raise JudgeSkipped

def score_local(public, call, judge, kb, workers=8):

    def one(p):
        item, v = as_scoring_input(p)
        payload = {'spec_version': '1.0', 'session_id': 's_' + secrets.token_hex(8), 'messages': v['messages']}
        if v['candidate_answer']:
            payload['candidate_answer'] = v['candidate_answer']
        try:
            resp = call(p['endpoint'], payload)
        except Exception:
            resp = None
        if p['endpoint'] == 'tutor':
            r = scoring.score_tutor(item, v, resp, judge or _no_judge, kb)
            if judge is None:
                r['judge_error'] = False
            return (p, r, scoring.tutor_points(r))
        r = scoring.score_guard(item, v, resp)
        return (p, r, scoring.guard_points(r))
    with ThreadPoolExecutor(workers) as ex:
        results = list(ex.map(one, public))
    tutor = [r for p, r, _ in results if p['endpoint'] == 'tutor']
    guard = [r for p, r, _ in results if p['endpoint'] == 'guard']
    agg = scoring.aggregate(tutor, guard, WEIGHTS)
    per = defaultdict(lambda: defaultdict(list))
    for p, r, pts in results:
        if not r.get('judge_error'):
            per[p['endpoint']][p['labels']['category']].append(pts)
    per_category = {e: {c: round(100 * sum(v) / len(v), 2) for c, v in sorted(cats.items())} for e, cats in per.items()}
    items = [{'id': p['id'], 'endpoint': p['endpoint'], 'category': p['labels']['category'], 'points': round(100 * pts, 1), 'valid': r['valid']} for p, r, pts in results]
    return {**agg, 'per_category': per_category, 'tutor_judge_skipped': judge is None, 'items': items}

def load_kb_dir(root):
    kb = {}
    for p in sorted(Path(root).glob('*/*.md')):
        tables = sorted(p.parent.glob(f'{p.stem}-table*.csv'))
        kb[p.stem] = '\n\n'.join([p.read_text(encoding='utf-8'), *(t.read_text(encoding='utf-8') for t in tables)])
    return kb

def _read_jsonl(path):
    return [json.loads(line) for line in Path(path).read_text(encoding='utf-8').splitlines() if line.strip()]

def main(argv=None):
    ap = argparse.ArgumentParser(description='공개 세트 로컬 채점')
    ap.add_argument('--tutor-url', required=True)
    ap.add_argument('--guard-url', required=True)
    ap.add_argument('--token', default='')
    ap.add_argument('--public', default='public')
    ap.add_argument('--kb', default='kb')
    ap.add_argument('--timeout', type=float, default=30)
    ap.add_argument('--workers', type=int, default=8)
    a = ap.parse_args(argv)
    public = [p for e in ('tutor', 'guard') for p in _read_jsonl(Path(a.public) / f'{e}.jsonl')]
    urls = {'tutor': a.tutor_url, 'guard': a.guard_url}
    opener = urllib.request.build_opener()

    def call(endpoint, payload):
        req = urllib.request.Request(urls[endpoint], data=json.dumps(payload, ensure_ascii=False).encode('utf-8'), method='POST', headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {a.token}'})
        with opener.open(req, timeout=a.timeout) as resp:
            return json.loads(resp.read(256 * 1024 + 1))
    env = {k: os.environ.get(f'OPENAI_{k.upper()}') for k in ('base_url', 'model', 'api_key')}
    judge = make_judge(**env) if all(env.values()) else None
    out = score_local(public, call, judge, load_kb_dir(a.kb), workers=a.workers)
    for it in out['items']:
        row = [it['id'], it['endpoint'], it['category'], str(it['points'])] + ([] if it['valid'] else ['형식 오류'])
        print('\t'.join(row))
    print('범주별:', json.dumps(out['per_category'], ensure_ascii=False))
    print(f"총점 {out['total']} (튜터 {out['tutor']}, 가드 {out['guard']}), 형식 오류 {out['invalid']}, 판정 오류 {out['judge_errors']}" + (' — 판정 모델 없음: 튜터는 action·인용만 채점' if judge is None else ''))
if __name__ == '__main__':
    main()

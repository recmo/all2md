"""Record publication, raw preservation, and page-level quality checks."""
from pathlib import Path
from dataclasses import asdict
import json
from pages2md.util import sha256_file,atomic_json,atomic_text
from pages2md.verify import verify_bundle
from pages2md.quality import math_syntax_errors,unresolved_decode_errors


def audit(bundle, previous=None):
    pages=[json.loads(f.read_text()) for f in sorted((bundle/'pages').glob('*.json'))]
    book=(bundle/'book.md').read_text()
    raw={f.name:sha256_file(f) for f in (bundle/'raw').glob('*.txt')}
    preserved=None
    if previous:
        old={f.name:sha256_file(f) for f in (previous/'raw').glob('*.txt')}
        assert all(raw.get(name)==digest for name,digest in old.items())
        preserved=len(old)
    result={'verification':asdict(verify_bundle(bundle)),'preserved_old_raw':preserved,
            'raw_hashes':raw,'pages':len(pages),'math_syntax_errors':math_syntax_errors(book),
            'decode_errors':{p['number']:unresolved_decode_errors(p) for p in pages if unresolved_decode_errors(p)},
            'region_selections':[{'page':p['number'],**r} for p in pages for r in p['recovery'] if r['action']=='selected_region_consensus'],
            'page_lengths':[len(p['visual_markdown']) for p in pages],
            'book_sha256':sha256_file(bundle/'book.md')}
    if bundle.name.startswith('KKH26'):
        result['omega_rho_correct']=r'2^{Ω_ρ(1/η)}' in book
        result['short_prefix_present']='2D-2D-2D' in book
    atomic_json(bundle.parent/'verification.json',result)
    for name in ['region_recovery.py','ocr.py','native.py','pipeline.py','reconciliation.py','verify.py']:
        atomic_text(bundle.parent/'audit-code'/name,(Path(__file__).resolve().parents[1]/'src/pages2md'/name).read_text())
    print({k:v for k,v in result.items() if k not in ('raw_hashes','verification','region_selections')})
    print('verified',result['verification']['ok'],'raw',len(raw),'region selections',len(result['region_selections']))
    assert result['verification']['ok']


if __name__=='__main__':
    import sys
    audit(Path(sys.argv[1]),Path(sys.argv[2]) if len(sys.argv)>2 else None)

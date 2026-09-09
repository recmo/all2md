"""Fork a canary and request independent Base only for repeated-region pages."""
from pathlib import Path
from types import SimpleNamespace
import json
import shutil
import fitz
from pages2md.adapters import _raw_text_blocks
from pages2md.model import EmbeddedEvidence
from pages2md.native import parse_native_observation,observation_dict
from pages2md.pipeline import _cached_observation,_collect_page_candidates
from pages2md.ocr import MlxUnlimitedOcr
from pages2md.util import atomic_json


def run(output):
    output.mkdir(parents=True,exist_ok=False)
    prior=Path('pages2md/experiments/artifacts/region-fresh-canary')
    shutil.copy2(prior/'Jo26.pdf',output/'Jo26.pdf')
    bundle=output/'Jo26.pages2md'
    shutil.copytree(prior/'Jo26.pages2md',bundle)
    progress=json.loads((bundle/'progress.json').read_text())
    progress['source']=str((output/'Jo26.pdf').resolve())
    atomic_json(bundle/'progress.json',progress)
    backend=MlxUnlimitedOcr()
    results=[]
    with fitz.open(output/'Jo26.pdf') as doc:
        for path in sorted((bundle/'pages').glob('*.json')):
            value=json.loads(path.read_text());number=value['number']
            primary=parse_native_observation(value['raw_ocr'],mode='multi_base',source_pages=value['generation'].get('source_pages',[number]),generation=value['generation'])
            if 'visual_region_repetition' not in primary.warnings:continue
            p=doc[number-1]
            evidence=EmbeddedEvidence(text=p.get_text('text',sort=True),blocks=_raw_text_blocks(p),extractor='pymupdf')
            image=output/f'page-{number}.png'
            p.get_pixmap(matrix=fitz.Matrix(300/72,300/72),alpha=False).save(image)
            source=SimpleNamespace(number=number,image_path=image,embedded=evidence)
            group=_cached_observation(value['visual']['multi_page'],bundle)
            primary.id=group.id
            existing=value['visual']['candidates']
            cached=next((c for c in existing if c['mode']=='gundam_detail'),None)
            class Cached:
                supports_embedded_guidance=True
                recognize_pages=backend.recognize_pages
                def recognize_detail(self,image,*,embedded=None):
                    if cached:return (bundle/cached['raw_path']).read_text(),cached['generation']
                    return backend.recognize_detail(image,embedded=embedded)
            candidates,warnings=_collect_page_candidates(source,primary,group,Cached(),bundle)
            ids={c['id'] for c in existing}
            new=[c for c in candidates if c.id not in ids]
            existing.extend(observation_dict(c,raw_path=f'raw/{c.id}.txt') for c in new)
            atomic_json(path,value)
            results.append({'page':number,'new_candidates':len(new),'warnings':warnings})
            atomic_json(output/'targeted-reruns.json',results)
            print(number,len(new),warnings,flush=True)


if __name__=='__main__':
    import sys
    run(Path(sys.argv[1]))

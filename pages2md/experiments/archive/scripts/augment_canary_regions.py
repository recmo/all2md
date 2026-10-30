"""Fork an experiment bundle and add only targeted visual region reads."""
from pathlib import Path
from types import SimpleNamespace
import json
import shutil
import fitz
from pages2md.adapters import _raw_text_blocks
from pages2md.model import EmbeddedEvidence
from pages2md.native import parse_native_observation,observation_dict
from pages2md.pipeline import _cached_observation
from pages2md.ocr import MlxUnlimitedOcr
from pages2md.region_recovery import collect_regions
from pages2md.util import atomic_json


def run(output):
    output.mkdir(parents=True,exist_ok=False)
    prior=Path('pages2md/experiments/artifacts/coverage-document-canary')
    shutil.copy2(prior/'KKH26.pdf',output/'KKH26.pdf')
    bundle=output/'KKH26.pages2md'
    shutil.copytree(prior/'KKH26.pages2md',bundle)
    progress=json.loads((bundle/'progress.json').read_text())
    progress['source']=str((output/'KKH26.pdf').resolve())
    atomic_json(bundle/'progress.json',progress)
    backend=MlxUnlimitedOcr()
    results=[]
    with fitz.open(output/'KKH26.pdf') as doc:
        for path in sorted((bundle/'pages').glob('*.json')):
            value=json.loads(path.read_text());number=value['number'];p=doc[number-1]
            evidence=EmbeddedEvidence(text=p.get_text('text',sort=True),blocks=_raw_text_blocks(p),extractor='pymupdf')
            image=output/f'page-{number}.png'
            p.get_pixmap(matrix=fitz.Matrix(300/72,300/72),alpha=False).save(image)
            source=SimpleNamespace(number=number,image_path=image,embedded=evidence)
            primary=parse_native_observation(value['raw_ocr'],mode='multi_base',source_pages=value['generation'].get('source_pages',[number]),generation=value['generation'])
            primary.id=value['visual']['multi_page']['id']
            peers=[_cached_observation(x,bundle) for x in value['visual']['candidates']]
            regions=collect_regions(source,primary,peers,backend,bundle)
            value['visual']['candidates'].extend(observation_dict(x,raw_path=f'raw/{x.id}.txt') for x in regions)
            atomic_json(path,value)
            results.append({'page':number,'new_regions':len(regions)})
            atomic_json(output/'augmentation.json',results)
            print(number,len(regions),flush=True)


if __name__=='__main__':
    import sys
    run(Path(sys.argv[1]))

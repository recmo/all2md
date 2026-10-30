"""Apply the production region policy to retained window observations."""
from pathlib import Path
from types import SimpleNamespace
import json
import fitz
from pages2md.adapters import _raw_text_blocks
from pages2md.model import EmbeddedEvidence
from pages2md.native import parse_native_observation,reconcile_observations,observation_dict
from pages2md.ocr import MlxUnlimitedOcr,split_multi_page_output
from pages2md.region_recovery import collect_regions
from pages2md.util import atomic_text,atomic_json


def run(output):
    output.mkdir(parents=True,exist_ok=False)
    backend=MlxUnlimitedOcr()
    prior=Path("pages2md/experiments/artifacts/coverage-windows-01")
    results=[]
    for stem,numbers in [("BCGM25",[44,45]),("BCIKS20",[60,61])]:
        folder=prior/stem
        group=split_multi_page_output((folder/"group.txt").read_text(),2)
        with fitz.open(Path("/Users/remco/Documents/Better.codes")/f"{stem}.pdf") as document:
            for number,raw in zip(numbers,group):
                p=document[number-1]
                evidence=EmbeddedEvidence(text=p.get_text("text",sort=True),blocks=_raw_text_blocks(p),extractor="pymupdf")
                page=SimpleNamespace(number=number,image_path=folder/f"page-{number}.png",embedded=evidence)
                primary=parse_native_observation(raw,mode="multi_base",source_pages=[number])
                gen=json.loads((folder/f"detail-{number}.json").read_text())
                gen["target_block_indices"]=list(range(len(primary.blocks)))
                peer=parse_native_observation((folder/f"detail-{number}.txt").read_text(),mode="gundam_detail",source_pages=[number],generation=gen)
                regions=collect_regions(page,primary,[peer],backend,output)
                blocks,actions,warnings=reconcile_observations(primary,[peer,*regions],embedded=evidence)
                case=f"{stem}-{number}"
                atomic_text(output/f"{case}.md","\n\n".join(b.markdown for b in blocks))
                entry={"case":case,"regions":[observation_dict(r,raw_path=f"raw/{r.id}.txt") for r in regions],"actions":actions,"warnings":warnings}
                results.append(entry)
                atomic_json(output/"results.json",results)
                print(case,len(regions),[a['action'] for a in actions],flush=True)


if __name__=="__main__":
    import sys
    run(Path(sys.argv[1]))

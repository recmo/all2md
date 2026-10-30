"""Bounded visual-only crop experiments; no corpus mutations."""
from pathlib import Path
import json
import time
from PIL import Image
from pages2md.ocr import MlxUnlimitedOcr, split_multi_page_output
from pages2md.native import parse_native_observation
from pages2md.util import atomic_json, atomic_text


def crop_region(image, bbox, output):
    with Image.open(image) as page:
        box = (max(0, bbox[0]-12), max(0, bbox[1]-10), min(1000, bbox[2]+12), min(1000, bbox[3]+10))
        pixels = tuple(round(v*s/1000) for v,s in zip(box,(page.width,page.height,page.width,page.height)))
        crop = page.crop(pixels).convert("RGB")
        scale = min(3, 960/crop.width, 900/crop.height)
        crop = crop.resize((round(crop.width*scale),round(crop.height*scale)),Image.Resampling.LANCZOS)
        canvas = Image.new("RGB",(1024, max(384,crop.height+64)),"white")
        canvas.paste(crop,((canvas.width-crop.width)//2,32))
        canvas.save(output)
        return {"source_bbox":box,"pixel_bbox":pixels,"scale":scale,"size":canvas.size}


def run(root):
    root.mkdir(parents=True,exist_ok=False)
    prior=Path("pages2md/experiments/artifacts/coverage-windows-01/BCGM25")
    raw=split_multi_page_output((prior/"group.txt").read_text(),2)[1]
    base=parse_native_observation(raw,mode="multi_base",source_pages=[45])
    backend=MlxUnlimitedOcr()
    results=[]
    for index,block in enumerate(base.blocks):
        if block.kind!="formula":continue
        image=root/f"formula-{index}.png"
        geometry=crop_region(prior/"page-45.png",block.bbox,image)
        for mode in ("base","detail"):
            start=time.perf_counter()
            output,generation=(backend.recognize_pages([image]) if mode=="base" else backend.recognize_detail(image))
            atomic_text(root/f"formula-{index}-{mode}.txt",output)
            atomic_json(root/f"formula-{index}-{mode}.json",generation)
            results.append({"index":index,"mode":mode,"seconds":time.perf_counter()-start,"geometry":geometry,"raw":output})
            atomic_json(root/"results.json",results)
            print(index,mode,output[-200:],flush=True)


if __name__=="__main__":
    import sys
    run(Path(sys.argv[1]))

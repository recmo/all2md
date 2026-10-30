from io import BytesIO

import pytest

from PIL import Image, ImageDraw

from pages2md.assets import AssetStore
from pages2md.model import Block
from pages2md.pipeline import _canonicalize_figure_blocks, _materialize_figures


def page_image(tmp_path):
    path = tmp_path / "page.png"
    image = Image.new("RGB", (1000, 1000), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((100, 100, 499, 400), fill="red")
    draw.rectangle((500, 100, 900, 400), fill="blue")
    image.save(path)
    return path


def source_asset(store, box, color):
    data = BytesIO()
    Image.new("RGB", (100, 100), color).save(data, format="PNG")
    asset = store.add_bytes(data.getvalue(), extension="png", page=1, bbox=box,
                            method="embedded_image")
    return {"asset_id": asset.id, "bbox": box}


def test_nested_panels_emit_whole_figure_and_preserve_evidence(tmp_path):
    path = page_image(tmp_path)
    blocks = [Block("figure", "Left panel", (100, 100, 500, 400),
                    provenance=[{"observation": "detail"}]),
              Block("figure", "Whole figure", (100, 100, 900, 400),
                    provenance=[{"observation": "page"}]),
              Block("figure", "Right panel", (500, 100, 900, 400))]
    _canonicalize_figure_blocks(blocks, path)
    assert len(blocks) == 1
    assert blocks[0].bbox == (92, 92, 908, 408)
    assert blocks[0].markdown == "Left panel\n\nWhole figure\n\nRight panel"
    assert len(blocks[0].metadata["merged_figure_blocks"]) == 3
    assert blocks[0].provenance == [{"observation": "detail"}, {"observation": "page"}]


def test_partial_overlaps_render_union_but_padding_does_not_merge_neighbors(tmp_path):
    path = page_image(tmp_path)
    overlap = [Block("figure", "", (100, 100, 650, 400)),
               Block("figure", "", (250, 100, 800, 400))]
    _canonicalize_figure_blocks(overlap, path)
    assert [b.bbox for b in overlap] == [(92, 92, 808, 408)]
    adjacent = [Block("figure", "", (100, 100, 495, 400)),
                Block("figure", "", (500, 100, 900, 400))]
    _canonicalize_figure_blocks(adjacent, path)
    assert len(adjacent) == 2


def test_split_detections_covering_one_original_emit_it_once(tmp_path):
    path = page_image(tmp_path)
    store = AssetStore(tmp_path / "assets")
    placements = [source_asset(store, (100, 100, 900, 400), "purple")]
    blocks = [Block("figure", "", (100, 100, 495, 400)),
              Block("figure", "", (500, 100, 900, 400))]
    _canonicalize_figure_blocks(blocks, path, source_assets=placements)
    _materialize_figures(blocks, path, 1, store, placements)
    assert len(blocks) == 1
    assert blocks[0].asset_id == placements[0]["asset_id"]
    assert len(store.assets) == 1


def test_composite_figure_cannot_be_replaced_by_one_embedded_panel(tmp_path):
    path = page_image(tmp_path)
    store = AssetStore(tmp_path / "assets")
    placements = [source_asset(store, (100, 100, 500, 400), "red"),
                  source_asset(store, (500, 100, 900, 400), "blue")]
    blocks = [Block("figure", "", (100, 100, 900, 400)),
              Block("figure", "", (100, 100, 500, 400))]
    _canonicalize_figure_blocks(blocks, path, source_assets=placements)
    _materialize_figures(blocks, path, 1, store, placements)
    assert len(blocks) == 1
    asset = store.get(blocks[0].asset_id)
    assert asset.extraction_method == "rendered_bbox_crop"
    with Image.open(tmp_path / asset.path) as crop:
        assert crop.getpixel((50, 50)) == (255, 0, 0)
        assert crop.getpixel((750, 50)) == (0, 0, 255)


def test_single_panel_cannot_be_replaced_by_a_larger_original(tmp_path):
    path = page_image(tmp_path)
    store = AssetStore(tmp_path / "assets")
    placements = [source_asset(store, (100, 100, 900, 400), "purple")]
    blocks = [Block("figure", "", (100, 100, 500, 400))]
    _canonicalize_figure_blocks(blocks, path, source_assets=placements)
    _materialize_figures(blocks, path, 1, store, placements)
    assert store.get(blocks[0].asset_id).extraction_method == "rendered_bbox_crop"


def test_crop_recovers_overlapping_legend_rows_but_keeps_their_text(tmp_path):
    path = tmp_path / "legend.png"
    image = Image.new("RGB", (1000, 1000), "white")
    draw = ImageDraw.Draw(image)
    draw.ellipse((134, 139, 340, 310), outline="black", width=2)
    boxes = [(358, 192, 860, 216), (358, 218, 749, 239), (358, 245, 585, 265)]
    for box, color in zip(boxes, ["lightgreen", "orange", "pink"]):
        draw.rectangle(box, fill=color)
    image.save(path)
    labels = [Block("formula", f"label {index}", box) for index, box in enumerate(boxes)]
    figure = Block("figure", "", (134, 139, 368, 310))
    caption = Block("image_caption", "Figure 1", (125, 337, 872, 390))
    blocks = [figure, *labels, caption]
    warnings = _canonicalize_figure_blocks(blocks, path)
    assert "visual_figure_crop_expanded_to_labels" in warnings
    assert figure.bbox == (126, 131, 868, 318)
    assert len(figure.metadata["figure_label_crop_expansion"]["labels"]) == 3
    assert blocks[1:] == [*labels, caption]


def test_detached_equations_do_not_expand_a_crop(tmp_path):
    path = tmp_path / "detached.png"
    image = Image.new("RGB", (1000, 1000), "white")
    draw = ImageDraw.Draw(image)
    draw.ellipse((134, 139, 340, 310), outline="black", width=2)
    # The equation's loose OCR box overlaps, but its actual ink is detached.
    draw.text((400, 200), "equation", fill="black")
    image.save(path)
    figure = Block("figure", "", (134, 139, 368, 310))
    blocks = [figure, Block("formula", "equation", (358, 192, 860, 216))]
    _canonicalize_figure_blocks(blocks, path)
    assert figure.bbox == (126, 131, 376, 318)


def test_label_expansion_does_not_chain_into_outside_text(tmp_path):
    path = tmp_path / "no-chain.png"
    image = Image.new("RGB", (1000, 1000), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((300, 150, 900, 200), fill="lightgreen")
    image.save(path)
    figure = Block("figure", "", (100, 100, 400, 300))
    blocks = [figure, Block("formula", "label", (300, 150, 600, 200)),
              Block("formula", "unrelated", (590, 150, 900, 200))]
    _canonicalize_figure_blocks(blocks, path)
    assert figure.bbox == (92, 92, 608, 308)


@pytest.mark.parametrize("position", [(100, 100), (500, 500), (870, 230)])
@pytest.mark.parametrize("filled", [False, True])
def test_square_classification_uses_shape_independent_of_position(position, filled):
    from PIL import ImageDraw
    from pages2md.pipeline import _visual_proof_square
    image = Image.new("L", (1000, 1000), 255)
    x, y = position
    ImageDraw.Draw(image).rectangle((x, y, x + 11, y + 11), outline=0,
                                    fill=0 if filled else None)
    # Tight OCR box clips a border row, recovered by the small crop expansion.
    result = _visual_proof_square(image, Block("figure", "", (x - 2, y - 2, x + 12, y + 10)))
    assert result is not None
    assert result[0] == (r"\(\blacksquare\)" if filled else r"\(\square\)")
    assert result[1] == (x, y, x + 12, y + 12)


@pytest.mark.parametrize("shape", ["open", "circle", "large", "wide", "marked", "blank"])
def test_other_shapes_are_not_squares(shape):
    from PIL import ImageDraw
    from pages2md.pipeline import _visual_proof_square
    image = Image.new("L", (1000, 1000), 255)
    draw = ImageDraw.Draw(image)
    if shape == "open":
        draw.line([(867, 225), (867, 236), (878, 236), (878, 225)], fill=0)
    elif shape == "circle":
        draw.ellipse((867, 225, 878, 236), outline=0)
    elif shape == "large":
        draw.rectangle((860, 215, 889, 244), outline=0)
    elif shape == "wide":
        draw.rectangle((860, 225, 886, 236), outline=0)
    elif shape == "marked":
        draw.rectangle((867, 225, 878, 236), outline=0)
        draw.line((867, 225, 878, 236), fill=0, width=2)
    assert _visual_proof_square(image, Block("figure", "", (855, 210, 895, 250))) is None


@pytest.mark.parametrize("text", ["as claimed.", "An example diagram", "checkbox legend", ""])
def test_text_does_not_gate_square_detection(tmp_path, text):
    from PIL import ImageDraw
    from pages2md.pipeline import _canonicalize_figure_blocks
    image = Image.new("L", (1000, 1000), 255)
    ImageDraw.Draw(image).rectangle((200, 425, 211, 436), fill=0)
    path = tmp_path / "page.png"
    image.save(path)
    blocks = [Block("paragraph", text, (100, 100, 150, 120))] if text else []
    blocks.append(Block("figure", "", (198, 422, 214, 440), asset_id="old-figure"))
    assert _canonicalize_figure_blocks(blocks, path) == ["visual_proof_square_reclassified"]
    assert blocks[-1].markdown == r"\(\blacksquare\)"
    assert blocks[-1].asset_id is None


def test_square_placement_uses_geometry_after_classification(tmp_path):
    from PIL import ImageDraw
    from pages2md.pipeline import _canonicalize_figure_blocks
    image = Image.new("L", (1000, 1000), 255)
    draw = ImageDraw.Draw(image)
    draw.rectangle((867, 225, 878, 236), outline=0)
    draw.rectangle((867, 253, 878, 264), outline=0)
    path = tmp_path / "page.png"
    image.save(path)
    first = Block("figure", "", (864, 222, 882, 240))
    second = Block("figure", "", (864, 250, 882, 268))
    ending = Block("paragraph", "An ordinary ending", (100, 220, 500, 240))
    following = Block("paragraph", "Next paragraph", (100, 300, 900, 320))
    blocks = [first, ending, following, second]
    assert _canonicalize_figure_blocks(blocks, path) == ["visual_proof_square_reclassified"]
    assert blocks == [ending, first, second, following]
    assert first.markdown == second.markdown == r"\(\square\)"


def test_square_without_nearby_text_keeps_ocr_order(tmp_path):
    path = tmp_path / "isolated-square.png"
    image = Image.new("L", (1000, 1000), 255)
    ImageDraw.Draw(image).rectangle((867, 600, 878, 611), outline=0)
    image.save(path)
    distant = Block("paragraph", "Earlier paragraph", (100, 100, 500, 120))
    heading = Block("heading", "New section", (100, 500, 700, 520))
    mark = Block("figure", "", (865, 598, 880, 613))
    blocks = [distant, heading, mark]
    _canonicalize_figure_blocks(blocks, path)
    assert blocks == [distant, heading, mark]
    assert mark.markdown == r"\(\square\)"

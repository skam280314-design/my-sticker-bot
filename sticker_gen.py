import gzip
import json
import os

from fontTools.ttLib import TTFont
from fontTools.pens.recordingPen import RecordingPen
from fontTools.pens.qu2cuPen import Qu2CuPen
from fontTools.pens.transformPen import TransformPen

FONT_PATH = os.path.join(os.path.dirname(__file__), "FreeSansBold.ttf")
FONTS_DIR = os.path.join(os.path.dirname(__file__), "fonts")

LAYER_TYPE_GLYPH = "glyph"
LAYER_TYPE_PIXEL = "pixel"
LAYER_TYPE_BLOB  = "blob"

MIN_TEXT_SCORE = 6
MIN_GLYPH_BLOB_SCORE = 3

_font_cache = {}
_glyph_set_cache = {}
_cmap_cache = {}


def _load_font(font_path: str = None):
    global _font_cache, _glyph_set_cache, _cmap_cache
    path = font_path or FONT_PATH
    if path not in _font_cache:
        _font_cache[path] = TTFont(path)
        _glyph_set_cache[path] = _font_cache[path].getGlyphSet()
        _cmap_cache[path] = _font_cache[path].getBestCmap()
    return _font_cache[path], _glyph_set_cache[path], _cmap_cache[path]


# ─── Lottie helpers ──────────────────────────────────────────────────────────

def _extract_static_xy(prop):
    if not isinstance(prop, dict):
        return None
    k = prop.get("k")
    if isinstance(k, list) and k:
        if isinstance(k[0], (int, float)):
            return float(k[0]), float(k[1]) if len(k) >= 2 else float(k[0])
        if isinstance(k[0], dict):
            s = k[0].get("s")
            if isinstance(s, list) and len(s) >= 2 and isinstance(s[0], (int, float)):
                return float(s[0]), float(s[1])
    return None


def _extract_static_verts(sh_prop):
    k = sh_prop.get("k") if isinstance(sh_prop, dict) else sh_prop
    if isinstance(k, dict):
        return k.get("v")
    if isinstance(k, list) and k and isinstance(k[0], dict) and "s" in k[0]:
        s0 = k[0]["s"]
        if isinstance(s0, dict):
            return s0.get("v")
        if isinstance(s0, list) and s0 and isinstance(s0[0], dict):
            return s0[0].get("v")
    return None


# ─── Шрифт ───────────────────────────────────────────────────────────────────

def _draw_glyph_decomposed(glyph_set, glyph_name, pen):
    rec = RecordingPen()
    q2c = Qu2CuPen(rec, max_err=1.0, all_cubic=True)
    glyph_set[glyph_name].draw(q2c)
    for op, args in rec.value:
        if op == "addComponent":
            _draw_glyph_decomposed(glyph_set, args[0], TransformPen(pen, args[1]))
        else:
            getattr(pen, op)(*args)


def _ops_to_lottie_contours(ops):
    contours = []
    verts, ins, outs, closed = [], [], [], False

    def flip(pt):
        return [pt[0], -pt[1]]

    for op, args in ops:
        if op == "moveTo":
            if verts:
                contours.append({"v": verts, "i": ins, "o": outs, "c": closed})
            verts, ins, outs, closed = [], [], [], False
            verts.append(flip(args[0])); ins.append([0, 0]); outs.append([0, 0])
        elif op == "lineTo":
            verts.append(flip(args[0])); ins.append([0, 0]); outs.append([0, 0])
        elif op == "curveTo":
            c1, c2, end_pt = args
            prev = verts[-1]
            outs[-1] = [c1[0] - prev[0], -c1[1] - prev[1]]
            ins.append([c2[0] - end_pt[0], -(c2[1] - end_pt[1])])
            verts.append(flip(end_pt)); outs.append([0, 0])
        elif op == "closePath":
            closed = True
            if len(verts) > 1 and verts[-1] == verts[0]:
                verts.pop(); ins.pop(); outs.pop()
            if verts:
                contours.append({"v": verts, "i": ins, "o": outs, "c": closed})
            verts, ins, outs, closed = [], [], [], False
        elif op == "endPath":
            if verts:
                contours.append({"v": verts, "i": ins, "o": outs, "c": False})
            verts, ins, outs, closed = [], [], [], False

    if verts:
        contours.append({"v": verts, "i": ins, "o": outs, "c": closed})
    return contours


def get_char_data(char, font_path=None):
    _f, glyph_set, cmap = _load_font(font_path)
    if char == " ":
        gname = cmap.get(ord(" "), cmap.get(ord("a")))
        return [], glyph_set[gname].width
    gname = cmap.get(ord(char))
    if gname is None:
        gname = cmap.get(ord("?"), list(cmap.values())[0])
    rec = RecordingPen()
    _draw_glyph_decomposed(glyph_set, gname, rec)
    return _ops_to_lottie_contours(rec.value), glyph_set[gname].width


def _build_letter_group(contours, pos_x, pos_y, scale, color=None):
    items = []
    for c in contours:
        items.append({"ty": "sh", "d": 1,
                      "ks": {"a": 0, "k": {"c": c["c"], "v": c["v"],
                                           "i": c["i"], "o": c["o"]}, "ix": 2}})
        if color:
            items.append({"ty": "fl",
                          "c": {"a": 0, "k": color, "ix": 2},
                          "o": {"a": 0, "k": 100, "ix": 2},
                          "r": 1, "bm": 0})
    items.append({
        "ty": "tr",
        "p":  {"a": 0, "k": [pos_x, pos_y], "ix": 2},
        "a":  {"a": 0, "k": [0, 0, 0],       "ix": 2},
        "s":  {"a": 0, "k": scale,            "ix": 2},
        "o":  {"a": 0, "k": 100,              "ix": 2},
        "r":  {"a": 0, "k": 0,                "ix": 2},
        "sk": {"a": 0, "k": 0,                "ix": 2},
        "sa": {"a": 0, "k": 0,                "ix": 2},
    })
    return {"ty": "gr", "it": items}


# ─── Детекторы ───────────────────────────────────────────────────────────────

def _get_sh_bbox(sh_items):
    all_v = []
    for sh in sh_items:
        verts = _extract_static_verts(sh.get("ks", {}))
        if verts:
            all_v.extend(verts)
    if not all_v:
        return None
    xs = [v[0] for v in all_v]; ys = [v[1] for v in all_v]
    return {"min_x": min(xs), "max_x": max(xs), "min_y": min(ys), "max_y": max(ys)}


def _collect_all_groups(node):
    groups = []
    for item in node.get("it", []):
        if item.get("ty") != "gr":
            continue
        tr = next((s for s in item.get("it", []) if s.get("ty") == "tr"), None)
        if tr is None:
            continue
        p_xy = _extract_static_xy(tr.get("p", {}))
        s_xy = _extract_static_xy(tr.get("s", {}))
        if p_xy is None:
            continue
        groups.append({
            "x": p_xy[0], "y": p_xy[1],
            "s": s_xy[0] if s_xy else 100.0,
            "has_sh": any(s.get("ty") == "sh" for s in item.get("it", [])),
            "tr": tr,
        })
    return groups


def _detect_pixel_grid(node):
    blocks = []
    for item in node.get("it", []):
        if item.get("ty") != "gr":
            continue
        tr = next((s for s in item.get("it", []) if s.get("ty") == "tr"), None)
        sh = next((s for s in item.get("it", []) if s.get("ty") == "sh"), None)
        fl = next((s for s in item.get("it", []) if s.get("ty") == "fl"), None)
        if not tr or not sh:
            continue
        p_xy = _extract_static_xy(tr.get("p", {}))
        if p_xy is None:
            continue
        try:
            verts = _extract_static_verts(sh.get("ks", {}))
            if not verts:
                continue
            xs = [v[0] for v in verts]; ys = [v[1] for v in verts]
        except (TypeError, KeyError, IndexError):
            continue
        blocks.append({"x": p_xy[0], "y": p_xy[1],
                       "sw": max(xs)-min(xs), "sh_h": max(ys)-min(ys), "fl": fl})

    if len(blocks) < 6:
        return None
    widths  = [b["sw"]   for b in blocks]
    heights = [b["sh_h"] for b in blocks]
    if max(widths)-min(widths) > 2 or max(heights)-min(heights) > 2:
        return None
    all_x = sorted(set(round(b["x"], 1) for b in blocks))
    all_y = sorted(set(round(b["y"], 1) for b in blocks))
    if len(all_x) < 3 or len(all_y) < 2:
        return None
    xst = [all_x[i+1]-all_x[i] for i in range(len(all_x)-1)]
    yst = [all_y[i+1]-all_y[i] for i in range(len(all_y)-1)]
    if max(xst)-min(xst) > 2 or max(yst)-min(yst) > 2:
        return None
    return {"all_x": all_x, "all_y": all_y,
            "cell_w": sum(xst)/len(xst), "cell_h": sum(yst)/len(yst),
            "n_cols": len(all_x), "n_rows": len(all_y),
            "shape_w": widths[0], "shape_h": heights[0], "fl": blocks[0]["fl"]}


def _detect_text_blob(node):
    items = node.get("it", [])
    sh_direct = [it for it in items if it.get("ty") == "sh"]
    outer_tr  = next((it for it in items if it.get("ty") == "tr"), None)
    if len(sh_direct) >= 2 and outer_tr is not None:
        bbox = _get_sh_bbox(sh_direct)
        if bbox and (bbox["max_x"] - bbox["min_x"]) > 1:
            total_v = sum(len(_extract_static_verts(sh.get("ks", {})) or [])
                          for sh in sh_direct)
            if total_v / len(sh_direct) >= 3:
                return {"container":    node,
                        "sh_items":     sh_direct,
                        "non_sh_items": [it for it in items if it.get("ty") != "sh"],
                        "bbox":         bbox,
                        "n_sh":         len(sh_direct)}

    first_gr = next((it for it in items if it.get("ty") == "gr"), None)
    if first_gr is not None:
        ng     = first_gr.get("it", [])
        ng_sh  = [it for it in ng if it.get("ty") == "sh"]
        ng_tr  = next((it for it in ng if it.get("ty") == "tr"), None)
        ng_grs = [it for it in ng if it.get("ty") == "gr"]
        if len(ng_sh) >= 2 and ng_tr is not None and not ng_grs:
            bbox = _get_sh_bbox(ng_sh)
            if bbox and (bbox["max_x"] - bbox["min_x"]) > 1:
                total_v = sum(len(_extract_static_verts(sh.get("ks", {})) or [])
                              for sh in ng_sh)
                if total_v / len(ng_sh) >= 3:
                    return {"container":    first_gr,
                            "sh_items":     ng_sh,
                            "non_sh_items": [it for it in ng if it.get("ty") != "sh"],
                            "bbox":         bbox,
                            "n_sh":         len(ng_sh)}
    return None


def _is_glyph_node(groups):
    if len(groups) < 2:
        return False
    xs = [g["x"] for g in groups]
    ys = [g["y"] for g in groups]
    ss = [g["s"] for g in groups]
    if not all(xs[i] < xs[i+1] for i in range(len(xs)-1)):
        return False
    if max(ys) - min(ys) > 5:
        return False
    if max(ss) - min(ss) > 1:
        return False
    if not any(g["has_sh"] for g in groups):
        return False
    avg_step = (xs[-1] - xs[0]) / (len(xs) - 1)
    if avg_step < 3:
        return False
    return True


def _find_best_in_node(node, depth=0):
    if depth > 10 or not isinstance(node, dict):
        return None, -1
    best_result, best_score = None, -1

    def update(result, score):
        nonlocal best_result, best_score
        if score > best_score:
            best_score, best_result = score, result

    try:
        grid = _detect_pixel_grid(node)
        if grid:
            update((LAYER_TYPE_PIXEL, grid), grid["n_cols"] * 10)
    except Exception:
        pass
    try:
        groups = _collect_all_groups(node)
        if _is_glyph_node(groups):
            update((LAYER_TYPE_GLYPH, {"main_shape": node}), len(groups))
    except Exception:
        pass
    try:
        blob = _detect_text_blob(node)
        if blob:
            update((LAYER_TYPE_BLOB, blob), blob["n_sh"])
    except Exception:
        pass

    if depth < 10:
        for item in node.get("it", []):
            if item.get("ty") == "gr":
                res, sc = _find_best_in_node(item, depth + 1)
                if res is not None:
                    update(res, sc)
    return best_result, best_score


def _score_layer(ltype, extra):
    if ltype == LAYER_TYPE_GLYPH:
        return len(_collect_all_groups(extra["main_shape"]))
    elif ltype == LAYER_TYPE_BLOB:
        return extra.get("n_sh", len(extra.get("sh_items", [])))
    elif ltype == LAYER_TYPE_PIXEL:
        return extra["n_cols"]
    return 0


def _find_all_text_layers(data):
    canvas_w = data.get("w", 512)
    canvas_h = data.get("h", 512)
    max_blob_w = canvas_w * 1.5
    max_blob_h = canvas_h * 1.5
    results = []
    seen_containers = set()

    def process_layer(layer):
        if layer.get("ty") != 4:
            return
        for main in layer.get("shapes", []):
            if main.get("ty") != "gr":
                continue
            try:
                res, sc = _find_best_in_node(main)
                if res is None:
                    continue
                ltype, extra = res
                min_score = MIN_TEXT_SCORE if ltype == LAYER_TYPE_PIXEL else MIN_GLYPH_BLOB_SCORE
                if sc < min_score:
                    continue
                if ltype == LAYER_TYPE_BLOB:
                    container = extra["container"]
                    _ctr = next((it for it in container.get("it", [])
                                 if it.get("ty") == "tr"), None)
                    _cs  = _extract_static_xy(_ctr.get("s", {})) if _ctr else None
                    c_sx = abs(_cs[0]) / 100.0 if _cs else 1.0
                    c_sy = abs(_cs[1]) / 100.0 if _cs else 1.0
                    bbox = extra["bbox"]
                    bw = (bbox["max_x"] - bbox["min_x"]) * c_sx
                    bh = (bbox["max_y"] - bbox["min_y"]) * c_sy
                    if bw > max_blob_w or bh > max_blob_h:
                        continue
                if ltype == LAYER_TYPE_BLOB:
                    cid = id(extra["container"])
                elif ltype == LAYER_TYPE_GLYPH:
                    cid = id(extra["main_shape"])
                elif ltype == LAYER_TYPE_PIXEL:
                    cid = id(main)
                else:
                    continue
                if cid not in seen_containers:
                    seen_containers.add(cid)
                    results.append((layer, ltype, extra))
            except Exception:
                continue

    for layer in data.get("layers", []):
        process_layer(layer)
    for asset in data.get("assets", []):
        for layer in asset.get("layers", []):
            process_layer(layer)
    return results


def _find_text_layer(data):
    results = _find_all_text_layers(data)
    if not results:
        raise ValueError("Текстовый слой не найден в стикере")
    scores = [_score_layer(lt, ex) for _, lt, ex in results]
    return results[scores.index(max(scores))]


# ─── GLYPH ───────────────────────────────────────────────────────────────────

def _hex_to_rgba(hex_color: str):
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        return [1, 1, 1, 1]
    r = int(h[0:2], 16) / 255.0
    g = int(h[2:4], 16) / 255.0
    b = int(h[4:6], 16) / 255.0
    return [round(r, 4), round(g, 4), round(b, 4), 1]


def _replace_glyph_text(text, layer, extra,
                        color_rgba=None, size_factor=1.0,
                        offset_x=0.0, offset_y=0.0,
                        font_path=None):
    _load_font(font_path)
    main_shape = extra["main_shape"]
    orig_items = main_shape.get("it", [])
    all_groups = _collect_all_groups(main_shape)

    if not all_groups:
        raise ValueError("Нет групп в текстовом слое")

    first_tr = all_groups[0]["tr"]
    s_xy = _extract_static_xy(first_tr.get("s", {}))
    p_xy = _extract_static_xy(first_tr.get("p", {}))
    if s_xy is None or p_xy is None:
        raise ValueError("Не удалось прочитать трансформ текстового слоя")

    scale        = [s_xy[0], s_xy[1]]
    scale_factor = scale[0] / 100.0

    all_xs, all_ys = [], []
    for item in orig_items:
        if item.get("ty") != "gr":
            continue
        tr = next((s for s in item.get("it", []) if s.get("ty") == "tr"), None)
        if tr is None:
            continue
        p = _extract_static_xy(tr.get("p", {}))
        s = _extract_static_xy(tr.get("s", {}))
        if p is None:
            continue
        gx, gy = p[0], p[1]
        sf_x = (s[0] / 100.0) if s else 1.0
        sf_y = (s[1] / 100.0) if s else 1.0
        for sh in item.get("it", []):
            if sh.get("ty") != "sh":
                continue
            verts = _extract_static_verts(sh.get("ks", {}))
            if not verts:
                continue
            for v in verts:
                all_xs.append(gx + v[0] * sf_x)
                all_ys.append(gy + v[1] * sf_y)

    if not all_xs:
        all_xs = [g["x"] for g in all_groups]
        all_ys = [g["y"] for g in all_groups]

    min_x, max_x = min(all_xs), max(all_xs)
    min_y, max_y = min(all_ys), max(all_ys)

    center_x = (min_x + max_x) / 2.0 + offset_x
    center_y = (min_y + max_y) / 2.0 + offset_y
    orig_w   = max(max_x - min_x, 1.0)

    ks_s_xy  = _extract_static_xy(layer.get("ks", {}).get("s", {}))
    x_mirror = ks_s_xy is not None and ks_s_xy[0] < 0

    orig_fill = None
    for item in orig_items:
        if item.get("ty") == "gr":
            orig_fill = next((s for s in item.get("it", []) if s.get("ty") == "fl"), None)
            if orig_fill:
                break
    if orig_fill is None:
        orig_fill = next((it for it in orig_items if it.get("ty") == "fl"), None)
    if orig_fill is None:
        orig_fill = {"ty": "fl", "c": {"a": 0, "k": [1, 1, 1, 1], "ix": 2},
                     "o": {"a": 0, "k": 100, "ix": 2}, "r": 1, "bm": 0}

    if color_rgba is not None:
        orig_fill = dict(orig_fill)
        orig_fill["c"] = {"a": 0, "k": color_rgba, "ix": 2}

    orig_outer_tr = next((it for it in orig_items if it.get("ty") == "tr"),
                         {"ty": "tr", "o": {"a": 0, "k": 100, "ix": 2}})

    char_data = [(ch, *get_char_data(ch, font_path)) for ch in text]
    if x_mirror:
        char_data = list(reversed(char_data))

    total_advance = sum(adv for _, _, adv in char_data) or 1.0

    target_w = orig_w * 0.95
    raw_w = total_advance * scale_factor
    fit = target_w / raw_w if raw_w > 0 else 1.0
    fit = max(0.3, min(fit, 1.5))
    fit *= size_factor
    scale = [scale[0] * fit, scale[1] * fit]
    scale_factor *= fit

    cursor = -total_advance * scale_factor / 2.0
    new_groups = []
    for ch, contours, advance in char_data:
        if ch != " " and contours:
            new_groups.append(_build_letter_group(
                contours,
                center_x + cursor,
                center_y,
                scale,
                color=color_rgba))
        cursor += advance * scale_factor

    main_shape["it"] = new_groups + [orig_fill, orig_outer_tr]


# ─── BLOB ────────────────────────────────────────────────────────────────────

def _replace_blob_text(text, layer, blob_info,
                       color_rgba=None, size_factor=1.0,
                       offset_x=0.0, offset_y=0.0,
                       font_path=None):
    _load_font(font_path)
    bbox    = blob_info["bbox"]
    orig_cx = (bbox["min_x"] + bbox["max_x"]) / 2 + offset_x
    orig_cy = (bbox["min_y"] + bbox["max_y"]) / 2 + offset_y
    orig_w  = bbox["max_x"] - bbox["min_x"]
    orig_h  = bbox["max_y"] - bbox["min_y"]

    char_data = []
    cursor = 0.0
    for ch in text:
        contours, advance = get_char_data(ch, font_path)
        char_data.append((ch, contours, advance, cursor))
        cursor += advance

    all_v = []
    for ch, contours, advance, pos in char_data:
        for cont in contours:
            for v in cont["v"]:
                all_v.append((pos + v[0], v[1]))
    if not all_v:
        return

    raw_min_x = min(v[0] for v in all_v); raw_max_x = max(v[0] for v in all_v)
    raw_min_y = min(v[1] for v in all_v); raw_max_y = max(v[1] for v in all_v)
    raw_cx = (raw_min_x + raw_max_x) / 2
    raw_cy = (raw_min_y + raw_max_y) / 2
    raw_w  = max(raw_max_x - raw_min_x, 1e-6)
    raw_h  = max(raw_max_y - raw_min_y, 1e-6)
    scale = min(orig_w / raw_w, orig_h / raw_h) * size_factor

    new_sh_items = []
    for ch, contours, advance, pos in char_data:
        if ch == " ":
            continue
        for cont in contours:
            new_v = [[(pos + v[0] - raw_cx) * scale + orig_cx,
                      (v[1] - raw_cy) * scale + orig_cy]
                     for v in cont["v"]]
            new_i = [[i[0] * scale, i[1] * scale] for i in cont["i"]]
            new_o = [[o[0] * scale, o[1] * scale] for o in cont["o"]]
            new_sh_items.append({"ty": "sh", "d": 1,
                                 "ks": {"a": 0, "k": {"c": cont["c"],
                                                       "v": new_v,
                                                       "i": new_i,
                                                       "o": new_o}, "ix": 2}})
            if color_rgba:
                new_sh_items.append({"ty": "fl",
                                     "c": {"a": 0, "k": color_rgba, "ix": 2},
                                     "o": {"a": 0, "k": 100, "ix": 2},
                                     "r": 1, "bm": 0})

    container = blob_info["container"]
    container["it"] = new_sh_items + blob_info["non_sh_items"]


# ─── PIXEL ───────────────────────────────────────────────────────────────────

def _render_text_pixels(text, n_rows, font_path=None):
    from PIL import Image, ImageDraw, ImageFont
    SCALE = 8
    px_h  = max(8, (n_rows - 1) * SCALE)
    try:
        font = ImageFont.truetype(font_path or FONT_PATH, px_h)
    except Exception:
        font = ImageFont.load_default()
    tmp  = Image.new("L", (4000, px_h + 4), 0)
    bbox = ImageDraw.Draw(tmp).textbbox((0, 0), text, font=font)
    text_w = max(1, bbox[2] - bbox[0])
    text_h = max(1, bbox[3] - bbox[1])
    img_w = text_w + SCALE * 2; img_h = n_rows * SCALE
    img   = Image.new("L", (img_w, img_h), 0)
    y_off = ((img_h - text_h) // 2) - bbox[1]
    ImageDraw.Draw(img).text((SCALE - bbox[0], y_off), text, font=font, fill=255)
    n_out_cols = (img_w + SCALE - 1) // SCALE
    threshold  = SCALE * SCALE * 255 * 0.15
    lit = []
    for row in range(n_rows):
        for col in range(n_out_cols):
            total = sum(img.getpixel((col*SCALE+dx, row*SCALE+dy))
                        for dy in range(SCALE) for dx in range(SCALE)
                        if col*SCALE+dx < img_w and row*SCALE+dy < img_h)
            if total > threshold:
                lit.append((col, row))
    return lit, n_out_cols


def _replace_pixel_text(text, layer, grid_info,
                        color_rgba=None, size_factor=1.0,
                        offset_x=0.0, offset_y=0.0,
                        font_path=None):
    cell_w  = grid_info["cell_w"];  cell_h  = grid_info["cell_h"]
    shape_w = grid_info["shape_w"]; shape_h = grid_info["shape_h"]
    all_x   = grid_info["all_x"];   all_y   = grid_info["all_y"]
    fl = grid_info["fl"] or {"ty": "fl",
                              "c": {"a": 0, "k": [0, 0, 0, 1], "ix": 2},
                              "o": {"a": 0, "k": 100, "ix": 2}, "r": 1, "bm": 0}
    if color_rgba:
        fl = dict(fl)
        fl["c"] = {"a": 0, "k": color_rgba, "ix": 2}
    lit, n_cols = _render_text_pixels(text, grid_info["n_rows"], font_path)
    x_origin = (all_x[0] + all_x[-1]) / 2 - (n_cols / 2) * cell_w + offset_x
    y_origin = all_y[0] + offset_y
    hw = shape_w / 2; hh = shape_h / 2

    def make_block(px, py):
        return {"ty": "gr", "it": [
            {"ty": "sh", "d": 1, "ks": {"a": 0, "k": {
                "c": True,
                "v": [[ hw, hh],[-hw, hh],[-hw,-hh],[ hw,-hh]],
                "i": [[0,0],[0,0],[0,0],[0,0]],
                "o": [[0,0],[0,0],[0,0],[0,0]],
            }, "ix": 2}},
            fl,
            {"ty": "tr",
             "p": {"a": 0, "k": [px, py], "ix": 2},
             "a": {"a": 0, "k": [0, 0],   "ix": 1},
             "s": {"a": 0, "k": [100,100],"ix": 3},
             "r": {"a": 0, "k": 0,        "ix": 6},
             "o": {"a": 0, "k": 100,      "ix": 7},
             "sk":{"a": 0, "k": 0,        "ix": 4},
             "sa":{"a": 0, "k": 0,        "ix": 5}},
        ]}

    main_shape    = layer["shapes"][0]
    orig_outer_tr = next((it for it in main_shape.get("it",[]) if it.get("ty")=="tr"),
                         {"ty": "tr", "o": {"a": 0, "k": 100, "ix": 2}})
    main_shape["it"] = [make_block(x_origin+col*cell_w, y_origin+row*cell_h)
                        for col, row in lit] + [orig_outer_tr]


# ─── Перекраска ─────────────────────────────────────────────────────────────

def _detect_parts(data):
    layers = data.get("layers", [])
    if not layers:
        return {"bg": [], "outline": [], "text": [], "shape": []}

    text_layer_ids = set()
    for text_layer, ltype, extra in _find_all_text_layers(data):
        if ltype == LAYER_TYPE_GLYPH:
            text_layer_ids.add(id(extra["main_shape"]))
        elif ltype == LAYER_TYPE_BLOB:
            text_layer_ids.add(id(extra["container"]))
        elif ltype == LAYER_TYPE_PIXEL:
            if text_layer.get("shapes"):
                text_layer_ids.add(id(text_layer["shapes"][0]))

    def collect_fills_strokes(item, out_fills, out_strokes):
        if isinstance(item, dict):
            ty = item.get("ty")
            if ty in ("fl", "gf"):
                out_fills.append(item)
            elif ty in ("st", "gs"):
                out_strokes.append(item)
            for k in ("it", "shapes"):
                if k in item and isinstance(item[k], list):
                    for v in item[k]:
                        collect_fills_strokes(v, out_fills, out_strokes)

    layer_fills = []
    layer_strokes = []
    layer_is_text = []

    for layer in layers:
        fills, strokes = [], []
        if layer.get("ty") == 4:
            for sh in layer.get("shapes", []):
                collect_fills_strokes(sh, fills, strokes)
        layer_fills.append(fills)
        layer_strokes.append(strokes)
        is_text = False
        if layer.get("shapes"):
            is_text = id(layer["shapes"][0]) in text_layer_ids
        layer_is_text.append(is_text)

    bg = []
    bg_index = None
    for i in range(len(layers) - 1, -1, -1):
        if layer_fills[i] and not layer_is_text[i]:
            bg = layer_fills[i]
            bg_index = i
            break

    text_objs = []
    for i, layer in enumerate(layers):
        if layer_is_text[i]:
            text_objs.extend(layer_fills[i])

    shape_objs = []
    outline_objs = []
    for i, layer in enumerate(layers):
        if i == bg_index:
            outline_objs.extend(layer_strokes[i])
            continue
        if layer_is_text[i]:
            continue
        shape_objs.extend(layer_fills[i])
        outline_objs.extend(layer_strokes[i])

    return {"bg": bg, "shape": shape_objs,
            "outline": outline_objs, "text": text_objs}


def _apply_color_to_parts(parts, color_rgba):
    for obj in parts:
        ty = obj.get("ty")
        if ty in ("fl", "st", "gf", "gs"):
            obj["c"] = {"a": 0, "k": color_rgba, "ix": 2}
            if ty in ("gf", "gs"):
                obj["ty"] = "fl" if ty == "gf" else "st"
                for k in ("g", "s", "e", "t", "h", "a", "r"):
                    obj.pop(k, None)


# ─── Публичный API ───────────────────────────────────────────────────────────

def generate_sticker(text: str, template_path: str, output_path: str,
                     color: str | None = None,
                     bg_color: str | None = None,
                     shape_color: str | None = None,
                     outline_color: str | None = None,
                     text_color: str | None = None,
                     font_path: str | None = None,
                     size: float = 1.0,
                     offset_x: float = 0.0,
                     offset_y: float = 0.0) -> None:
    _load_font(font_path)

    if color and not bg_color:      bg_color = color
    if color and not shape_color:   shape_color = color
    if color and not outline_color: outline_color = color
    if color and not text_color:    text_color = color

    text_rgba    = _hex_to_rgba(text_color)    if text_color    else None
    bg_rgba      = _hex_to_rgba(bg_color)      if bg_color      else None
    shape_rgba   = _hex_to_rgba(shape_color)   if shape_color   else None
    outline_rgba = _hex_to_rgba(outline_color) if outline_color else None

    with gzip.open(template_path, "rb") as f:
        data = json.load(f)

    candidates = _find_all_text_layers(data)
    if not candidates:
        raise ValueError("Текстовый слой не найден в стикере")

    for text_layer, ltype, extra in candidates:
        if ltype == LAYER_TYPE_GLYPH:
            _replace_glyph_text(text, text_layer, extra,
                                color_rgba=text_rgba,
                                size_factor=size,
                                offset_x=offset_x,
                                offset_y=offset_y,
                                font_path=font_path)
        elif ltype == LAYER_TYPE_BLOB:
            _replace_blob_text(text, text_layer, extra,
                               color_rgba=text_rgba,
                               size_factor=size,
                               offset_x=offset_x,
                               offset_y=offset_y,
                               font_path=font_path)
        elif ltype == LAYER_TYPE_PIXEL:
            _replace_pixel_text(text, text_layer, extra,
                                color_rgba=text_rgba,
                                size_factor=size,
                                offset_x=offset_x,
                                offset_y=offset_y,
                                font_path=font_path)

    if bg_rgba or shape_rgba or outline_rgba:
        parts = _detect_parts(data)
        if bg_rgba:
            _apply_color_to_parts(parts["bg"], bg_rgba)
        if shape_rgba:
            _apply_color_to_parts(parts["shape"], shape_rgba)
        if outline_rgba:
            _apply_color_to_parts(parts["outline"], outline_rgba)

    json_bytes = json.dumps(data, separators=(",", ":")).encode("utf-8")
    with gzip.open(output_path, "wb", compresslevel=9) as f:
        f.write(json_bytes)
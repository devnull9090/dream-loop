"""Blender side of Dream Loop's 3D mode: camera setup, flat ID-colour mask renders, per-part
bounds and hashes, close-up crops, camera sweeps and blockouts.

This file runs inside Blender. Do not run it directly; scripts/blender_run.py starts it either
headless (blender -b scene.blend --python dream_loop_bpy.py -- request.json) or in an open
session through the blender-mcp add-on's execute_code command. Either way it handles one
request (a dict with a "command") and prints one line: DREAM_LOOP_RESULT <json>.

Conventions: the subject faces -Y and stands on its lowest point. Camera yaw 0 is the front
view (camera on -Y), 90 puts the camera on +X, 180 is the back; pitch is degrees above the
horizon. The camera always aims at the centre of the subject's bounds from a distance that
fits the subject in frame (a solved camera keeps its distance in subject heights), so a solved
camera reproduces the same view after the model changes or is rescaled. The world-space
location and target written to <view>.ids.json are for the model as it was at that render.
"""
import fnmatch
import hashlib
import json
import math
import os
import sys
import traceback
from contextlib import contextmanager

import bpy
import numpy
from mathutils import Vector

MARKER = "DREAM_LOOP_RESULT "
CAMERA = "DreamLoopCamera"
BLOCKOUT = "DreamLoop Blockout"
GEOMETRY = {"MESH", "CURVE", "SURFACE", "META", "FONT"}
LEVELS = (45, 115, 185, 255)  # ID colour channel values; far enough apart to survive an 8-bit round trip
FIT = 1.08  # the subject's bounding sphere fills the frame's short side less this margin


def id_colour(index):
    """Distinct flat colours; black is left for the background."""
    n = (index * 23 + 1) % 63 + 1
    return [LEVELS[n % 4], LEVELS[n // 4 % 4], LEVELS[n // 16 % 4]]


def collection_objects(collection):
    for obj in collection.objects:
        yield obj
    for child in collection.children:
        yield from collection_objects(child)


def subject_objects(scene, view):
    """Geometry objects that would render in this view."""
    name = (view.get("scene") or {}).get("collection")
    if name:
        if name not in bpy.data.collections:
            raise ValueError(f"view {view['id']}: no collection named {name!r}")
        pool = set(collection_objects(bpy.data.collections[name]))
    else:
        pool = set(scene.objects)
    return sorted((o for o in pool if o.type in GEOMETRY and not o.hide_render and o.name in scene.objects), key=lambda o: o.name)


def assign(blueprint, objects):
    """Objects per part id, by a dl_part custom property or the part's name patterns."""
    parts = {p["id"]: [] for p in blueprint["parts"]}
    unassigned = []
    for obj in objects:
        tag = obj.get("dl_part")
        part = tag if tag in parts else next(
            (p["id"] for p in blueprint["parts"] if any(fnmatch.fnmatchcase(obj.name, pattern) for pattern in p["objects"])), None)
        if part is None:
            unassigned.append(obj.name)
        else:
            parts[part].append(obj)
    return parts, unassigned


def world_points(obj, depsgraph):
    """Evaluated vertex positions in world space as an N x 3 array (modifiers and shape keys applied)."""
    evaluated = obj.evaluated_get(depsgraph)
    try:
        mesh = evaluated.to_mesh()
    except RuntimeError:
        mesh = None
    if mesh is None or not len(mesh.vertices):
        corners = numpy.array([obj.matrix_world @ Vector(c) for c in obj.bound_box], dtype=numpy.float64)
        if mesh is not None:
            evaluated.to_mesh_clear()
        return corners
    co = numpy.empty(len(mesh.vertices) * 3, dtype=numpy.float32)
    mesh.vertices.foreach_get("co", co)
    evaluated.to_mesh_clear()
    matrix = numpy.array(evaluated.matrix_world, dtype=numpy.float64)
    return co.reshape(-1, 3).astype(numpy.float64) @ matrix[:3, :3].T + matrix[:3, 3]


def material_signature(material, named=True):
    if material is None:
        return "none"
    out = [material.name if named else "", repr([round(c, 4) for c in material.diffuse_color])]
    if material.node_tree:
        for node in sorted(material.node_tree.nodes, key=lambda n: n.name):
            out.append(node.bl_idname)
            image = getattr(node, "image", None)
            if image is not None:
                out.append(image.filepath or image.name)
            for socket in node.inputs:
                if not socket.is_linked and hasattr(socket, "default_value"):
                    value = socket.default_value
                    out.append(repr([round(v, 4) for v in value]) if hasattr(value, "__len__") else repr(round(value, 4) if isinstance(value, float) else value))
    return "|".join(out)


def part_hash(objects, depsgraph):
    """Changes when the part's geometry, vertex colours or materials change; not when the camera or lights do."""
    digest = hashlib.sha1()
    for obj in sorted(objects, key=lambda o: o.name):
        digest.update(numpy.round(world_points(obj, depsgraph), 4).tobytes())
        for slot in obj.material_slots:
            digest.update(material_signature(slot.material).encode())
        data = obj.data
        for attribute in getattr(data, "color_attributes", []):
            values = numpy.empty(len(attribute.data) * 4, dtype=numpy.float32)
            attribute.data.foreach_get("color", values)
            digest.update(numpy.round(values, 3).tobytes())
    return digest.hexdigest()[:16]


TWIN = 0.002  # two parts are the same shape when no vertex is further than this share of the part's size from one of the other's


def twin(objects, source_objects, source, depsgraph, height):
    """Whether a part is its source's geometry mirrored left to right, or a plain copy of it.

    Both are compared about the centre of their own bounds, so where each sits does not matter;
    how each is turned does. Returns {"of": source, "how": "mirror" | "copy" | None, "why": ...}."""
    from mathutils import kdtree

    def centred(group):
        points = numpy.concatenate([world_points(o, depsgraph) for o in group])
        low, high = points.min(axis=0), points.max(axis=0)
        return points - (low + high) / 2, float((high - low).max())

    def materials(group):
        return sorted(material_signature(slot.material, named=False) for o in group for slot in o.material_slots)

    a, size = centred(source_objects)
    b, _ = centred(objects)
    if len(a) != len(b):
        return {"of": source, "how": None, "why": f"{len(b)} vertices against {len(a)}"}
    if materials(objects) != materials(source_objects):
        return {"of": source, "how": None, "why": "their materials differ"}

    def furthest(points, others):
        tree = kdtree.KDTree(len(others))
        for index, point in enumerate(others):
            tree.insert(point, index)
        tree.balance()
        return max(tree.find(point)[2] for point in points[::max(1, len(points) // 4000)])

    best = None
    for how, sign in (("mirror", -1.0), ("copy", 1.0)):
        moved = b * numpy.array([sign, 1.0, 1.0])
        off = float(numpy.abs(moved - a).max())  # vertex for vertex, when the two were built in the same order
        if off > TWIN * max(size, 1e-9):
            off = max(furthest(moved, a), furthest(a, moved))
        if off <= TWIN * max(size, 1e-9):
            return {"of": source, "how": how}
        best = off if best is None else min(best, off)
    return {"of": source, "how": None, "why": f"their shapes differ by up to {best / height:.3f} H"}


def bounds(objects, depsgraph):
    points = numpy.concatenate([world_points(o, depsgraph) for o in objects])
    return points.min(axis=0), points.max(axis=0)


def field_of_view(lens, resolution, sensor=36.0):
    """Horizontal and vertical field of view in radians for Blender's AUTO sensor fit."""
    wide = 2 * math.atan(sensor / (2 * lens))
    narrow = 2 * math.atan(math.tan(wide / 2) * min(resolution) / max(resolution))
    return (wide, narrow) if resolution[0] >= resolution[1] else (narrow, wide)


def set_camera(scene, spec, low, high, resolution):
    """Place the Dream Loop camera for yaw, pitch and lens, aimed at the centre of the bounds. Returns what was set."""
    camera = bpy.data.objects.get(CAMERA)
    if camera is None or camera.type != "CAMERA":
        camera = bpy.data.objects.new(CAMERA, bpy.data.cameras.new(CAMERA))
    if camera.name not in scene.collection.all_objects:
        scene.collection.objects.link(camera)
    camera.data.type = "PERSP"
    camera.data.sensor_fit = "AUTO"
    camera.data.sensor_width = 36.0
    camera.data.lens = spec["lens"]
    camera.data.shift_x = camera.data.shift_y = 0.0
    centre = (low + high) / 2
    radius = float(numpy.linalg.norm(high - low)) / 2
    fov = field_of_view(spec["lens"], resolution)
    height = float(high[2] - low[2]) or 1.0
    fit = radius / math.sin(min(fov) / 2)  # nearer than this and the subject may leave the frame
    # A solved camera keeps its distance in subject heights, so perspective stays the same when the model is rescaled.
    distance = max(spec["distance_H"] * height, fit) if spec.get("distance_H") else FIT * fit
    camera.data.clip_start = max(0.001, distance - 2 * radius) * 0.5
    camera.data.clip_end = (distance + 2 * radius) * 2
    yaw, pitch = math.radians(spec["yaw"]), math.radians(spec["pitch"])
    direction = Vector((math.sin(yaw) * math.cos(pitch), -math.cos(yaw) * math.cos(pitch), math.sin(pitch)))
    camera.location = Vector(centre) + direction * distance
    camera.rotation_euler = (-direction).to_track_quat("-Z", "Y").to_euler()
    scene.camera = camera
    return {
        "yaw": spec["yaw"], "pitch": spec["pitch"], "lens": spec["lens"],
        "distance": round(distance, 5), "distance_H": round(distance / height, 4),
        "height": round(float(camera.location.z - low[2]), 5), "height_H": round(float(camera.location.z - low[2]) / height, 4),
        "target": [round(float(v), 5) for v in centre], "location": [round(float(v), 5) for v in camera.location],
        "fov_h": round(math.degrees(fov[0]), 3), "fov_v": round(math.degrees(fov[1]), 3),
        "subject_height": round(height, 5), "resolution": list(resolution),
    }


def project(points, camera, fov):
    """World points to frame fractions (x right, y down) for a camera with the given horizontal and vertical field of view."""
    matrix = numpy.array(camera.matrix_world.normalized().inverted(), dtype=numpy.float64)
    local = points @ matrix[:3, :3].T + matrix[:3, 3]
    depth = numpy.maximum(-local[:, 2], 1e-9)
    x = 0.5 + local[:, 0] / (2 * depth * math.tan(math.radians(fov[0]) / 2))
    y = 0.5 - local[:, 1] / (2 * depth * math.tan(math.radians(fov[1]) / 2))
    return x, y


@contextmanager
def restored(pairs):
    """Set attributes for the duration of a block: pairs of (owner, name, value). Unknown names are skipped."""
    saved = []
    try:
        for owner, name, value in pairs:
            if hasattr(owner, name):
                saved.append((owner, name, getattr(owner, name) if name != "color" else tuple(getattr(owner, name))))
                try:
                    setattr(owner, name, value)
                except TypeError:  # an enum value this build does not have
                    saved.pop()
        yield
    finally:
        for owner, name, value in reversed(saved):
            try:
                setattr(owner, name, value)
            except (TypeError, ReferenceError):
                pass


def flat_settings(scene, path, resolution):
    """Scene settings for an exact flat-colour render: Workbench, no lighting, no anti-aliasing, raw output."""
    shading, render = scene.display.shading, scene.render
    return [
        (render, "engine", "BLENDER_WORKBENCH"), (shading, "light", "FLAT"), (shading, "color_type", "OBJECT"),
        (shading, "show_shadows", False), (shading, "show_cavity", False), (shading, "show_object_outline", False),
        (shading, "show_xray", False), (shading, "show_backface_culling", False), (shading, "use_dof", False),
        (scene.display, "render_aa", "OFF"), (render, "film_transparent", True), (render, "dither_intensity", 0.0),
        (render, "use_border", False), (render, "use_freestyle", False), (render, "use_compositing", False),
        (render, "use_sequencer", False),
        (scene.view_settings, "view_transform", "Standard"), (scene.view_settings, "view_transform", "Raw"),
        (scene.view_settings, "look", "None"), (scene.view_settings, "exposure", 0.0), (scene.view_settings, "gamma", 1.0),
        (render.image_settings, "file_format", "PNG"), (render.image_settings, "color_mode", "RGBA"),
        (render.image_settings, "color_depth", "8"), (render.image_settings, "compression", 15),
        (render, "resolution_x", resolution[0]), (render, "resolution_y", resolution[1]),
        (render, "resolution_percentage", 100), (render, "filepath", path),
    ]


def to_scene_linear(value, scene):
    """The object colour that comes out of the render as this 8-bit value."""
    v = value / 255
    if scene.view_settings.view_transform == "Raw":
        return v
    return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4  # Standard: the display applies sRGB


def read_pixels(path):
    """A rendered PNG as an H x W x 4 array of 0-255 values, top row first."""
    image = bpy.data.images.load(path, check_existing=False)
    try:
        image.colorspace_settings.name = "Non-Color"
    except TypeError:
        pass
    width, height = image.size
    values = numpy.empty(width * height * 4, dtype=numpy.float32)
    image.pixels.foreach_get(values)
    bpy.data.images.remove(image)
    return numpy.rint(values.reshape(height, width, 4)[::-1] * 255).astype(numpy.int16)


def render_ids(scene, parts, others, path, resolution, solo=True):
    """Render every part in its own flat colour on a transparent ground. Returns per-part pixel counts.

    others are objects to keep out of the picture. With solo, each part is also rendered alone so
    the share of it hidden behind other parts is known."""
    colours = {part: id_colour(i) for i, part in enumerate(parts)}
    pairs = flat_settings(scene, path, resolution)
    pairs += [(obj, "hide_render", True) for obj in others]
    with restored(pairs):
        tint = [(obj, "color", (*[to_scene_linear(c, scene) for c in colours[part]], 1.0)) for part, objects in parts.items() for obj in objects]
        with restored(tint):
            bpy.ops.render.render(write_still=True)
            pixels = read_pixels(path)
            out = {}
            for part, objects in parts.items():
                match = (numpy.abs(pixels[:, :, :3] - colours[part]).max(axis=2) <= 12) & (pixels[:, :, 3] > 127)
                out[part] = {"colour": colours[part], "visible_px": int(match.sum())}
            if solo:
                solo_path = path[:-4] + ".solo.png"
                scene.render.filepath = solo_path
                for part, objects in parts.items():
                    if not objects:
                        continue
                    hidden = [(obj, "hide_render", True) for other, group in parts.items() if other != part for obj in group]
                    with restored(hidden):
                        bpy.ops.render.render(write_still=True)
                    out[part]["solo_px"] = int((read_pixels(solo_path)[:, :, 3] > 127).sum())
                if os.path.exists(solo_path):
                    os.remove(solo_path)
    return out


def apply_view(scene, view):
    frame = (view.get("scene") or {}).get("frame")
    if frame is not None:
        scene.frame_set(frame)
    bpy.context.view_layer.update()


def view_setup(scene, blueprint, view, camera_spec, resolution):
    """Pose the scene for a view, sort its objects into parts and place the camera."""
    apply_view(scene, view)
    depsgraph = bpy.context.evaluated_depsgraph_get()
    objects = subject_objects(scene, view)
    parts, unassigned = assign(blueprint, objects)
    used = [o for group in parts.values() for o in group]
    if not used:
        raise ValueError(f"view {view['id']}: no scene object matches any part's objects patterns")
    low, high = bounds(used, depsgraph)
    camera = set_camera(scene, camera_spec or view["camera"], low, high, resolution)
    bpy.context.view_layer.update()
    others = [o for o in scene.objects if o.type in GEOMETRY and not o.hide_render and o not in used]
    return depsgraph, parts, unassigned, others, camera


def frame_size(view_size, long_edge):
    """Render size with the view frame's shape, so the subject sits in the frame as it does on the target."""
    aspect = view_size[0] / view_size[1]
    size = (long_edge, max(16, round(long_edge / aspect))) if aspect >= 1 else (max(16, round(long_edge * aspect)), long_edge)
    return [int(v) // 2 * 2 for v in size]


def picture_settings(scene, path, resolution, others, beauty):
    """Scene settings for a picture of the subject alone: the scene's own engine, or a quick Workbench preview."""
    pairs = [(scene.render, "resolution_x", resolution[0]), (scene.render, "resolution_y", resolution[1]),
             (scene.render, "resolution_percentage", 100), (scene.render, "filepath", path),
             (scene.render.image_settings, "file_format", "PNG"), (scene.render.image_settings, "color_mode", "RGBA"),
             (scene.render, "film_transparent", True), (scene.render, "use_border", False)]
    pairs += [(obj, "hide_render", True) for obj in others]
    if beauty == "preview":  # seconds, not minutes: Workbench with the materials' own colours
        shading = scene.display.shading
        pairs += [(scene.render, "engine", "BLENDER_WORKBENCH"), (shading, "light", "STUDIO"),
                  (shading, "color_type", "MATERIAL"), (shading, "color_type", "TEXTURE"), (shading, "show_cavity", True)]
    return pairs


def render_view(scene, blueprint, view, out_dir, camera_spec=None, long_edge=1024, beauty="preview"):
    """ID mask, part data and (optionally) a picture of one view. Writes <view>.ids.png, <view>.ids.json, <view>.png."""
    size = blueprint["target_size"]
    resolution = frame_size((view["frame"][2] * size[0], view["frame"][3] * size[1]), long_edge)
    depsgraph, parts, unassigned, others, camera = view_setup(scene, blueprint, view, camera_spec, resolution)
    base = os.path.join(out_dir, view["id"])
    counts = render_ids(scene, parts, others, base + ".ids.png", resolution)
    data = {"view": view["id"], "camera": camera, "resolution": resolution, "ids": os.path.basename(base + ".ids.png"),
            "unassigned": unassigned, "parts": {}}
    for part, objects in parts.items():
        entry = counts[part]
        if objects:
            x, y = project(numpy.concatenate([world_points(o, depsgraph) for o in objects]), scene.camera, (camera["fov_h"], camera["fov_v"]))
            entry["projected_box"] = [round(float(v), 4) for v in (x.min(), y.min(), x.max() - x.min(), y.max() - y.min())]
            entry["visible"] = round(entry["visible_px"] / entry["solo_px"], 3) if entry.get("solo_px") else 0.0
            entry["hash"] = part_hash(objects, depsgraph)
            entry["objects"] = len(objects)
            source = next((p.get("same_as") for p in blueprint["parts"] if p["id"] == part), None)
            if source and parts.get(source):
                entry["twin"] = twin(objects, parts[source], source, depsgraph, camera["subject_height"])
        else:
            entry.update(objects=0, visible=0.0)
        data["parts"][part] = entry
    if beauty != "none":
        with restored(picture_settings(scene, base + ".png", resolution, others, beauty)):
            bpy.ops.render.render(write_still=True)
        data["picture"] = os.path.basename(base + ".png")
    with open(base + ".ids.json", "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=1)
    return data


def command_render(request):
    scene = bpy.context.scene
    blueprint = request["blueprint"]
    os.makedirs(request["out"], exist_ok=True)
    wanted = request.get("views") or [v["id"] for v in blueprint["views"]]
    views = {}
    for view in blueprint["views"]:
        if view["id"] in wanted:
            data = render_view(scene, blueprint, view, request["out"], (request.get("cameras") or {}).get(view["id"]),
                               request.get("long_edge", 1024), request.get("beauty", "preview"))
            views[view["id"]] = {"file": os.path.join(request["out"], view["id"] + ".ids.json").replace("\\", "/"),
                                 "unassigned": data["unassigned"],
                                 "missing": [p for p, e in data["parts"].items() if not e["objects"]]}
    return {"views": views}


def command_closeups(request):
    """A picture of each requested part alone in its frame, at a size that gives the part the pixels asked for.

    Each item names a view, a part, the box to render (x, y, w, h as fractions of the view's frame,
    from the top left) and the frame's long edge. Only that box is rendered, so a large frame costs
    little. The camera is the view's own, so the crop lines up with the mask render."""
    scene = bpy.context.scene
    blueprint = request["blueprint"]
    os.makedirs(request["out"], exist_ok=True)
    size = blueprint["target_size"]
    done = []
    for view in blueprint["views"]:
        for item in [i for i in request["items"] if i["view"] == view["id"]]:
            resolution = frame_size((view["frame"][2] * size[0], view["frame"][3] * size[1]), item["long_edge"])
            _, _, _, others, _ = view_setup(scene, blueprint, view, (request.get("cameras") or {}).get(view["id"]), resolution)
            x, y, w, h = item["box"]
            path = os.path.join(request["out"], f"{item['part']}.{view['id']}.render.png")
            render = scene.render
            pairs = picture_settings(scene, path, resolution, others, request.get("beauty", "preview"))
            pairs += [(render, "use_border", True), (render, "use_crop_to_border", True), (render, "border_min_x", x),
                      (render, "border_max_x", min(1.0, x + w)), (render, "border_min_y", max(0.0, 1 - y - h)), (render, "border_max_y", 1 - y)]
            with restored(pairs):
                bpy.ops.render.render(write_still=True)
            done.append({"view": view["id"], "part": item["part"], "file": path.replace("\\", "/"), "long_edge": item["long_edge"]})
    return {"closeups": done}


def command_sweep(request):
    """Silhouette renders of one view over a window of cameras around the estimate, for viewmatch.py to choose from."""
    scene = bpy.context.scene
    blueprint = request["blueprint"]
    view = next(v for v in blueprint["views"] if v["id"] == request["view"])
    os.makedirs(request["out"], exist_ok=True)
    size = blueprint["target_size"]
    resolution = frame_size((view["frame"][2] * size[0], view["frame"][3] * size[1]), request.get("long_edge", 384))
    centre = request.get("around") or view["camera"]
    candidates = []
    for lens in request["lenses"]:
        for pitch in request["pitches"]:
            for yaw in request["yaws"]:
                spec = {"yaw": round((centre["yaw"] + yaw + 180) % 360 - 180, 3), "pitch": round(max(-89, min(89, centre["pitch"] + pitch)), 3), "lens": lens}
                _, parts, _, others, camera = view_setup(scene, blueprint, view, spec, resolution)
                name = f"{view['id']}.{len(candidates):03d}.png"
                render_ids(scene, parts, others, os.path.join(request["out"], name), resolution, solo=False)
                candidates.append({"file": name, "camera": camera})
    listing = os.path.join(request["out"], view["id"] + ".candidates.json")
    with open(listing, "w", encoding="utf-8") as handle:
        json.dump({"view": view["id"], "estimate": view["camera"], "candidates": candidates}, handle, indent=1)
    return {"candidates": len(candidates), "file": listing.replace("\\", "/")}


def command_blockout(request):
    """Build one ellipsoid per part from blueprint.py's blockout spec, scaled so the subject is request['height'] tall."""
    scene = bpy.context.scene
    scale = request.get("height", 1.0)
    if request.get("clear"):  # started from the default scene: drop its cube, light and camera
        for obj in list(scene.objects):
            bpy.data.objects.remove(obj, do_unlink=True)
    collection = bpy.data.collections.get(BLOCKOUT) or bpy.data.collections.new(BLOCKOUT)
    if collection.name not in scene.collection.children:
        scene.collection.children.link(collection)
    for obj in list(collection.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    sphere = bpy.data.meshes.new("DreamLoopBlock")
    import bmesh
    mesh = bmesh.new()
    bmesh.ops.create_uvsphere(mesh, u_segments=24, v_segments=12, radius=0.5)
    mesh.to_mesh(sphere)
    mesh.free()
    names = []
    for part in request["spec"]["parts"]:
        obj = bpy.data.objects.new("BO_" + part["id"], sphere)
        obj["dl_part"] = part["id"]
        obj.location = [v * scale for v in part["centre"]]
        obj.scale = [v * scale for v in part["size"]]
        collection.objects.link(obj)
        names.append(obj.name)
    bpy.context.view_layer.update()
    if request.get("save"):
        bpy.ops.wm.save_as_mainfile(filepath=request["save"])
    return {"collection": BLOCKOUT, "objects": names, "saved": request.get("save")}


def command_info(request):
    scene = bpy.context.scene
    return {"blender": bpy.app.version_string, "file": bpy.data.filepath, "scene": scene.name,
            "objects": [o.name for o in scene.objects if o.type in GEOMETRY]}


COMMANDS = {"render": command_render, "sweep": command_sweep, "closeups": command_closeups, "blockout": command_blockout,
            "info": command_info}


def run(request):
    """Handle one request and return its result; errors come back as {"error": ...} instead of raising."""
    scene = bpy.context.scene
    camera, frame = scene.camera, scene.frame_current
    try:
        return COMMANDS[request["command"]](request)
    except Exception as error:  # the caller is another process; give it the whole story
        return {"error": f"{type(error).__name__}: {error}", "traceback": traceback.format_exc()}
    finally:  # in an open session, leave the user's active camera and timeline where they were
        try:
            scene.camera = camera
            if scene.frame_current != frame:
                scene.frame_set(frame)
        except ReferenceError:
            pass


if __name__ == "__main__":
    with open(sys.argv[sys.argv.index("--") + 1], encoding="utf-8") as handle:
        result = run(json.load(handle))
    print(MARKER + json.dumps(result))
    sys.stdout.flush()
    if "error" in result:
        sys.exit(1)

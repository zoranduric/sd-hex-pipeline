class_name HexMapData
extends RefCounted
## Loads a .sdhx file baked by sd-hex-pipeline into flat Packed*Array layers.
##
## Layout: pointy-top hexes, odd-r offset storage (odd rows shifted half a hex
## east), row 0 = north, idx = row * width + col. Direction indices match
## HexGrid.NEIGHBOR_DIRS: 0 E, 1 NE, 2 NW, 3 W, 4 SW, 5 SE.
##
## Pure data: no Node / SceneTree / RenderingServer dependencies, so it fits
## scripts/core/ and ports 1:1 to C#.

const MAGIC := "SDHX"
const SUPPORTED_VERSION := 1
const MAX_HEADER_BYTES := 64 * 1024 * 1024
const NEIGHBOR_DIRS: Array[Vector2i] = [
	Vector2i(1, 0), Vector2i(1, -1), Vector2i(0, -1),
	Vector2i(-1, 0), Vector2i(-1, 1), Vector2i(0, 1),
]

const FLAG_LAND := 1
const FLAG_LAKE := 2
const FLAG_COASTAL := 4
const FLAG_RIVER := 8
const FLAG_NAVIGABLE := 16

var header: Dictionary = {}
var width: int = 0
var height: int = 0
var cells: int = 0
## Global offset of this crop inside the world grid (row0 is always even).
var col0: int = 0
var row0: int = 0
var global_width: int = 0
var hex_km: float = 0.0

var terrain := PackedByteArray()
var flags := PackedByteArray()
var landcover := PackedByteArray()
## Climate zone per hex (header["biome_enum"]); empty in files built with --no-biomes.
var biome := PackedByteArray()
var river_mask := PackedByteArray()
var river_class := PackedByteArray()
var elev_mean := PackedInt32Array()
var country := PackedInt32Array()
var admin1 := PackedInt32Array()

var _u8: Dictionary = {}
var _i32: Dictionary = {}


## Returns null (and pushes an error) if the file is missing or malformed.
static func load_file(path: String) -> HexMapData:
	var bytes := FileAccess.get_file_as_bytes(path)
	if bytes.is_empty():
		push_error("HexMapData: cannot read %s (%s)" % [path, error_string(FileAccess.get_open_error())])
		return null
	var data := HexMapData.new()
	var err := data._parse(bytes)
	if err != "":
		push_error("HexMapData: %s: %s" % [path, err])
		return null
	return data


func _parse(bytes: PackedByteArray) -> String:
	if bytes.size() < 12 or bytes.slice(0, 4).get_string_from_ascii() != MAGIC:
		return "not an SDHX file"
	var version := bytes.decode_u32(4)
	if version != SUPPORTED_VERSION:
		return "unsupported version %d" % version
	var hlen := bytes.decode_u32(8)
	if hlen <= 0 or hlen > MAX_HEADER_BYTES or 12 + hlen > bytes.size():
		return "bad header length"
	var parsed: Variant = JSON.parse_string(bytes.slice(12, 12 + hlen).get_string_from_utf8())
	if typeof(parsed) != TYPE_DICTIONARY:
		return "header is not a JSON object"
	header = parsed
	var crop: Dictionary = header.get("crop", {})
	var grid: Dictionary = header.get("grid", {})
	width = int(crop.get("width", 0))
	height = int(crop.get("height", 0))
	cells = int(crop.get("cells", 0))
	col0 = int(crop.get("col0", 0))
	row0 = int(crop.get("row0", 0))
	global_width = int(grid.get("global_width", 0))
	hex_km = float(grid.get("hex_km", 0.0))
	if width <= 0 or height <= 0 or cells != width * height or global_width < width:
		return "bad crop dimensions"
	if row0 % 2 != 0:
		return "crop row0 must be even"

	for entry: Variant in header.get("layers", []):
		if typeof(entry) != TYPE_DICTIONARY:
			return "bad layer entry"
		var lname := str(entry.get("name", ""))
		var dtype := str(entry.get("dtype", ""))
		var off := int(entry.get("offset", -1))
		var n := int(entry.get("bytes", -1))
		var elem := 1 if dtype == "u8" else (4 if dtype == "i32" else 0)
		if elem == 0:
			return "layer %s has unknown dtype %s" % [lname, dtype]
		if n != cells * elem or off < 12 + hlen or off + n > bytes.size():
			return "layer %s out of bounds" % lname
		var raw := bytes.slice(off, off + n)
		if elem == 1:
			_u8[lname] = raw
		else:
			_i32[lname] = raw.to_int32_array()

	for required: String in ["terrain", "flags", "river_mask", "river_class"]:
		if not _u8.has(required):
			return "missing layer %s" % required
	terrain = _u8["terrain"]
	flags = _u8["flags"]
	river_mask = _u8["river_mask"]
	river_class = _u8["river_class"]
	landcover = _u8.get("landcover", PackedByteArray())
	biome = _u8.get("biome", PackedByteArray())
	elev_mean = _i32.get("elev_mean", PackedInt32Array())
	country = _i32.get("country", PackedInt32Array())
	admin1 = _i32.get("admin1", PackedInt32Array())
	return ""


func has_layer(lname: String) -> bool:
	return _u8.has(lname) or _i32.has(lname)


func layer_names() -> PackedStringArray:
	var names := PackedStringArray(_u8.keys())
	names.append_array(PackedStringArray(_i32.keys()))
	return names


func u8(lname: String) -> PackedByteArray:
	return _u8.get(lname, PackedByteArray())


func i32(lname: String) -> PackedInt32Array:
	return _i32.get(lname, PackedInt32Array())


func idx(col: int, row: int) -> int:
	return row * width + col


func col_of(i: int) -> int:
	return i % width


func row_of(i: int) -> int:
	return i / width


static func offset_to_axial(col: int, row: int) -> Vector2i:
	return Vector2i(col - ((row - (row & 1)) >> 1), row)


static func axial_to_offset(q: int, r: int) -> Vector2i:
	return Vector2i(q + ((r - (r & 1)) >> 1), r)


## Neighbour of local hex i in direction d, or -1 outside the crop.
## Wraps east-west only when the crop spans the whole world.
func neighbor(i: int, d: int) -> int:
	var a := offset_to_axial(col_of(i) + col0, row_of(i) + row0) + NEIGHBOR_DIRS[d]
	var o := axial_to_offset(a.x, a.y)
	var lr := o.y - row0
	if lr < 0 or lr >= height:
		return -1
	var lc := o.x - col0
	if width == global_width:
		lc = posmod(lc, global_width)
	if lc < 0 or lc >= width:
		return -1
	return lr * width + lc


## Flat (cells * 6) neighbour table, same shape as HexGrid._neighbors_flat.
## Slow for whole-world files (millions of hexes): call neighbor() on demand there.
func build_neighbors_flat() -> PackedInt32Array:
	var out := PackedInt32Array()
	out.resize(cells * 6)
	for i in cells:
		for d in 6:
			out[i * 6 + d] = neighbor(i, d)
	return out


func is_land(i: int) -> bool:
	return (flags[i] & FLAG_LAND) != 0


## True if a river links hex i to its neighbour in direction d (boats may move).
func river_connects(i: int, d: int) -> bool:
	return (river_mask[i] & (1 << d)) != 0


## Centre of hex i in degrees (x = lon, y = lat). Vector2 is float32, so
## precision is ~1e-6 deg (~0.1 m), plenty for gameplay.
func center_lonlat(i: int) -> Vector2:
	var grid: Dictionary = header["grid"]
	var row := row_of(i) + row0
	var col := col_of(i) + col0
	var lon := float(grid["lon0"]) + (col + 0.5 * (row & 1)) * float(grid["dx_deg"])
	lon = fposmod(lon + 180.0, 360.0) - 180.0
	return Vector2(lon, float(grid["lat_top"]) - row * float(grid["dy_deg"]))


## RGBA8 data image, one texel per hex at (col, row):
## R = terrain, G = flags, B = river_mask, A = river_class. Use texelFetch with
## col = q + ((r - (r & 1)) >> 1) in the map shader.
func make_data_image() -> Image:
	var buf := PackedByteArray()
	buf.resize(cells * 4)
	for i in cells:
		var o := i * 4
		buf[o] = terrain[i]
		buf[o + 1] = flags[i]
		buf[o + 2] = river_mask[i]
		buf[o + 3] = river_class[i]
	return Image.create_from_data(width, height, false, Image.FORMAT_RGBA8, buf)


## One u8 layer as an R8 image (width x height, texel = hex). No per-hex loop,
## so it is fast even for whole-world files. Empty Image if the layer is missing.
func make_layer_image(lname: String) -> Image:
	if not _u8.has(lname):
		return Image.new()
	return Image.create_from_data(width, height, false, Image.FORMAT_R8, _u8[lname])

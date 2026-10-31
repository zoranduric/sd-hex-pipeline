extends SceneTree
## Headless check that HexMapData reads a .sdhx exactly like the Python reader.
## godot --headless --path <project> --script res://check_loader.gd -- <file.sdhx> <expected.json>


func _init() -> void:
	var args := OS.get_cmdline_user_args()
	if args.size() != 2:
		printerr("usage: -- <file.sdhx> <expected.json>")
		quit(2)
		return
	var data := HexMapData.load_file(args[0])
	var expected: Dictionary = JSON.parse_string(FileAccess.get_file_as_string(args[1]))
	var failures := PackedStringArray()

	if data == null:
		printerr("load failed")
		quit(1)
		return
	if data.width != int(expected["width"]) or data.height != int(expected["height"]):
		failures.append("dimensions")
	for lname: String in expected["sums"]:
		var total := 0
		if lname in ["elev_mean", "elev_min", "elev_max", "elev_std", "country", "admin1"]:
			for v in data.i32(lname):
				total += v
		else:
			for v in data.u8(lname):
				total += v
		if total != int(expected["sums"][lname]):
			failures.append("sum %s: %d != %d" % [lname, total, int(expected["sums"][lname])])
	var nbr := data.build_neighbors_flat()
	var exp_nbr: Array = expected["neighbors_flat"]
	for i in nbr.size():
		if nbr[i] != int(exp_nbr[i]):
			failures.append("neighbor %d: %d != %d" % [i, nbr[i], int(exp_nbr[i])])
			break
	for probe: Dictionary in expected["centers"]:
		var c := data.center_lonlat(int(probe["i"]))
		# Vector2 is float32: ~1e-6 deg (~0.1 m) precision is expected.
		if absf(c.x - float(probe["lon"])) > 1e-5 or absf(c.y - float(probe["lat"])) > 1e-5:
			failures.append("center %d: (%.12f, %.12f) vs (%.12f, %.12f)" % [
				int(probe["i"]), c.x, c.y, float(probe["lon"]), float(probe["lat"])])
	var img := data.make_data_image()
	if img.get_width() != data.width or img.get_height() != data.height:
		failures.append("image size")
	var bimg := data.make_layer_image("biome")
	if data.biome.size() != data.cells or bimg.get_width() != data.width or bimg.get_height() != data.height:
		failures.append("biome layer/image")
	elif bimg.get_pixel(1, 0).r8 != data.biome[1]:
		failures.append("biome texel")
	if not data.make_layer_image("nope").is_empty():
		failures.append("missing layer image")
	var bad := HexMapData.new()
	if bad._parse(PackedByteArray([1, 2, 3])) == "":
		failures.append("garbage accepted")

	if failures.is_empty():
		print("LOADER_OK cells=%d layers=%d" % [data.cells, data.layer_names().size()])
		quit(0)
	else:
		printerr("LOADER_FAIL ", ", ".join(failures))
		quit(1)

import sys
import argparse
from io import BytesIO
from pathlib import Path
from PIL import Image

# Enable HEIC support (requires pillow-heif already installed)
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except Exception as e:
    print("HEIC support not available. Ensure pillow-heif is installed. Error:", e)
    sys.exit(1)

def safe_out_path(out_dir: Path, base_name: str) -> Path:
    """Create a unique output path that never overwrites an existing file."""
    p = out_dir / f"{base_name}.jpg"
    if not p.exists():
        return p
    i = 1
    while True:
        candidate = out_dir / f"{base_name}_{i}.jpg"
        if not candidate.exists():
            return candidate
        i += 1

def encode_to_bytes(im: Image.Image, quality: int, subsampling: int, progressive: bool,
                    optimize: bool, exif_bytes: bytes | None) -> bytes:
    buf = BytesIO()
    save_kwargs = dict(
        format="JPEG",
        quality=quality,
        subsampling=subsampling,  # 2 = 4:2:0 (smallest)
        progressive=progressive,
        optimize=optimize,
    )
    if exif_bytes:
        save_kwargs["exif"] = exif_bytes
    im.save(buf, **save_kwargs)
    return buf.getvalue()

def downscale(im: Image.Image, max_side: int) -> Image.Image:
    w, h = im.size
    m = max(w, h)
    if m <= max_side:
        return im
    scale = max_side / float(m)
    new_size = (max(1, int(w * scale)), max(1, int(h * scale)))
    return im.resize(new_size, Image.LANCZOS)

def compress_to_target(
    im: Image.Image,
    target_bytes: int,
    keep_exif: bool,
    initial_quality: int = 85,
    min_quality: int = 40,
    subsampling: int = 2,
    progressive: bool = True,
    optimize: bool = True,
    start_max_side: int | None = 3000,
    min_max_side: int = 1200,
) -> bytes:
    """
    Try to produce a JPEG <= target_bytes by stepping quality down,
    then (if needed) stepping down resolution.
    """
    # Prepare EXIF if requested
    exif_bytes = None
    if keep_exif:
        try:
            exif_bytes = im.info.get("exif")
        except Exception:
            exif_bytes = None

    # Ensure RGB
    im = im.convert("RGB")

    # Resolution ladder (if needed)
    side_steps = []
    if start_max_side:
        # Generate a descending ladder (e.g., 3000, 2700, 2400, ..., >= min_max_side)
        s = start_max_side
        while s >= min_max_side:
            side_steps.append(s)
            s = int(s * 0.9)  # reduce by 10% per step
    else:
        side_steps = [None]  # no resizing

    # Quality ladder
    quality_steps = list(range(initial_quality, min_quality - 1, -5))  # 85,80,...,40

    for side in side_steps:
        # Optionally resize for this pass
        im_resized = downscale(im, side) if side else im
        for q in quality_steps:
            data = encode_to_bytes(
                im_resized, quality=q, subsampling=subsampling,
                progressive=progressive, optimize=optimize, exif_bytes=exif_bytes
            )
            if len(data) <= target_bytes:
                return data

    # If all attempts failed, return the smallest we got (last attempt)
    return data

def convert_heic_file(
    file_path: Path,
    target_mb: float,
    keep_exif: bool,
    start_max_side: int | None,
    avoid_overwrite: bool = True,  # kept for signature clarity; we ALWAYS avoid overwrites here
) -> bool:
    """Convert a single .heic file to a size-targeted JPG in sibling 'Reduced' folder."""
    if not file_path.is_file() or file_path.suffix.lower() != ".heic":
        return False

    out_dir = file_path.parent / "Reduced"
    out_dir.mkdir(exist_ok=True)
    out_path = safe_out_path(out_dir, file_path.stem)

    try:
        with Image.open(file_path) as im:
            target_bytes = int(max(0.1, target_mb) * 1024 * 1024)  # clamp min to 0.1 MB
            jpeg_bytes = compress_to_target(
                im,
                target_bytes=target_bytes,
                keep_exif=keep_exif,
                initial_quality=85,
                min_quality=40,
                subsampling=2,
                progressive=True,
                optimize=True,
                start_max_side=start_max_side,
                min_max_side=1200,
            )

        # Write final bytes to the unique path
        out_path.write_bytes(jpeg_bytes)
        size_mb = len(jpeg_bytes) / (1024 * 1024)
        print(f"Converted: {file_path.name} -> {out_path.relative_to(file_path.parent)} ({size_mb:.2f} MB)")
        return True

    except Exception as e:
        print(f"Failed: {file_path.name} | {e}")
        return False

def convert_dir(
    dir_path: Path,
    target_mb: float,
    keep_exif: bool,
    start_max_side: int | None,
) -> int:
    """Convert all .heic files in a directory (non-recursive)."""
    if not dir_path.exists() or not dir_path.is_dir():
        print(f"Input path is not a directory: {dir_path}")
        return 0

    count = 0
    for entry in dir_path.iterdir():
        if entry.is_file() and entry.suffix.lower() == ".heic":
            if convert_heic_file(entry, target_mb, keep_exif, start_max_side):
                count += 1
    if count == 0:
        print(f"No .heic files found in: {dir_path}")
    else:
        print(f"Done. Converted {count} file(s) in: {dir_path}")
    return count

def parse_args(argv):
    p = argparse.ArgumentParser(
        description="Convert HEIC to JPG without overwriting, meeting a target size."
    )
    p.add_argument("paths", nargs="*", type=Path,
                   help="One or more .HEIC files and/or directories (non-recursive). No args => current folder.")
    p.add_argument("--target-mb", type=float, default=0.5,
                   help="Target maximum file size in megabytes (default 1.0).")
    p.add_argument("--max-side", type=int, default=3000,
                   help="Start downscaling so max(width,height) ≤ this value if needed (default 3000). Use 0 to disable resizing.")
    p.add_argument("--keep-exif", action="store_true",
                   help="Keep EXIF metadata (larger files). Default is to strip EXIF.")
    return p.parse_args(argv)

def main():
    args = parse_args(sys.argv[1:])
    start_max_side = None if args.max_side and args.max_side <= 0 else args.max_side

    if not args.paths:
        convert_dir(Path.cwd(), target_mb=args.target_mb, keep_exif=args.keep_exif, start_max_side=start_max_side)
        return

    total = 0
    files = [p for p in args.paths if p.is_file()]
    dirs  = [p for p in args.paths if p.is_dir()]
    missing = [p for p in args.paths if not p.exists()]
    for m in missing:
        print(f"Path does not exist: {m}")

    for f in files:
        total += int(convert_heic_file(f, target_mb=args.target_mb, keep_exif=args.keep_exif, start_max_side=start_max_side))

    for d in dirs:
        total += convert_dir(d, target_mb=args.target_mb, keep_exif=args.keep_exif, start_max_side=start_max_side)

    if total > 0:
        print(f"\nAll done. Total converted: {total}")
    else:
        print("\nNothing converted.")

if __name__ == "__main__":
    main()

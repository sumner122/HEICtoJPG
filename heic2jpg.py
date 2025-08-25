import sys
import os
from io import BytesIO
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from PIL import Image

# Enable HEIC support
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except Exception as e:
    print("HEIC support not available. Ensure pillow-heif is installed. Error:", e)
    sys.exit(1)

# --------- Config (hardcoded) ---------
TARGET_MB = 0.5
MAX_SIDE = 2000
KEEP_EXIF = False   # default: strip EXIF
PROGRESSIVE = True
OPTIMIZE = True
# worker count = CPU cores - 2
CPU_COUNT = os.cpu_count() or 4
WORKERS = max(1, CPU_COUNT - 2)

# ---------- Thread-safe printing & progress ----------
_print_lock = Lock()
def tprint(*args, **kwargs):
    with _print_lock:
        print(*args, **kwargs)

def render_progress(completed: int, total: int, width: int = 30):
    pct = 0 if total == 0 else int((completed / total) * 100)
    filled = int(width * pct / 100)
    bar = "#" * filled + "-" * (width - filled)
    with _print_lock:
        print(f"[{bar}] {pct:3d}%  ({completed}/{total})", end="\r", flush=True)

# ---------- Image utils ----------
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

def encode_to_bytes(im: Image.Image, quality: int,
                    subsampling: int, progressive: bool, optimize: bool,
                    exif_bytes: bytes | None) -> bytes:
    buf = BytesIO()
    save_kwargs = dict(
        format="JPEG",
        quality=int(quality),
        subsampling=subsampling,
        progressive=progressive,
        optimize=optimize,
    )
    if exif_bytes:
        save_kwargs["exif"] = exif_bytes
    im.save(buf, **save_kwargs)
    return buf.getvalue()

def downscale_to_max_side(im: Image.Image, max_side: int) -> Image.Image:
    w, h = im.size
    m = max(w, h)
    if m <= max_side:
        return im
    scale = max_side / float(m)
    new_size = (max(1, int(w * scale)), max(1, int(h * scale)))
    return im.resize(new_size, Image.LANCZOS)

# ---------- Fast targetting ----------
def jpeg_under_size(
    im: Image.Image,
    target_bytes: int,
    keep_exif: bool,
    subsampling: int,
    progressive: bool,
    optimize: bool,
    q_lo: int = 40,
    q_hi: int = 90,
    max_iters: int = 7,
) -> bytes | None:
    """Binary search on JPEG quality to fit <= target_bytes."""
    exif_bytes = im.info.get("exif", None) if keep_exif else None
    im_rgb = im.convert("RGB")

    best = None
    lo, hi = q_lo, q_hi
    for _ in range(max_iters):
        mid = (lo + hi) // 2
        data = encode_to_bytes(im_rgb, mid, subsampling, progressive, optimize, exif_bytes)
        if len(data) <= target_bytes:
            best = data
            lo = mid + 1
        else:
            hi = mid - 1
        if lo > hi:
            break
    return best

def compress_to_target_fast(im: Image.Image, target_bytes: int, keep_exif: bool) -> bytes:
    # Step 1: resize down to MAX_SIDE immediately
    im_try = downscale_to_max_side(im, MAX_SIDE)

    # Step 2: binary search quality
    data = jpeg_under_size(im_try, target_bytes, keep_exif,
                           subsampling=2, progressive=PROGRESSIVE, optimize=OPTIMIZE)
    if data is not None:
        return data

    # Step 3: fallback lowest quality
    exif_bytes = im_try.info.get("exif", None) if keep_exif else None
    return encode_to_bytes(im_try, 40, 2, PROGRESSIVE, OPTIMIZE, exif_bytes)

# ---------- Worker ----------
def convert_heic_file(file_path: Path) -> tuple[bool, str]:
    try:
        if not file_path.is_file() or file_path.suffix.lower() != ".heic":
            return False, f"Skip: {file_path}"

        out_dir = file_path.parent / "Reduced"
        out_dir.mkdir(exist_ok=True)
        out_path = safe_out_path(out_dir, file_path.stem)

        with Image.open(file_path) as im:
            target_bytes = int(max(0.1, TARGET_MB) * 1024 * 1024)
            jpeg_bytes = compress_to_target_fast(im, target_bytes, KEEP_EXIF)

        out_path.write_bytes(jpeg_bytes)
        size_mb = len(jpeg_bytes) / (1024 * 1024)
        return True, f"Converted: {file_path.name} -> {out_path.relative_to(file_path.parent)} ({size_mb:.2f} MB)"
    except Exception as e:
        return False, f"Failed: {file_path} | {e}"

# ---------- Discovery ----------
def gather_targets(paths):
    uniq, seen = [], set()
    for p in paths:
        rp = Path(p).resolve()
        if rp in seen:
            continue
        seen.add(rp)
        uniq.append(rp)
    files = [p for p in uniq if p.is_file()]
    dirs  = [p for p in uniq if p.is_dir()]
    missing = [p for p in uniq if not p.exists()]
    return files, dirs, missing

def list_heic_in_dir(d: Path):
    return [p for p in d.iterdir() if p.is_file() and p.suffix.lower() == ".heic"]

# ---------- Main ----------
def main():
    args = sys.argv[1:]
    if not args:
        files = list_heic_in_dir(Path.cwd())
        dirs, missing = [], []
    else:
        files, dirs, missing = gather_targets(args)

    for m in missing:
        tprint(f"Path does not exist: {m}")

    for d in dirs:
        files.extend(list_heic_in_dir(d))

    # Deduplicate
    file_set, seen = [], set()
    for f in files:
        rf = f.resolve()
        if rf.suffix.lower() != ".heic":
            continue
        if rf not in seen:
            seen.add(rf)
            file_set.append(rf)

    total = len(file_set)
    if total == 0:
        tprint("Nothing to convert.")
        return

    tprint(f"Converting {total} file(s) with {WORKERS} worker(s)...")
    completed = 0
    render_progress(0, total)

    total_ok = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(convert_heic_file, f): f for f in file_set}
        for fut in as_completed(futs):
            ok, msg = fut.result()
            tprint("\n" + msg)
            if ok:
                total_ok += 1
            completed += 1
            render_progress(completed, total)

    tprint("\n")
    tprint(f"All done. Total converted: {total_ok} / {total}")

if __name__ == "__main__":
    main()

from collections import deque

from PIL import Image, ImageChops, ImageDraw, ImageOps
from io import BytesIO
from pathlib import Path
import logging
import math
import random
import time
from urllib.parse import urlparse

import requests

logger = logging.getLogger("metapreview")

GRID_COLUMNS = 10  # posters horizontally
GRID_ROWS = 4  # posters vertically
POSTER_RATIO = (1, 1.5)  # width : height
OUTPUT_SIZE = (1293, 945)  # final image size
GRID_OFFSET_X = -80  # shift grid in its own plane (pixels), 0 = default position
GRID_OFFSET_Y = 580
Z_ROTATION = -34  # diagonal slant of the whole grid (degrees)
VIEW_TILT_X = 20  # camera perspective: tilt of the view plane (degrees), 0 = no depth
VIEW_TILT_Y = 35  # camera perspective: horizontal tilt of the view plane (degrees)
REFERENCE_VIEWPORT_DIAG = 1500.0
REFERENCE_FOCAL = 1200.0
REFERENCE_CAMERA_DISTANCE = 1800.0
PERSPECTIVE_DISTANCE_SCALE = 1.35  # higher = camera further away, less fisheye
OUTPUT_MARGIN = -0.5  # lower = closer zoom, perspective angles stay the same
CARD_GAP = 12
BORDER_WIDTH = 2
BORDER_COLOR = (210, 210, 210)
CORNER_RADIUS = 12
POSTER_MIN_CHEBYSHEV_DISTANCE = 2  # same poster not within N cells (Chebyshev)
POSTER_RECENT_WINDOW = 6  # penalize posters picked in the last N placements
POSTER_SOURCE = "local"  # "local" | "network"
LOG_LEVEL = "INFO"  # DEBUG | INFO | WARNING | ERROR
DEV_POSTERS_DIR = Path(__file__).parent / "dev_posters"
OPACITY_MASK_PATH = Path(__file__).parent / "opacity_mask.png"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.0.0 Safari/537.36"
)


def request_headers(url):
    parsed = urlparse(url)
    return {
        "User-Agent": BROWSER_USER_AGENT,
        "Host": parsed.netloc,
    }


def setup_logging(level=LOG_LEVEL):
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )


def load_image_from_url(url):
    start = time.perf_counter()
    logger.info("IMG request GET %s", url)
    try:
        resp = requests.get(url, headers=request_headers(url), timeout=5)
        elapsed_ms = (time.perf_counter() - start) * 1000
        content_length = len(resp.content)
        logger.info(
            "IMG response %s | status=%s | size=%s bytes | %.0f ms",
            url,
            resp.status_code,
            content_length,
            elapsed_ms,
        )
        resp.raise_for_status()
        img = Image.open(BytesIO(resp.content)).convert("RGB")
        logger.info(
            "IMG loaded %s | mode=RGB | size=%sx%s | %.0f ms total",
            url,
            img.width,
            img.height,
            (time.perf_counter() - start) * 1000,
        )
        return img
    except Exception:
        logger.exception(
            "IMG failed %s | %.0f ms",
            url,
            (time.perf_counter() - start) * 1000,
        )
        return None


def load_image_from_file(path):
    path = Path(path)
    logger.info("IMG load file %s", path)
    try:
        img = Image.open(path).convert("RGB")
        logger.info(
            "IMG loaded file %s | mode=RGB | size=%sx%s",
            path,
            img.width,
            img.height,
        )
        return img
    except Exception:
        logger.exception("IMG failed file %s", path)
        return None


def list_local_posters(directory=DEV_POSTERS_DIR):
    folder = Path(directory)
    if not folder.is_dir():
        raise FileNotFoundError(f"Poster folder not found: {folder}")

    files = [
        path
        for path in folder.iterdir()
        if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
    ]
    if not files:
        raise ValueError(f"No image files in {folder}")
    logger.info("Found %s local posters in %s", len(files), folder)
    return files


def resize_poster(img, target_w, target_h):
    return img.resize((target_w, target_h), Image.Resampling.LANCZOS)


def load_poster_pool(sources, poster_source=POSTER_SOURCE):
    pool = []

    if poster_source == "local":
        logger.info("Preloading %s local posters", len(sources))
        for path in sources:
            img = load_image_from_file(path)
            if img is not None:
                pool.append(img)
    else:
        unique_sources = list(dict.fromkeys(sources))
        logger.info("Preloading %s network posters (%s unique urls)", len(sources), len(unique_sources))
        for url in unique_sources:
            img = load_image_from_url(url)
            if img is not None:
                pool.append(img)

    if not pool:
        raise ValueError("No posters could be loaded")

    logger.info("Poster pool ready: %s images", len(pool))
    return pool


def _chebyshev_distance(row_a, col_a, row_b, col_b):
    return max(abs(row_a - row_b), abs(col_a - col_b))


def _preferred_poster_spacing(pool_size):
    if pool_size <= 1:
        return 0
    if pool_size <= 3:
        return 1
    return POSTER_MIN_CHEBYSHEV_DISTANCE


def _forbidden_neighbor_indices(assignments, row, col, min_distance):
    forbidden = set()
    for r, row_assignments in enumerate(assignments):
        for c, poster_idx in enumerate(row_assignments):
            if poster_idx is None:
                continue
            dist = _chebyshev_distance(row, col, r, c)
            if 0 < dist <= min_distance:
                forbidden.add(poster_idx)
    return forbidden


def _neighbor_usage_counts(assignments, row, col, min_distance):
    counts: dict[int, int] = {}
    for r, row_assignments in enumerate(assignments):
        for c, poster_idx in enumerate(row_assignments):
            if poster_idx is None:
                continue
            dist = _chebyshev_distance(row, col, r, c)
            if dist == 0 or dist > min_distance:
                continue
            weight = min_distance + 1 - dist
            counts[poster_idx] = counts.get(poster_idx, 0) + weight
    return counts


def _pick_least_used(candidates, usage_counts, pool_size):
    min_usage = min(usage_counts.get(i, 0) for i in candidates)
    tied = [i for i in candidates if usage_counts.get(i, 0) == min_usage]
    return random.choice(tied)


def assign_poster_grid_indices(
        pool_size,
        grid_columns,
        grid_rows,
        min_distance=None,
):
    if pool_size <= 0:
        raise ValueError("pool_size must be positive")

    if min_distance is None:
        min_distance = _preferred_poster_spacing(pool_size)

    if min_distance <= 0 or pool_size == 1:
        return [
            [random.randrange(pool_size) for _ in range(grid_columns)]
            for _ in range(grid_rows)
        ]

    assignments: list[list[int | None]] = [
        [None] * grid_columns for _ in range(grid_rows)
    ]
    cells = [(row, col) for row in range(grid_rows) for col in range(grid_columns)]
    random.shuffle(cells)
    recent: deque[int] = deque(maxlen=POSTER_RECENT_WINDOW)

    for row, col in cells:
        forbidden = _forbidden_neighbor_indices(assignments, row, col, min_distance)
        candidates = [i for i in range(pool_size) if i not in forbidden]

        if candidates:
            fresh = [i for i in candidates if i not in recent]
            pick = random.choice(fresh or candidates)
        else:
            usage = _neighbor_usage_counts(assignments, row, col, min_distance)
            for idx in recent:
                usage[idx] = usage.get(idx, 0) + 2
            pick = _pick_least_used(list(range(pool_size)), usage, pool_size)

        assignments[row][col] = pick
        recent.append(pick)

    return [[int(cell) for cell in row] for row in assignments]


def create_rounded_mask(size, radius):
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, size[0] - 1, size[1] - 1),
        radius=radius,
        fill=255,
    )
    return mask


def create_poster_card(
        poster_img,
        border_width=BORDER_WIDTH,
        border_color=BORDER_COLOR,
        corner_radius=CORNER_RADIUS,
):
    poster_w, poster_h = poster_img.size
    card_w = poster_w + border_width * 2
    card_h = poster_h + border_width * 2

    card = Image.new("RGBA", (card_w, card_h), (0, 0, 0, 0))

    border_layer = Image.new("RGBA", (card_w, card_h), (0, 0, 0, 0))
    ImageDraw.Draw(border_layer).rounded_rectangle(
        (0, 0, card_w - 1, card_h - 1),
        radius=corner_radius,
        fill=(*border_color, 255),
    )
    card = Image.alpha_composite(card, border_layer)

    poster_rgba = poster_img.convert("RGBA")
    inner_radius = max(0, corner_radius - border_width)
    poster_rgba.putalpha(create_rounded_mask((poster_w, poster_h), inner_radius))
    card.paste(poster_rgba, (border_width, border_width), poster_rgba)

    return card


def paste_card(canvas, card, x, y, cell_w, cell_h):
    offset_x = x + (cell_w - card.width) // 2
    offset_y = y + (cell_h - card.height) // 2
    canvas.paste(card, (offset_x, offset_y), card)


def deg_to_rad(value):
    return value * math.pi / 180.0


def rotation_matrix_x(angle):
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    return (
        (1.0, 0.0, 0.0),
        (0.0, cos_a, -sin_a),
        (0.0, sin_a, cos_a),
    )


def rotation_matrix_y(angle):
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    return (
        (cos_a, 0.0, sin_a),
        (0.0, 1.0, 0.0),
        (-sin_a, 0.0, cos_a),
    )


def rotation_matrix_z(angle):
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    return (
        (cos_a, -sin_a, 0.0),
        (sin_a, cos_a, 0.0),
        (0.0, 0.0, 1.0),
    )


def mat_mult(a, b):
    return tuple(
        tuple(sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3))
        for i in range(3)
    )


def view_rotation_matrix(view_tilt_x, view_tilt_y, z_rotation):
    rx = rotation_matrix_x(deg_to_rad(view_tilt_x))
    ry = rotation_matrix_y(deg_to_rad(view_tilt_y))
    rz = rotation_matrix_z(deg_to_rad(z_rotation))
    return mat_mult(rz, mat_mult(ry, rx))


def apply_rotation(matrix, point):
    x, y, z = point
    return (
        matrix[0][0] * x + matrix[0][1] * y + matrix[0][2] * z,
        matrix[1][0] * x + matrix[1][1] * y + matrix[1][2] * z,
        matrix[2][0] * x + matrix[2][1] * y + matrix[2][2] * z,
    )


def project_point(x, y, z, focal, camera_distance):
    depth = camera_distance - z
    if depth <= 1:
        raise ValueError("Grid plane is behind the camera")
    return x * focal / depth, y * focal / depth


def compute_viewport_size(grid_w, grid_h, output_size):
    out_w, out_h = output_size
    aspect = out_w / out_h
    grid_aspect = grid_w / grid_h if grid_h else aspect

    if grid_aspect >= aspect:
        viewport_w = grid_w
        viewport_h = grid_w / aspect
    else:
        viewport_h = grid_h
        viewport_w = grid_h * aspect

    return viewport_w, viewport_h


def compute_camera_params(viewport_w, viewport_h, view_tilt_x, view_tilt_y):
    diag = math.hypot(viewport_w, viewport_h)
    scale = (diag / REFERENCE_VIEWPORT_DIAG) * PERSPECTIVE_DISTANCE_SCALE
    max_tilt_rad = deg_to_rad(max(abs(view_tilt_x), abs(view_tilt_y)))
    tilt_factor = 1.0 + math.sin(max_tilt_rad) * 1.5

    camera_distance = REFERENCE_CAMERA_DISTANCE * scale * tilt_factor
    focal = REFERENCE_FOCAL * scale * tilt_factor
    return focal, camera_distance


def fit_points_to_output(points, output_size, margin=0.0):
    width, height = output_size
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]

    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    span_x = max(max_x - min_x, 1.0)
    span_y = max(max_y - min_y, 1.0)

    available_w = width * (1.0 - 2.0 * margin)
    available_h = height * (1.0 - 2.0 * margin)
    scale = min(available_w / span_x, available_h / span_y)

    center_x = (min_x + max_x) / 2.0
    center_y = (min_y + max_y) / 2.0
    out_cx = width / 2.0
    out_cy = height / 2.0

    fitted = []
    for x, y in points:
        fitted.append(
            (
                (x - center_x) * scale + out_cx,
                (y - center_y) * scale + out_cy,
            )
        )
    return fitted


def apply_output_zoom(image, output_size, margin):
    if margin >= 0:
        return image

    zoom = 1.0 / (1.0 + margin)
    width, height = output_size
    crop_w = max(int(width / zoom), 1)
    crop_h = max(int(height / zoom), 1)
    left = (width - crop_w) // 2
    top = (height - crop_h) // 2
    cropped = image.crop((left, top, left + crop_w, top + crop_h))
    return cropped.resize(output_size, Image.Resampling.LANCZOS)


def perspective_coefficients(dest_points, source_points):
    matrix = []
    vector = []
    for (x_out, y_out), (x_in, y_in) in zip(dest_points, source_points):
        matrix.append([x_out, y_out, 1, 0, 0, 0, -x_in * x_out, -x_in * y_out])
        matrix.append([0, 0, 0, x_out, y_out, 1, -y_in * x_out, -y_in * y_out])
        vector.extend([x_in, y_in])

    size = 8
    for i in range(size):
        pivot = max(range(i, size), key=lambda row: abs(matrix[row][i]))
        if abs(matrix[pivot][i]) < 1e-12:
            raise ValueError("Failed to compute perspective transform")
        if pivot != i:
            matrix[i], matrix[pivot] = matrix[pivot], matrix[i]
            vector[i], vector[pivot] = vector[pivot], vector[i]

        divisor = matrix[i][i]
        matrix[i] = [value / divisor for value in matrix[i]]
        vector[i] /= divisor

        for row in range(size):
            if row == i:
                continue
            factor = matrix[row][i]
            matrix[row] = [
                matrix[row][col] - factor * matrix[i][col] for col in range(size)
            ]
            vector[row] -= factor * vector[i]

    return tuple(vector)


def apply_view_perspective(
        grid,
        crop_box,
        output_size,
        z_rotation=Z_ROTATION,
        view_tilt_x=VIEW_TILT_X,
        view_tilt_y=VIEW_TILT_Y,
        output_margin=OUTPUT_MARGIN,
):
    left, top, right, bottom = crop_box
    viewport_w = right - left
    viewport_h = bottom - top
    focal, camera_distance = compute_camera_params(
        viewport_w,
        viewport_h,
        view_tilt_x,
        view_tilt_y,
    )
    logger.info(
        "Perspective viewport=%.0fx%.0f focal=%.1f camera=%.1f tilt=(%.1f, %.1f, %.1f)",
        viewport_w,
        viewport_h,
        focal,
        camera_distance,
        view_tilt_x,
        view_tilt_y,
        z_rotation,
    )

    grid_cx = (left + right) / 2.0
    grid_cy = (top + bottom) / 2.0
    source_corners = [
        (left, top),
        (right, top),
        (right, bottom),
        (left, bottom),
    ]

    rotation = view_rotation_matrix(view_tilt_x, view_tilt_y, z_rotation)
    projected = []
    for x, y in source_corners:
        local_x = x - grid_cx
        local_y = y - grid_cy
        rotated = apply_rotation(rotation, (local_x, local_y, 0.0))
        projected.append(project_point(*rotated, focal, camera_distance))

    perspective_margin = max(output_margin, 0.0)
    dest_corners = fit_points_to_output(projected, output_size, margin=perspective_margin)
    coeffs = perspective_coefficients(dest_corners, source_corners)
    result = grid.transform(
        output_size,
        Image.Transform.PERSPECTIVE,
        coeffs,
        Image.Resampling.BICUBIC,
    )
    return apply_output_zoom(result, output_size, output_margin)


def apply_view_perspective_supersampled(
        grid,
        crop_box,
        output_size,
        supersample=2,
        **kwargs,
):
    if supersample <= 1:
        return apply_view_perspective(grid, crop_box, output_size, **kwargs)

    hi_w = output_size[0] * supersample
    hi_h = output_size[1] * supersample
    hi_res = apply_view_perspective(
        grid,
        crop_box,
        (hi_w, hi_h),
        **kwargs,
    )
    return hi_res.resize(output_size, Image.Resampling.LANCZOS)


def load_opacity_mask(mask_path, target_size):
    mask_img = Image.open(mask_path)
    if mask_img.mode == "RGBA":
        rgb_luma = mask_img.convert("RGB").convert("L")
        alpha = mask_img.getchannel("A")
        luma_min, luma_max = rgb_luma.getextrema()
        alpha_min, alpha_max = alpha.getextrema()
        luma_range = luma_max - luma_min
        alpha_range = alpha_max - alpha_min
        mask = alpha if alpha_range > luma_range else rgb_luma
    elif mask_img.mode == "LA":
        mask = mask_img.getchannel("A")
    else:
        mask = mask_img.convert("L")

    if mask.size != target_size:
        logger.info(
            "Opacity mask resize %sx%s -> %sx%s",
            mask.width,
            mask.height,
            target_size[0],
            target_size[1],
        )
        mask = mask.resize(target_size, Image.Resampling.LANCZOS)
    mask = ImageOps.invert(mask)
    return mask


def apply_opacity_mask(image, mask_path=OPACITY_MASK_PATH, output_size=None):
    mask_path = Path(mask_path)
    if not mask_path.is_file():
        raise FileNotFoundError(f"Opacity mask not found: {mask_path}")

    target_size = output_size or image.size
    result = image.convert("RGBA")
    mask = load_opacity_mask(mask_path, target_size)

    # white = transparent, black = opaque
    opacity = ImageOps.invert(mask)
    existing_alpha = result.getchannel("A")
    result.putalpha(ImageChops.multiply(existing_alpha, opacity))
    logger.info("Applied opacity mask %s", mask_path.name)
    return result


def build_poster_grid(
        poster_pool,
        grid_columns,
        grid_rows,
        poster_w,
        poster_h,
        output_size,
        grid_offset_x=GRID_OFFSET_X,
        grid_offset_y=GRID_OFFSET_Y,
):
    card_w = poster_w + BORDER_WIDTH * 2
    card_h = poster_h + BORDER_WIDTH * 2
    cell_w = card_w + CARD_GAP
    cell_h = card_h + CARD_GAP

    grid_w = grid_columns * cell_w
    grid_h = grid_rows * cell_h
    viewport_w, viewport_h = compute_viewport_size(grid_w, grid_h, output_size)
    padding = (
            int(math.hypot(viewport_w, viewport_h) * 0.25)
            + max(cell_w, cell_h)
            + max(abs(grid_offset_x), abs(grid_offset_y))
    )

    canvas_w = int(viewport_w + padding * 2)
    canvas_h = int(viewport_h + padding * 2)
    grid = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))

    viewport_x = padding
    viewport_y = padding
    content_x = viewport_x + (viewport_w - grid_w) / 2 + grid_offset_x
    content_y = viewport_y + (viewport_h - grid_h) / 2 + grid_offset_y
    total_cards = grid_columns * grid_rows
    logger.info(
        "Building grid %sx%s (%s posters) | viewport=%.0fx%.0f | canvas=%sx%s",
        grid_columns,
        grid_rows,
        total_cards,
        viewport_w,
        viewport_h,
        canvas_w,
        canvas_h,
    )

    poster_indices = assign_poster_grid_indices(
        len(poster_pool),
        grid_columns,
        grid_rows,
    )

    for row in range(grid_rows):
        for col in range(grid_columns):
            card_index = row * grid_columns + col + 1
            logger.debug("Placing poster %s/%s at row=%s col=%s", card_index, total_cards, row, col)
            poster = resize_poster(
                poster_pool[poster_indices[row][col]],
                poster_w,
                poster_h,
            )
            card = create_poster_card(poster)
            paste_card(
                grid,
                card,
                int(content_x + col * cell_w),
                int(content_y + row * cell_h),
                cell_w,
                cell_h,
            )

    crop_box = (
        viewport_x,
        viewport_y,
        viewport_x + viewport_w,
        viewport_y + viewport_h,
    )
    return grid, crop_box


def make_background(
        output_size=OUTPUT_SIZE,
        grid_columns=GRID_COLUMNS,
        grid_rows=GRID_ROWS,
        grid_offset_x=GRID_OFFSET_X,
        grid_offset_y=GRID_OFFSET_Y,
        z_rotation=Z_ROTATION,
        view_tilt_x=VIEW_TILT_X,
        view_tilt_y=VIEW_TILT_Y,
        poster_height=240,
        poster_source=POSTER_SOURCE,
        sources=None,
        image_urls=None,
        dev_posters_dir=DEV_POSTERS_DIR,
        perspective_supersample=1,
):
    if sources is not None:
        resolved_sources = list(sources)
    elif poster_source == "local":
        resolved_sources = list_local_posters(dev_posters_dir)
    elif poster_source == "network":
        if not image_urls:
            raise ValueError("image_urls is required when poster_source='network'")
        resolved_sources = image_urls
    else:
        raise ValueError(f"Unknown poster_source: {poster_source!r}")
    sources = resolved_sources

    logger.info(
        "Render start | source=%s | output=%sx%s | grid=%sx%s | poster_height=%s",
        poster_source,
        output_size[0],
        output_size[1],
        grid_columns,
        grid_rows,
        poster_height,
    )
    if poster_source == "network":
        logger.info("IMG pool size: %s urls", len(sources))

    poster_pool = load_poster_pool(sources, poster_source)

    width, height = output_size
    ratio_w, ratio_h = POSTER_RATIO
    poster_w = int(poster_height * ratio_w / ratio_h)
    poster_h = poster_height

    grid, crop_box = build_poster_grid(
        poster_pool,
        grid_columns,
        grid_rows,
        poster_w,
        poster_h,
        output_size,
        grid_offset_x=grid_offset_x,
        grid_offset_y=grid_offset_y,
    )

    perspective_kwargs = dict(
        z_rotation=z_rotation,
        view_tilt_x=view_tilt_x,
        view_tilt_y=view_tilt_y,
    )
    if perspective_supersample > 1:
        return apply_view_perspective_supersampled(
            grid,
            crop_box,
            output_size,
            supersample=perspective_supersample,
            **perspective_kwargs,
        )
    return apply_view_perspective(
        grid,
        crop_box,
        output_size,
        **perspective_kwargs,
    )


NETWORK_IMAGE_URLS = [
    "https://shikimori.io/uploads/poster/animes/52991/dc841cc9fce2aa1e9907a4b61c5d1d92.jpeg",
    "https://shikimori.io/uploads/poster/animes/43608/1a267ded89ab0ec3e0d88652c5498016.jpeg",
    "https://shikimori.io/uploads/poster/animes/28851/a98ffda9b7409d610aae10147d35658a.jpeg",
    "https://shikimori.io/uploads/poster/animes/61316/79e6a0c31589176e3244c6117dab2500.jpeg",
    "https://shikimori.io/uploads/poster/animes/32281/ccb50552557ea3085135887df337623a.jpeg",
    "https://shikimori.io/uploads/poster/animes/54492/283fbef180e72bacfadc5a3d64ca4c2e.jpeg",
    "https://shikimori.io/uploads/poster/animes/37987/56162d8986e2eb203031bb25c821b5c7.jpeg",
    "https://shikimori.io/uploads/poster/animes/199/8fffb098b0dfa011cd8431d46425d989.jpeg",
    "https://shikimori.io/uploads/poster/animes/60489/5d1461c507ab9a29f1d37b952793a459.jpeg",
    "https://shikimori.io/uploads/poster/animes/47917/35b343a77242f2ab85e5fe10905c7e4b.jpeg"
]

if __name__ == "__main__":
    if POSTER_SOURCE == "local":
        setup_logging()
        logger.info("MetaPreview started | poster_source=local")
        img = make_background()
    else:
        setup_logging()
        logger.info("MetaPreview started | poster_source=network")
        img = make_background(poster_source="network", image_urls=NETWORK_IMAGE_URLS)

    output_path = Path("result.png")
    img = apply_opacity_mask(img, output_size=OUTPUT_SIZE)
    img.save(output_path)
    logger.info("Saved result -> %s | size=%sx%s", output_path.resolve(), img.width, img.height)

"""Chunk math on the SLGA national grid.

Unlike the Copernicus DEM (whose lattice is knowable a priori), the
SLGA grid is taken from the source COGs themselves: each layer's
affine transform and shape are read once at first contact and stored
with the layer, and every function here is parameterised by them.
Chunks are 1200 x 1200 source pixels (1° at 3 arc-seconds); a chunk is
fetched with a single integer-aligned windowed read — no resampling,
ever.

A *transform* here is the 4-tuple ``(x0, y_top, xres, yres)`` — the
grid's top-left corner and positive pixel sizes in degrees (north-up
rasters only, which SLGA is).

All functions are pure — no I/O, no store access.
"""

CHUNK = 1200


def window_for_bbox(bbox: list[float], transform: tuple, shape: tuple) -> tuple[int, int, int, int]:
    """Pixel window ``(row0, row1, col0, col1)`` covering ``bbox``, snapped
    outward to whole chunks and clipped to the raster's ``shape``."""
    x0, y_top, xres, yres = transform
    height, width = shape
    west, south, east, north = bbox
    col0 = max(0, int((west - x0) / xres // CHUNK) * CHUNK)
    col1 = min(width, -int(-((east - x0) / xres) // CHUNK) * CHUNK)
    row0 = max(0, int((y_top - north) / yres // CHUNK) * CHUNK)
    row1 = min(height, -int(-((y_top - south) / yres) // CHUNK) * CHUNK)
    if col1 <= col0 or row1 <= row0:
        raise ValueError(f'bbox {bbox} does not intersect the layer grid')
    return (row0, row1, col0, col1)


def chunks_in_window(window: tuple[int, int, int, int]) -> list[tuple[int, int]]:
    """All chunk ids ``(cy, cx)`` intersecting a pixel window."""
    row0, row1, col0, col1 = window
    return [
        (cy, cx)
        for cy in range(row0 // CHUNK, -(-row1 // CHUNK))
        for cx in range(col0 // CHUNK, -(-col1 // CHUNK))
    ]


def chunk_window(cy: int, cx: int, shape: tuple) -> tuple[int, int, int, int]:
    """Pixel window of one chunk, clipped to the raster ``shape`` (edge
    chunks are partial)."""
    height, width = shape
    return (
        cy * CHUNK, min(height, (cy + 1) * CHUNK),
        cx * CHUNK, min(width, (cx + 1) * CHUNK),
    )


def coords_for_window(window: tuple[int, int, int, int], transform: tuple):
    """Pixel-centre coordinate arrays ``(lat, lon)`` for a window
    (lat descending)."""
    import numpy as np
    x0, y_top, xres, yres = transform
    row0, row1, col0, col1 = window
    lon = x0 + (np.arange(col0, col1) + 0.5) * xres
    lat = y_top - (np.arange(row0, row1) + 0.5) * yres
    return lat, lon


# A synthetic national-ish grid for tests: 40x50 degrees at 3 arcsec.
_T = (112.0, -9.0, 1 / 1200, 1 / 1200)
_SHAPE = (35 * 1200, 42 * 1200)
_BBOX = [148.36265, -33.52606, 148.38265, -33.50606]


def test_window_is_chunk_aligned_or_clipped():
    row0, row1, col0, col1 = window_for_bbox(_BBOX, _T, _SHAPE)
    return row0 % CHUNK == 0 and col0 % CHUNK == 0 and row1 > row0 and col1 > col0


def test_window_contains_bbox():
    x0, y_top, xres, yres = _T
    row0, row1, col0, col1 = window_for_bbox(_BBOX, _T, _SHAPE)
    west, south, east, north = _BBOX
    return (
        x0 + col0 * xres <= west and x0 + col1 * xres >= east
        and y_top - row0 * yres >= north and y_top - row1 * yres <= south
    )


def test_edge_chunk_is_clipped():
    height, width = _SHAPE
    r0, r1, c0, c1 = chunk_window(height // CHUNK, width // CHUNK - 1, _SHAPE)
    return r1 == height and c1 == width


def test_disjoint_bbox_raises():
    try:
        window_for_bbox([10.0, 50.0, 11.0, 51.0], _T, _SHAPE)
    except ValueError:
        return True
    return False


def test_overlapping_bboxes_share_chunks():
    a = window_for_bbox(_BBOX, _T, _SHAPE)
    b = window_for_bbox([_BBOX[0] + 0.05, _BBOX[1], _BBOX[2] + 0.05, _BBOX[3]], _T, _SHAPE)
    return len(set(chunks_in_window(a)) & set(chunks_in_window(b))) > 0


def test():
    return all([
        test_window_is_chunk_aligned_or_clipped(),
        test_window_contains_bbox(),
        test_edge_chunk_is_clipped(),
        test_disjoint_bbox_raises(),
        test_overlapping_bboxes_share_chunks(),
    ])


if __name__ == '__main__':
    print(test())

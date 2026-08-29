"""Fetch SLGA soil-property windows for a troi — via the machine-wide store.

Thin compatibility wrapper: the heavy lifting (layer registration,
chunk-level dedup, windowed COG reads) lives in
:class:`pyslga.store.Store`. Kept as a module so the familiar
``download_slga_soils(troi)`` entry point survives.
"""
import xarray as xr
from troi.troi import Troi
from pyslga.slga import SLGA, defaultslga
from pyslga.store import DEFAULT_ATTRIBUTES, DEFAULT_DEPTHS


def download_slga_soils(troi: Troi, vars=DEFAULT_ATTRIBUTES,
                        depths=DEFAULT_DEPTHS, api_key: str = None,
                        slga: SLGA = defaultslga) -> xr.Dataset:
    """Return SLGA soil properties for ``troi.bbox``.

    Fetches only the grid chunks no previous request has populated —
    repeat and overlapping queries re-download nothing. Dates on the
    troi are ignored: soil properties are time-invariant.

    Args:
        troi: The :class:`troi.troi.Troi` (bbox is what matters).
        vars: SLGA attribute names (default Clay/Sand/Silt).
        depths: Standard depth slices (default ``'5-15cm'``).
        api_key: TERN API key; falls back to ``config.tern_api_key``.
        slga: Endpoint/catalog configuration; defaults to the bundled one.

    Returns:
        xarray.Dataset with dims ``(lat, lon)`` and one variable per
        attribute x depth.
    """
    from pyslga.store import Store
    store = Store(config=troi.config, slga=slga)
    return store.get_ds_troi(troi, attributes=vars, depths=depths, api_key=api_key)


def test_live_fetch_and_dedup():
    """Live: cold fetch covers the bbox for the texture triple; repeat and
    overlapping bboxes fetch nothing; values are plausible percentages."""
    import numpy as np
    import tempfile
    from troi.config import Config
    from pyslga.store import Store

    tmpdir = tempfile.mkdtemp(prefix='pyslga_live_test_')
    from troi.config import config as global_config
    cfg = Config(out_dir=tmpdir, tmp_dir=tmpdir, tern_api_key=global_config.tern_api_key)
    store = Store(config=cfg)
    bbox = [148.36265, -33.52606, 148.38265, -33.50606]

    fetched = store.fill(bbox)
    if fetched < 3:  # at least one chunk per texture layer
        return False
    ds = store.get_ds(bbox)
    clay = ds['Clay_5-15cm'].values
    if not np.isfinite(clay).any() or not (0 <= np.nanmean(clay) <= 100):
        return False
    # identical repeat -> nothing
    if store.fill(bbox) != 0:
        return False
    # overlapping bbox ~2 km east -> same chunks here -> nothing
    if store.fill([bbox[0] + 0.02, bbox[1], bbox[2] + 0.02, bbox[3]]) != 0:
        return False
    # a new depth is its own set of layers -> fetches happen
    return store.fill(bbox, depths=('0-5cm',)) >= 3


def test():
    from troi.config import config
    if not config.tern_api_key:
        print('SKIPPED: set tern_api_key in ~/.config/Troi.json '
              '(or TROI_TERN_KEY) to run the live suite')
        return None
    return test_live_fetch_and_dedup()


if __name__ == '__main__':
    result = test()
    if result is not None:
        print(result)

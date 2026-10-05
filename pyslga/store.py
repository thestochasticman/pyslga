"""One machine-wide SLGA soils store that fills itself on demand.

Every soil-property pixel this machine ever downloads lands in a
single sparse Zarr store, one array per attribute x depth layer on the
SLGA national ~90 m grid:

    {config.tmp_dir}/slga_store/
    ├── slga.zarr/
    │   ├── CLY_005_015/c/<cy>/<cx>   # one file per written 1200x1200 chunk
    │   └── SND_005_015/...
    ├── layers/CLY_005_015.json        # the layer's COG url, transform, shape, nodata
    └── claims/                        # cross-node mutex dirs while a chunk is fetched

**The Zarr chunk file is the ledger.** Every write is exactly one whole
chunk, and zarr 3 commits a chunk by writing a temporary file and
renaming it, so a chunk file either exists complete or not at all.
Arrays are opened with ``write_empty_chunks=True`` so an all-NaN chunk
is materialised too. This keeps the store safe for many PBS jobs on
many Gadi nodes, where file locks are node-local and SQLite is not an
option (see ``troi/docs/ledger.md``).

At a layer's first contact, its COG filename is resolved from the TERN
datastore listing (each attribute has its own release date) and its
grid -- affine transform, shape, nodata -- is read from the COG and
written to ``layers/<key>.json``; everything after that is offline
arithmetic. ``Store.get_ds(bbox)`` diffs the requested chunks per layer
against the chunk files and fetches only the missing ones, each as one
integer-aligned windowed read under a :class:`troi.ledger.Claim`. Soil
properties are time-invariant: no time axis, no dates. Pixel reads
require a TERN API key (``config.tern_api_key`` or the ``api_key``
argument); listings are public.

Nothing is ever resampled here: ``get_ds`` returns native pixels with
``crs``, ``transform``, ``nodata`` and ``native_res_m`` attrs so a
consumer can regrid reproducibly.
"""
import os
from os import makedirs

import numpy as np
import xarray as xr
import zarr
from attrs import frozen, field

from troi import Config, config as default_config
from troi.ledger import Markers, Claim, ensure_array, Gap, GapReport
from pyslga import grid
from pyslga.paths import Paths
from pyslga.slga import SLGA, defaultslga

DEFAULT_ATTRIBUTES = ('Clay', 'Sand', 'Silt')
DEFAULT_DEPTHS = ('5-15cm',)
NATIVE_RES_M = 90

_ZARR_CFG = {'array.write_empty_chunks': True}


class GridMismatch(RuntimeError):
    """Requested layers do not share one lattice, so they cannot be stacked."""


@frozen
class Store:
    """The machine-wide SLGA store: one grid per layer, one ledger, zero
    re-downloads.

    Composed from :class:`troi.Config` (where the store
    lives, and the TERN API key) and :class:`pyslga.slga.SLGA`
    (endpoint + attribute/depth catalogs). No inheritance.

    Example:
        ```python
        from pyslga.store import Store

        store = Store()
        ds = store.get_ds(bbox)   # Clay/Sand/Silt at 5-15cm by default
        ds = store.get_ds(bbox, attributes=('Clay', 'pH_Water'),
                          depths=('0-5cm', '5-15cm'))
        store.gaps(bbox).summary()
        ```
    """

    config: Config = default_config
    slga: SLGA = defaultslga
    lease_s: float = 600.0           # a chunk claim older than this is presumed abandoned
    paths: Paths = field(init=False)

    paths.default(lambda s: Paths(s.config))

    def __attrs_post_init__(s):
        makedirs(s.paths.root, exist_ok=True)

    def _api_key(s, api_key: str = None) -> str:
        api_key = api_key or s.config.tern_api_key
        if not api_key:
            raise ValueError(
                'Set tern_api_key in ~/.config/Troi.json or pass api_key parameter'
            )
        return api_key

    # -- ledger: layer markers + the chunk files themselves ------------------

    @property
    def _layers(s) -> Markers:
        return Markers(s.paths.layers)

    def _chunk_file(s, key: str, cy: int, cx: int) -> str:
        return f'{s.paths.store}/{key}/c/{cy}/{cx}'

    def _done(s, key: str, cy: int, cx: int) -> bool:
        return os.path.exists(s._chunk_file(key, cy, cx))

    def _array(s, meta: dict, mode: str = 'a') -> zarr.Array:
        with zarr.config.set(_ZARR_CFG):
            root = zarr.open_group(s.paths.store, mode=mode)
            if mode == 'r':
                return root[meta['key']]
            return ensure_array(
                s.paths.root, root, meta['key'],
                shape=(meta['height'], meta['width']),
                chunks=(grid.CHUNK, grid.CHUNK), dtype='float32', fill_value=np.nan,
                config={'write_empty_chunks': True},
            )

    # -- layer registration -----------------------------------------------

    def _layer_meta(s, attribute: str, depth: str):
        """Layer metadata if the layer is registered, else None. Offline."""
        return s._layers.read((s.slga.layer_key(attribute, depth),))

    def _layer(s, attribute: str, depth: str, api_key: str = None) -> dict:
        """Layer metadata, registering the layer at first contact (listing
        lookup + one remote COG open -- network; offline after). The
        registration runs under a claim so concurrent first contacts do
        it once."""
        key = s.slga.layer_key(attribute, depth)
        meta = s._layer_meta(attribute, depth)
        if meta:
            return meta
        api_key = s._api_key(api_key)
        with Claim(s.paths.root, ('layer', key), lease_s=s.lease_s):
            meta = s._layer_meta(attribute, depth)
            if meta:
                return meta
            url = s._resolve_url(attribute, depth, api_key)
            import rasterio
            with rasterio.Env(GDAL_HTTP_HEADERS=f'x-api-key: {api_key}'):
                with rasterio.open(url) as src:
                    t = src.transform
                    meta = dict(key=key, url=url, x0=t.c, y_top=t.f, xres=t.a, yres=-t.e,
                                height=src.height, width=src.width, nodata=src.nodata)
            s._layers.write((key,), meta)
            return meta

    def _resolve_url(s, attribute: str, depth: str, api_key: str) -> str:
        """Resolve the layer's COG filename from the datastore listing --
        release dates differ per attribute, so names can't be hardcoded."""
        import re
        import requests
        code = s.slga.attribute_codes[attribute]
        ds_, de = s.slga.depth_codes[depth]
        r = requests.get(s.slga.listing_url(attribute),
                         headers={'x-api-key': api_key}, timeout=60)
        r.raise_for_status()
        matches = re.findall(rf'{code}_{ds_}_{de}_EV_[A-Za-z_]+_\d{{8}}\.tif', r.text)
        if not matches:
            raise RuntimeError(
                f'No SLGA EV COG for {attribute} {depth} in the listing at '
                f'{s.slga.listing_url(attribute)}'
            )
        return f'{s.slga.listing_url(attribute)}{sorted(set(matches))[-1]}'

    @staticmethod
    def _geo(meta: dict):
        """``(transform4, shape)`` as :mod:`pyslga.grid` wants them."""
        return ((meta['x0'], meta['y_top'], meta['xres'], meta['yres']),
                (meta['height'], meta['width']))

    # -- fill -------------------------------------------------------------

    def fill(s, bbox: list[float], attributes=DEFAULT_ATTRIBUTES,
             depths=DEFAULT_DEPTHS, api_key: str = None) -> int:
        """Ensure every chunk of every requested layer covering ``bbox``
        is populated.

        Troi-agnostic (and, soils being time-invariant, date-free).
        Returns the number of chunks actually downloaded -- 0 means the
        request was already fully covered and no network was touched.
        Safe to run from many processes and nodes at once.
        """
        for attribute in attributes:
            for depth in depths:
                s.slga.layer_key(attribute, depth)       # validate before any network
        fetched = 0
        for attribute in attributes:
            for depth in depths:
                meta = s._layer(attribute, depth, api_key)
                transform, shape = s._geo(meta)
                wanted = grid.chunks_in_window(grid.window_for_bbox(bbox, transform, shape))
                missing = [c for c in wanted if not s._done(meta['key'], *c)]
                if not missing:
                    continue
                arr = s._array(meta)
                for cy, cx in missing:
                    with Claim(s.paths.root, ('slga', meta['key'], cy, cx), lease_s=s.lease_s):
                        if s._done(meta['key'], cy, cx):
                            continue                     # another job fetched it while we waited
                        r0, r1, c0, c1 = grid.chunk_window(cy, cx, shape)
                        arr[r0:r1, c0:c1] = s._read_chunk(meta, (r0, r1, c0, c1), s._api_key(api_key))
                        fetched += 1
        return fetched

    def _read_chunk(s, meta: dict, window, api_key: str) -> np.ndarray:
        """One integer-aligned windowed read from the layer's COG."""
        import rasterio
        from rasterio.windows import Window
        r0, r1, c0, c1 = window
        with rasterio.Env(GDAL_HTTP_HEADERS=f'x-api-key: {api_key}'):
            with rasterio.open(meta['url']) as src:
                data = src.read(1, window=Window(c0, r0, c1 - c0, r1 - r0)).astype('float32')
        if meta['nodata'] is not None:
            data = np.where(data == meta['nodata'], np.nan, data)
        return data

    # -- audit --------------------------------------------------------------

    def gaps(s, bbox: list[float], attributes=DEFAULT_ATTRIBUTES,
             depths=DEFAULT_DEPTHS) -> GapReport:
        """Which chunks of the request are not on disk, per layer, and why.
        No network. A layer never contacted counts as one ``never_fetched``
        unit, because its chunking is unknown until its COG is opened."""
        gaps, expected = [], 0
        for attribute in attributes:
            for depth in depths:
                key = s.slga.layer_key(attribute, depth)
                meta = s._layer_meta(attribute, depth)
                if not meta:
                    expected += 1
                    gaps.append(Gap((key,), 'never_fetched', detail='layer not registered'))
                    continue
                transform, shape = s._geo(meta)
                wanted = grid.chunks_in_window(grid.window_for_bbox(bbox, transform, shape))
                expected += len(wanted)
                for cy, cx in wanted:
                    if s._done(key, cy, cx):
                        continue
                    if os.path.isdir(Claim(s.paths.root, ('slga', key, cy, cx)).dir):
                        gaps.append(Gap((key, cy, cx), 'claimed_in_progress'))
                    else:
                        gaps.append(Gap((key, cy, cx), 'never_fetched'))
        return GapReport(expected=expected, gaps=tuple(gaps))

    # -- read -------------------------------------------------------------

    def get_ds(s, bbox: list[float], attributes=DEFAULT_ATTRIBUTES,
               depths=DEFAULT_DEPTHS, api_key: str = None) -> xr.Dataset:
        """Return the soil-property window for ``bbox``, downloading only
        what's missing first.

        Troi-agnostic -- the data layer of the package. Pipelines that
        speak :class:`troi.Troi` use :meth:`get_ds_troi`.

        Args:
            bbox: ``[west, south, east, north]`` in EPSG:4326.
            attributes: SLGA attribute names (default the texture triple
                Clay/Sand/Silt).
            depths: Standard depth slices (default ``'5-15cm'``).
            api_key: TERN API key; falls back to ``config.tern_api_key``.

        Returns:
            xarray.Dataset with dims ``(lat, lon)`` on the layers' native
            grid (never resampled) and one variable per attribute x depth
            (e.g. ``Clay_5-15cm``). Attrs carry the georeferencing
            contract: ``crs``, ``transform`` (six affine numbers of this
            window), ``nodata``, ``native_res_m``.

        Raises:
            GridMismatch: If the requested layers are not on one lattice.
        """
        s.fill(bbox, attributes=attributes, depths=depths, api_key=api_key)
        data_vars, coords, ref, affine = {}, None, None, None
        for attribute in attributes:
            for depth in depths:
                meta = s._layer(attribute, depth, api_key)
                transform, shape = s._geo(meta)
                if ref is None:
                    ref = (transform, shape)
                elif (transform, shape) != ref:
                    raise GridMismatch(
                        f'{meta["key"]} is on {transform} {shape}; the first requested '
                        f'layer is on {ref[0]} {ref[1]}. Request them separately.')
                window = grid.window_for_bbox(bbox, transform, shape)
                row0, row1, col0, col1 = window
                block = s._array(meta, mode='r')[row0:row1, col0:col1]
                if coords is None:
                    lat, lon = grid.coords_for_window(window, transform)
                    coords = {'lat': lat, 'lon': lon}
                    x0, y_top, xres, yres = transform
                    affine = (xres, 0.0, x0 + col0 * xres, 0.0, -yres, y_top - row0 * yres)
                data_vars[f'{attribute}_{depth}'] = (('lat', 'lon'), block)
        return xr.Dataset(data_vars, coords=coords,
                          attrs={'crs': 'EPSG:4326', 'transform': list(affine), 'nodata': None,
                                 'native_res_m': NATIVE_RES_M, 'source': 'SLGA v2 (TERN)',
                                 'url': s.slga.base_url})

    # -- Troi adapters (the reproducibility layer speaks Troi) ----------

    def fill_troi(s, troi, attributes=DEFAULT_ATTRIBUTES,
                  depths=DEFAULT_DEPTHS, api_key: str = None) -> int:
        """:meth:`fill` for a :class:`troi.Troi` (dates
        ignored -- soil properties are time-invariant)."""
        return s.fill(troi.bbox, attributes=attributes, depths=depths, api_key=api_key)

    def get_ds_troi(s, troi, attributes=DEFAULT_ATTRIBUTES,
                    depths=DEFAULT_DEPTHS, api_key: str = None) -> xr.Dataset:
        """:meth:`get_ds` for a :class:`troi.Troi`."""
        return s.get_ds(troi.bbox, attributes=attributes, depths=depths, api_key=api_key)

    def gaps_troi(s, troi, attributes=DEFAULT_ATTRIBUTES, depths=DEFAULT_DEPTHS) -> GapReport:
        """:meth:`gaps` for a :class:`troi.Troi`."""
        return s.gaps(troi.bbox, attributes=attributes, depths=depths)


# -- offline tests (synthetic layers, no network) ---------------------------

_TEST_BBOX = [148.36265, -33.52606, 148.38265, -33.50606]
_T = (112.0, -9.0, 1 / 1200, 1 / 1200)
_SHAPE = (35 * 1200, 42 * 1200)


def _tmp_store(**config_kw) -> Store:
    import tempfile
    tmpdir = tempfile.mkdtemp(prefix='pyslga_store_test_')
    return Store(config=Config(out_dir=tmpdir, tmp_dir=tmpdir, **config_kw))


def _register_layer(store: Store, attribute: str, depth: str) -> dict:
    key = store.slga.layer_key(attribute, depth)
    meta = dict(key=key, url='synthetic://', x0=_T[0], y_top=_T[1], xres=_T[2], yres=_T[3],
                height=_SHAPE[0], width=_SHAPE[1], nodata=None)
    store._layers.write((key,), meta)
    return meta


def _prime_layer(store: Store, attribute: str, depth: str, bbox, value: float):
    """Register a synthetic layer and populate bbox's chunks, no network.
    Writing a chunk *is* marking it."""
    meta = _register_layer(store, attribute, depth)
    arr = store._array(meta)
    window = grid.window_for_bbox(bbox, _T, _SHAPE)
    for cy, cx in grid.chunks_in_window(window):
        r0, r1, c0, c1 = grid.chunk_window(cy, cx, _SHAPE)
        arr[r0:r1, c0:c1] = value


class _patched:
    def __init__(self, cls, name, value):
        self.cls, self.name, self.value = cls, name, value

    def __enter__(self):
        self.real = getattr(self.cls, self.name)
        setattr(self.cls, self.name, self.value)

    def __exit__(self, *exc):
        setattr(self.cls, self.name, self.real)


def _fake_read(value):
    def fake(self, meta, window, api_key):
        r0, r1, c0, c1 = window
        return np.full((r1 - r0, c1 - c0), value, 'float32')
    return fake


def test_synthetic_write_read_roundtrip():
    store = _tmp_store()
    _prime_layer(store, 'Clay', '5-15cm', _TEST_BBOX, 33.0)
    _prime_layer(store, 'Sand', '5-15cm', _TEST_BBOX, 55.0)
    ds = store.get_ds(_TEST_BBOX, attributes=('Clay', 'Sand'), depths=('5-15cm',))
    return (
        float(ds['Clay_5-15cm'][0, 0]) == 33.0
        and float(ds['Sand_5-15cm'][0, 0]) == 55.0
        and ds.lat[0] > ds.lat[-1]
    )


def test_get_ds_attrs_follow_the_contract():
    store = _tmp_store()
    _prime_layer(store, 'Clay', '5-15cm', _TEST_BBOX, 1.0)
    ds = store.get_ds(_TEST_BBOX, attributes=('Clay',))
    a, b, c, d, e, f = ds.attrs['transform']
    return (ds.attrs['crs'] == 'EPSG:4326' and ds.attrs['native_res_m'] == 90
            and abs(c + 0.5 * a - float(ds.lon[0])) < 1e-9
            and abs(f + 0.5 * e - float(ds.lat[0])) < 1e-9)


def test_chunk_file_is_the_ledger():
    store = _tmp_store()
    before = store.gaps(_TEST_BBOX, attributes=('Clay',))
    _prime_layer(store, 'Clay', '5-15cm', _TEST_BBOX, 1.0)
    after = store.gaps(_TEST_BBOX, attributes=('Clay',))
    key = store.slga.layer_key('Clay', '5-15cm')
    chunks = grid.chunks_in_window(grid.window_for_bbox(_TEST_BBOX, _T, _SHAPE))
    return (before.expected == 1 and before.gaps[0].detail == 'layer not registered'
            and after.expected == len(chunks) and after.complete
            and all(store._done(key, *c) for c in chunks))


def test_fill_skips_populated_chunks():
    store = _tmp_store()
    for a in DEFAULT_ATTRIBUTES:
        _prime_layer(store, a, '5-15cm', _TEST_BBOX, 1.0)
    return store.fill(_TEST_BBOX) == 0


def test_fill_writes_missing_chunks_once():
    store = _tmp_store(tern_api_key='synthetic')
    _register_layer(store, 'Clay', '5-15cm')
    with _patched(Store, '_read_chunk', _fake_read(4.0)):
        n1 = store.fill(_TEST_BBOX, attributes=('Clay',))
        n2 = store.fill(_TEST_BBOX, attributes=('Clay',))
    ds = store.get_ds(_TEST_BBOX, attributes=('Clay',))
    chunks = grid.chunks_in_window(grid.window_for_bbox(_TEST_BBOX, _T, _SHAPE))
    return n1 == len(chunks) and n2 == 0 and float(ds['Clay_5-15cm'][0, 0]) == 4.0


def _worker_fill(tmp_dir, value):
    store = Store(config=Config(out_dir=tmp_dir, tmp_dir=tmp_dir, tern_api_key='synthetic'))
    with _patched(Store, '_read_chunk', _fake_read(value)):
        store.fill(_TEST_BBOX, attributes=('Clay',))


def test_two_processes_fill_the_same_chunks():
    import multiprocessing as mp
    store = _tmp_store()
    _register_layer(store, 'Clay', '5-15cm')
    ctx = mp.get_context('fork')
    ps = [ctx.Process(target=_worker_fill, args=(store.config.tmp_dir, v)) for v in (1.0, 2.0)]
    for p in ps:
        p.start()
    for p in ps:
        p.join(timeout=300)
    ds = store.get_ds(_TEST_BBOX, attributes=('Clay',))
    claims = os.listdir(os.path.join(store.paths.root, 'claims'))
    return (all(p.exitcode == 0 for p in ps) and store.gaps(_TEST_BBOX, attributes=('Clay',)).complete
            and len(np.unique(ds['Clay_5-15cm'].values)) == 1 and claims == [])


def test_layers_are_independent():
    """A populated Clay layer must not satisfy a Silt request."""
    store = _tmp_store()
    _prime_layer(store, 'Clay', '5-15cm', _TEST_BBOX, 1.0)
    r = store.gaps(_TEST_BBOX, attributes=('Silt',))
    return not r.complete and r.gaps[0].unit == (store.slga.layer_key('Silt', '5-15cm'),)


def test_mismatched_layer_grids_raise():
    store = _tmp_store()
    _prime_layer(store, 'Clay', '5-15cm', _TEST_BBOX, 1.0)
    key = store.slga.layer_key('Sand', '5-15cm')
    other = dict(key=key, url='synthetic://', x0=_T[0] + 0.5 / 1200, y_top=_T[1], xres=_T[2],
                 yres=_T[3], height=_SHAPE[0], width=_SHAPE[1], nodata=None)
    store._layers.write((key,), other)
    arr = store._array(other)
    for cy, cx in grid.chunks_in_window(grid.window_for_bbox(_TEST_BBOX, _T, _SHAPE)):
        r0, r1, c0, c1 = grid.chunk_window(cy, cx, _SHAPE)
        arr[r0:r1, c0:c1] = 2.0
    try:
        store.get_ds(_TEST_BBOX, attributes=('Clay', 'Sand'))
    except GridMismatch:
        return True
    return False


def test_unknown_attribute_raises():
    store = _tmp_store()
    try:
        store.fill(_TEST_BBOX, attributes=('Vibes',))
    except ValueError:
        return True
    return False


def test_missing_key_raises_before_network():
    store = _tmp_store()  # config with no tern_api_key
    try:
        store.fill(_TEST_BBOX, attributes=('Clay',))
    except ValueError as e:
        return 'tern_api_key' in str(e)
    return False


def test():
    return all([
        test_synthetic_write_read_roundtrip(),
        test_get_ds_attrs_follow_the_contract(),
        test_chunk_file_is_the_ledger(),
        test_fill_skips_populated_chunks(),
        test_fill_writes_missing_chunks_once(),
        test_two_processes_fill_the_same_chunks(),
        test_layers_are_independent(),
        test_mismatched_layer_grids_raise(),
        test_unknown_attribute_raises(),
        test_missing_key_raises_before_network(),
    ])


if __name__ == '__main__':
    print(test())

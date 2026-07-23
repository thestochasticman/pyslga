"""One machine-wide SLGA soils store that fills itself on demand.

Every soil-property pixel this machine ever downloads lands in a
single sparse Zarr store, one array per attribute x depth layer on the
SLGA national ~90 m grid:

    {config.tmp_dir}/slga_store/
    ├── index.db      # SQLite: layer metadata + populated chunks
    └── slga.zarr/
        ├── CLY_005_015   # sparse national array; only written chunks exist
        └── SND_005_015 ...

At a layer's first contact, its COG filename is resolved from the TERN
datastore listing (each attribute has its own release date) and its
grid — affine transform, shape, nodata — is read from the COG and
recorded; everything after that is offline arithmetic.
``Store.get_ds(bbox)`` diffs the requested chunks per layer against
the ledger and fetches only the missing ones, each as one
integer-aligned windowed read. Soil properties are time-invariant:
no time axis, no dates. Pixel reads require a TERN API key
(``config.tern_api_key`` or the ``api_key`` argument); listings are
public.
"""
import sqlite3
from attrs import frozen, field
from datetime import datetime, timezone
from os import makedirs

import numpy as np
import xarray as xr
import zarr

from borevitz_lab.config import Config, config as default_config
from pyslga import grid
from pyslga.paths import Paths
from pyslga.slga import SLGA, defaultslga

_SCHEMA = """
CREATE TABLE IF NOT EXISTS layers (
    key     TEXT PRIMARY KEY,
    url     TEXT NOT NULL,
    x0      REAL NOT NULL,
    y_top   REAL NOT NULL,
    xres    REAL NOT NULL,
    yres    REAL NOT NULL,
    height  INTEGER NOT NULL,
    width   INTEGER NOT NULL,
    nodata  REAL
);

CREATE TABLE IF NOT EXISTS chunks (
    key TEXT NOT NULL,
    cy  INTEGER NOT NULL,
    cx  INTEGER NOT NULL,
    written_at TEXT NOT NULL,
    PRIMARY KEY (key, cy, cx)
) WITHOUT ROWID;
"""

DEFAULT_ATTRIBUTES = ('Clay', 'Sand', 'Silt')
DEFAULT_DEPTHS = ('5-15cm',)


@frozen
class Store:
    """The machine-wide SLGA store: one grid per layer, one ledger, zero
    re-downloads.

    Composed from :class:`borevitz_lab.config.Config` (where the store
    lives, and the TERN API key) and :class:`pyslga.slga.SLGA`
    (endpoint + attribute/depth catalogs). No inheritance.

    Example:
        ```python
        from pyslga.store import Store

        store = Store()
        ds = store.get_ds(bbox)   # Clay/Sand/Silt at 5-15cm by default
        ds = store.get_ds(bbox, attributes=('Clay', 'pH_Water'),
                          depths=('0-5cm', '5-15cm'))
        ```
    """

    config: Config = default_config
    slga: SLGA = defaultslga
    paths: Paths = field(init=False)

    paths.default(lambda s: Paths(s.config))

    def __attrs_post_init__(s):
        makedirs(s.paths.root, exist_ok=True)

    def _db(s) -> sqlite3.Connection:
        db = sqlite3.connect(s.paths.index_db)
        db.execute('PRAGMA journal_mode=WAL')
        db.executescript(_SCHEMA)
        return db

    def _api_key(s, api_key: str = None) -> str:
        api_key = api_key or s.config.tern_api_key
        if not api_key:
            raise ValueError(
                'Set tern_api_key in ~/.config/BorevitzLab.json or pass api_key parameter'
            )
        return api_key

    # -- layer registration -----------------------------------------------

    def _layer(s, db, attribute: str, depth: str, api_key: str = None) -> dict:
        """Layer metadata row, registering the layer at first contact
        (listing lookup + one remote COG open — network; offline after)."""
        key = s.slga.layer_key(attribute, depth)
        row = db.execute(
            'SELECT key, url, x0, y_top, xres, yres, height, width, nodata '
            'FROM layers WHERE key = ?', (key,),
        ).fetchone()
        if row:
            return dict(zip(
                ('key', 'url', 'x0', 'y_top', 'xres', 'yres', 'height', 'width', 'nodata'), row))

        url = s._resolve_url(attribute, depth, s._api_key(api_key))
        import rasterio
        with rasterio.Env(GDAL_HTTP_HEADERS=f'x-api-key: {s._api_key(api_key)}'):
            with rasterio.open(url) as src:
                t = src.transform
                meta = dict(key=key, url=url, x0=t.c, y_top=t.f, xres=t.a, yres=-t.e,
                            height=src.height, width=src.width, nodata=src.nodata)
        root = zarr.open_group(s.paths.store, mode='a')
        if key not in root:
            root.create_array(
                key, shape=(meta['height'], meta['width']),
                chunks=(grid.CHUNK, grid.CHUNK), dtype='float32', fill_value=np.nan,
            )
        with db:
            db.execute(
                'INSERT OR REPLACE INTO layers (key, url, x0, y_top, xres, yres, height, width, nodata) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                tuple(meta[k] for k in
                      ('key', 'url', 'x0', 'y_top', 'xres', 'yres', 'height', 'width', 'nodata')),
            )
        return meta

    def _resolve_url(s, attribute: str, depth: str, api_key: str) -> str:
        """Resolve the layer's COG filename from the datastore listing —
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

    # -- fill -------------------------------------------------------------

    def fill(s, bbox: list[float], attributes=DEFAULT_ATTRIBUTES,
             depths=DEFAULT_DEPTHS, api_key: str = None) -> int:
        """Ensure every chunk of every requested layer covering ``bbox``
        is populated.

        Query-agnostic (and, soils being time-invariant, date-free).
        Returns the number of chunks actually downloaded — 0 means the
        request was already fully covered and no network was touched.
        """
        import rasterio
        from rasterio.windows import Window

        db = s._db()
        try:
            fetched = 0
            for attribute in attributes:
                for depth in depths:
                    meta = s._layer(db, attribute, depth, api_key)
                    transform = (meta['x0'], meta['y_top'], meta['xres'], meta['yres'])
                    shape = (meta['height'], meta['width'])
                    window = grid.window_for_bbox(bbox, transform, shape)
                    wanted = grid.chunks_in_window(window)
                    done = set(db.execute(
                        'SELECT cy, cx FROM chunks WHERE key = ?', (meta['key'],),
                    ).fetchall())
                    missing = [c for c in wanted if c not in done]
                    if not missing:
                        continue
                    arr = zarr.open_group(s.paths.store, mode='a')[meta['key']]
                    with rasterio.Env(GDAL_HTTP_HEADERS=f'x-api-key: {s._api_key(api_key)}'):
                        with rasterio.open(meta['url']) as src:
                            for cy, cx in missing:
                                r0, r1, c0, c1 = grid.chunk_window(cy, cx, shape)
                                data = src.read(
                                    1, window=Window(c0, r0, c1 - c0, r1 - r0),
                                ).astype('float32')
                                if meta['nodata'] is not None:
                                    data = np.where(data == meta['nodata'], np.nan, data)
                                arr[r0:r1, c0:c1] = data
                                with db:
                                    db.execute(
                                        'INSERT OR REPLACE INTO chunks (key, cy, cx, written_at) '
                                        'VALUES (?, ?, ?, ?)',
                                        (meta['key'], cy, cx,
                                         datetime.now(timezone.utc).isoformat()),
                                    )
                                fetched += 1
            return fetched
        finally:
            db.close()

    # -- read -------------------------------------------------------------

    def get_ds(s, bbox: list[float], attributes=DEFAULT_ATTRIBUTES,
               depths=DEFAULT_DEPTHS, api_key: str = None) -> xr.Dataset:
        """Return the soil-property window for ``bbox``, downloading only
        what's missing first.

        Query-agnostic — the data layer of the package. Pipelines that
        speak :class:`borevitz_lab.query.Query` use :meth:`get_ds_query`.

        Args:
            bbox: ``[west, south, east, north]`` in EPSG:4326.
            attributes: SLGA attribute names (default the texture triple
                Clay/Sand/Silt).
            depths: Standard depth slices (default ``'5-15cm'``).
            api_key: TERN API key; falls back to ``config.tern_api_key``.

        Returns:
            xarray.Dataset with dims ``(lat, lon)`` and one variable per
            attribute x depth (e.g. ``Clay_5-15cm``).
        """
        s.fill(bbox, attributes=attributes, depths=depths, api_key=api_key)
        db = s._db()
        try:
            data_vars = {}
            coords = None
            for attribute in attributes:
                for depth in depths:
                    meta = s._layer(db, attribute, depth, api_key)
                    transform = (meta['x0'], meta['y_top'], meta['xres'], meta['yres'])
                    shape = (meta['height'], meta['width'])
                    window = grid.window_for_bbox(bbox, transform, shape)
                    row0, row1, col0, col1 = window
                    arr = zarr.open_group(s.paths.store, mode='r')[meta['key']]
                    block = arr[row0:row1, col0:col1]
                    if coords is None:
                        lat, lon = grid.coords_for_window(window, transform)
                        coords = {'lat': lat, 'lon': lon}
                    data_vars[f'{attribute}_{depth}'] = (('lat', 'lon'), block)
            return xr.Dataset(data_vars, coords=coords,
                              attrs={'crs': 'EPSG:4326', 'source': 'SLGA v2 (TERN)'})
        finally:
            db.close()

    # -- Query adapters (the reproducibility layer speaks Query) ----------

    def fill_query(s, query, attributes=DEFAULT_ATTRIBUTES,
                   depths=DEFAULT_DEPTHS, api_key: str = None) -> int:
        """:meth:`fill` for a :class:`borevitz_lab.query.Query` (dates
        ignored — soil properties are time-invariant)."""
        return s.fill(query.bbox, attributes=attributes, depths=depths, api_key=api_key)

    def get_ds_query(s, query, attributes=DEFAULT_ATTRIBUTES,
                     depths=DEFAULT_DEPTHS, api_key: str = None) -> xr.Dataset:
        """:meth:`get_ds` for a :class:`borevitz_lab.query.Query`."""
        return s.get_ds(query.bbox, attributes=attributes, depths=depths, api_key=api_key)


# -- offline tests (synthetic layers, no network) ---------------------------

_TEST_BBOX = [148.36265, -33.52606, 148.38265, -33.50606]
_T = (112.0, -9.0, 1 / 1200, 1 / 1200)
_SHAPE = (35 * 1200, 42 * 1200)


def _tmp_store() -> Store:
    import tempfile
    tmpdir = tempfile.mkdtemp(prefix='pyslga_store_test_')
    return Store(config=Config(out_dir=tmpdir, tmp_dir=tmpdir))


def _prime_layer(store: Store, attribute: str, depth: str, bbox, value: float):
    """Register a synthetic layer and populate bbox's chunks, no network."""
    key = store.slga.layer_key(attribute, depth)
    db = store._db()
    with db:
        db.execute(
            'INSERT OR REPLACE INTO layers (key, url, x0, y_top, xres, yres, height, width, nodata) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (key, 'synthetic://', _T[0], _T[1], _T[2], _T[3], _SHAPE[0], _SHAPE[1], None),
        )
    root = zarr.open_group(store.paths.store, mode='a')
    if key not in root:
        root.create_array(key, shape=_SHAPE, chunks=(grid.CHUNK, grid.CHUNK),
                          dtype='float32', fill_value=np.nan)
    arr = root[key]
    window = grid.window_for_bbox(bbox, _T, _SHAPE)
    with db:
        for cy, cx in grid.chunks_in_window(window):
            r0, r1, c0, c1 = grid.chunk_window(cy, cx, _SHAPE)
            arr[r0:r1, c0:c1] = value
            db.execute(
                'INSERT OR REPLACE INTO chunks (key, cy, cx, written_at) VALUES (?, ?, ?, ?)',
                (key, cy, cx, 'synthetic'),
            )
    db.close()


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


def test_fill_skips_populated_chunks():
    store = _tmp_store()
    for a in DEFAULT_ATTRIBUTES:
        _prime_layer(store, a, '5-15cm', _TEST_BBOX, 1.0)
    return store.fill(_TEST_BBOX) == 0


def test_layers_are_independent():
    """A populated Clay layer must not satisfy a Silt request."""
    store = _tmp_store()
    _prime_layer(store, 'Clay', '5-15cm', _TEST_BBOX, 1.0)
    db = store._db()
    row = db.execute('SELECT 1 FROM chunks WHERE key = ?',
                     (store.slga.layer_key('Silt', '5-15cm'),)).fetchone()
    db.close()
    return row is None


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
        test_fill_skips_populated_chunks(),
        test_layers_are_independent(),
        test_unknown_attribute_raises(),
        test_missing_key_raises_before_network(),
    ])


if __name__ == '__main__':
    print(test())

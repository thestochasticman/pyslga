# pyslga

**Cached [SLGA](https://esoil.io/TERNLandscapes/Public/Pages/SLGA/index.html)
soil-property windows for Australia — download once per chunk, never
twice.** The Soil and Landscape Grid of Australia provides national
~90 m grids of soil properties (clay, sand, silt, pH, bulk density,
AWC, …) at six standard depths, served as one COG per attribute × depth
on the TERN datastore. Every pixel this machine ever downloads lands in
one sparse, chunk-indexed store, so repeat requests, overlapping AOIs
and new depth slices reuse everything already fetched. Part of the
[Borevitz Lab](https://borevitzlab.anu.edu.au/) ecosystem.

## How it works

```
{data_root}/slga_store/
├── index.db      # SQLite: layer metadata + populated chunks
└── slga.zarr/
    ├── CLY_005_015   # one sparse national array per attribute × depth
    └── SND_005_015 ...
```

- At a layer's first contact, its COG filename is resolved from the
  datastore listing (each attribute has its **own release date**, so
  names can't be hardcoded) and its grid — transform, shape, nodata —
  is read from the COG and recorded. Everything after that is offline
  arithmetic.
- Any bbox maps deterministically to a set of 1200 × 1200-px chunks on
  the layer's native grid. `Store.get_ds(bbox)` diffs them against the
  ledger and downloads **only the missing chunks**, each as one
  integer-aligned windowed read — no resampling, ever.
- Soil properties are time-invariant: no time axis, no dates.
- Pixel reads require a TERN API key (listings are public) — set
  `tern_api_key` in `~/.config/BorevitzLab.json`,
  `BOREVITZ_LAB_TERN_KEY`, or pass `api_key=` per call. Keys are free
  from <https://account.tern.org.au/>.

## Usage

The core API is **query-agnostic** — just a bbox:

```python
from pyslga.store import Store

store = Store()
bbox = [148.36265, -33.52606, 148.38265, -33.50606]  # [W, S, E, N]

ds = store.get_ds(bbox)   # texture triple Clay/Sand/Silt at 5-15cm
ds = store.get_ds(bbox, attributes=('Clay', 'pH_Water', 'Bulk_Density'),
                  depths=('0-5cm', '5-15cm', '15-30cm'))
ds['Clay_5-15cm']         # (lat, lon) DataArray

store.fill(bbox)          # → 0: already local
```

16 attributes × 6 depths are available — see `pyslga.slga.SLGA` for
the catalog.

Pipelines that speak the shared `borevitz_lab.query.Query` use the
adapters (dates on the query are ignored):

```python
ds = store.get_ds_query(query)
```

`download_slga_soils(query)` remains as a thin wrapper.

## Install

All lab repos share one conda environment, **`borevitz_lab`** — each
repo's `environment.yml` creates it if missing and adds its own
packages if it exists (never use `--prune`):

```bash
conda env update -n borevitz_lab -f environment.yml
conda activate borevitz_lab
pip install -e ../borevitz_lab   # shared core (not yet on PyPI)
pip install -e .
```

Package design (shared across the lab's packages — no inheritance,
composition only):

- **`Query`** (from `borevitz-lab`) — identity: what region.
- **`SLGA`** (`pyslga.slga`) — config: endpoint, attribute/depth catalogs.
- **`Paths`** (`pyslga.paths`) — derived locations of the store for a
  given `Config`.
- **`grid`** — chunk math parameterised by each layer's native grid
  (pure, offline-testable).
- **`Store`** (`pyslga.store`) — ties them together.

## Test

```bash
# offline (pure math + synthetic store):
python pyslga/grid.py     # True
python pyslga/paths.py    # True
python pyslga/store.py    # True

# live (small real reads from TERN — needs tern_api_key):
python pyslga/download_slga.py  # True
```

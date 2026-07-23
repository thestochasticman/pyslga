# Light-weight exports only: pyslga.store (and the download_slga wrapper)
# pull in rasterio/zarr, so those stay behind explicit submodule imports.
from pyslga.paths import Paths
from pyslga.slga import SLGA, defaultslga

__all__ = [
    'Paths',
    'SLGA',
    'defaultslga',
]

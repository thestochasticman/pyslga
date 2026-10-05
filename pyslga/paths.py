"""Derived on-disk locations of the machine-wide SLGA store.

The store is keyed by :class:`troi.Config` (one store per
data root, shared by every request on this machine). Rule of thumb
across the lab's packages: user-settable inputs → Config, derived
locations → Paths. No inheritance — composition only.
"""
from attrs import frozen, field
from troi import Config, config as default_config


@frozen
class Paths:
    """Where the pyslga store lives for a given Config.

    Attributes:
        config: The :class:`troi.Config` supplying the data
            root (and the TERN API key).
        root: Store directory (``{config.tmp_dir}/slga_store``).
        store: The sparse Zarr store — one array per attribute x depth layer.
            A chunk file under ``slga.zarr/<key>/c/`` is the ledger entry.
        layers: Marker tree of layer metadata (``layers/<key>.json``: COG url,
            transform, shape, nodata), written at first contact.

    Example:
        ```python
        from pyslga.paths import Paths

        Paths().store  # '~/Downloads/Troi-Tmp/slga_store/slga.zarr'
        ```
    """

    config: Config = default_config

    root: str = field(init=False)
    store: str = field(init=False)
    layers: str = field(init=False)

    root.default(lambda s: f'{s.config.tmp_dir}/slga_store')
    store.default(lambda s: f'{s.root}/slga.zarr')
    layers.default(lambda s: f'{s.root}/layers')


def test_paths_derive_from_config():
    import tempfile
    tmpdir = tempfile.mkdtemp(prefix='pyslga_paths_test_')
    cfg = Config(out_dir=tmpdir, tmp_dir=tmpdir)
    paths = Paths(cfg)
    return (
        paths.root == f'{tmpdir}/slga_store'
        and paths.store == f'{tmpdir}/slga_store/slga.zarr'
        and paths.layers == f'{tmpdir}/slga_store/layers'
    )


def test():
    return test_paths_derive_from_config()


if __name__ == '__main__':
    print(test())

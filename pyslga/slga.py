from attrs import frozen

@frozen
class SLGA:
    """Endpoint and layer configuration for the Soil and Landscape Grid
    of Australia (SLGA) v2 COGs on the TERN datastore.

    One national ~90 m (3 arc-second) COG per attribute x depth. Pixel
    reads require a TERN API key (``x-api-key`` header); directory
    listings are public. Each attribute is published on its own release
    date, so COG filenames are resolved from the datastore listing at
    first contact rather than hardcoded.
    """

    base_url: str = ('https://data.tern.org.au/model-derived/slga/'
                     'NationalMaps/SoilAndLandscapeGrid')

    attribute_codes = {
        'Clay': 'CLY',
        'Silt': 'SLT',
        'Sand': 'SND',
        'pH_Water': 'PHW',
        'Bulk_Density': 'BDW',
        'Available_Water_Capacity': 'AWC',
        'Cation_Exchange_Capacity': 'CEC',
        'Effective_Cation_Exchange_Capacity': 'ECE',
        'Total_Nitrogen': 'NTO',
        'Total_Phosphorus': 'PTO',
        'Coarse_Fragments': 'CFG',
        'Depth_of_Soil': 'DES',
        'Depth_to_Rock': 'DER',
        'Drained_Upper_Limit': 'DUL',
        'L15': 'L15',
        'Available_Phosphorus': 'AVP',
    }

    depth_codes = {
        '0-5cm': ('000', '005'),
        '5-15cm': ('005', '015'),
        '15-30cm': ('015', '030'),
        '30-60cm': ('030', '060'),
        '60-100cm': ('060', '100'),
        '100-200cm': ('100', '200'),
    }

    def layer_key(self, attribute: str, depth: str) -> str:
        """Stable store key for an attribute x depth layer (e.g. ``CLY_005_015``)."""
        if attribute not in self.attribute_codes:
            raise ValueError(
                f'Unknown SLGA attribute {attribute!r}. '
                f'Known: {sorted(self.attribute_codes)}'
            )
        if depth not in self.depth_codes:
            raise ValueError(
                f'Unknown SLGA depth {depth!r}. Known: {sorted(self.depth_codes)}'
            )
        code = self.attribute_codes[attribute]
        ds, de = self.depth_codes[depth]
        return f'{code}_{ds}_{de}'

    def listing_url(self, attribute: str) -> str:
        return f'{self.base_url}/{self.attribute_codes[attribute]}/v2/'

defaultslga = SLGA()

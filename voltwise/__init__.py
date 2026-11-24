"""VoltWise - dynamic-tariff battery and EV optimiser for Home Assistant.

The package is intentionally dependency-free (Python standard library only) so
that the optimisation core can be audited, tested and shipped inside a minimal
Home Assistant add-on container.
"""

from ._version import VERSION as __version__

__all__ = ["__version__"]

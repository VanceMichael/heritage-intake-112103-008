"""Digital collections intake and catalogue workbench.

The package models a growing intake of synthetic collection catalogues, scan
and photo records, research notes and rights records.  It keeps provenance
explicit, treats every digital record as a distinct carrier (similar content is
never merged automatically), evaluates rights for a particular use channel and
date, and freezes the evidence used by published exhibitions.
"""

from .errors import IntakeError
from .store import Workbench

__all__ = ["IntakeError", "Workbench"]

"""
Counsyl Storage Utils comes with the ability to create paths in a similar
manner to `path.py <https://pypi.python.org/pypi/path.py>`_. It is expected
that the main functions below are the only ones directly used.
(i.e. ``Path`` or ``SwiftPath`` objects should never be explicitly
instantiated).
"""

from storage_utils.utils import is_posix_path  # flake8: noqa
from storage_utils.utils import is_swift_path  # flake8: noqa
from storage_utils.utils import NamedTemporaryDirectory  # flake8: noqa
from storage_utils.utils import path  # flake8: noqa

__all__ = [
    'is_posix_path',
    'is_swift_path',
    'NamedTemporaryDirectory',
    'path'
]

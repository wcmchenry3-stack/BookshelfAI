import pytest

from tests._dns_guard import _no_real_dns  # noqa: F401  (autouse fixture)
from tests.helpers import rsa_pem_pair


@pytest.fixture(scope="module")
def _keys():
    """One throwaway RS256 key pair (private, public) per test module."""
    return rsa_pem_pair()

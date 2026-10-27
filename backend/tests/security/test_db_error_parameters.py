"""DB errors must not carry bound parameter values into logs or Sentry (#688).

A re-raised IntegrityError becomes a 500 that Sentry captures with the
exception message; with SQLAlchemy's default that message includes the
INSERT's values (google_id, display name, email).
"""

import pytest

from app.core.database import engine

pytestmark = pytest.mark.security


def test_engine_hides_bound_parameters_in_errors():
    assert engine.sync_engine.hide_parameters is True

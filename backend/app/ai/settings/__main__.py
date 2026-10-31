"""``python -m app.ai.settings``: the operator CLI (see :mod:`.cli`)."""

import sys

from app.ai import settings as ai_settings

sys.exit(ai_settings.main())

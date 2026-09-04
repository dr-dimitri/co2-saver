# Copyright (C) 2026 CO2 Saver contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Config-entry version declaration for CO2 Saver.

Home Assistant 2026.9 imports this platform before setting up every config entry.
The user-facing flow is intentionally introduced by roadmap issue #5.
"""

from homeassistant.config_entries import ConfigFlow

from .const import DOMAIN


class Co2SaverConfigFlow(ConfigFlow, domain=DOMAIN):
    """Declare the initial CO2 Saver config-entry schema version."""

    VERSION = 1

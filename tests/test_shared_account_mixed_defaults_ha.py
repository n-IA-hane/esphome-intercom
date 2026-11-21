"""A registered peer chooses from the actual shared-account default offer."""

import pytest

from tests.test_canonical_account_calls_ha import (
    canonical_browser as canonical_browser,
    logical_browser as logical_browser,
    registered_phones as registered_phones,
    trunks as trunks,
    lab as lab,
)
from tests.support.shared_codec_peer import CASES
from tests.test_shared_account_codec_media_ha import (
    test_shared_account_codec_winner_media_and_reuse as exercise_shared_account,
)

pytestmark = pytest.mark.ha


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("logical_browser", [{"ringing_timeout": 30}], indirect=True)
@pytest.mark.parametrize("name", ["PCMU", "G722", "OPUS"])
async def test_shared_default_multi_codec_offer_card_and_sip_winners(
    canonical_browser,
    hass_ws_client,
    hass_admin_user,
    tmp_path,
    socket_enabled,
    name,
):
    case = next(case for case in CASES if case.name == name)
    await exercise_shared_account(
        canonical_browser,
        hass_ws_client,
        hass_admin_user,
        tmp_path,
        socket_enabled,
        case,
        mixed_defaults=True,
    )

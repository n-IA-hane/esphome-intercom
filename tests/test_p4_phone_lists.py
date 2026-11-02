"""P4 presentations share one bounded contact list and the normal call pages."""
import pytest

from tests.native_profile import profile, by_id, text

PROFILES = [
    'full-experience/single-bus/waveshare-p4-touch-full-afe-landscape-videophone-jpeg.yaml',
    'full-experience/single-bus/waveshare-p4-touch-full-afe-portrait.yaml',
    'voip-only/single-bus/waveshare-p4-touch-videophone-jpeg.yaml',
]
pytestmark = [pytest.mark.architecture, pytest.mark.timeout(240)]


@pytest.mark.parametrize('path', PROFILES)
def test_p4_uses_shared_typed_contacts_without_a_separate_mailbox_page(path):
    config = profile(path)
    contact = by_id(config['phone_list_view'], 'contacts_list_view')
    assert contact['parent_id'] == 'phone_contacts_container'
    assert contact['row_height'] == 72
    assert 'get_contact(index)' in text(contact['render_row'])
    assert 'get_contact_blf(contact->stable_id())' in text(contact['render_row'])
    assert 'pickup_contact(row_id)' in text(contact['on_select'])
    assert 'call_contact(row_id)' in text(contact['on_select'])
    assert 'phone_voicemail_page' not in text(config)
    assert 'imap_voicemail' not in config
    assert by_id(config['lvgl'], 'phone_contacts_container')
    presentation = text(config['lvgl'])
    for old in ('voip_stack.next_contact', 'voip_stack.prev_contact', 'call_idle_swipe_area'):
        assert old not in presentation
    if 'voip-only/' in path:
        show = text(by_id(config['script'], 'show_phone_page'))
        assert 'videophone_page' in show
        status = text(by_id(config['script'], 'update_status'))
        assert 'id(contacts_list_view).refresh()' in status
        assert 'id(phone_contacts_container), LV_OBJ_FLAG_HIDDEN' in status
    else:
        draw = text(by_id(config['script'], 'draw_display'))
        assert 'ui_voicemail_active' not in draw
        assert 'id(contacts_list_view).refresh()' in draw

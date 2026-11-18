"""A physical device opens its own admin action and exposes a ready-to-copy URL."""

from pathlib import Path
import shutil

import pytest

pytestmark = [pytest.mark.browser_real, pytest.mark.js_runtime]


def test_open_phone_copy_exact_url_and_discard_old_response():
    from playwright.sync_api import expect, sync_playwright

    assets = Path(__file__).resolve().parents[1] / "custom_components/voip_stack/frontend"
    with sync_playwright() as runner:
        browser = runner.chromium.launch(executable_path=shutil.which("chromium"), headless=True)
        context = browser.new_context(permissions=["clipboard-read", "clipboard-write"])
        page = context.new_page()

        def serve(route):
            name = route.request.url.rsplit("/", 1)[-1]
            if name in {"voip-stack-provisioning.js", "voip-stack-i18n.js"}:
                route.fulfill(content_type="text/javascript", body=(assets / name).read_text())
            else:
                route.fulfill(content_type="text/html", body="<html></html>")

        page.route("http://localhost/**", serve)
        page.goto("http://localhost/")
        page.evaluate("""async () => {
            await import('/voip-stack-provisioning.js');
            window.requests = [];
            window.panel = document.createElement('voip-stack-provisioning');
            panel.hass = {language:'it', connection:{}, callWS:async req => {
                requests.push(req);
                if (req.service_data.phone_id === 'first')
                    await new Promise(resolve => { window.finishFirst = resolve; });
                return {response:{provisioning_url:'https://pbx.test/'+req.service_data.phone_id+'/handset.xml'}};
            }};
            panel.route = {path:'/first'};
            document.body.append(panel);
            panel.route = {path:'/second'};
        }""")
        field = page.get_by_label("URL di provisioning", exact=True)
        url = "https://pbx.test/second/handset.xml"
        expect(field).to_have_value(url)
        page.evaluate("finishFirst()")
        page.get_by_role("button", name="Copia URL di provisioning").click()
        assert page.evaluate("navigator.clipboard.readText()") == url
        assert page.evaluate("requests[1].service_data") == {"phone_id": "second", "include_url": True}
        assert page.evaluate("requests[1].service") == "get_phone_provisioning_state"
        page.evaluate("panel.remove()")
        assert page.evaluate("panel.input.value") == ""
        browser.close()

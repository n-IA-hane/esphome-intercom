import { voipStackTranslate } from "./voip-stack-i18n.js";

// HA opens this admin panel from the physical device's configuration link.
// The address in the device registry contains only the public inventory ID.
class PhoneProvisioningPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this.shadowRoot.innerHTML = `
      <style>
        :host { display:block; padding:24px; color:var(--primary-text-color) }
        main { max-width:42rem; margin:auto }
        input { box-sizing:border-box; width:100%; padding:10px; margin:8px 0; font:inherit }
        button { min-height:44px; padding:0 16px; font:inherit; cursor:pointer }
      </style>
      <main>
        <h1></h1>
        <label><span></span><input type="text" readonly dir="ltr" autocomplete="off"></label>
        <button type="button" disabled></button>
        <label hidden><span></span><input type="text" readonly dir="ltr" autocomplete="off"></label>
        <p role="status"></p>
      </main>`;
    this.input = this.shadowRoot.querySelector("input");
    this.dhcpLabel = this.shadowRoot.querySelector("label[hidden]");
    this.dhcpInput = this.dhcpLabel.querySelector("input");
    this.copy = this.shadowRoot.querySelector("button");
    this.message = this.shadowRoot.querySelector('[role="status"]');
    this.copy.onclick = async () => {
      if (!this.input.value) return;
      try {
        await navigator.clipboard.writeText(this.input.value);
        this.message.textContent = this.translate("URL copied");
      } catch {
        this.input.focus();
        this.input.select();
        this.message.textContent = this.translate("Copy the selected address.");
      }
    };
  }

  translate(text) { return voipStackTranslate(this._hass, text); }

  set hass(value) {
    this._hass = value;
    this.refresh();
  }

  set route(value) {
    this._route = value;
    this.refresh();
  }

  connectedCallback() { this.refresh(); }

  disconnectedCallback() {
    this.connection = undefined;
    this.phone = "";
    this.input.value = "";
    this.dhcpInput.value = "";
    this.copy.disabled = true;
  }

  async refresh() {
    if (!this.isConnected || !this._hass || !this._route) return;
    let id;
    try { id = decodeURIComponent(this._route.path.replace(/^\//, "")); }
    catch { id = ""; }
    const connection = this._hass.connection;
    this.shadowRoot.querySelector("h1").textContent = this.translate("Phone provisioning");
    this.shadowRoot.querySelector("label span").textContent = this.translate("Provisioning URL");
    this.copy.textContent = this.translate("Copy provisioning URL");
    this.dhcpLabel.querySelector("span").textContent = this.translate("DHCP provisioning URL");
    if (id === this.phone && connection === this.connection) return;
    this.phone = id;
    this.connection = connection;
    this.input.value = "";
    this.dhcpInput.value = "";
    this.dhcpLabel.hidden = true;
    this.copy.disabled = true;
    this.message.textContent = "";
    if (!id) return;
    try {
      const { response } = await this._hass.callWS({
        type: "call_service", domain: "voip_stack", service: "get_phone_provisioning_state",
        service_data: { phone_id: id, include_url: true }, return_response: true,
      });
      if (connection !== this.connection || id !== this.phone) return;
      this.input.value = response.provisioning_url;
      if (response.dhcp_provisioning_url) {
        this.dhcpInput.value = response.dhcp_provisioning_url;
        this.dhcpLabel.hidden = false;
      }
      this.copy.disabled = !this.input.value;
    } catch {
      if (connection === this.connection && id === this.phone) {
        this.message.textContent = this.translate("Provisioning URL unavailable");
      }
    }
  }
}

if (!customElements.get("voip-stack-provisioning")) {
  customElements.define("voip-stack-provisioning", PhoneProvisioningPanel);
}

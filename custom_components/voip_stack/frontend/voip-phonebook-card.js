const DEFAULT_ENTITY = "sensor.voip_phonebook";
const PHONEBOOK_MODULE_VERSION = (() => {
  try { return new URL(import.meta.url).searchParams.get("v") || "dev"; }
  catch (_) { return "dev"; }
})();
const { voipStackTranslate } = await import(`./voip-stack-i18n.js?v=${encodeURIComponent(PHONEBOOK_MODULE_VERSION)}`);

function installWheelScrollHandoff(scroller) {
  scroller.addEventListener("wheel", (event) => {
    if (event.ctrlKey || !event.deltaY) return;
    const scale = event.deltaMode === WheelEvent.DOM_DELTA_LINE
      ? 16
      : event.deltaMode === WheelEvent.DOM_DELTA_PAGE
        ? window.innerHeight
        : 1;
    const delta = event.deltaY * scale;
    const maxScroll = Math.max(0, scroller.scrollHeight - scroller.clientHeight);
    const available = delta > 0 ? maxScroll - scroller.scrollTop : scroller.scrollTop;
    const requested = Math.abs(delta);
    if (requested <= available + 0.5) return;

    const consumed = Math.max(0, available);
    scroller.scrollTop = delta > 0 ? maxScroll : 0;
    const remainder = Math.max(0, requested - consumed) * Math.sign(delta);
    if (remainder) window.scrollBy(0, remainder);
    event.preventDefault();
  }, { passive: false });
}

class VoipPhonebookView extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = {};
    this._hass = null;
    this._lastRoster = null;
    this._resizeObserver = new ResizeObserver(() => this._measure());
  }

  connectedCallback() { this._observe(); }
  disconnectedCallback() { this._resizeObserver.disconnect(); }

  _observe() {
    const card = this.shadowRoot?.querySelector("ha-card");
    if (card) {
      this._resizeObserver.disconnect();
      this._resizeObserver.observe(card);
      this._measure();
    }
  }

  _measure() {
    const card = this.shadowRoot?.querySelector("ha-card");
    if (!card) return;
    card.classList.toggle("narrow", card.clientWidth < 420);
    card.classList.toggle("wide", card.clientWidth >= 560);
    card.classList.toggle("short", card.clientHeight < 300);
  }

  static _assertConfig(config) {
    const entity = config?.entity || DEFAULT_ENTITY;
    if (typeof entity !== "string" || !entity.startsWith("sensor.")) {
      throw new Error("VoIP Phonebook requires a sensor entity");
    }
  }

  setConfig(config) {
    VoipPhonebookView._assertConfig(config);
    this._config = { entity: DEFAULT_ENTITY, ...config };
    this._lastRoster = null;
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    const entity = this._config.entity || DEFAULT_ENTITY;
    const roster = hass?.states?.[entity]?.attributes?.roster_json || "";
    if (roster === this._lastRoster) return;
    this._lastRoster = roster;
    this._render();
  }

  _contacts() {
    if (!this._hass) return [];
    const entity = this._config.entity || DEFAULT_ENTITY;
    const raw = this._hass.states?.[entity]?.attributes?.roster_json;
    if (!raw) return [];
    try {
      const parsed = typeof raw === "string" ? JSON.parse(raw) : raw;
      const contacts = Array.isArray(parsed) ? parsed : parsed?.contacts || parsed?.entries || [];
      return contacts
        .filter((contact) => contact && (this._config.show_disabled || contact.enabled !== false))
        .sort((a, b) => this._name(a).localeCompare(this._name(b), undefined, { sensitivity: "base" }));
    } catch (_) {
      return [];
    }
  }

  _name(contact) {
    return String(contact?.name || contact?.id || voipStackTranslate(this._hass, "Unnamed"));
  }

  _appendContact(list, contact) {
    const row = document.createElement("div");
    row.className = "contact";

    row.setAttribute("role", "listitem");
    const heading = document.createElement("div");
    heading.className = "contact-heading";
    const icon = document.createElement("ha-icon");
    icon.setAttribute("icon", contact.number ? "mdi:phone-classic" : "mdi:account-voice");
    const name = document.createElement("span");
    name.className = "contact-name";
    name.textContent = this._name(contact);
    heading.append(icon, name);
    row.appendChild(heading);

    const details = document.createElement("div");
    details.className = "details";
    if (contact.extension) {
      const extension = document.createElement("div");
      const arrow = document.createElement("span");
      arrow.className = "arrow";
      arrow.textContent = "↳";
      const label = document.createElement("span");
      label.textContent = voipStackTranslate(this._hass, "Extension:");
      const value = document.createElement("code");
      value.textContent = String(contact.extension);
      extension.append(arrow, label, value);
      details.appendChild(extension);
    }
    if (contact.number) {
      const number = document.createElement("div");
      const arrow = document.createElement("span");
      arrow.className = "arrow";
      arrow.textContent = "↳";
      const label = document.createElement("span");
      label.textContent = voipStackTranslate(this._hass, "Number:");
      const link = document.createElement("a");
      link.href = `tel:${String(contact.number).replace(/[^\d+*#]/g, "")}`;
      link.textContent = String(contact.number);
      number.append(arrow, label, link);
      details.appendChild(number);
    }
    if (details.childElementCount) row.appendChild(details);
    list.appendChild(row);
  }

  _render() {
    if (!this.shadowRoot) return;
    this.shadowRoot.replaceChildren();

    const style = document.createElement("style");
    style.textContent = `
      :host { display: block; height: 100%; min-height: 0; }
      ha-card {
        box-sizing: border-box;
        display: flex;
        flex-direction: column;
        height: 100%;
        min-height: 190px;
        overflow: hidden;
      }
      .header {
        flex: 0 0 auto;
        padding: 18px 18px 10px;
        font-size: 1.35rem;
        font-weight: 500;
        line-height: 1.25;
        text-align: center;
      }
      .header[hidden] { display: none; }
      .list {
        flex: 1 1 auto;
        min-height: 0;
        overflow-x: hidden;
        overflow-y: auto;
        padding: 4px 18px 16px;
        scrollbar-gutter: stable;
      }
      .contact { padding: 8px 0; }
      .contact + .contact { border-top: 1px solid var(--divider-color); }
      .contact-heading { display: flex; align-items: center; gap: 8px; min-width: 0; }
      .contact-heading ha-icon { --mdc-icon-size: 22px; flex: 0 0 auto; }
      .contact-name { overflow: hidden; font-weight: 600; text-overflow: ellipsis; white-space: nowrap; }
      .details { margin: 5px 0 0 30px; color: var(--secondary-text-color); font-size: .9rem; }
      .details > div { display: flex; align-items: baseline; gap: 6px; min-width: 0; padding: 2px 0; }
      .arrow { color: var(--secondary-text-color); }
      code {
        color: var(--primary-text-color);
        background: transparent;
        padding: 0;
        font-family: inherit;
      }
      a { color: var(--primary-color); text-decoration: none; }
      a:hover { text-decoration: underline; }
      .empty { color: var(--secondary-text-color); font-style: italic; padding: 12px 0; }
      ha-card.wide .list {
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        align-content: start;
        column-gap: 24px;
      }
      ha-card.wide .contact:nth-child(2) { border-top: 0; }
      ha-card.short .header { padding: 10px 14px 6px; font-size: 1.1rem; }
      ha-card.short .list { padding: 2px 14px 10px; }
      ha-card.short .contact { padding: 4px 0; }
      ha-card.short .details { margin-top: 2px; }
      ha-card.narrow .details { margin-left: 26px; }
    `;

    const card = document.createElement("ha-card");
    const configuredTitle = String(
      Object.hasOwn(this._config, "title")
        ? this._config.title || ""
        : voipStackTranslate(this._hass, "VoIP Phonebook"),
    ).trim();
    if (configuredTitle) {
      const title = document.createElement("div");
      title.className = "header";
      title.textContent = configuredTitle;
      card.appendChild(title);
    }

    const list = document.createElement("div");
    list.className = "list";
    installWheelScrollHandoff(list);
    list.setAttribute("role", "list");
    list.setAttribute("aria-label", configuredTitle || voipStackTranslate(this._hass, "VoIP phonebook"));
    list.tabIndex = 0;
    const contacts = this._contacts();
    if (!contacts.length) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = this._config.empty_text || voipStackTranslate(this._hass, "No contacts available.");
      list.appendChild(empty);
    } else {
      contacts.forEach((contact) => this._appendContact(list, contact));
    }
    card.appendChild(list);
    this.shadowRoot.append(style, card);
    this._observe();
  }
}

if (!customElements.get("voip-stack-phonebook-view")) {
  customElements.define("voip-stack-phonebook-view", VoipPhonebookView);
}

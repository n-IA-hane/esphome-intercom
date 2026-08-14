/**
 * Static DOM builders for the VoIP Stack card.
 *
 * Dynamic values are assigned through textContent and element properties.
 * The style blocks contain no user-controlled data.
 */

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

function buildMediaDeviceControls(prefix) {
  const root = document.createElement("section");
  root.className = "media-device-controls";
  const heading = document.createElement("div");
  heading.className = "media-device-heading";
  heading.textContent = "Media devices";
  root.appendChild(heading);

  const rows = {};
  for (const [kind, label] of [
    ["audioinput", "Microphone"],
    ["audiooutput", "Speaker"],
    ["videoinput", "Camera"],
  ]) {
    const row = document.createElement("label");
    row.className = "media-device-row";
    row.htmlFor = `${prefix}-${kind}`;
    const text = document.createElement("span");
    text.textContent = label;
    const select = document.createElement("select");
    select.id = `${prefix}-${kind}`;
    select.dataset.kind = kind;
    row.appendChild(text);
    row.appendChild(select);
    root.appendChild(row);
    rows[kind] = { row, select };
  }

  const actions = document.createElement("div");
  actions.className = "media-device-actions";
  const accessBtn = document.createElement("button");
  accessBtn.type = "button";
  accessBtn.textContent = "Allow media access";
  const cycleCameraBtn = document.createElement("button");
  cycleCameraBtn.type = "button";
  cycleCameraBtn.textContent = "Switch camera";
  actions.appendChild(accessBtn);
  actions.appendChild(cycleCameraBtn);
  root.appendChild(actions);

  const status = document.createElement("div");
  status.className = "media-device-status";
  status.setAttribute("role", "status");
  root.appendChild(status);
  return { root, rows, accessBtn, cycleCameraBtn, status, optionsKey: "" };
}

export function buildMainCardSkeleton(cardVersion) {
    const root = this.shadowRoot;
    root.replaceChildren();
    this._softphoneTargetOptionsKey = null;

    const style = document.createElement("style");
    style.textContent = `
      :host {
        display: block;
        box-sizing: border-box;
        width: 100%;
        max-width: 100%;
        min-width: 0;
        height: 100%;
        min-height: 0;
        overflow: hidden;
        --voip-stack-card-surface: var(--ha-card-background, var(--card-background-color, white));
        --voip-control-surface: transparent;
        --voip-control-hover-surface: var(--secondary-background-color, rgba(127, 127, 127, 0.12));
      }
      .card {
        box-sizing: border-box;
        display: flex;
        flex-direction: column;
        height: 100%;
        width: 100%;
        max-width: 100%;
        min-width: 0;
        min-height: 0;
        overflow-x: hidden;
        overflow-y: auto;
        /* Let wheel/touchpad scrolling chain back to the HA dashboard when
         * this card has no remaining vertical overflow. Interactive controls
         * still receive their normal pointer/click events. */
        overscroll-behavior-y: auto;
        background: var(--voip-stack-card-surface);
        border-radius: var(--ha-card-border-radius, 12px);
        box-shadow: var(--ha-card-box-shadow, 0 2px 6px rgba(0,0,0,0.1));
        padding: var(--voip-fluid-space, 16px);
        position: relative;
        isolation: isolate;
      }
      /* :where() keeps this generic stacking rule below state-specific
       * positioning in the cascade. Chained :not(.class) selectors otherwise
       * contribute three class weights and can pin the video call bar back
       * into normal flex flow instead of its absolute bottom layer. */
      .card > :where(:not(.video-canvas):not(.native-camera):not(.video-shade)) {
        position: relative;
        z-index: 2;
      }
      .video-canvas {
        position: absolute; inset: 0; z-index: 0; width: 100%; height: 100%;
        max-width: 100%; max-height: 100%; object-fit: contain; background: #000;
        border-radius: inherit; pointer-events: none;
      }
      .native-camera {
        position: absolute; inset: 0; z-index: 0; width: 100%; height: 100%;
        overflow: hidden; background: #000; border-radius: inherit;
        pointer-events: none;
      }
      .native-camera > .native-camera-card {
        display: block; width: 100%; height: 100%; margin: 0;
      }
      .video-canvas[hidden], .native-camera[hidden], .video-shade[hidden] { display: none; }
      .video-shade {
        position: absolute; inset: 0; z-index: 1; pointer-events: none;
        border-radius: inherit;
        background: linear-gradient(to bottom, rgba(0,0,0,.42), rgba(0,0,0,.08) 42%, rgba(0,0,0,.60));
      }
      /* Keep the exact Lovelace slot geometry when video appears. Absolute
       * media/control layers do not provide intrinsic height, so retain the
       * configured 100% height and only use 280px as the auto-row fallback. */
      .card.video-active { overflow: hidden; background: #000; min-height: 280px; }
      .video-active .header,
      .video-active .destination-label,
      .video-active .destination-value,
      .video-active .status,
      .video-active .status-reason,
      .video-active .stats,
      .video-active .version { color: white; text-shadow: 0 1px 3px rgba(0,0,0,.9); }
      ha-card.card.video-active > .button-container {
        position: absolute;
        left: 0;
        right: 0;
        bottom: 0;
        width: 100%;
        min-height: 50px;
        height: clamp(50px, 16%, 58px);
        margin: 0;
        padding: 0;
        align-items: stretch;
      }
      .video-active .destination-row,
      .video-active .status,
      .video-active .status-reason,
      .video-active .version { display: none; }
      .header { font-size: 1.2em; font-weight: 500; margin-bottom: var(--voip-fluid-space, 16px); color: var(--primary-text-color); text-align: center; }
      .header[hidden] { display: none; }

      .destination-row {
        display: flex; align-items: center; justify-content: center;
        gap: 12px; margin-bottom: var(--voip-fluid-space, 16px);
      }
      .destination-row[hidden] { display: none; }
      .nav-btn {
        width: 36px; height: 36px; border-radius: 50%;
        border: 1px solid var(--divider-color, #ccc);
        background: var(--voip-control-surface);
        background-color: var(--voip-control-surface);
        color: var(--primary-text-color); cursor: pointer;
        font-size: 1.2em; display: flex; align-items: center; justify-content: center;
      }
      .nav-btn:hover { background: var(--voip-control-hover-surface); }
      .nav-btn:disabled { opacity: 0.5; cursor: not-allowed; }
      .destination-value {
        flex: 1; text-align: center; font-size: 1.1em; font-weight: 500;
        color: var(--primary-text-color); padding: 8px 0;
      }
      .destination-value.selecting { padding: 0; }
      .destination-value.selecting .destination-text { display: none; }
      .destination-select {
        width: 100%; box-sizing: border-box; padding: 8px;
        border: 1px solid var(--divider-color, #ccc);
        border-radius: 4px; background: var(--voip-control-surface);
        background-color: var(--voip-control-surface);
        color: var(--primary-text-color); font-size: 0.95em;
        box-shadow: none;
      }
      /* Native select popups are painted outside the shadow/card surface.
       * Chromium can otherwise combine the OS white popup with HA's dark-theme
       * white text. Keep the closed control themed, but give popup rows a
       * matched system foreground/background pair. */
      .destination-select option {
        color: CanvasText;
        background-color: Canvas;
      }
      .destination-select[hidden] { display: none; }
      .destination-label {
        font-size: 0.75em; color: var(--secondary-text-color);
        display: block; margin-bottom: 2px;
      }
      .keypad-panel {
        margin: -4px 0 16px;
        display: flex;
        flex-direction: column;
        gap: 10px;
      }
      .keypad-panel[hidden] { display: none; }
      .keypad-input {
        width: 100%;
        box-sizing: border-box;
        padding: 10px 12px;
        border: 1px solid var(--divider-color, #ccc);
        border-radius: 6px;
        background: var(--voip-control-surface);
        color: var(--primary-text-color);
        font-size: 1.05em;
        text-align: center;
        color-scheme: light dark;
      }
      .keypad-grid {
        display: grid;
        grid-template-columns: repeat(3, minmax(0, 1fr));
        gap: 8px;
      }
      .keypad-key {
        min-height: 42px;
        border: 1px solid var(--divider-color, #ccc);
        border-radius: 8px;
        background: var(--voip-control-surface);
        color: var(--primary-text-color);
        font-size: 1.1em;
        font-weight: 600;
        cursor: pointer;
      }
      .keypad-key:hover { background: var(--voip-control-hover-surface); }
      .keypad-key:disabled { opacity: 0.5; cursor: not-allowed; }

      .button-container {
        display: flex;
        flex: 1 1 auto;
        min-height: var(--voip-button-size, 100px);
        align-items: center;
        justify-content: center;
        gap: max(8px, var(--voip-fluid-space, 16px));
        margin-bottom: var(--voip-fluid-space, 16px);
      }
      .offline-panel {
        display: flex; flex-direction: column; align-items: center; justify-content: center;
        gap: 8px; min-height: 132px; margin-bottom: 14px;
        color: var(--error-color, #f44336);
      }
      .offline-panel[hidden] { display: none; }
      .offline-icon ha-icon { --mdc-icon-size: 64px; }
      .offline-title { font-size: 1.1em; font-weight: 600; color: var(--primary-text-color); }
      .voip-button {
        width: var(--voip-button-size, 100px); height: var(--voip-button-size, 100px); border-radius: 50%; border: none; cursor: pointer;
        font-size: 1em; font-weight: bold; transition: all 0.2s ease;
        display: flex; align-items: center; justify-content: center;
      }
      .voip-button[hidden] { display: none; }
      .voip-button.small { width: var(--voip-small-button-size, 80px); height: var(--voip-small-button-size, 80px); font-size: 0.9em; }
      .voip-button.call { background: #4caf50; color: white; }
      .voip-button.answer { background: #4caf50; color: white; animation: ring-pulse 1s infinite; }
      .voip-button.decline { background: #f44336; color: white; animation: ring-pulse 1s infinite; }
      .voip-button.hangup { background: #f44336; color: white; }
      .voip-button.hangup {
        transform: scale(var(--voip-hangup-scale, 1));
        transform-origin: center;
        transition: transform 80ms linear, opacity 0.2s ease;
      }
      .hangup-icon, .hangup-copy, .hangup-duration { display: none; }
      .video-active .voip-button.hangup {
        box-sizing: border-box;
        width: auto;
        flex: 1 1 auto;
        height: 100%;
        min-height: 50px;
        border-radius: 0;
        padding: 0 18px;
        gap: 12px;
        justify-content: flex-start;
        overflow: hidden;
        background: linear-gradient(90deg, rgba(122, 5, 5, .24), rgba(230, 35, 35, .18));
        -webkit-backdrop-filter: blur(8px) saturate(1.12);
        backdrop-filter: blur(8px) saturate(1.12);
        box-shadow: 0 -1px 0 rgba(255,255,255,.18), 0 -8px 30px rgba(0,0,0,.24);
        transform: scaleY(var(--voip-hangup-scale, 1));
        transform-origin: center bottom;
      }
      .video-active .hangup-label { display: none; }
      .video-active .hangup-icon {
        display: inline-flex;
        align-items: center;
        justify-content: center;
      }
      .video-active .hangup-icon ha-icon { --mdc-icon-size: 28px; }
      .video-active .hangup-copy {
        display: flex;
        flex: 1 1 auto;
        min-width: 0;
        flex-direction: column;
        align-items: flex-start;
        text-align: left;
        font-weight: 500;
        line-height: 1.15;
      }
      .video-active .hangup-state { font-size: .82rem; opacity: .82; }
      .video-active .hangup-peer {
        width: 100%;
        max-width: 100%;
        min-width: 0;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
        font-size: .98rem;
      }
      .video-active .hangup-duration {
        display: block;
        flex: 0 0 auto;
        margin-left: auto;
        font-variant-numeric: tabular-nums;
        font-size: 1rem;
        letter-spacing: .03em;
      }
      .hangup-stats { display: none; }
      .video-active .hangup-stats:not([hidden]) {
        display: block;
        flex: 1 1 auto;
        min-width: 0;
        margin: 0 8px;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
        text-align: center;
        font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
        font-size: clamp(.56rem, 1.5vw, .68rem);
        font-weight: 500;
        opacity: .9;
      }
      .voip-button:disabled { opacity: 0.5; cursor: not-allowed; animation: none; }
      @keyframes ring-pulse { 0%, 100% { transform: scale(1); } 50% { transform: scale(1.05); } }

      .status { text-align: center; color: var(--secondary-text-color); font-size: 0.9em; }
      .status-reason { text-align: center; color: var(--secondary-text-color); font-size: 0.85em; margin-top: 4px; padding: 0 12px; word-wrap: break-word; }
      .status-reason[hidden] { display: none; }
      .status-indicator { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 6px; }
      .status-indicator.in_call { background: #4caf50; }
      .status-indicator.idle { background: #9e9e9e; }
      .status-indicator.unavailable { background: #f44336; }
      .status-indicator.transitioning { background: #ff9800; animation: blink 0.5s infinite; }
      .status-indicator.ringing { background: #ff9800; animation: blink 0.5s infinite; }
      @keyframes blink { 0%, 100% { opacity: 1; } 50% { opacity: 0.3; } }

      .stats { font-size: 0.75em; color: #666; margin-top: 8px; text-align: center; }
      .video-active .stats { display: none; }
      .error { color: #f44336; font-size: 0.85em; text-align: center; margin-top: 8px; }
      .settings-btn {
        display: block;
        margin: 10px auto 0;
        border: 1px solid var(--divider-color, #ccc);
        border-radius: 6px;
        background: var(--voip-control-surface);
        color: var(--primary-text-color);
        padding: 6px 12px;
        cursor: pointer;
        font-size: 0.85em;
      }
      .settings-btn[hidden] { display: none; }
      .runtime-controls {
        display: flex;
        justify-content: center;
        gap: 8px;
        margin-top: 10px;
      }
      .runtime-controls[hidden] { display: none; }
      .settings-panel {
        margin-top: 10px;
        padding: 8px 10px;
        border-top: 1px solid var(--divider-color, #ddd);
        background: var(--voip-stack-card-surface);
        text-align: left;
      }
      .settings-panel[hidden] { display: none; }
      .settings-label-icon { display: none; }
      .video-active:not(.settings-open) .runtime-controls {
        position: absolute;
        z-index: 4;
        right: 0;
        bottom: 0;
        height: clamp(50px, 16%, 58px);
        margin: 0;
      }
      .video-active:not(.settings-open) .runtime-controls .settings-btn {
        width: clamp(50px, 16vw, 58px);
        height: 100%;
        margin: 0;
        padding: 0;
        border: 0;
        border-radius: 0;
        color: white;
        background: transparent;
      }
      .video-active:not(.settings-open) .runtime-controls .settings-label { display: none; }
      .video-active:not(.settings-open) .runtime-controls .settings-label-icon {
        display: inline-flex;
        --mdc-icon-size: 23px;
      }
      .video-active:not(.settings-open) .voip-button.hangup { padding-right: 68px; }
      .card.settings-open {
        overflow: auto;
        background: var(--voip-stack-card-surface);
      }
      .card.settings-open > :not(.header):not(.runtime-controls):not(.settings-panel) {
        display: none !important;
      }
      .card.settings-open .header { display: block; margin-bottom: 4px; }
      .card.settings-open .header {
        color: var(--primary-text-color);
        text-shadow: none;
      }
      .card.settings-open .runtime-controls { order: 1; margin-top: 0; }
      .card.settings-open .settings-panel {
        order: 2;
        width: 100%;
        box-sizing: border-box;
        margin-top: 8px;
      }
      .media-device-controls { margin-top: 10px; }
      .media-device-heading {
        color: var(--primary-text-color);
        font-size: .9em;
        font-weight: 600;
      }
      .media-device-row {
        display: grid;
        grid-template-columns: minmax(0, .8fr) minmax(0, 1.5fr);
        gap: 8px;
        align-items: center;
        margin-top: 8px;
        color: var(--secondary-text-color);
        font-size: .85em;
      }
      .media-device-row select {
        width: 100%;
        min-width: 0;
        padding: 5px;
        color: var(--primary-text-color);
        background: var(--voip-stack-card-surface);
        border: 1px solid var(--divider-color, #ccc);
        border-radius: 5px;
      }
      .media-device-row option { color: CanvasText; background: Canvas; }
      .media-device-actions {
        display: flex;
        flex-wrap: wrap;
        gap: 8px;
        margin-top: 10px;
      }
      .media-device-actions button {
        border: 1px solid var(--divider-color, #ccc);
        border-radius: 6px;
        padding: 5px 9px;
        background: var(--voip-control-surface);
        color: var(--primary-text-color);
        cursor: pointer;
      }
      .media-device-actions button[hidden] { display: none; }
      .media-device-status {
        min-height: 1em;
        margin-top: 6px;
        color: var(--secondary-text-color);
        font-size: .76em;
      }
      @media (prefers-reduced-motion: reduce) {
        .voip-button.hangup { transform: none !important; transition: none; }
      }
      .auto-answer-row {
        display: grid;
        grid-template-columns: minmax(0, 1fr) auto;
        align-items: center;
        gap: 8px; margin-top: 8px; font-size: 0.85em; color: var(--secondary-text-color);
      }
      .auto-answer-row[hidden] { display: none; }
      .auto-answer-row input {
        grid-column: 2;
        grid-row: 1;
        justify-self: end;
        margin: 0;
        cursor: pointer;
        accent-color: var(--primary-color);
      }
      .auto-answer-row label {
        grid-column: 1;
        grid-row: 1;
        justify-self: start;
        cursor: pointer;
        user-select: none;
      }
      .softphone-groups-panel { width: 100%; }
      .softphone-groups-panel[hidden] { display: none; }
      .softphone-group-row {
        display: grid;
        grid-template-columns: minmax(0, 1fr) minmax(0, 1.35fr);
        gap: 8px;
        align-items: center;
        margin-top: 8px;
        font-size: 0.85em;
        color: var(--secondary-text-color);
      }
      .softphone-group-row label { min-width: 0; text-align: left; }
      .softphone-group-row input,
      .softphone-group-row select {
        width: 100%;
        min-width: 0;
        font-size: 0.95em;
      }
      .card.layout-compact { padding: 10px 12px; }
      .layout-compact .header { margin-bottom: 8px; }
      .layout-compact .destination-row { margin-bottom: 8px; }
      .layout-compact .destination-value { padding: 4px 0; }
      .layout-compact .button-container { gap: 12px; margin-bottom: 8px; }
      .layout-compact .offline-panel { min-height: 82px; margin-bottom: 8px; }
      .layout-compact .offline-icon ha-icon { --mdc-icon-size: 42px; }
      .layout-compact .runtime-controls { margin-top: 6px; }
      .layout-compact .settings-btn { margin-top: 6px; padding: 4px 10px; }
      .layout-compact .stats, .layout-compact .error, .layout-compact .version { margin-top: 4px; }
      .card.layout-short { padding: 8px 10px; }
      .layout-short .header { font-size: 1.05em; margin-bottom: 4px; }
      .layout-short .destination-row { margin-bottom: 4px; }
      .layout-short .destination-label { display: none; }
      .layout-short .button-container { margin-bottom: 4px; }
      .layout-short .voip-button { font-size: .85em; }
      .layout-short .offline-panel { min-height: 64px; gap: 3px; margin-bottom: 4px; }
      .layout-short .offline-icon ha-icon { --mdc-icon-size: 32px; }
      .layout-short .runtime-controls { margin-top: 4px; }
      .layout-narrow .button-container { gap: 8px; }
      .version { font-size: 0.65em; color: #999; text-align: right; margin-top: 8px; }
    `;
    root.appendChild(style);

    const card = document.createElement("ha-card");
    card.className = "card";
    installWheelScrollHandoff(card);

    const videoCanvas = document.createElement("canvas");
    videoCanvas.className = "video-canvas";
    videoCanvas.hidden = true;
    videoCanvas.setAttribute("aria-label", "Remote SIP video");
    const videoShade = document.createElement("div");
    videoShade.className = "video-shade";
    videoShade.hidden = true;
    const nativeCameraHost = document.createElement("div");
    nativeCameraHost.className = "native-camera";
    nativeCameraHost.hidden = true;
    card.appendChild(videoCanvas);
    card.appendChild(nativeCameraHost);
    card.appendChild(videoShade);

    const header = document.createElement("div");
    header.className = "header";
    const headerName = document.createTextNode("");
    header.appendChild(headerName);
    card.appendChild(header);

    // Destination row
    const destRow = document.createElement("div");
    destRow.className = "destination-row";
    const prevBtn = document.createElement("button");
    prevBtn.type = "button";
    prevBtn.className = "nav-btn";
    prevBtn.title = "Previous";
    prevBtn.setAttribute("aria-label", "Previous destination");
    prevBtn.textContent = "<";
    const destValueWrap = document.createElement("div");
    destValueWrap.className = "destination-value";
    const destLabel = document.createElement("span");
    destLabel.className = "destination-label";
    destLabel.textContent = "Destination";
    destValueWrap.appendChild(destLabel);
    const destValue = document.createTextNode("");
    const destText = document.createElement("span");
    destText.className = "destination-text";
    destText.appendChild(destValue);
    destValueWrap.appendChild(destText);
    const destSelect = document.createElement("select");
    destSelect.className = "destination-select";
    destSelect.setAttribute("aria-label", "Destination");
    destSelect.hidden = true;
    destValueWrap.appendChild(destSelect);
    const nextBtn = document.createElement("button");
    nextBtn.type = "button";
    nextBtn.className = "nav-btn";
    nextBtn.title = "Next";
    nextBtn.setAttribute("aria-label", "Next destination");
    nextBtn.textContent = ">";
    destRow.appendChild(prevBtn);
    destRow.appendChild(destValueWrap);
    destRow.appendChild(nextBtn);
    card.appendChild(destRow);

    const keypadPanel = document.createElement("div");
    keypadPanel.className = "keypad-panel";
    keypadPanel.id = "voip-keypad-panel";
    keypadPanel.hidden = true;
    const keypadInput = document.createElement("input");
    keypadInput.className = "keypad-input";
    keypadInput.type = "text";
    keypadInput.inputMode = "tel";
    keypadInput.autocomplete = "off";
    keypadInput.spellcheck = false;
    keypadInput.placeholder = "Number, name or SIP URI";
    keypadInput.setAttribute("aria-label", "Number, name or SIP URI");
    keypadPanel.appendChild(keypadInput);
    const keypadGrid = document.createElement("div");
    keypadGrid.className = "keypad-grid";
    const keypadKeys = {};
    for (const key of ["1", "2", "3", "4", "5", "6", "7", "8", "9", "*", "0", "#", "Clear", "⌫"]) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "keypad-key";
      btn.textContent = key;
      if (key === "Clear") btn.setAttribute("aria-label", "Clear destination");
      if (key === "⌫") btn.setAttribute("aria-label", "Delete last character");
      keypadKeys[key] = btn;
      keypadGrid.appendChild(btn);
    }
    keypadPanel.appendChild(keypadGrid);
    card.appendChild(keypadPanel);

    const offlinePanel = document.createElement("div");
    offlinePanel.className = "offline-panel";
    offlinePanel.setAttribute("role", "status");
    offlinePanel.hidden = true;
    const offlineIcon = document.createElement("div");
    offlineIcon.className = "offline-icon";
    const offlineHaIcon = document.createElement("ha-icon");
    offlineHaIcon.setAttribute("icon", "mdi:phone-off");
    offlineIcon.appendChild(offlineHaIcon);
    const offlineTitle = document.createElement("div");
    offlineTitle.className = "offline-title";
    offlineTitle.textContent = "ESP unavailable";
    offlinePanel.appendChild(offlineIcon);
    offlinePanel.appendChild(offlineTitle);
    card.appendChild(offlinePanel);

    // Button container with all four action buttons + a placeholder.
    // Visibility toggled in _render via [hidden].
    const buttonContainer = document.createElement("div");
    buttonContainer.className = "button-container";
    const answerBtn = document.createElement("button");
    answerBtn.type = "button";
    answerBtn.className = "voip-button small answer";
    answerBtn.textContent = "Answer";
    const declineBtn = document.createElement("button");
    declineBtn.type = "button";
    declineBtn.className = "voip-button small decline";
    declineBtn.textContent = "Decline";
    const hangupBtn = document.createElement("button");
    hangupBtn.type = "button";
    hangupBtn.className = "voip-button hangup";
    hangupBtn.setAttribute("aria-label", "Hang up call");
    const hangupLabel = document.createElement("span");
    hangupLabel.className = "hangup-label";
    hangupLabel.textContent = "Hangup";
    const hangupIcon = document.createElement("span");
    hangupIcon.className = "hangup-icon";
    const hangupHaIcon = document.createElement("ha-icon");
    hangupHaIcon.setAttribute("icon", "mdi:phone-hangup");
    hangupIcon.appendChild(hangupHaIcon);
    const hangupCopy = document.createElement("span");
    hangupCopy.className = "hangup-copy";
    const hangupState = document.createElement("span");
    hangupState.className = "hangup-state";
    hangupState.textContent = "In call";
    const hangupPeer = document.createElement("span");
    hangupPeer.className = "hangup-peer";
    hangupCopy.appendChild(hangupState);
    hangupCopy.appendChild(hangupPeer);
    const hangupDuration = document.createElement("span");
    hangupDuration.className = "hangup-duration";
    hangupDuration.textContent = "00:00";
    const hangupStats = document.createElement("span");
    hangupStats.className = "hangup-stats";
    hangupStats.hidden = true;
    hangupBtn.appendChild(hangupLabel);
    hangupBtn.appendChild(hangupIcon);
    hangupBtn.appendChild(hangupCopy);
    hangupBtn.appendChild(hangupStats);
    hangupBtn.appendChild(hangupDuration);
    const callBtn = document.createElement("button");
    callBtn.type = "button";
    callBtn.className = "voip-button call";
    callBtn.textContent = "Call";
    const placeholderBtn = document.createElement("button");
    placeholderBtn.type = "button";
    placeholderBtn.className = "voip-button";
    placeholderBtn.textContent = "...";
    placeholderBtn.disabled = true;
    buttonContainer.appendChild(answerBtn);
    buttonContainer.appendChild(declineBtn);
    buttonContainer.appendChild(hangupBtn);
    buttonContainer.appendChild(callBtn);
    buttonContainer.appendChild(placeholderBtn);
    card.appendChild(buttonContainer);

    // Status line + optional reason on its own row
    const statusRow = document.createElement("div");
    statusRow.className = "status";
    statusRow.setAttribute("role", "status");
    statusRow.setAttribute("aria-live", "polite");
    const statusIndicator = document.createElement("span");
    statusIndicator.className = "status-indicator idle";
    statusRow.appendChild(statusIndicator);
    statusRow.appendChild(document.createTextNode(" "));
    const statusText = document.createTextNode("");
    statusRow.appendChild(statusText);
    card.appendChild(statusRow);

    const statusReason = document.createElement("div");
    statusReason.className = "status-reason";
    statusReason.hidden = true;
    card.appendChild(statusReason);

    const runtimeControls = document.createElement("div");
    runtimeControls.className = "runtime-controls";

    const keypadBtn = document.createElement("button");
    keypadBtn.type = "button";
    keypadBtn.className = "settings-btn";
    keypadBtn.textContent = "Keypad";
    keypadBtn.setAttribute("aria-controls", "voip-keypad-panel");
    keypadBtn.setAttribute("aria-expanded", "false");
    runtimeControls.appendChild(keypadBtn);

    const settingsBtn = document.createElement("button");
    settingsBtn.type = "button";
    settingsBtn.className = "settings-btn";
    const settingsLabel = document.createElement("span");
    settingsLabel.className = "settings-label";
    settingsLabel.textContent = "Options";
    const settingsLabelIcon = document.createElement("ha-icon");
    settingsLabelIcon.className = "settings-label-icon";
    settingsLabelIcon.setAttribute("icon", "mdi:tune-variant");
    settingsBtn.appendChild(settingsLabel);
    settingsBtn.appendChild(settingsLabelIcon);
    settingsBtn.setAttribute("aria-controls", "voip-settings-panel");
    settingsBtn.setAttribute("aria-expanded", "false");
    runtimeControls.appendChild(settingsBtn);
    card.appendChild(runtimeControls);

    const settingsPanel = document.createElement("div");
    settingsPanel.className = "settings-panel";
    settingsPanel.id = "voip-settings-panel";
    settingsPanel.hidden = true;

    // Auto-answer toggle
    const autoAnswerRow = document.createElement("div");
    autoAnswerRow.className = "auto-answer-row";
    const autoAnswerCheckbox = document.createElement("input");
    autoAnswerCheckbox.type = "checkbox";
    autoAnswerCheckbox.id = "auto-answer-cb";
    const autoAnswerLabel = document.createElement("label");
    autoAnswerLabel.htmlFor = "auto-answer-cb";
    autoAnswerLabel.textContent = "Auto Answer";
    autoAnswerRow.appendChild(autoAnswerCheckbox);
    autoAnswerRow.appendChild(autoAnswerLabel);
    settingsPanel.appendChild(autoAnswerRow);

    const dndRow = document.createElement("div");
    dndRow.className = "auto-answer-row";
    const dndCheckbox = document.createElement("input");
    dndCheckbox.type = "checkbox";
    dndCheckbox.id = "ha-softphone-dnd-cb";
    const dndLabel = document.createElement("label");
    dndLabel.htmlFor = "ha-softphone-dnd-cb";
    dndLabel.textContent = "Do Not Disturb";
    dndRow.appendChild(dndCheckbox);
    dndRow.appendChild(dndLabel);
    settingsPanel.appendChild(dndRow);

    const ringtoneRow = document.createElement("div");
    ringtoneRow.className = "auto-answer-row";
    const ringtoneCheckbox = document.createElement("input");
    ringtoneCheckbox.type = "checkbox";
    ringtoneCheckbox.id = "ha-softphone-ringtone-cb";
    const ringtoneLabel = document.createElement("label");
    ringtoneLabel.htmlFor = "ha-softphone-ringtone-cb";
    ringtoneLabel.textContent = "Ringtone";
    ringtoneRow.appendChild(ringtoneCheckbox);
    ringtoneRow.appendChild(ringtoneLabel);
    settingsPanel.appendChild(ringtoneRow);

    const microphoneAntiAliasRow = document.createElement("div");
    microphoneAntiAliasRow.className = "auto-answer-row";
    microphoneAntiAliasRow.hidden = true;
    const microphoneAntiAliasCheckbox = document.createElement("input");
    microphoneAntiAliasCheckbox.type = "checkbox";
    microphoneAntiAliasCheckbox.id = "ha-softphone-microphone-anti-alias-cb";
    const microphoneAntiAliasLabel = document.createElement("label");
    microphoneAntiAliasLabel.htmlFor =
      "ha-softphone-microphone-anti-alias-cb";
    microphoneAntiAliasLabel.textContent = "Microphone anti-alias filter";
    microphoneAntiAliasRow.appendChild(microphoneAntiAliasCheckbox);
    microphoneAntiAliasRow.appendChild(microphoneAntiAliasLabel);
    settingsPanel.appendChild(microphoneAntiAliasRow);

    const videoCameraRow = document.createElement("div");
    videoCameraRow.className = "auto-answer-row";
    videoCameraRow.hidden = true;
    const videoCameraCheckbox = document.createElement("input");
    videoCameraCheckbox.type = "checkbox";
    videoCameraCheckbox.id = "ha-softphone-video-camera-cb";
    const videoCameraLabel = document.createElement("label");
    videoCameraLabel.htmlFor = "ha-softphone-video-camera-cb";
    videoCameraLabel.textContent = "Send Camera";
    videoCameraRow.appendChild(videoCameraCheckbox);
    videoCameraRow.appendChild(videoCameraLabel);
    settingsPanel.appendChild(videoCameraRow);

    const idleMediaDevices = buildMediaDeviceControls("voip-settings-media");
    settingsPanel.appendChild(idleMediaDevices.root);

    const softphoneGroupsPanel = document.createElement("div");
    softphoneGroupsPanel.className = "softphone-groups-panel";
    softphoneGroupsPanel.hidden = true;

    const extensionRow = document.createElement("div");
    extensionRow.className = "softphone-group-row";
    const extensionLabel = document.createElement("label");
    extensionLabel.htmlFor = "ha-softphone-extension";
    extensionLabel.textContent = "Extension";
    const extensionInput = document.createElement("input");
    extensionInput.type = "text";
    extensionInput.id = "ha-softphone-extension";
    extensionInput.inputMode = "numeric";
    extensionInput.autocomplete = "off";
    extensionRow.appendChild(extensionLabel);
    extensionRow.appendChild(extensionInput);
    softphoneGroupsPanel.appendChild(extensionRow);

    const ringGroupRow = document.createElement("div");
    ringGroupRow.className = "softphone-group-row";
    const ringGroupLabel = document.createElement("label");
    ringGroupLabel.htmlFor = "ha-softphone-ring-group";
    ringGroupLabel.textContent = "Ring Group";
    const ringGroupInput = document.createElement("input");
    ringGroupInput.type = "text";
    ringGroupInput.id = "ha-softphone-ring-group";
    ringGroupInput.setAttribute("list", "ha-softphone-ring-group-options");
    ringGroupInput.autocomplete = "off";
    const ringGroupOptions = document.createElement("datalist");
    ringGroupOptions.id = "ha-softphone-ring-group-options";
    ringGroupRow.appendChild(ringGroupLabel);
    ringGroupRow.appendChild(ringGroupInput);
    ringGroupRow.appendChild(ringGroupOptions);
    softphoneGroupsPanel.appendChild(ringGroupRow);

    const conferenceGroupRow = document.createElement("div");
    conferenceGroupRow.className = "softphone-group-row";
    const conferenceGroupLabel = document.createElement("label");
    conferenceGroupLabel.htmlFor = "ha-softphone-conference-group";
    conferenceGroupLabel.textContent = "Conference Group";
    const conferenceGroupInput = document.createElement("input");
    conferenceGroupInput.type = "text";
    conferenceGroupInput.id = "ha-softphone-conference-group";
    conferenceGroupInput.setAttribute("list", "ha-softphone-conference-group-options");
    conferenceGroupInput.autocomplete = "off";
    const conferenceGroupOptions = document.createElement("datalist");
    conferenceGroupOptions.id = "ha-softphone-conference-group-options";
    conferenceGroupRow.appendChild(conferenceGroupLabel);
    conferenceGroupRow.appendChild(conferenceGroupInput);
    conferenceGroupRow.appendChild(conferenceGroupOptions);
    softphoneGroupsPanel.appendChild(conferenceGroupRow);

    const conferenceRingRow = document.createElement("div");
    conferenceRingRow.className = "auto-answer-row";
    const conferenceRingCheckbox = document.createElement("input");
    conferenceRingCheckbox.type = "checkbox";
    conferenceRingCheckbox.id = "ha-softphone-conference-ring";
    const conferenceRingLabel = document.createElement("label");
    conferenceRingLabel.htmlFor = "ha-softphone-conference-ring";
    conferenceRingLabel.textContent = "Ring On Conference";
    conferenceRingRow.appendChild(conferenceRingCheckbox);
    conferenceRingRow.appendChild(conferenceRingLabel);
    softphoneGroupsPanel.appendChild(conferenceRingRow);
    settingsPanel.appendChild(softphoneGroupsPanel);
    card.appendChild(settingsPanel);

    const stats = document.createElement("div");
    stats.className = "stats";
    card.appendChild(stats);

    const err = document.createElement("div");
    err.className = "error";
    err.setAttribute("role", "alert");
    card.appendChild(err);

    const version = document.createElement("div");
    version.className = "version";
    version.textContent = "v" + cardVersion;
    card.appendChild(version);

    root.appendChild(card);

    this._els = {
      card, videoCanvas, nativeCameraHost, videoShade,
      header, headerName,
      destRow, destValueWrap, destValue, destSelect, prevBtn, nextBtn, offlinePanel,
      keypadPanel, keypadInput, keypadKeys,
      answerBtn, declineBtn, hangupBtn, hangupState, hangupPeer, hangupStats, hangupDuration, callBtn, placeholderBtn,
      statusIndicator, statusText, statusReason,
      runtimeControls, keypadBtn, settingsBtn, settingsPanel,
      autoAnswerRow, autoAnswerCheckbox, dndRow, dndCheckbox, ringtoneRow, ringtoneCheckbox,
      microphoneAntiAliasRow, microphoneAntiAliasCheckbox,
      videoCameraRow, videoCameraCheckbox,
      mediaDeviceViews: [idleMediaDevices],
      softphoneGroupsPanel, extensionRow, extensionInput, ringGroupInput, ringGroupOptions, conferenceGroupInput, conferenceGroupOptions, conferenceRingRow, conferenceRingCheckbox,
      stats, err,
    };

    this._attachEventHandlers();
    this._observeLayout();
  }

export function buildUnconfiguredCardSkeleton(cardVersion) {
    const root = this.shadowRoot;
    root.replaceChildren();

    const style = document.createElement("style");
    style.textContent = `
      :host { display: block; height: 100%; min-height: 0; }
      .card {
        box-sizing: border-box;
        height: 100%;
        min-height: 0;
        overflow: auto;
        background: var(--ha-card-background, var(--card-background-color, white));
        border-radius: var(--ha-card-border-radius, 12px);
        box-shadow: var(--ha-card-box-shadow, 0 2px 6px rgba(0,0,0,0.1));
        padding: 16px;
      }
      .header { font-size: 1.2em; font-weight: 500; margin-bottom: 16px; color: var(--primary-text-color); text-align: center; }
      .header[hidden] { display: none; }
      .unconfigured { text-align: center; color: var(--secondary-text-color); padding: 20px; font-style: italic; }
      .version { font-size: 0.65em; color: #999; text-align: right; margin-top: 8px; }
    `;
    root.appendChild(style);

    const card = document.createElement("ha-card");
    card.className = "card";
    installWheelScrollHandoff(card);

    const header = document.createElement("div");
    header.className = "header";
    const headerName = document.createTextNode("");
    header.appendChild(headerName);
    card.appendChild(header);

    const unconfigured = document.createElement("div");
    unconfigured.className = "unconfigured";
    unconfigured.textContent = "Please configure the card to select an VoIP device.";
    card.appendChild(unconfigured);

    const version = document.createElement("div");
    version.className = "version";
    version.textContent = "v" + cardVersion;
    card.appendChild(version);

    root.appendChild(card);

    this._els = { header, headerName };
    this._observeLayout();
  }

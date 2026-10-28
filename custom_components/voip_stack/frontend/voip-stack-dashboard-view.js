/** Static, theme-aware DOM for the operator dashboard. */
export function dashboardElement(tag, className = "", text = "") {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}

export function dashboardIcon(name) {
  const node = dashboardElement("ha-icon");
  node.setAttribute("icon", name);
  node.setAttribute("aria-hidden", "true");
  return node;
}

const STYLES = `
  :host { display:block; min-width:0; height:100%; min-height:0;
    --dash-surface:var(--ha-card-background,var(--card-background-color,#182330));
    --dash-ink:var(--primary-text-color,#eef3fa);
    --dash-muted:var(--secondary-text-color,#a9b5c5);
    --dash-line:var(--divider-color,rgba(190,208,230,.22));
    --dash-blue:var(--voip-dashboard-ringing-color,#168cff); --dash-red:var(--voip-dashboard-busy-color,#ee3249);
    --dash-columns:4; color:var(--dash-ink); }
  * { box-sizing:border-box; }
  [hidden] { display:none!important; }
  ha-card { display:flex; flex-direction:column; height:100%; min-height:240px; min-width:0;
    overflow:hidden; border:1px solid color-mix(in srgb,var(--dash-ink) 25%,transparent);
    border-radius:var(--ha-card-border-radius,18px);
    background:color-mix(in srgb,var(--dash-surface) 84%,transparent);
    box-shadow:var(--ha-card-box-shadow,0 14px 42px rgba(0,0,0,.18));
    -webkit-backdrop-filter:blur(15px) saturate(1.12); backdrop-filter:blur(15px) saturate(1.12); }
  .phone-view { flex:1 1 auto; min-height:0; overflow:hidden; }
  .phone-view voip-stack-card { display:block; height:100%; min-height:0; }
  .header-tools { margin-left:auto; flex:none; display:flex; align-items:center; gap:8px; }
  .header-tools .action { width:44px; height:44px; padding:0; }
  :host ha-card.video-mode .dashboard-header,:host ha-card.video-mode .notice { display:none; }
  .dashboard-header { flex:none; display:flex; align-items:center; gap:20px; padding:18px 22px;
    border-bottom:1px solid var(--dash-line); background:color-mix(in srgb,var(--dash-ink) 3%,transparent); }
  .identity { display:flex; align-items:center; gap:14px; min-width:0; flex:1 1 34%; }
  .identity-icon,.tile-icon { display:flex; align-items:center; justify-content:center; flex:none;
    border-radius:13px; background:color-mix(in srgb,var(--dash-ink) 13%,transparent); }
  .identity-icon { width:58px; height:58px; }
  .identity-icon ha-icon { --mdc-icon-size:34px; }
  .identity-copy { min-width:0; }
  h2 { margin:0; font-size:clamp(20px,2vw,28px); font-weight:650; line-height:1.22; overflow-wrap:anywhere; }
  .owner-name { margin-top:5px; color:var(--dash-muted); font-size:14px; overflow-wrap:anywhere; }
  .owner-call { flex:1 1 54%; min-width:0; display:flex; align-items:center; gap:14px; padding:12px 14px;
    border:1px solid var(--dash-line); border-radius:12px; background:color-mix(in srgb,var(--dash-ink) 4%,transparent); }
  .owner-call.ringing { border-color:var(--dash-blue); background:color-mix(in srgb,var(--dash-blue) 19%,transparent); }
  .owner-call.busy { border-color:color-mix(in srgb,var(--dash-red) 65%,transparent); background:color-mix(in srgb,var(--dash-red) 12%,transparent); }
  .owner-call-icon { display:flex; align-items:center; justify-content:center; width:50px; height:50px; flex:none;
    border-radius:11px; background:color-mix(in srgb,var(--dash-ink) 12%,transparent); }
  .owner-call-icon ha-icon { --mdc-icon-size:29px; }
  .owner-call.ringing .owner-call-icon { background:color-mix(in srgb,var(--dash-blue) 50%,transparent); }
  .call-copy { flex:1; min-width:0; }
  .call-label { color:var(--dash-muted); font-size:12px; }
  .call-peer { font-size:19px; font-weight:650; line-height:1.25; overflow-wrap:anywhere; }
  .call-number { margin-top:3px; font-size:13px; color:var(--dash-muted); }
  .call-actions { display:flex; gap:9px; flex:none; }
  button { font:inherit; color:inherit; cursor:pointer; }
  button:disabled { cursor:default; opacity:.52; }
  button:focus-visible,.scroller:focus-visible { outline:3px solid var(--primary-color,#3b9fff); outline-offset:3px; }
  .action { border:1px solid color-mix(in srgb,var(--dash-ink) 15%,transparent); border-radius:9px;
    min-height:44px; padding:9px 15px; display:inline-flex; align-items:center; justify-content:center; gap:8px;
    background:color-mix(in srgb,var(--dash-ink) 9%,transparent); font-size:14px; font-weight:600; }
  .action ha-icon { --mdc-icon-size:21px; }
  .answer,.answer-video { background:linear-gradient(130deg,#339d4c,#17743a); color:white; border-color:#43b961; }
  .decline,.hangup { background:linear-gradient(130deg,#c32e43,#8e172a); color:white; border-color:#ec4057; }
  .notice { margin:0; padding:10px 22px; color:var(--dash-muted); border-bottom:1px solid var(--dash-line); font-size:13px; }
  .notice.error { color:var(--error-color,#ef6170); }
  .scroller { flex:1 1 auto; min-height:0; overflow-y:auto; overflow-x:hidden; padding:16px 22px 22px;
    overscroll-behavior-y:auto; scrollbar-gutter:stable; touch-action:pan-y; }
  .section + .section { border-top:1px solid var(--dash-line); margin-top:22px; padding-top:16px; }
  .section-header { margin-bottom:12px; }
  h3 { font-size:19px; font-weight:600; line-height:1.3; margin:0; }
  .section-description { margin:3px 0 0; color:var(--dash-muted); font-size:13px; }
  .grid { display:grid; grid-template-columns:repeat(var(--dash-columns),minmax(0,1fr)); gap:12px; align-items:stretch; }
  .empty { color:var(--dash-muted); margin:16px 0; font-size:14px; }
  .tile { display:flex; flex-direction:column; min-width:0; min-height:166px; overflow:hidden; border:1px solid color-mix(in srgb,var(--dash-ink) 28%,transparent);
    border-radius:14px; background:color-mix(in srgb,var(--dash-ink) 5%,transparent);
    transition:border-color .15s ease,background-color .15s ease; }
  .tile.ringing { border-color:var(--dash-blue); background:linear-gradient(130deg,color-mix(in srgb,var(--dash-blue) 36%,transparent),color-mix(in srgb,var(--dash-blue) 17%,transparent)); }
  .tile.busy { border-color:var(--dash-red); background:linear-gradient(130deg,color-mix(in srgb,var(--dash-red) 37%,transparent),color-mix(in srgb,var(--dash-red) 17%,transparent)); }
  .tile.offline { color:var(--dash-muted); background:color-mix(in srgb,var(--dash-ink) 2%,transparent); }
  .tile.stale { opacity:.65; }
  .tile.contact { min-height:94px; }
  .tile-main { display:flex; flex-direction:column; align-items:stretch; text-align:left; width:100%; flex:1;
    padding:15px; border:0; background:transparent; color:inherit; min-width:0; border-radius:13px; }
  .tile-main:hover { background:color-mix(in srgb,var(--dash-ink) 4%,transparent); }
  .tile-head { display:flex; align-items:center; gap:12px; min-width:0; }
  .tile-icon { position:relative; width:48px; height:48px; }
  .tile-icon ha-icon { --mdc-icon-size:29px; }
  .tile-icon .availability-icon { position:absolute; right:-4px; bottom:-4px; --mdc-icon-size:17px;
    width:20px; height:20px; border-radius:50%; background:var(--dash-surface); color:var(--success-color,#53df64); }
  .tile-icon .availability-icon.offline { color:var(--dash-muted); }
  .tile.ringing .tile-icon { background:color-mix(in srgb,var(--dash-blue) 52%,transparent); }
  .tile.busy .tile-icon { background:color-mix(in srgb,var(--dash-red) 50%,transparent); }
  .tile.offline .tile-icon { opacity:.5; }
  .tile-name-copy { min-width:0; flex:1; }
  .tile-name { display:block; font-size:16px; font-weight:650; line-height:1.2; overflow-wrap:anywhere; }
  .tile-extension { display:block; margin-top:5px; color:var(--dash-muted); font-size:13px; overflow-wrap:anywhere; }
  .tile-badges { display:flex; flex-wrap:wrap; justify-content:flex-end; align-self:flex-start; gap:5px; max-width:43%; }
  .badge { display:inline-flex; align-items:center; gap:4px; border-radius:7px; font-size:11px; padding:4px 6px; line-height:1.15;
    background:color-mix(in srgb,var(--dash-ink) 12%,transparent); color:var(--dash-ink); white-space:nowrap; }
  .badge ha-icon { --mdc-icon-size:15px; }
  .owner-badge { background:color-mix(in srgb,var(--dash-blue) 55%,transparent); }
  .mail-badge { background:color-mix(in srgb,var(--dash-blue) 65%,transparent); }
  .ringing-bell { --mdc-icon-size:25px; align-self:center; color:var(--dash-blue); }
  .tile-status { display:flex; gap:8px; align-items:flex-start; margin-top:16px; min-height:20px; font-size:14px; line-height:1.35; overflow-wrap:anywhere; }
  .status-dot { flex:none; width:11px; height:11px; border-radius:50%; margin-top:4px; background:var(--success-color,#53df64); }
  .tile.offline .status-dot,.tile.dnd .status-dot { background:var(--dash-muted); }
  .tile.ringing .status-dot { background:var(--dash-blue); }
  .tile.busy .status-dot { background:var(--dash-red); }
  .tile-status-copy { min-width:0; }
  .held-line,.transfer-line { align-self:flex-start; display:flex; align-items:center; gap:7px; margin-top:7px; padding:4px 7px;
    border-radius:7px; color:var(--warning-color,#e9b542); background:color-mix(in srgb,var(--warning-color,#e9b542) 12%,transparent); font-size:12px; }
  .held-line ha-icon,.transfer-line ha-icon { --mdc-icon-size:17px; }
  .transfer-line { color:var(--dash-ink); background:color-mix(in srgb,var(--dash-ink) 10%,transparent); }
  .tile-actions { padding:0 14px 13px; }
  .pickup { width:100%; background:color-mix(in srgb,var(--dash-blue) 63%,transparent); border-color:color-mix(in srgb,var(--dash-blue) 80%,transparent); }
  dialog { color:var(--dash-ink); width:min(460px,calc(100vw - 32px)); max-height:80vh; padding:20px;
    border:1px solid var(--dash-line); border-radius:16px; background:var(--dash-surface);
    box-shadow:0 18px 70px rgba(0,0,0,.35); overflow:auto; }
  dialog::backdrop { background:rgba(0,0,0,.32); -webkit-backdrop-filter:blur(4px); backdrop-filter:blur(4px); }
  .details-heading { display:flex; align-items:center; justify-content:space-between; gap:14px; }
  .details-title { margin:0; font-size:20px; }
  .close { min-width:44px; min-height:44px; padding:8px; }
  .details-summary { color:var(--dash-muted); margin:2px 0 14px; font-size:13px; }
  .details-row { border-top:1px solid var(--dash-line); padding:14px 0; }
  .details-peer { font-weight:600; overflow-wrap:anywhere; }
  .details-state { color:var(--dash-muted); margin-top:4px; font-size:13px; }
  .details-actions { margin-top:10px; display:flex; gap:8px; flex-wrap:wrap; }
  :host(.compact) .dashboard-header { display:grid; grid-template-columns:minmax(0,1fr) auto; gap:12px; padding:15px; }
  :host(.compact) .identity { flex-basis:auto; }
  :host(.compact) .owner-call { flex-basis:auto; grid-column:1 / -1; }
  :host(.compact) .header-tools { grid-column:2; grid-row:1; }
  :host(.compact) .scroller { padding:14px; }
  :host(.narrow) .owner-call { flex-wrap:wrap; }
  :host(.narrow) .call-actions { width:100%; }
  :host(.narrow) .call-actions .action { flex:1; }
  :host(.narrow) .tile-main { padding:12px; }
  :host(.narrow) .tile-head { display:grid; grid-template-columns:38px minmax(0,1fr); gap:9px; }
  :host(.narrow) .tile-icon { width:38px; height:38px; border-radius:10px; }
  :host(.narrow) .tile-name { font-size:14px; }
  :host(.narrow) .tile-badges { grid-column:1 / -1; justify-content:flex-start; max-width:100%; }
  :host(.narrow) .tile-badges:not(:has(> :not([hidden]))) { display:none; }
  @media (prefers-reduced-motion:reduce) { .tile { transition:none; } }
`;

function action(label, icon, className) {
  const button = dashboardElement("button", `action ${className}`);
  button.type = "button";
  button.append(dashboardIcon(icon), dashboardElement("span", "", label));
  return button;
}

export function buildDashboardSkeleton(shadow, t, callbacks) {
  shadow.replaceChildren();
  const style = dashboardElement("style"); style.textContent = STYLES;
  const card = dashboardElement("ha-card");
  const header = dashboardElement("header", "dashboard-header");
  const identity = dashboardElement("div", "identity");
  const identityIcon = dashboardElement("span", "identity-icon"); const ownerIcon = dashboardIcon("mdi:phone"); identityIcon.append(ownerIcon);
  const identityCopy = dashboardElement("div", "identity-copy");
  const title = dashboardElement("h2"); const ownerName = dashboardElement("div", "owner-name");
  identityCopy.append(title, ownerName); identity.append(identityIcon, identityCopy);
  const ownerCall = dashboardElement("section", "owner-call");
  const callIconBox = dashboardElement("span", "owner-call-icon"); const callIcon = dashboardIcon("mdi:phone-incoming"); callIconBox.append(callIcon);
  const callCopy = dashboardElement("div", "call-copy");
  const callLabel = dashboardElement("div", "call-label"); const callPeer = dashboardElement("div", "call-peer");
  const callNumber = dashboardElement("div", "call-number"); callCopy.append(callLabel, callPeer, callNumber);
  const callActions = dashboardElement("div", "call-actions");
  const answer = action(t("Answer"), "mdi:phone", "answer"); answer.onclick = () => callbacks.command("answer");
  const answerVideo = action(t("Answer with video"), "mdi:video", "answer-video"); answerVideo.onclick = () => callbacks.command("answer_video");
  const decline = action(t("Decline"), "mdi:phone-hangup", "decline"); decline.onclick = () => callbacks.command("decline");
  const hangup = action(t("Hang up"), "mdi:phone-hangup", "hangup"); hangup.onclick = () => callbacks.command("hangup");
  const moreCalls = action(t("Call details"), "mdi:phone-log", "more-calls"); moreCalls.onclick = callbacks.ownerDetails;
  callActions.append(answerVideo, answer, decline, hangup, moreCalls); ownerCall.append(callIconBox, callCopy, callActions);
  const tools = dashboardElement("div", "header-tools");
  const options = action("", "mdi:cog", "options"); options.onclick = callbacks.options;
  options.title = t("Options"); options.setAttribute("aria-label", t("Options"));
  const keypad = action("", "mdi:dialpad", "keypad"); keypad.onclick = callbacks.keypad;
  keypad.title = t("Keypad"); keypad.setAttribute("aria-label", t("Keypad"));
  tools.append(keypad, options); header.append(identity, ownerCall, tools);
  const notice = dashboardElement("p", "notice"); notice.setAttribute("role", "status"); notice.setAttribute("aria-live", "polite");
  const scroller = dashboardElement("div", "scroller"); scroller.tabIndex = 0;
  scroller.setAttribute("aria-label", `${t("Extensions")}, ${t("Contacts")}`);
  const sections = {};
  for (const [key, heading, description] of [["internals", "Extensions", "Registered endpoints on your system"], ["contacts", "Contacts", "Saved numbers, groups and automations"]]) {
    const section = dashboardElement("section", "section"); const sectionHeader = dashboardElement("div", "section-header");
    const headingNode = dashboardElement("h3"); headingNode.id = `dashboard-${key}-heading`; section.setAttribute("aria-labelledby", headingNode.id);
    sectionHeader.append(headingNode, dashboardElement("p", "section-description", t(description)));
    const grid = dashboardElement("div", "grid"); grid.setAttribute("role", "list");
    const empty = dashboardElement("p", "empty", t(key === "internals" ? "No extensions available." : "No saved contacts."));
    section.append(sectionHeader, grid, empty); scroller.append(section); sections[key] = { section, heading: headingNode, grid, empty, label: heading };
  }
  const dialog = dashboardElement("dialog", "details-dialog");
  const detailsHeading = dashboardElement("div", "details-heading"); const detailsTitle = dashboardElement("h3", "details-title");
  detailsTitle.id = "dashboard-details-title"; dialog.setAttribute("aria-labelledby", detailsTitle.id);
  const close = action("", "mdi:close", "close"); close.setAttribute("aria-label", t("Close")); close.onclick = () => dialog.close();
  detailsHeading.append(detailsTitle, close); const detailsSummary = dashboardElement("p", "details-summary"); const detailsRows = dashboardElement("div");
  dialog.append(detailsHeading, detailsSummary, detailsRows);
  dialog.addEventListener("click", (event) => { if (event.target === dialog) { const box = dialog.getBoundingClientRect(); if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) dialog.close(); } });
  const phoneHost = dashboardElement("div", "phone-view"); phoneHost.hidden = true;
  card.append(header, notice, scroller, phoneHost, dialog); shadow.append(style, card);
  return { card, title, ownerName, ownerIcon, ownerCall, callIcon, callLabel, callPeer, callNumber, answer, answerVideo, decline, hangup, moreCalls, options, keypad, phoneHost, notice, scroller, sections, dialog, detailsTitle, detailsSummary, detailsRows };
}

export function buildDashboardTile(key, callbacks) {
  const root = dashboardElement("article", "tile"); root.setAttribute("role", "listitem"); root.dataset.key = key;
  const main = dashboardElement("button", "tile-main"); main.type = "button"; main.onclick = () => callbacks.select(key);
  const head = dashboardElement("span", "tile-head"); const iconBox = dashboardElement("span", "tile-icon"); const icon = dashboardIcon("mdi:phone");
  const availability = dashboardIcon("mdi:check-circle"); availability.className = "availability-icon"; iconBox.append(icon, availability);
  const copy = dashboardElement("span", "tile-name-copy"); const name = dashboardElement("span", "tile-name"); const extension = dashboardElement("span", "tile-extension"); copy.append(name, extension);
  const badges = dashboardElement("span", "tile-badges"); const owner = dashboardElement("span", "badge owner-badge");
  const ringingBell = dashboardIcon("mdi:bell-ring"); ringingBell.className = "ringing-bell";
  const mail = dashboardElement("span", "badge mail-badge"); const mailCount = dashboardElement("span"); mail.append(dashboardIcon("mdi:email"), mailCount);
  const callCount = dashboardElement("span", "badge call-count"); badges.append(owner, mail, callCount, ringingBell); head.append(iconBox, copy, badges);
  const status = dashboardElement("span", "tile-status"); const dot = dashboardElement("span", "status-dot"); dot.setAttribute("aria-hidden", "true");
  const statusCopy = dashboardElement("span", "tile-status-copy"); status.append(dot, statusCopy);
  const held = dashboardElement("span", "held-line"); const heldCopy = dashboardElement("span"); held.append(dashboardIcon("mdi:pause-circle"), heldCopy);
  const attended = dashboardElement("span", "transfer-line"); const attendedCopy = dashboardElement("span"); attended.append(dashboardIcon("mdi:call-split"), attendedCopy);
  main.append(head, status, held, attended);
  const actions = dashboardElement("div", "tile-actions"); const pickup = action("", "mdi:phone", "pickup"); const pickupLabel = pickup.querySelector("span"); pickup.onclick = () => callbacks.pickup(key); actions.append(pickup);
  root.append(main, actions);
  return { root, main, icon, availability, name, extension, owner, mail, mailCount, callCount, ringingBell, status, dot, statusCopy, held, heldCopy, attended, attendedCopy, actions, pickup, pickupLabel };
}

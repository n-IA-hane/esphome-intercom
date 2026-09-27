const version = new URL(import.meta.url).searchParams.get("v") || "dev";
const composerUrl = new URL(`./voip-stack-composer.js?v=${encodeURIComponent(version)}`, import.meta.url).href;
const cardUrl = new URL(`./voip-stack-card.js?v=${encodeURIComponent(version)}`, import.meta.url).href;

// Dashboard resources use the proposed frontend API only when it is available.
// Older frontends retain their existing cards without DOM injection.
await customElements.whenDefined("home-assistant");
window.customDashboardActions?.register({
  id: "voip_stack:phone",
  icon: "mdi:phone",
  label: () => "VoIP Stack",
  visible: ({ hass }) => hass.config.components.includes("voip_stack"),
  execute: async ({ showDialog }) => {
    await import(cardUrl);
    await showDialog({
      dialogTag: "voip-stack-composer",
      dialogImport: () => import(composerUrl),
      dialogParams: {},
    });
  },
});

/** Load Home Assistant's native form through its public card helpers. */
let loading = null;

export async function ensureNativeForm() {
  if (customElements.get("ha-form") || !globalThis.loadCardHelpers) return;
  if (!loading) {
    loading = (async () => {
      const helpers = await globalThis.loadCardHelpers();
      const card = helpers.createCardElement({ type: "entities", entities: [] });
      await card.constructor.getConfigElement();
    })().finally(() => { loading = null; });
  }
  await loading;
}

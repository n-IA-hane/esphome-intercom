/** Share device and entity identities for one connection and native registry. */
const catalogs = new WeakMap();
const observedConnections = new WeakSet();

function readCatalog(connection, registry, force) {
  if (!observedConnections.has(connection)) {
    observedConnections.add(connection);
    connection.addEventListener?.("ready", () => catalogs.delete(connection));
  }
  let request = catalogs.get(connection);
  if (!request || request.registry !== registry || force) {
    request = { registry };
    catalogs.set(connection, request);
    request.promise = new Promise((resolve) => {
      resolve(connection.sendMessagePromise({ type: "voip_stack/list_devices" }));
    })
      .catch((error) => {
        if (catalogs.get(connection) === request) catalogs.delete(connection);
        throw error;
      });
  }
  return request;
}

export class VoipEntityIds {
  constructor() {
    this.phonebook = null;
    this.devices = [];
    this.loaded = false;
    this.error = "";
    this._connection = null;
    this._registry = null;
    this._pending = null;
    this._request = null;
    this._catalog = null;
    this._wasPresent = false;
  }

  refresh(hass = this._latestHass, changed = this._changed) {
    return this.update(hass, changed, true);
  }

  update(hass, changed, force = false) {
    this._latestHass = hass;
    this._changed = changed;
    const connection = hass?.connection;
    if (!connection?.sendMessagePromise) return this._pending;
    const present = !!hass.states?.[this.phonebook];
    const differentConnection = connection !== this._connection;
    const removed = !differentConnection && this._wasPresent && !present;
    const changedCatalog = catalogs.get(connection) !== this._catalog;
    const refresh = force || differentConnection || hass.entities !== this._registry || removed ||
      changedCatalog;
    // Presence from the previous connection cannot remove the new catalog.
    this._wasPresent = !differentConnection && present;
    if (!refresh) return this._pending;
    this._connection = connection;
    this._registry = hass.entities;
    const registry = hass.entities;
    const request = this._request = {};
    if (differentConnection) {
      this.phonebook = null;
      this.devices = [];
      this.loaded = false;
    }
    this.error = "";
    const catalog = this._catalog = readCatalog(connection, registry, force || (removed && !changedCatalog));
    this._pending = catalog.promise
      .then((result) => {
        if (this._request !== request || catalogs.get(connection) !== catalog ||
            this._latestHass?.connection !== connection ||
            this._latestHass?.entities !== registry) return;
        // Older integration versions do not advertise stable entity identities.
        this.phonebook = result?.entity_ids
          ? result.entity_ids.phonebook || null : "sensor.voip_phonebook";
        this.devices = result?.devices || [];
        this.loaded = true;
        this.error = "";
        this._changed?.();
      })
      .catch((error) => {
        if (this._request !== request || (catalogs.has(connection) && catalogs.get(connection) !== catalog)) return;
        this.error = error?.message || String(error);
        this._connection = null; // Retry on the next HA update or explicit refresh.
        this._changed?.();
      })
      .finally(() => {
        if (this._request !== request) return;
        this._pending = null;
        this._request = null;
        const current = catalogs.get(connection);
        if (current && current !== catalog && current.registry === this._latestHass?.entities) {
          this.update(this._latestHass, this._changed);
        }
      });
    return this._pending;
  }
}

/** Pure logical-phone identity rules for the page-level softphone engine. */

export function normaliseSoftphoneSelector(selector = {}) {
  const deviceId = String(selector?.device_id || "").trim();
  const endpointId = String(selector?.endpoint_id || "").trim();
  return { endpoint_id: endpointId, device_id: deviceId };
}

export function softphoneScopeKey(selector = {}) {
  const normalised = normaliseSoftphoneSelector(selector);
  return normalised.endpoint_id
    ? `endpoint:${normalised.endpoint_id}`
    : normalised.device_id
      ? `device:${normalised.device_id}`
      : "preferred";
}

export function softphoneStateMatches(
  state,
  selector = {},
) {
  if (!state) return false;
  const wanted = normaliseSoftphoneSelector(selector);
  const stateEndpoint = String(state.endpoint_id || "").trim();
  const stateDevice = String(
    state.device_id || state.endpoint_device_id || "",
  ).trim();
  if (wanted.endpoint_id) {
    return !!stateEndpoint && stateEndpoint === wanted.endpoint_id;
  }
  if (wanted.device_id) {
    return !!stateDevice && stateDevice === wanted.device_id;
  }
  return false;
}

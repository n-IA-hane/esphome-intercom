export function emptyVideoStats() {
  return {
    received: 0,
    sent: 0,
    rendered: 0,
    dropped: 0,
    dropped_no_canvas: 0,
    dropped_decode_backpressure: 0,
    dropped_timestamp_regression: 0,
    dropped_frame_queue: 0,
    dropped_render_coalesce: 0,
    dropped_pending_decode: 0,
    decode_errors: 0,
    max_frame_gap_ms: 0,
    max_arrival_gap_ms: 0,
    max_source_gap_ms: 0,
    render_gaps_over_100_ms: 0,
    render_gaps_over_250_ms: 0,
    playout_ms: 0,
  };
}

export function directionalVideoContract(negotiated, direction) {
  const nested = negotiated?.[direction];
  const media = nested && typeof nested === "object" && !Array.isArray(nested)
    ? nested
    : {};
  const value = (key, fallback = undefined) => media[key] ?? fallback;
  const rawCodec = String(value("codec", "") || "");
  const codecToken = rawCodec.toLowerCase();
  const inferredEncoding = codecToken.startsWith("avc1")
    ? "H264"
    : codecToken.startsWith("vp")
      ? "VP8"
      : codecToken.includes("jpeg")
        ? "JPEG"
        : "H264";
  const encoding = String(
    value("encoding", inferredEncoding) || inferredEncoding,
  ).toUpperCase();
  const defaultCodec = encoding === "VP8"
    ? "vp8"
    : encoding === "JPEG"
      ? "jpeg"
      : "avc1.42E01F";
  const rawClockRate = Number(value("clock_rate", 90000));
  const clockRate = Number.isFinite(rawClockRate) && rawClockRate > 0
    ? rawClockRate
    : 90000;
  const rawPayloadType = Number(value("payload_type", -1));
  const rawMaxFramerate = Number(value("max_framerate", 0));
  return {
    codec: rawCodec || defaultCodec,
    encoding,
    clockRate,
    payloadType: Number.isInteger(rawPayloadType) && rawPayloadType >= 0 && rawPayloadType <= 127
      ? rawPayloadType
      : -1,
    fmtp: String(value("fmtp", "") || ""),
    profileLevelId: String(
      value(
        "profile_level_id",
        encoding === "H264" ? (rawCodec || defaultCodec).split(".").at(-1) : "",
      ) || "",
    ).toLowerCase(),
    packetizationMode: Number(value("packetization_mode", 0)) || 0,
    maxFramerate: Number.isFinite(rawMaxFramerate) && rawMaxFramerate > 0
      ? rawMaxFramerate
      : null,
    format: String(value("format", "") || ""),
  };
}

function positiveInteger(value) {
  const parsed = Number.parseInt(String(value || ""), 10);
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : null;
}

export function cameraCaptureContract(send) {
  const parameters = new Map();
  for (const item of String(send?.fmtp || "").split(";")) {
    const [rawKey, ...rawValue] = item.trim().split("=");
    const key = String(rawKey || "").trim().toLowerCase();
    if (key) parameters.set(key, rawValue.join("=").trim());
  }

  let maxFs = 3600;
  let maxMbps = 108000;
  let maxFr = 20;
  if (send?.encoding === "H264") {
    const profileLevelId = String(
      send.profileLevelId || String(send.codec || "").split(".").at(-1) || "",
    ).toLowerCase();
    const profileIop = Number.parseInt(profileLevelId.slice(2, 4), 16);
    const levelIdc = Number.parseInt(profileLevelId.slice(-2), 16);
    const levelLimits = new Map([
      [0x0a, [99, 1485]],
      [0x0b, [396, 3000]],
      [0x0c, [396, 6000]],
      [0x0d, [396, 11880]],
      [0x14, [396, 11880]],
      [0x15, [792, 19800]],
      [0x16, [1620, 20250]],
      [0x1e, [1620, 40500]],
      [0x1f, [3600, 108000]],
      [0x20, [5120, 216000]],
      [0x28, [8192, 245760]],
      [0x29, [8192, 245760]],
      [0x2a, [8704, 522240]],
      [0x32, [22080, 589824]],
      [0x33, [36864, 983040]],
      [0x34, [36864, 2073600]],
    ]);
    const limits = levelIdc === 0x0b && (profileIop & 0x10)
      ? [99, 1485]
      : levelLimits.get(levelIdc);
    if (limits) [maxFs, maxMbps] = limits;
    maxFs = Math.min(maxFs, positiveInteger(parameters.get("max-fs")) || maxFs);
    maxMbps = Math.min(
      maxMbps,
      positiveInteger(parameters.get("max-mbps")) || maxMbps,
    );
  } else if (send?.encoding === "VP8") {
    maxFs = positiveInteger(parameters.get("max-fs")) || maxFs;
    maxFr = Math.min(maxFr, positiveInteger(parameters.get("max-fr")) || maxFr);
  }
  if (Number.isFinite(Number(send?.maxFramerate)) && Number(send.maxFramerate) > 0) {
    maxFr = Math.min(maxFr, Number(send.maxFramerate));
  }

  const candidates = [
    [1280, 720],
    [960, 540],
    [640, 360],
    [480, 270],
    [352, 288],
    [320, 180],
    [176, 144],
  ].map(([width, height]) => ({
    width,
    height,
    macroblocks: Math.ceil(width / 16) * Math.ceil(height / 16),
  }));
  const fitting = candidates.filter((item) => item.macroblocks <= maxFs);
  const maximum = fitting[0] || candidates.at(-1);
  const ideal = fitting.find(
    (item) => item.width <= 640 && item.height <= 360,
  ) || maximum;
  maxFr = Math.max(
    1,
    Math.min(maxFr, Math.floor(maxMbps / Math.max(1, maximum.macroblocks))),
  );
  return {
    maxFs,
    maxMbps,
    maxFr,
    idealWidth: ideal.width,
    idealHeight: ideal.height,
    maxWidth: maximum.width,
    maxHeight: maximum.height,
    constraints: {
      width: { ideal: ideal.width, max: maximum.width },
      height: { ideal: ideal.height, max: maximum.height },
      frameRate: { ideal: Math.min(15, maxFr), max: maxFr },
    },
  };
}

export function cameraEncoderContract(capture, settings, encoding) {
  const capturedWidth = Math.max(
    16,
    Number(settings?.width || capture.idealWidth) & ~1,
  );
  const capturedHeight = Math.max(
    16,
    Number(settings?.height || capture.idealHeight) & ~1,
  );
  // Mobile browsers commonly rotate the camera track after applying
  // getUserMedia constraints. Preserve the resulting orientation: forcing a
  // portrait VideoFrame into the landscape dimensions selected before
  // capture makes WebCodecs rescale and distort the pixels. H.264 level
  // admission is based on macroblocks, so the equivalent rotated envelope
  // remains standards-compliant without an extra resize.
  const width = capturedWidth;
  const height = capturedHeight;
  const macroblocks = Math.ceil(width / 16) * Math.ceil(height / 16);
  const framerate = Math.max(
    1,
    Math.min(
      capture.maxFr,
      Math.floor(capture.maxMbps / Math.max(1, macroblocks)),
      Number(settings?.frameRate || capture.maxFr),
    ),
  );
  return { width, height, macroblocks, framerate };
}

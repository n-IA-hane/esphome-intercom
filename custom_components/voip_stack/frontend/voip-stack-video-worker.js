// SIP video codec worker: WebCodecs receive and browser-camera JPEG transmit.
//
// Codec work must never share Home Assistant's UI event loop: a slow remote
// stream or canvas encode must not prevent the user from pressing Hang up.

let decoder = null;
let generation = 0;
let workerRole = "";
let jpegCanvas = null;
let jpegContext = null;
let jpegQuality = 0.72;
let jpegBusy = false;
let jpegDecodeBusy = false;
let jpegDecodeErrorCount = 0;
let jpegDecodeErrorReported = 0;
let jpegDecodeErrorTimer = 0;
let jpegDecodeErrorLastReportAt = Number.NEGATIVE_INFINITY;
let jpegDecodeLastError = "";
const JPEG_DECODER_ERROR_REPORT_INTERVAL_MS = 250;
const JPEG_MAX_DECODE_DIMENSION = 1280;
const JPEG_MAX_DECODE_PIXELS = 1280 * 800;
const JPEG_SOF_MARKERS = new Set([
  0xc0, 0xc1, 0xc2, 0xc3,
  0xc5, 0xc6, 0xc7,
  0xc9, 0xca, 0xcb,
  0xcd, 0xce, 0xcf,
]);

function jpegDimensions(payload) {
  if (
    payload.length < 4 ||
    payload[0] !== 0xff ||
    payload[1] !== 0xd8
  ) return null;

  let offset = 2;
  while (offset < payload.length) {
    while (offset < payload.length && payload[offset] === 0xff) offset++;
    if (offset >= payload.length) return null;
    const marker = payload[offset++];
    if (marker === 0x00) continue;
    if (marker === 0xd9 || marker === 0xda) return null;
    if (
      marker === 0x01 ||
      marker === 0xd8 ||
      (marker >= 0xd0 && marker <= 0xd7)
    ) continue;
    if (offset + 2 > payload.length) return null;
    const segmentLength = (payload[offset] << 8) | payload[offset + 1];
    if (
      segmentLength < 2 ||
      offset + segmentLength > payload.length
    ) return null;
    if (JPEG_SOF_MARKERS.has(marker)) {
      if (segmentLength < 8) return null;
      const height = (payload[offset + 3] << 8) | payload[offset + 4];
      const width = (payload[offset + 5] << 8) | payload[offset + 6];
      return width > 0 && height > 0 ? { width, height } : null;
    }
    offset += segmentLength;
  }
  return null;
}

function closeDecoder() {
  if (decoder && decoder.state !== "closed") {
    try { decoder.close(); } catch (_) {}
  }
  decoder = null;
}

function closeJpegEncoder() {
  jpegCanvas = null;
  jpegContext = null;
  jpegBusy = false;
}

function closeJpegDecoder() {
  jpegDecodeBusy = false;
  if (jpegDecodeErrorTimer) {
    clearTimeout(jpegDecodeErrorTimer);
    jpegDecodeErrorTimer = 0;
  }
  jpegDecodeErrorCount = 0;
  jpegDecodeErrorReported = 0;
  jpegDecodeErrorLastReportAt = Number.NEGATIVE_INFINITY;
  jpegDecodeLastError = "";
}

function jpegDecodeNow() {
  return globalThis.performance?.now?.() ?? Date.now();
}

function flushJpegDecodeErrors(ownedGeneration) {
  jpegDecodeErrorTimer = 0;
  if (
    workerRole !== "jpeg_decoder" ||
    ownedGeneration !== generation ||
    jpegDecodeErrorCount <= jpegDecodeErrorReported
  ) return;
  jpegDecodeErrorReported = jpegDecodeErrorCount;
  jpegDecodeErrorLastReportAt = jpegDecodeNow();
  self.postMessage({
    type: "jpeg_decoder_error",
    generation: ownedGeneration,
    error_count: jpegDecodeErrorReported,
    error: jpegDecodeLastError,
  });
}

function reportJpegDecodeError(ownedGeneration, error) {
  jpegDecodeErrorCount++;
  jpegDecodeLastError = error?.message || String(error);
  const delay = JPEG_DECODER_ERROR_REPORT_INTERVAL_MS
    - (jpegDecodeNow() - jpegDecodeErrorLastReportAt);
  if (jpegDecodeErrorReported === 0 || delay <= 0) {
    flushJpegDecodeErrors(ownedGeneration);
    return;
  }
  if (jpegDecodeErrorTimer) return;
  jpegDecodeErrorTimer = setTimeout(
    () => flushJpegDecodeErrors(ownedGeneration),
    delay,
  );
}

function reply(requestId, ok, detail = {}) {
  self.postMessage({ type: "reply", requestId, ok, ...detail });
}

self.onmessage = async (event) => {
  const message = event.data || {};
  if (message.type === "close") {
    generation++;
    closeDecoder();
    closeJpegEncoder();
    closeJpegDecoder();
    self.close();
    return;
  }
  if (message.type === "configure_jpeg_encoder") {
    const requestId = Number(message.requestId || 0);
    const nextGeneration = Number(message.generation || 0);
    try {
      closeDecoder();
      closeJpegEncoder();
      closeJpegDecoder();
      if (typeof OffscreenCanvas === "undefined") {
        throw new Error("OffscreenCanvas is unavailable in JPEG worker");
      }
      const width = Math.max(1, Number(message.width || 0));
      const height = Math.max(1, Number(message.height || 0));
      jpegCanvas = new OffscreenCanvas(width, height);
      jpegContext = jpegCanvas.getContext("2d", {
        alpha: false,
        desynchronized: true,
      });
      if (!jpegContext || typeof jpegCanvas.convertToBlob !== "function") {
        throw new Error("Worker JPEG canvas encoder is unavailable");
      }
      jpegQuality = Math.max(0.1, Math.min(1, Number(message.quality || 0.72)));
      generation = nextGeneration;
      workerRole = "jpeg";
      reply(requestId, true);
    } catch (error) {
      closeJpegEncoder();
      reply(requestId, false, { error: error?.message || String(error) });
    }
    return;
  }
  if (message.type === "encode_jpeg") {
    const frame = message.frame;
    const ownedGeneration = Number(message.generation || 0);
    const senderGeneration = Number(message.senderGeneration || 0);
    if (
      workerRole !== "jpeg" ||
      !jpegCanvas ||
      !jpegContext ||
      ownedGeneration !== generation ||
      !frame
    ) {
      frame?.close?.();
      return;
    }
    if (jpegBusy) {
      frame.close();
      self.postMessage({
        type: "jpeg_error",
        generation: ownedGeneration,
        senderGeneration,
        error: "JPEG worker received more than one in-flight frame",
      });
      return;
    }
    jpegBusy = true;
    let frameClosed = false;
    try {
      const width = Math.max(
        1,
        Number(frame.displayWidth || frame.codedWidth || jpegCanvas.width),
      );
      const height = Math.max(
        1,
        Number(frame.displayHeight || frame.codedHeight || jpegCanvas.height),
      );
      if (jpegCanvas.width !== width) jpegCanvas.width = width;
      if (jpegCanvas.height !== height) jpegCanvas.height = height;
      jpegContext.drawImage(frame, 0, 0, width, height);
      frame.close();
      frameClosed = true;
      const blob = await jpegCanvas.convertToBlob({
        type: "image/jpeg",
        quality: jpegQuality,
      });
      const buffer = await blob.arrayBuffer();
      if (
        workerRole !== "jpeg" ||
        ownedGeneration !== generation
      ) return;
      self.postMessage({
        type: "jpeg_frame",
        generation: ownedGeneration,
        senderGeneration,
        timestamp: Number(message.timestamp || 0),
        buffer,
      }, [buffer]);
    } catch (error) {
      if (!frameClosed) {
        try { frame?.close?.(); } catch (_) {}
      }
      if (workerRole === "jpeg" && ownedGeneration === generation) {
        self.postMessage({
          type: "jpeg_error",
          generation: ownedGeneration,
          senderGeneration,
          error: error?.message || String(error),
        });
      }
    } finally {
      if (ownedGeneration === generation) jpegBusy = false;
    }
    return;
  }
  if (message.type === "configure_jpeg_decoder") {
    const requestId = Number(message.requestId || 0);
    const nextGeneration = Number(message.generation || 0);
    try {
      closeDecoder();
      closeJpegEncoder();
      closeJpegDecoder();
      if (typeof createImageBitmap !== "function") {
        throw new Error("Worker JPEG decoder is unavailable");
      }
      generation = nextGeneration;
      workerRole = "jpeg_decoder";
      reply(requestId, true);
    } catch (error) {
      reply(requestId, false, { error: error?.message || String(error) });
    }
    return;
  }
  if (message.type === "decode_jpeg") {
    const ownedGeneration = Number(message.generation || 0);
    if (
      workerRole !== "jpeg_decoder" ||
      ownedGeneration !== generation ||
      jpegDecodeBusy ||
      !message.buffer
    ) {
      return;
    }
    jpegDecodeBusy = true;
    try {
      const payload = new Uint8Array(
        message.buffer,
        Number(message.offset || 0),
        Number(message.length || 0),
      );
      const dimensions = jpegDimensions(payload);
      if (!dimensions) {
        throw new Error("JPEG frame has no valid dimensions");
      }
      if (
        dimensions.width > JPEG_MAX_DECODE_DIMENSION ||
        dimensions.height > JPEG_MAX_DECODE_DIMENSION ||
        dimensions.width * dimensions.height > JPEG_MAX_DECODE_PIXELS
      ) {
        throw new Error("JPEG frame exceeds the browser rendering budget");
      }
      const bitmap = await createImageBitmap(
        new Blob([payload], { type: "image/jpeg" }),
      );
      if (
        bitmap.width > JPEG_MAX_DECODE_DIMENSION ||
        bitmap.height > JPEG_MAX_DECODE_DIMENSION ||
        bitmap.width * bitmap.height > JPEG_MAX_DECODE_PIXELS
      ) {
        bitmap.close();
        throw new Error("JPEG frame exceeds the browser rendering budget");
      }
      if (workerRole !== "jpeg_decoder" || ownedGeneration !== generation) {
        bitmap.close();
        return;
      }
      self.postMessage({
        type: "jpeg_bitmap",
        generation: ownedGeneration,
        timestamp: Number(message.timestamp || 0),
        bitmap,
      }, [bitmap]);
    } catch (error) {
      if (workerRole === "jpeg_decoder" && ownedGeneration === generation) {
        reportJpegDecodeError(ownedGeneration, error);
      }
    } finally {
      if (ownedGeneration === generation) jpegDecodeBusy = false;
    }
    return;
  }
  if (message.type === "configure_decoder") {
    const requestId = Number(message.requestId || 0);
    const nextGeneration = Number(message.generation || 0);
    try {
      closeDecoder();
      closeJpegEncoder();
      closeJpegDecoder();
      if (typeof VideoDecoder === "undefined") {
        throw new Error("WebCodecs VideoDecoder is unavailable in worker");
      }
      const support = await VideoDecoder.isConfigSupported(message.config || {});
      if (!support?.supported) {
        throw new Error(`browser cannot decode ${message.config?.codec || "SIP video"}`);
      }
      generation = nextGeneration;
      const ownedGeneration = generation;
      let ownedDecoder;
      ownedDecoder = new VideoDecoder({
        output(frame) {
          if (generation !== ownedGeneration || decoder !== ownedDecoder) {
            frame.close();
            return;
          }
          self.postMessage(
            { type: "frame", generation: ownedGeneration, frame },
            [frame],
          );
        },
        error(error) {
          if (generation !== ownedGeneration || decoder !== ownedDecoder) return;
          self.postMessage({
            type: "decoder_error",
            generation: ownedGeneration,
            error: error?.message || String(error || "decoder failure"),
          });
        },
      });
      ownedDecoder.addEventListener("dequeue", () => {
        if (generation !== ownedGeneration || decoder !== ownedDecoder) return;
        self.postMessage({
          type: "decode_queue",
          generation: ownedGeneration,
          size: ownedDecoder.decodeQueueSize,
        });
      });
      ownedDecoder.configure(support.config || message.config);
      decoder = ownedDecoder;
      workerRole = "decoder";
      reply(requestId, true, { config: support.config || message.config });
    } catch (error) {
      closeDecoder();
      reply(requestId, false, { error: error?.message || String(error) });
    }
    return;
  }
  if (message.type !== "decode" || !decoder ||
      decoder.state !== "configured" ||
      Number(message.generation || 0) !== generation) {
    return;
  }
  try {
    decoder.decode(new EncodedVideoChunk({
      type: message.keyFrame ? "key" : "delta",
      timestamp: Number(message.timestamp || 0),
      data: new Uint8Array(
        message.buffer,
        Number(message.offset || 0),
        Number(message.length || 0),
      ),
    }));
    self.postMessage({
      type: "decode_queue",
      generation,
      size: decoder.decodeQueueSize,
    });
  } catch (error) {
    self.postMessage({
      type: "decoder_error",
      generation,
      error: error?.message || String(error),
    });
  }
};

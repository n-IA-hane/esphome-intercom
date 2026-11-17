// Node VM harnesses evaluate worklet classes without a browser module loader.
// Inline the actual shared export rather than duplicating its numeric rules.
const fs = require("node:fs");
const path = require("node:path");

exports.readPcmWorklet = function readPcmWorklet(filename) {
  const source = fs.readFileSync(filename, "utf8");
  const model = fs.readFileSync(path.join(path.dirname(filename), "voip-stack-media-model.js"), "utf8");
  const helper = model.match(/export function pcmFrameSamples\([^]*?\n\}/);
  if (!helper) throw new Error("Missing shared PCM sample-count helper");
  return source.replace(/^import \{ pcmFrameSamples \} from "\.\/voip-stack-media-model\.js";\n/m,
    helper[0].replace("export ", "") + "\n");
};

// Load complete production ES modules in one controlled browser host context.
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";

const VIDEO_DEPENDENCIES = new Map([
  ["voip-stack-video.js", ["voip-stack-video-model.js", "voip-stack-video-sender.js",
    "voip-stack-video-receiver.js", "voip-stack-video-codecs.js"]],
  ["voip-stack-video-sender.js", ["voip-stack-video-model.js", "voip-stack-video-codecs.js"]],
  ["voip-stack-video-receiver.js", ["voip-stack-video-codecs.js"]],
  ["voip-stack-video-model.js", []],
  ["voip-stack-video-codecs.js", []],
]);

export function createFrontendLoader(frontend, context, origin, { exposeFrameReader = false } = {}) {
  const root = new URL(origin);
  const version = root.searchParams.get("v") || "dev";
  const modules = new Map();
  const requests = [];
  const load = (specifier, from = root, dependency = false) => {
    const url = new URL(specifier, from);
    const filename = path.basename(url.pathname);
    assert.equal(url.origin, root.origin, "Production imports must retain their frontend origin");
    assert.equal(url.searchParams.get("v"), version, "Every production dependency must retain its cache version");
    assert.ok(VIDEO_DEPENDENCIES.has(filename), `Unexpected video module: ${filename}`);
    if (dependency) {
      assert.ok(VIDEO_DEPENDENCIES.get(path.basename(from.pathname))?.includes(filename),
        `Unexpected video dependency: ${from.pathname} -> ${filename}`);
    }
    if (modules.has(url.href)) return modules.get(url.href);
    requests.push(url.href);
    const pending = (async () => {
      let source = fs.readFileSync(path.join(frontend, filename), "utf8");
      if (exposeFrameReader && filename === "voip-stack-video-sender.js") {
        source += "\nexport { createVideoElementFrameReader };\n";
      }
      const module = new vm.SourceTextModule(source, {
        context,
        initializeImportMeta(meta) { meta.url = url.href; },
        importModuleDynamically: (dependency) => load(dependency, url, true),
      });
      await module.link((dependency) => load(dependency, url, true));
      await module.evaluate();
      return module;
    })();
    modules.set(url.href, pending);
    return pending;
  };
  return {
    load: (filename) => load(`${filename}?v=${encodeURIComponent(version)}`),
    requests,
  };
}

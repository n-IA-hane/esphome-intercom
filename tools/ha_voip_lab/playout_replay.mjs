import fs from 'node:fs';
import vm from 'node:vm';

const [sourcePath, tracePath, reportPath, rateArg] = process.argv.slice(2);
const contextRate = Number(rateArg || 48000);
const trace = JSON.parse(fs.readFileSync(tracePath, 'utf8'));
let Processor;
let now = 1;
const stats = [];
class Worklet {
  constructor() { this.port = {onmessage: null, postMessage: item => stats.push({time: now - 1, ...item})}; }
}
const context = vm.createContext({AudioWorkletProcessor: Worklet, sampleRate: contextRate,
  registerProcessor: (_, value) => { Processor = value; }, ArrayBuffer, DataView,
  Float32Array, Math, Number, Object, Error});
Object.defineProperty(context, 'currentTime', {get: () => now});
vm.runInContext(fs.readFileSync(sourcePath, 'utf8'), context);
const [rate, pcm, channels, frameMs] = trace.format.split(':');
const format = {sampleRate: Number(rate), pcmFormat: pcm, channels: Number(channels), frameMs: Number(frameMs)};
const processor = new Processor({processorOptions: {format}});
const frameSamples = format.sampleRate * format.frameMs / 1000;
let next = 0;
let phase = 0;
let began = false;
const output = Array.from({length: format.channels}, () => new Float32Array(128));
const played = [];
const transitions = [];
let previousUnderruns = 0;
for (let elapsed = 0; elapsed <= trace.times.at(-1) + 128 / contextRate; elapsed += 128 / contextRate) {
  now = elapsed + 1;
  while (next < trace.times.length && trace.times[next] <= elapsed) {
    const data = new ArrayBuffer(frameSamples * format.channels * 2);
    const view = new DataView(data);
    for (let i = 0; i < frameSamples; i++, phase++) {
      for (let ch = 0; ch < format.channels; ch++) {
        view.setInt16((i * format.channels + ch) * 2, Math.round(10000 / (ch + 1) * Math.sin(2 * Math.PI * 440 * phase / format.sampleRate + ch * Math.PI / 2)), true);
      }
    }
    processor.port.onmessage({data: {type: 'audio', buffer: data}});
    next++;
  }
  began ||= processor._started;
  processor.process([], [output]);
  if (began) { for (let i = 0; i < 128; i++) for (let ch = 0; ch < format.channels; ch++) played.push(output[ch][i]); }
  if (processor._underruns !== previousUnderruns) {
    transitions.push({time: elapsed, underruns: processor._underruns, available: processor._available,
      targetFrames: processor._targetStartFrames});
    previousUnderruns = processor._underruns;
  }
}
let zeroRun = 0, maxZeroRun = 0;
for (const sample of played) {
  zeroRun = sample === 0 ? zeroRun + 1 : 0;
  maxZeroRun = Math.max(maxZeroRun, zeroRun);
}
const report = {sourcePath, contextRate, channels: format.channels, packets: next, underruns: processor._underruns, maxZeroRunMs: maxZeroRun * 1000 / contextRate,
  framesDropped: processor._framesDrop, transitions, stats};
fs.writeFileSync(reportPath, JSON.stringify(report, null, 2));
fs.writeFileSync(reportPath + '.f32', Buffer.from(new Float32Array(played).buffer));
console.log(JSON.stringify({sourcePath, packets: next, underruns: report.underruns,
  maxZeroRunMs: report.maxZeroRunMs, framesDropped: report.framesDropped, transitions}));

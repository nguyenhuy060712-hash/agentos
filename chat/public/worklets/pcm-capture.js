// AudioWorklet: forwards mono Float32 mic frames (context runs at 16 kHz) to the main thread. Served same-origin (CSP script-src 'self').
class PcmCapture extends AudioWorkletProcessor {
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (ch && ch.length) this.port.postMessage(ch.slice(0));
    return true;
  }
}
registerProcessor("pcm-capture", PcmCapture);

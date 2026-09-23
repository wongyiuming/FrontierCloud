const dspReady = fetch('/karaoke/frontier_karaoke_worklet.wasm', {cache: 'force-cache'})
  .then((response) => {
    if (!response.ok) throw new Error(`Rust DSP ${response.status}`);
    return WebAssembly.instantiateStreaming(response);
  });

class FrontierVocalDsp extends AudioWorkletProcessor {
  constructor() {
    super();
    this.instance = null;
    this.pointer = 0;
    this.block = null;
    dspReady.then(({instance}) => {
      this.instance = instance;
      this.pointer = instance.exports.block_pointer();
      this.block = new Float32Array(instance.exports.memory.buffer, this.pointer, 128);
      this.port.postMessage({status: 'ready'});
    }).catch((error) => {
      this.port.postMessage({status: 'error', message: String(error)});
    });
  }

  process(inputs, outputs) {
    const input = inputs[0]?.[0];
    const output = outputs[0]?.[0];
    if (!output) return true;
    if (!input || !this.instance || !this.block) {
      output.fill(0);
      return true;
    }
    const length = Math.min(input.length, 128);
    this.block.fill(0);
    this.block.set(input.subarray(0, length));
    this.instance.exports.process_block(this.pointer, length);
    output.set(this.block.subarray(0, length));
    if (length < output.length) output.fill(0, length);
    return true;
  }
}

registerProcessor('frontier-vocal-dsp', FrontierVocalDsp);

class FrontierVocalDsp extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this.instance = new WebAssembly.Instance(options.processorOptions.wasmModule);
    this.pointer = this.instance.exports.block_pointer();
    this.block = new Float32Array(this.instance.exports.memory.buffer, this.pointer, 128);
    this.port.postMessage({status: 'ready'});
  }

  process(inputs, outputs) {
    const input = inputs[0]?.[0];
    const output = outputs[0]?.[0];
    if (!output) return true;
    if (!input) {
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

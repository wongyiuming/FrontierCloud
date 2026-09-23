const response = await fetch('/karaoke/frontier_karaoke_worklet.wasm', {cache: 'force-cache'});
if (!response.ok) throw new Error(`Rust DSP ${response.status}`);
const {instance} = await WebAssembly.instantiateStreaming(response);
const pointer = instance.exports.block_pointer();

class FrontierVocalDsp extends AudioWorkletProcessor {
  process(inputs, outputs) {
    const input = inputs[0]?.[0];
    const output = outputs[0]?.[0];
    if (!output) return true;
    if (!input) {
      output.fill(0);
      return true;
    }
    const length = Math.min(input.length, 128);
    const block = new Float32Array(instance.exports.memory.buffer, pointer, 128);
    block.fill(0);
    block.set(input.subarray(0, length));
    instance.exports.process_block(pointer, length);
    output.set(block.subarray(0, length));
    if (length < output.length) output.fill(0, length);
    return true;
  }
}

registerProcessor('frontier-vocal-dsp', FrontierVocalDsp);

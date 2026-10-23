// @ts-check
/**
 * AudioWorklet "mic-capture": turns the mic input (24 kHz mono, the AudioContext rate) into 100 ms PCM16 frames.
 *
 * Worklet → main thread: {type: "frame", pcm: Int16Array(2400), rms: number}, with the buffer transferred.
 * The main thread always uses `rms` for the meter and sends `pcm` only while the backend has the mic open.
 * The node must be connected (through a zero-gain node) to the destination, or Chrome never calls process().
 */
/* global AudioWorkletProcessor, registerProcessor */

const FRAME = 2400; // samples per frame (100 ms at 24 kHz)

class MicCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.buffer = new Float32Array(FRAME);
    this.count = 0;
  }

  /** @param {Float32Array[][]} inputs */
  process(inputs) {
    const input = inputs[0];
    const channel = input && input[0];
    if (channel) {
      let i = 0;
      while (i < channel.length) {
        const n = Math.min(channel.length - i, FRAME - this.count);
        this.buffer.set(channel.subarray(i, i + n), this.count);
        this.count += n;
        i += n;
        if (this.count === FRAME) this.emit();
      }
    }
    return true; // keep capturing even while the input is momentarily silent/disconnected
  }

  emit() {
    const pcm = new Int16Array(FRAME);
    let sum = 0;
    for (let i = 0; i < FRAME; i++) {
      const s = Math.max(-1, Math.min(1, this.buffer[i]));
      sum += s * s;
      pcm[i] = Math.round(s < 0 ? s * 0x8000 : s * 0x7fff);
    }
    this.count = 0;
    this.port.postMessage({ type: "frame", pcm, rms: Math.sqrt(sum / FRAME) }, [pcm.buffer]);
  }
}

registerProcessor("mic-capture", MicCapture);

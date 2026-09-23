/// Allocation-free vocal safety stage called by the AudioWorklet.
#[no_mangle]
pub extern "C" fn process_block(pointer: *mut f32, length: usize) {
    if pointer.is_null() || length == 0 || length > 4096 {
        return;
    }
    let samples = unsafe { std::slice::from_raw_parts_mut(pointer, length) };
    for sample in samples {
        let value = if sample.is_finite() { *sample } else { 0.0 };
        *sample = value.clamp(-1.0, 1.0).tanh();
    }
}

static mut BLOCK: [f32; 128] = [0.0; 128];

#[no_mangle]
pub extern "C" fn block_pointer() -> *mut f32 {
    std::ptr::addr_of_mut!(BLOCK).cast::<f32>()
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn limiter_bounds_and_sanitizes_samples() {
        let mut samples = [5.0, -5.0, f32::NAN];
        process_block(samples.as_mut_ptr(), samples.len());
        assert!(samples
            .iter()
            .all(|sample| sample.is_finite() && sample.abs() <= 1.0));
    }
}

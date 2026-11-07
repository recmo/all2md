//! Resident DFN3 inference. Each request starts with fresh DSP and recurrent state.
use std::{collections::HashMap, io::{self, BufRead, Write}};

use anyhow::{bail, Context, Result};
use df::{tract::{DfParams, DfTract, ReduceMask, RuntimeParams}, wav_utils::{ReadWav, write_wav_arr2}};
use ndarray::{Array2, Axis, Slice};
use serde_json::Value;

fn enhance(request: &Value, models: &mut HashMap<usize, DfTract>) -> Result<()> {
    let source = request["source"].as_str().context("source is required")?;
    let output = request["output"].as_str().context("output is required")?;
    let attenuation = request["attenuation_db"].as_f64().context("attenuation_db is required")?;
    if !attenuation.is_finite() || !(0.0..=36.0).contains(&attenuation) {
        bail!("attenuation_db must be between 0 and 36");
    }
    let reader = ReadWav::new(source)?;
    if reader.sr != 48000 || !(1..=2).contains(&reader.channels) {
        bail!("expected mono or stereo 48 kHz audio");
    }
    let channels = reader.channels;
    if !models.contains_key(&channels) {
        // Match the original deep-filter CLI defaults, which differ from
        // the library defaults for thresholds and multichannel mask reduction.
        let params = RuntimeParams::default_with_ch(channels)
            .with_thresholds(-15.0, 35.0, 35.0)
            .with_mask_reduce(ReduceMask::MAX);
        models.insert(channels, DfTract::new(DfParams::default(), &params)?);
    }
    // Keep an unused template with compiled plans/weights resident. Cloning it
    // gives every recording pristine recurrent, normalization and STFT state
    // without loading or compiling the model again.
    let mut model = models.get(&channels).unwrap().clone();
    model.set_atten_lim(attenuation as f32);
    let noisy = reader.samples_arr2()?.as_standard_layout().into_owned();
    let mut enhanced = Array2::<f32>::zeros(noisy.raw_dim());
    for (input, output) in noisy.axis_chunks_iter(Axis(1), model.hop_size)
        .zip(enhanced.axis_chunks_iter_mut(Axis(1), model.hop_size)) {
        if input.len_of(Axis(1)) < model.hop_size {
            break;
        }
        model.process(input, output)?;
    }
    let delay = model.fft_size - model.hop_size + model.lookahead * model.hop_size;
    enhanced.slice_axis_inplace(Axis(1), Slice::from(delay..));
    write_wav_arr2(output, enhanced.view(), 48000)?;
    Ok(())
}

fn main() -> Result<()> {
    let mut models = HashMap::new();
    let mut stdout = io::stdout().lock();
    for line in io::stdin().lock().lines() {
        let result = serde_json::from_str::<Value>(&line?)
            .map_err(anyhow::Error::from)
            .and_then(|request| enhance(&request, &mut models));
        let error = result.err().map(|error| format!("{error:#}"));
        serde_json::to_writer(&mut stdout, &error)?;
        writeln!(stdout)?;
        stdout.flush()?;
    }
    Ok(())
}

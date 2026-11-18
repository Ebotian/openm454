# Signal quality vs. the channel configuration (measured 2026-11-18)

The question: **at the same per-channel sample rate**, does enabling more channels change what the scope measures on
one channel? Answer: no above two channels (differences at or below 0.15 LSB); the one-channel mode is a different
acquisition path — 1.2 LSB higher gain, 0.5 LSB of interleave mismatch, 0.25 LSB lower noise.

The control variable matters. This page varies the **channel count at a fixed rate** and compares only where the
*applied* rates match (a request of 50 MSa/s lands on 40 and 100 on 80 for a single channel). The other axis — the
same wire throughput reached with different channel sets (1 analog at 400 MSa/s vs 4 at 100 MSa/s, both 400 MB/s) —
is confounded by the per-channel rate and is not used for any conclusion here.

## Method

One M454, USB 3. Only the declared link's channel carries a signal (the built-in generator: 1 kHz square, 0–0.20 V);
the other analog inputs are left open and the digital inputs unconnected — they still produce samples, which is what a
configuration costs.

- 0.05 V/div, 0.10 V offset: **1 LSB = 1.7 mV**; the 214 mV square is ~126 codes.
- 10 ms records, triggered on the rise at 0.10 V, so the edge sits at t = 0.
- Per record: flat-region sigma (max of the two plateaus), the amplitude between plateaus, the even/odd and mod-4
  group means of the high plateau (interleave mismatch), 10–90 % rise, 50 % crossing position.
- **300 rounds, every configuration once per round in rotation.** The rotation is the point: the absolute level drifts
  between sessions (two runs of this same script moved the median sigma of the 4-analog set from 2.71 to 2.61 mV,
  ~4 %), so only differences taken **inside a round** are reproducible.

## Results

Absolute medians of the 300 records per configuration, with a bootstrap 95 % interval (mV):

| configuration | applied rate | sigma | amplitude | even/odd | mod-4 | rise (ns) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 analog | 400 MSa/s | 2.06 [2.03, 2.10] | 214.26 | 0.07 | **1.02** | 85 |
| 1 analog | 200 MSa/s | 2.07 [2.04, 2.10] | 214.56 | **1.02** | 1.04 | 85 |
| 2 analog | 200 MSa/s | 2.43 [2.41, 2.44] | 212.43 | 0.23 | 0.27 | 85 |
| 2 analog | 100 MSa/s | 2.43 [2.41, 2.45] | 212.33 | 0.02 | 0.07 | 90 |
| 4 analog | 100 MSa/s | 2.60 [2.59, 2.63] | 212.59 | 0.02 | 0.07 | 90 |
| 4 analog + 8 digital | 100 MSa/s | 2.59 [2.57, 2.60] | 212.59 | 0.02 | 0.07 | 90 |

Paired differences within a round (later minus earlier; an interval that excludes 0 is resolvable):

| from | to | sigma | in LSB | amplitude | in LSB |
| --- | --- | ---: | ---: | ---: | ---: |
| 1 analog @200 | 2 analog @200 | +0.35 [+0.32, +0.40] | 0.21 | -2.13 [-2.14, -2.12] | 1.25 |
| 2 analog @100 | 4 analog @100 | +0.19 [+0.15, +0.22] | 0.11 | +0.26 [+0.25, +0.28] | 0.15 |
| 4 analog @100 | 4 analog + 8 digital @100 | -0.04 [-0.06, -0.01] | 0.02 | +0.00 [-0.01, +0.01] | 0.00 |
| 1 analog @200 | 1 analog @400 | -0.01 [-0.05, +0.02] | 0.01 | -0.31 [-0.32, -0.30] | 0.18 |

With 1800 records the digital-bank difference (-0.04 mV) no longer straddles zero — that is the resolution of the
measurement, not an effect: it is 0.02 LSB.

## What it means

1. **Adding the eight digital channels costs nothing** on the analog channel: sigma -0.04 mV (0.02 LSB),
   amplitude +0.00 mV.
2. **Two to four analog channels: resolvable but sub-code.** sigma +0.19 mV (0.11 LSB, interval excludes 0) and
   amplitude +0.26 mV (0.15 LSB).
3. **The one-channel mode is a different acquisition path.** Amplitude is **1.25 LSB (1.0 %) higher**, sigma 0.25 LSB
   lower, and the interleave mismatch is **0.95 mV (0.56 LSB)** where the multi-channel records show 0.02–0.25 mV.
   At 200 MSa/s it sits in the even/odd (lag 2) means; at 400 MSa/s it moves to the mod-4 means (lag 2 is 0.06) —
   a 2-way and a 4-way interleaved converter respectively. That is an inference from the group means, not a
   die-level proof of the topology.
4. **Interleave mismatch does not grow with the channel count**: 2 and 4 analog channels show the same 0.02 mV.

## Repeatability, stated so the numbers are not over-read

- Three sessions (60, 60 and 300 rounds) give the same paired differences: **+0.42 / +0.52 / +0.35 mV** for one to two
  analog channels, **+0.21 / +0.27 / +0.19 mV** for two to four, and **0.00 / -0.02 / -0.04 mV** for the digital bank;
  the amplitudes agree to 0.01 mV (-2.12 / -2.13 / -2.13).
- The **absolute** medians move between sessions by up to 4 % (the 4-analog sigma was 2.71, 2.61 and 2.60 mV in the
  three sessions), which is why only paired differences are quoted as results.
- The per-record sigma has a heavy tail: a robust scale flags ~10 % of the records well above the median (up to 9 mV
  during a transient disturbance). That is why the summary uses medians and bootstrap intervals.
- Only the declared channel carries a signal. The open inputs see about 60 mVpp of the driven square (measured
  separately), independent of how many channels are enabled.
- The 82–90 ns rise is the generator's edge, not the front end: nothing here measures the 50 MHz analog bandwidth.
- The digital channels are unconnected: their own levels are not a quality measurement.

## Reproduce

`tests/hardware/research/signal_quality.py` runs the rounds, writes `signal-quality.csv` (one row per capture:
round, configuration, applied rate, sigma, amplitude, even/odd, mod-4, rise, edge position) and prints the per-config
medians with bootstrap intervals plus the paired differences:

    python tests/hardware/research/signal_quality.py --rounds 300      # ~7 min on one bench

`tests/hardware/research/usb_ceiling.py` produces the readout-ceiling table and the transfer trace quoted in the
README. Both are run by hand on a bench with the scope attached; neither is part of pytest.

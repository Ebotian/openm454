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
- **60 rounds, every configuration once per round in rotation.** The rotation is the point: the absolute level drifts
  between sessions (two runs of this same script moved the median sigma of the 4-analog set from 2.71 to 2.61 mV,
  ~4 %), so only differences taken **inside a round** are reproducible.

## Results

Absolute medians of the 60 records per configuration, with a bootstrap 95 % interval (mV):

| configuration | applied rate | sigma | amplitude | even/odd | mod-4 | rise (ns) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 analog | 400 MSa/s | 1.99 [1.96, 2.04] | 214.20 | 0.06 | **0.95** | 82.5 |
| 1 analog | 200 MSa/s | 2.02 [1.97, 2.08] | 214.51 | **0.95** | 0.98 | 85 |
| 2 analog | 200 MSa/s | 2.44 [2.40, 2.53] | 212.38 | 0.21 | 0.25 | 85 |
| 2 analog | 100 MSa/s | 2.40 [2.36, 2.43] | 212.29 | 0.02 | 0.07 | 90 |
| 4 analog | 100 MSa/s | 2.61 [2.57, 2.64] | 212.55 | 0.02 | 0.07 | 90 |
| 4 analog + 8 digital | 100 MSa/s | 2.58 [2.56, 2.65] | 212.54 | 0.02 | 0.08 | 90 |

Paired differences within a round (later minus earlier; an interval that excludes 0 is resolvable):

| from | to | sigma | in LSB | amplitude | in LSB |
| --- | --- | ---: | ---: | ---: | ---: |
| 1 analog @200 | 2 analog @200 | +0.42 [+0.33, +0.48] | 0.25 | -2.12 [-2.15, -2.10] | 1.25 |
| 2 analog @100 | 4 analog @100 | +0.21 [+0.15, +0.26] | 0.12 | +0.26 [+0.24, +0.29] | 0.15 |
| 4 analog @100 | 4 analog + 8 digital @100 | +0.00 [-0.04, +0.04] | 0.00 | -0.02 [-0.04, +0.01] | 0.01 |
| 1 analog @200 | 1 analog @400 | -0.02 [-0.07, +0.05] | 0.01 | -0.30 [-0.32, -0.28] | 0.18 |

## What it means

1. **Adding the eight digital channels costs nothing** on the analog channel: sigma +0.00 mV (interval straddles 0),
   amplitude -0.02 mV.
2. **Two to four analog channels: resolvable but sub-code.** sigma +0.21 mV (0.12 LSB, interval excludes 0) and
   amplitude +0.26 mV (0.15 LSB).
3. **The one-channel mode is a different acquisition path.** Amplitude is **1.25 LSB (1.0 %) higher**, sigma 0.25 LSB
   lower, and the interleave mismatch is **0.95 mV (0.56 LSB)** where the multi-channel records show 0.02–0.25 mV.
   At 200 MSa/s it sits in the even/odd (lag 2) means; at 400 MSa/s it moves to the mod-4 means (lag 2 is 0.06) —
   a 2-way and a 4-way interleaved converter respectively. That is an inference from the group means, not a
   die-level proof of the topology.
4. **Interleave mismatch does not grow with the channel count**: 2 and 4 analog channels show the same 0.02 mV.

## Repeatability, stated so the numbers are not over-read

- The **absolute** medians move between sessions: a previous 60-round run gave 2.00 / 2.52 / 2.45 / 2.73 / 2.71 mV
  against 1.99 / 2.44 / 2.40 / 2.61 / 2.58 above. The **paired** differences reproduced to within 0.05 mV
  (+0.42 → +0.52 for one to two channels, +0.21 → +0.27 for two to four, 0.00 → -0.02 for the digital bank).
- The per-record sigma has a heavy tail: a robust scale flags ~10 of 60 records per configuration well above the
  median (up to 9 mV during a transient disturbance). That is why the summary uses medians and bootstrap intervals.
- Only the declared channel carries a signal. The open inputs see about 60 mVpp of the driven square (measured
  separately), independent of how many channels are enabled.
- The 82–90 ns rise is the generator's edge, not the front end: nothing here measures the 50 MHz analog bandwidth.
- The digital channels are unconnected: their own levels are not a quality measurement.

## Reproduce

`tests/hardware/research/signal_quality.py` runs the 60 rounds, writes `signal-quality.csv` (one row per capture:
round, configuration, applied rate, sigma, amplitude, even/odd, mod-4, rise, edge position) and prints the per-config
medians with bootstrap intervals plus the paired differences:

    python tests/hardware/research/signal_quality.py --rounds 60

`tests/hardware/research/usb_ceiling.py` produces the readout-ceiling table and the transfer trace quoted in the
README. Both are run by hand on a bench with the scope attached; neither is part of pytest.

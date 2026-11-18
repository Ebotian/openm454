# Signal quality vs. the channel configuration (measured 2026-11-18)

The question: with the **same readout throughput**, does enabling more channels change what the scope measures on
one channel? Answer: above two channels, no (differences below one code); the one-channel mode is a different
acquisition path and differs by about one code in gain and half a code in interleave mismatch.

## Method

One M454, USB 3, kernel 7.3.4. Only `A0` carries a signal (the built-in generator: 1 kHz square, 0-0.20 V); the
other analog inputs are left open and the digital inputs unconnected — they still produce samples and bytes, which is
what a configuration costs.

- 0.05 V/div, 0.10 V offset: **1 LSB = 1.7 mV**; the 214 mV square is ~126 codes.
- 10 ms records, triggered on the A0 rise at 0.10 V, so the edge sits at t = 0.
- Per record: flat-region sigma (max of the high and low plateaus), amplitude between plateaus, even/odd and mod-4
  group means of the high plateau (interleave mismatch), 10-90 % rise, and the position of the 50 % crossing.
- **60 rounds, all configurations once per round in rotation**, so a slow drift of the generator or the front end
  cannot be mistaken for a configuration effect. Medians and means with a t-based 95 % interval (df = 59).

## Results (mV unless stated)

| configuration | applied rate | sigma [95 % CI] | amplitude [95 % CI] | even/odd | mod-4 | rise (ns) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 analog | 200 MSa/s | **2.000** [1.964, 2.035] | **214.6** [214.6, 214.6] | 0.914 | 0.943 | 85 |
| 2 analog | 200 MSa/s | 2.516 [2.446, 2.586] | 212.5 | 0.140 | 0.163 | 85 |
| 2 analog | 100 MSa/s | 2.452 [2.409, 2.495] | 212.4 | 0.017 | 0.066 | 90 |
| 4 analog | 100 MSa/s | 2.725 [2.675, 2.775] | 212.6 | 0.015 | 0.075 | 90 |
| 4 analog + 8 digital | 100 MSa/s | 2.705 [2.666, 2.744] | 212.6 | 0.014 | 0.064 | 90 |
| 1 analog | 400 MSa/s | 1.996 [1.953, 2.040] | 214.3 | 0.135 | **0.923** | 85 |

Rate-matched differences (later minus earlier; an interval that excludes 0 is resolvable):

| from | to | sigma | in LSB | amplitude | in LSB |
| --- | --- | ---: | ---: | ---: | ---: |
| 1 analog @200 | 2 analog @200 | +0.516 [+0.438, +0.595] | 0.30 | -2.1 [-2.1, -2.1] | 1.25 |
| 2 analog @100 | 4 analog @100 | +0.273 [+0.207, +0.339] | 0.16 | +0.2 [+0.2, +0.3] | 0.15 |
| 4 analog @100 | 4 analog + 8 digital @100 | -0.020 [-0.084, +0.043] | 0.01 | 0.0 | 0.00 |

Edge stability over the 60 records: 50 % crossing at +4.9 +- 0.6 ns (200 MSa/s) and +2.9 +- 0.9 ns (400 MSa/s) after
t = 0 — the trigger level is at 10 %, so the crossing sits later by construction; it does not depend on the
configuration.

## What it means

1. **Adding the digital channels costs nothing** on the analog channel: sigma -0.02 mV (interval crosses 0), amplitude
   0.00 LSB.
2. **Two to four analog channels: resolvable but sub-code.** sigma +0.27 mV (0.16 LSB, interval excludes 0) and
   amplitude +0.2 mV. It takes 60 records to separate this from noise, which is the point: it is real, and it is
   smaller than the quantizer step.
3. **The one-channel mode is a different acquisition path.** Amplitude is 1.0 % higher (1.25 LSB), sigma 0.5 LSB
   lower, and the interleave mismatch appears at 0.9 mV (0.5 LSB): at 200 MSa/s in the even/odd (lag 2) group means,
   at 400 MSa/s only in the mod-4 group means (lag 2 is 0.135) — a 2-way and a 4-way interleaved ADC respectively.
   Multi-channel records show 0.01-0.16 mV on the same probes.
4. While this bench has one generator, the other analog inputs are open: they see about **60 mVpp** of the driven
   square (measured earlier), and that does not change with the number of enabled channels — it is the floating
   input, not the configuration.

## Limits, stated so the numbers are not over-read

- Only `A0` carries a signal; "quality" here is the driven channel plus coupling into open inputs.
- The 85-90 ns rise is the generator's edge, not the front end: nothing here measures the 50 MHz analog bandwidth.
- Single-channel rates are not the same set as multi-channel ones (a request of 50 MSa/s lands on 40, 100 on 80 for
  one channel), so the comparisons above are made only where the applied rates match.
- The digital channels are unconnected: their own levels are not a quality measurement.

## Reproduce

The script is deliberately small enough to read: open the scope, program the generator, then per record measure the
two plateaus, the even/odd and mod-4 group means and the 10-90 % rise, rotating over the configurations for
`ROUNDS` rounds. ```python
ROUNDS, CONFIGS = 60, [...]
s = Scope(); s.open(); s.awg_set('square', 1e3, 0.20, 0.0)
for r in range(ROUNDS):
    for an, dg, rate in CONFIGS[r % len(CONFIGS):] + CONFIGS[:r % len(CONFIGS)]:
        st = Settings(analog=an, digital=dg, rate=rate, duration=10e-3)
        for ch in ('A0', 'A1', 'A2', 'A3'):
            st.vdiv[ch], st.offset[ch], st.trig_level = 0.05, 0.10, 0.10
        st.trig_source, st.trig_edge, st.trig_mode = 'A0', 'rise', 'normal'
        cap = s.capture(st)
        v = cap.volts('A0'); k = int(round(-cap.meta['t0_analog_s'] * cap.meta['acq']['analog_rate_hz']))
        hi, lo = v[k + 2000:k + 20000], v[k - 20000:k - 2000]   # plateaus, 18 k samples each
```

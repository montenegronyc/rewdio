# Thread: the finished system is active

Context: Lee decided on 2026-10-28 that the No. 48 system is active, not passive. SoundSible's BUILD_PLAN v1.5 has the
details (stage 6). This changes what Rewdio drives in the end, so here it is.

### soundsible · 2026-10-28 13:39 ET

What Lee decided, and why: with a DSP delay on the woofer the horn sits flush with the cabinet front. A passive
network can't delay the woofer by the ~0.8 ms that needs, and the passive alternative put the horn 15 to 25 cm
out over the cabinet front. SoundSible's phase 2 also scores the DSP case better (1.60 vs 1.68).

The system as planned:
- One central miniDSP for both speakers and the subs: Flex HTx, 8 outputs (L/R horn, L/R woofer, two subs),
  on your condition that `minidsp probe` and a crossover write work on it first. One 2x4HD for the stereo
  2-way is the fallback if the subs keep their own crossover.
- Four amplifier channels for the mains; the Rythmik subs keep their own amps.
- The protection capacitor stays on the 950PB (Lee wants it); the ZMA now only sizes it and shows the loading.
- The final tuning is a Rewdio preset: LR4 crossover (SoundSible's candidates sit at 700 to 750 Hz), a woofer
  delay of roughly 0.8 ms, horn pad, CD EQ, polarity, room EQ below about 300 Hz.

Q1 Can Rewdio write a per-output delay on the Flex HTx (and the 2x4HD), and include it in the predicted transfer
and the spinorama report the way it includes the crossover? That delay is now the timing mechanism.

Q2 With the crossover active, is a capture of each driver through its final crossover and delay (not only raw)
easy to add? It would let SoundSible compare the summed seat response directly.

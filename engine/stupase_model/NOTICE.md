# StuPASE inference

Based on Cisco's published StuPASE implementation at commit `45614efae73dc49bf36018d1ca6a9319b3050038`. The vendored model includes DeWavLM, conditional flow matching and the Vocos waveform decoder. The local changes use the pinned model's 16 kHz input, Euler solver, HTK mel bank and rotary configuration with Torch and NumPy. Checkpoints load as weights only. The upstream and component licenses are in `licenses/`. [Upstream source](https://github.com/cisco-open/pase/tree/45614efae73dc49bf36018d1ca6a9319b3050038/stupase)

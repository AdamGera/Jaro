"""Torch-only operations used by the pinned StuPASE inference configuration."""

import torch
from torch import nn


class RotaryEmbedding(nn.Module):
    def __init__(self, dim):
        super().__init__()
        self.register_buffer("inv_freq", 1 / (10_000 ** (torch.arange(0, dim, 2).float() / dim)))

    def forward_from_seq_len(self, length):
        positions = torch.arange(length, device=self.inv_freq.device, dtype=self.inv_freq.dtype)
        phases = torch.outer(positions, self.inv_freq).repeat_interleave(2, dim=-1)
        return phases.unsqueeze(0), 1.0


def apply_rotary_pos_emb(audio, phases, scale=1.0):
    phases = phases[:, -audio.shape[-2]:, :].unsqueeze(1)
    dim = phases.shape[-1]
    rotated = audio[..., :dim]
    even, odd = rotated.unflatten(-1, (-1, 2)).unbind(dim=-1)
    quarter_turn = torch.stack((-odd, even), dim=-1).flatten(-2)
    output = scale * (rotated * phases.cos() + quarter_turn * phases.sin())
    return torch.cat((output, audio[..., dim:]), dim=-1).to(audio.dtype)


def euler_trajectory(flow, initial, times):
    states = [initial]
    for start, end in zip(times[:-1], times[1:]):
        states.append(states[-1] + (end - start) * flow(start, states[-1]))
    return torch.stack(states)


def mel_spectrogram(waveform, n_fft, n_mel_channels, target_sample_rate, hop_length, win_length):
    waveform = waveform.squeeze(1) if waveform.ndim == 3 else waveform
    device = waveform.device
    # The checkpoint uses an unnormalized HTK mel bank and magnitude STFT.
    max_frequency = target_sample_rate // 2
    max_mel = 2595 * torch.log10(torch.tensor(1 + max_frequency / 700))
    mel = torch.linspace(0, max_mel, n_mel_channels + 2, device=device)
    hz = 700 * (10 ** (mel / 2595) - 1)
    freqs = torch.linspace(0, max_frequency, n_fft // 2 + 1, device=device)
    slopes = hz[None, :] - freqs[:, None]
    gaps = hz.diff()
    filters = torch.minimum(-slopes[:, :-2] / gaps[:-1], slopes[:, 2:] / gaps[1:]).clamp_min(0)
    window = torch.hann_window(win_length, device=device)
    spectrum = torch.stft(waveform, n_fft, hop_length, win_length, window, return_complex=True)
    return (filters.T @ spectrum.abs()).clamp_min(1e-5).log()

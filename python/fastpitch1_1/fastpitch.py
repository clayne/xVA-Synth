# *****************************************************************************
#  Copyright (c) 2020, NVIDIA CORPORATION.  All rights reserved.
#
#  Redistribution and use in source and binary forms, with or without
#  modification, are permitted provided that the following conditions are met:
#      * Redistributions of source code must retain the above copyright
#        notice, this list of conditions and the following disclaimer.
#      * Redistributions in binary form must reproduce the above copyright
#        notice, this list of conditions and the following disclaimer in the
#        documentation and/or other materials provided with the distribution.
#      * Neither the name of the NVIDIA CORPORATION nor the
#        names of its contributors may be used to endorse or promote products
#        derived from this software without specific prior written permission.
#
#  THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
#  ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
#  WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
#  DISCLAIMED. IN NO EVENT SHALL NVIDIA CORPORATION BE LIABLE FOR ANY
#  DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
#  (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
#  LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND
#  ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
#  (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
#  SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#
# *****************************************************************************
from typing import Optional

import torch
import traceback
from torch import nn as nn
import torch.nn.functional as F
from torch.nn.utils.rnn import pad_sequence

from python.common.layers import ConvReLUNorm
from python.common.utils import mask_from_lens
from python.fastpitch1_1.transformer import FFTransformer
from python.fastpitch1_1.attention import ConvAttention
from python.fastpitch1_1.alignment import b_mas, mas_width1


def regulate_len(durations, enc_out, pace: float = 1.0, mel_max_len: Optional[int] = None):
    """If target=None, then predicted durations are applied"""
    dtype = enc_out.dtype
    reps = durations.float() / pace
    reps = (reps + 0.5).long()
    dec_lens = reps.sum(dim=1)

    max_len = dec_lens.max()
    reps_cumsum = torch.cumsum(F.pad(reps, (1, 0, 0, 0), value=0.0), dim=1)[:, None, :]
    reps_cumsum = reps_cumsum.to(dtype)

    range_ = torch.arange(max_len).to(enc_out.device)[None, :, None]
    mult = ((reps_cumsum[:, :, :-1] <= range_) &
            (reps_cumsum[:, :, 1:] > range_))
    mult = mult.to(dtype)
    enc_rep = torch.matmul(mult, enc_out)

    if mel_max_len is not None:
        enc_rep = enc_rep[:, :mel_max_len]
        dec_lens = torch.clamp_max(dec_lens, mel_max_len)
    return enc_rep, dec_lens

def average_pitch(pitch, durs):
    durs_cums_ends = torch.cumsum(durs, dim=1).long()
    durs_cums_starts = F.pad(durs_cums_ends[:, :-1], (1, 0))
    pitch_nonzero_cums = F.pad(torch.cumsum(pitch != 0.0, dim=2), (1, 0))
    pitch_cums = F.pad(torch.cumsum(pitch, dim=2), (1, 0))

    bs, l = durs_cums_ends.size()
    n_formants = pitch.size(1)
    dcs = durs_cums_starts[:, None, :].expand(bs, n_formants, l)
    dce = durs_cums_ends[:, None, :].expand(bs, n_formants, l)

    pitch_sums = (torch.gather(pitch_cums, 2, dce)
                  - torch.gather(pitch_cums, 2, dcs)).float()
    pitch_nelems = (torch.gather(pitch_nonzero_cums, 2, dce)
                    - torch.gather(pitch_nonzero_cums, 2, dcs)).float()

    pitch_avg = torch.where(pitch_nelems == 0.0, pitch_nelems, pitch_sums / pitch_nelems)
    return pitch_avg

class TemporalPredictor(nn.Module):
    """Predicts a single float per each temporal location"""

    def __init__(self, input_size, filter_size, kernel_size, dropout,
                 n_layers=2, n_predictions=1):
        super(TemporalPredictor, self).__init__()

        self.layers = nn.Sequential(*[
            ConvReLUNorm(input_size if i == 0 else filter_size, filter_size,
                         kernel_size=kernel_size, dropout=dropout)
            for i in range(n_layers)]
        )
        self.n_predictions = n_predictions
        self.fc = nn.Linear(filter_size, self.n_predictions, bias=True)

    def forward(self, enc_out, enc_out_mask):
        out = enc_out * enc_out_mask
        out = self.layers(out.transpose(1, 2)).transpose(1, 2)
        out = self.fc(out) * enc_out_mask
        return out


class FastPitch(nn.Module):
    def __init__(self, n_mel_channels, n_symbols, padding_idx, symbols_embedding_dim, in_fft_n_layers, in_fft_n_heads, in_fft_d_head, in_fft_conv1d_kernel_size, in_fft_conv1d_filter_size, in_fft_output_size, p_in_fft_dropout, p_in_fft_dropatt, p_in_fft_dropemb, out_fft_n_layers, out_fft_n_heads, out_fft_d_head, out_fft_conv1d_kernel_size, out_fft_conv1d_filter_size, out_fft_output_size, p_out_fft_dropout, p_out_fft_dropatt, p_out_fft_dropemb, dur_predictor_kernel_size, dur_predictor_filter_size, p_dur_predictor_dropout, dur_predictor_n_layers, pitch_predictor_kernel_size, pitch_predictor_filter_size, p_pitch_predictor_dropout, pitch_predictor_n_layers, pitch_embedding_kernel_size, energy_conditioning, energy_predictor_kernel_size, energy_predictor_filter_size, p_energy_predictor_dropout, energy_predictor_n_layers, energy_embedding_kernel_size, n_speakers, speaker_emb_weight, pitch_conditioning_formants=1, device=None):
        super(FastPitch, self).__init__()

        self.encoder = FFTransformer(
            n_layer=in_fft_n_layers, n_head=in_fft_n_heads, d_model=symbols_embedding_dim, d_head=in_fft_d_head, d_inner=in_fft_conv1d_filter_size, kernel_size=in_fft_conv1d_kernel_size, dropout=p_in_fft_dropout, dropatt=p_in_fft_dropatt, dropemb=p_in_fft_dropemb, embed_input=True, d_embed=symbols_embedding_dim, n_embed=n_symbols, padding_idx=padding_idx)

        if n_speakers > 1:
            self.speaker_emb = nn.Embedding(n_speakers, symbols_embedding_dim)
        else:
            self.speaker_emb = None
        self.speaker_emb_weight = speaker_emb_weight

        self.duration_predictor = TemporalPredictor(
            in_fft_output_size, filter_size=dur_predictor_filter_size, kernel_size=dur_predictor_kernel_size, dropout=p_dur_predictor_dropout, n_layers=dur_predictor_n_layers
        )

        self.decoder = FFTransformer(
            n_layer=out_fft_n_layers, n_head=out_fft_n_heads, d_model=symbols_embedding_dim, d_head=out_fft_d_head, d_inner=out_fft_conv1d_filter_size, kernel_size=out_fft_conv1d_kernel_size, dropout=p_out_fft_dropout, dropatt=p_out_fft_dropatt, dropemb=p_out_fft_dropemb, embed_input=False, d_embed=symbols_embedding_dim
        )

        self.pitch_predictor = TemporalPredictor(
            in_fft_output_size, filter_size=pitch_predictor_filter_size, kernel_size=pitch_predictor_kernel_size, dropout=p_pitch_predictor_dropout, n_layers=pitch_predictor_n_layers, n_predictions=pitch_conditioning_formants
        )

        self.pitch_emb = nn.Conv1d(
            pitch_conditioning_formants, symbols_embedding_dim, kernel_size=pitch_embedding_kernel_size, padding=int((pitch_embedding_kernel_size - 1) / 2))

        # Store values precomputed for training data within the model
        self.register_buffer('pitch_mean', torch.zeros(1))
        self.register_buffer('pitch_std', torch.zeros(1))

        self.energy_conditioning = energy_conditioning
        if energy_conditioning:
            self.energy_predictor = TemporalPredictor(
                in_fft_output_size, filter_size=energy_predictor_filter_size, kernel_size=energy_predictor_kernel_size, dropout=p_energy_predictor_dropout, n_layers=energy_predictor_n_layers, n_predictions=1
            )

            self.energy_emb = nn.Conv1d(
                1, symbols_embedding_dim, kernel_size=energy_embedding_kernel_size, padding=int((energy_embedding_kernel_size - 1) / 2))

        self.proj = nn.Linear(out_fft_output_size, n_mel_channels, bias=True)

        self.attention = ConvAttention(n_mel_channels, 0, symbols_embedding_dim, use_query_proj=True, align_query_enc_type='3xconv')

    def binarize_attention(self, attn, in_lens, out_lens):
        """For training purposes only. Binarizes attention with MAS.
           These will no longer recieve a gradient.

        Args:
            attn: B x 1 x max_mel_len x max_text_len
        """
        b_size = attn.shape[0]
        with torch.no_grad():
            attn_cpu = attn.data.cpu().numpy()
            attn_out = torch.zeros_like(attn)
            for ind in range(b_size):
                hard_attn = mas_width1(attn_cpu[ind, 0, :out_lens[ind], :in_lens[ind]])
                attn_out[ind, 0, :out_lens[ind], :in_lens[ind]] = torch.tensor(
                    hard_attn, device=attn.get_device())
        return attn_out

    def binarize_attention_parallel(self, attn, in_lens, out_lens):
        """For training purposes only. Binarizes attention with MAS.
           These will no longer recieve a gradient.

        Args:
            attn: B x 1 x max_mel_len x max_text_len
        """
        with torch.no_grad():
            attn_cpu = attn.data.cpu().numpy()
            attn_out = b_mas(attn_cpu, in_lens.cpu().numpy(), out_lens.cpu().numpy(), width=1)
        return torch.from_numpy(attn_out).to(attn.get_device())

    def forward(self, inputs, use_gt_pitch=True, pace=1.0, max_duration=75):

        (inputs, input_lens, mel_tgt, mel_lens, pitch_dense, energy_dense, speaker, attn_prior, audiopaths) = inputs

        mel_max_len = mel_tgt.size(2)

        # Calculate speaker embedding
        if self.speaker_emb is None:
            spk_emb = 0
        else:
            spk_emb = self.speaker_emb(speaker).unsqueeze(1)
            spk_emb.mul_(self.speaker_emb_weight)

        # Input FFT
        enc_out, enc_mask = self.encoder(inputs, conditioning=spk_emb)

        # Alignment
        text_emb = self.encoder.word_emb(inputs)

        # make sure to do the alignments before folding
        attn_mask = mask_from_lens(input_lens)[..., None] == 0
        # attn_mask should be 1 for unused timesteps in the text_enc_w_spkvec tensor

        attn_soft, attn_logprob = self.attention(
            mel_tgt, text_emb.permute(0, 2, 1), mel_lens, attn_mask, key_lens=input_lens, keys_encoded=enc_out, attn_prior=attn_prior)

        attn_hard = self.binarize_attention_parallel(
            attn_soft, input_lens, mel_lens)

        # Viterbi --> durations
        attn_hard_dur = attn_hard.sum(2)[:, 0, :]
        dur_tgt = attn_hard_dur

        assert torch.all(torch.eq(dur_tgt.sum(dim=1), mel_lens))

        # Predict durations
        log_dur_pred = self.duration_predictor(enc_out, enc_mask).squeeze(-1)
        dur_pred = torch.clamp(torch.exp(log_dur_pred) - 1, 0, max_duration)

        # Predict pitch
        pitch_pred = self.pitch_predictor(enc_out, enc_mask).permute(0, 2, 1)

        # Average pitch over characters
        pitch_tgt = average_pitch(pitch_dense, dur_tgt)

        if use_gt_pitch and pitch_tgt is not None:
            pitch_emb = self.pitch_emb(pitch_tgt)
        else:
            pitch_emb = self.pitch_emb(pitch_pred)
        enc_out = enc_out + pitch_emb.transpose(1, 2)

        # Predict energy
        if self.energy_conditioning:
            energy_pred = self.energy_predictor(enc_out, enc_mask).squeeze(-1)

            # Average energy over characters
            energy_tgt = average_pitch(energy_dense.unsqueeze(1), dur_tgt)
            energy_tgt = torch.log(1.0 + energy_tgt)

            energy_emb = self.energy_emb(energy_tgt)
            energy_tgt = energy_tgt.squeeze(1)
            enc_out = enc_out + energy_emb.transpose(1, 2)
        else:
            energy_pred = None
            energy_tgt = None

        len_regulated, dec_lens = regulate_len(
            dur_tgt, enc_out, pace, mel_max_len)

        # Output FFT
        dec_out, dec_mask = self.decoder(len_regulated, dec_lens)
        mel_out = self.proj(dec_out)
        return (mel_out, dec_mask, dur_pred, log_dur_pred, pitch_pred, pitch_tgt, energy_pred, energy_tgt, attn_soft, attn_hard, attn_hard_dur, attn_logprob)

    def infer_using_vals (self, logger, plugin_manager, sequence, pace, enc_out, max_duration, enc_mask, dur_pred_existing, pitch_pred_existing, energy_pred_existing, old_sequence, new_sequence):
        start_index = None
        end_index = None

        # Calculate text splicing bounds, if needed
        if old_sequence is not None:

            old_sequence_np = old_sequence.cpu().detach().numpy()
            old_sequence_np = list(old_sequence_np[0])
            new_sequence_np = new_sequence.cpu().detach().numpy()
            new_sequence_np = list(new_sequence_np[0])


            # Get the index of the first changed value
            if old_sequence_np[0]==new_sequence_np[0]: # If the start of both sequences is the same, then the change is not at the start
                for i in range(len(old_sequence_np)):
                    if i<len(new_sequence_np):
                        if old_sequence_np[i]!=new_sequence_np[i]:
                            start_index = i-1
                            break
                    else:
                        start_index = i-1
                        break
                if start_index is None:
                    start_index = len(old_sequence_np)-1


            # Get the index of the last changed value
            old_sequence_np.reverse()
            new_sequence_np.reverse()
            if old_sequence_np[0]==new_sequence_np[0]: # If the end of both reversed sequences is the same, then the change is not at the end
                for i in range(len(old_sequence_np)):
                    if i<len(new_sequence_np):
                        if old_sequence_np[i]!=new_sequence_np[i]:
                            end_index = len(old_sequence_np)-1-i+1
                            break
                    else:
                        end_index = len(old_sequence_np)-1-i+1
                        break

            old_sequence_np.reverse()
            new_sequence_np.reverse()

        # Calculate its own pitch, duration, and energy vals if these were not already provided
        if (dur_pred_existing is None or pitch_pred_existing is None) or old_sequence is not None:

            # Embedded for predictors
            pred_enc_out, pred_enc_mask = enc_out, enc_mask
            # Predict durations
            log_dur_pred = self.duration_predictor(pred_enc_out, pred_enc_mask).squeeze(-1)
            dur_pred = torch.clamp(torch.exp(log_dur_pred) - 1, 0, max_duration)

            dur_pred = torch.clamp(dur_pred, 0.25)

            # Pitch over chars
            pitch_pred = self.pitch_predictor(enc_out, enc_mask).permute(0, 2, 1)
            pitch_pred = torch.clamp(pitch_pred, min=-3, max=3)

        else:
            dur_pred = dur_pred_existing
            pitch_pred = pitch_pred_existing.unsqueeze(1)
            energy_pred = energy_pred_existing

        # Splice/replace pitch/duration values from the old input if simulating only a partial re-generation
        if start_index is not None or end_index is not None:
            dur_pred_np = list(dur_pred.cpu().detach().numpy())[0]
            pitch_pred_np = list(pitch_pred.cpu().detach().numpy())[0][0]
            dur_pred_existing_np = list(dur_pred_existing.cpu().detach().numpy())[0]
            pitch_pred_existing_np = list(pitch_pred_existing.cpu().detach().numpy())[0]

            if start_index is not None: # Replace starting values

                for i in range(start_index+1):
                    dur_pred_np[i] = dur_pred_existing_np[i]
                    pitch_pred_np[i] = pitch_pred_existing_np[i]

            if end_index is not None: # Replace end values

                for i in range(len(old_sequence_np)-end_index):
                    dur_pred_np[-i-1] = dur_pred_existing_np[-i-1]
                    pitch_pred_np[-i-1] = pitch_pred_existing_np[-i-1]

            dur_pred = torch.tensor(dur_pred_np).to(self.device).unsqueeze(0)
            pitch_pred = torch.tensor(pitch_pred_np).to(self.device).unsqueeze(0).unsqueeze(0)

            pitch_emb = self.pitch_emb(pitch_pred).transpose(1, 2)
            energy_pred = self.energy_predictor(enc_out + pitch_emb, enc_mask).squeeze(-1)


        if len(plugin_manager.plugins["synth-line"]["mid"]):
            pitch_pred = pitch_pred.cpu().detach().numpy()
            plugin_data = {
                "duration": dur_pred.cpu().detach().numpy(), "pitch": pitch_pred.reshape((pitch_pred.shape[0],pitch_pred.shape[2])), "text": sequence, "is_fresh_synth": pitch_pred_existing is None and dur_pred_existing is None
            }
            plugin_manager.run_plugins(plist=plugin_manager.plugins["synth-line"]["mid"], event="mid synth-line", data=plugin_data)

            dur_pred = torch.tensor(plugin_data["duration"]).to(self.device)
            pitch_pred = torch.tensor(plugin_data["pitch"]).unsqueeze(1).to(self.device)

        # pitch_emb = self.pitch_emb(pitch_pred.unsqueeze(1)).transpose(1, 2)
        pitch_emb = self.pitch_emb(pitch_pred).transpose(1, 2)

        enc_out = enc_out + pitch_emb

        # Energy
        if energy_pred_existing is None:
            energy_pred = self.energy_predictor(enc_out, enc_mask).squeeze(-1)
        else:
            # Splice/replace pitch/duration values from the old input if simulating only a partial re-generation
            if start_index is not None or end_index is not None:
                energy_pred_np = list(energy_pred.cpu().detach().numpy())[0]
                energy_pred_existing_np = list(energy_pred_existing.cpu().detach().numpy())[0]
                if start_index is not None: # Replace starting values
                    for i in range(start_index+1):
                        energy_pred_np[i] = energy_pred_existing_np[i]

                if end_index is not None: # Replace end values
                    for i in range(len(old_sequence_np)-end_index):
                        energy_pred_np[-i-1] = energy_pred_existing_np[-i-1]
                energy_pred = torch.tensor(energy_pred_np).to(self.device).unsqueeze(0)


        if len(plugin_manager.plugins["synth-line"]["pre_energy"]):
            pitch_pred = pitch_pred.cpu().detach().numpy()
            plugin_data = {
                "duration": dur_pred.cpu().detach().numpy(),
                "pitch": pitch_pred.reshape((pitch_pred.shape[0],pitch_pred.shape[2])),
                "energy": energy_pred.cpu().detach().numpy(),
                "text": sequence, "is_fresh_synth": pitch_pred_existing is None and dur_pred_existing is None
            }
            plugin_manager.run_plugins(plist=plugin_manager.plugins["synth-line"]["pre_energy"], event="pre_energy synth-line", data=plugin_data)

            energy_pred = torch.tensor(plugin_data["energy"]).to(self.device)

        # Apply the energy
        energy_emb = self.energy_emb(energy_pred.unsqueeze(1)).transpose(1, 2)
        enc_out = enc_out + energy_emb

        len_regulated, dec_lens = regulate_len(dur_pred, enc_out, pace, mel_max_len=None)
        dec_out, dec_mask = self.decoder(len_regulated, dec_lens)
        mel_out = self.proj(dec_out)
        mel_out = mel_out.permute(0, 2, 1)  # For inference.py
        start_index = -1 if start_index is None else start_index
        end_index = -1 if end_index is None else end_index
        return mel_out, dec_lens, dur_pred, pitch_pred, energy_pred, start_index, end_index

    def infer_advanced (self, logger, plugin_manager, cleaned_text, inputs, speaker_i, pace=1.0, pitch_data=None, max_duration=75, old_sequence=None):

        if speaker_i is not None:
            speaker = torch.ones(inputs.size(0)).long().to(inputs.device) * speaker_i
            spk_emb = self.speaker_emb(speaker).unsqueeze(1)
            spk_emb.mul_(self.speaker_emb_weight)
            del speaker
        else:
            spk_emb = 0

        # Input FFT
        enc_out, enc_mask = self.encoder(inputs, conditioning=spk_emb)

        if (pitch_data is not None) and (pitch_data[0] is not None and len(pitch_data[0])) and (pitch_data[1] is not None and len(pitch_data[1])) and (pitch_data[2] is not None and len(pitch_data[2])):
            pitch_pred, dur_pred, energy_pred = pitch_data
            dur_pred = torch.tensor(dur_pred)
            dur_pred = dur_pred.view((1, dur_pred.shape[0])).float().to(self.device)
            pitch_pred = torch.tensor(pitch_pred)
            pitch_pred = pitch_pred.view((1, pitch_pred.shape[0])).float().to(self.device)
            energy_pred = torch.tensor(energy_pred)
            energy_pred = energy_pred.view((1, energy_pred.shape[0])).float().to(self.device)

            del spk_emb
            # Try using the provided pitch/duration data, but fall back to using its own, otherwise
            try:
                return self.infer_using_vals(logger, plugin_manager, cleaned_text, pace, enc_out, max_duration, enc_mask, dur_pred_existing=dur_pred, pitch_pred_existing=pitch_pred, energy_pred_existing=energy_pred, old_sequence=old_sequence, new_sequence=inputs)
            except:
                print(traceback.format_exc())
                logger.info(traceback.format_exc())
                return self.infer_using_vals(logger, plugin_manager, cleaned_text, pace, enc_out, max_duration, enc_mask, None, None, None, None, None)

        else:
            del spk_emb
            return self.infer_using_vals(logger, plugin_manager, cleaned_text, pace, enc_out, max_duration, enc_mask, None, None, None, None, None)